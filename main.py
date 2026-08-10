import json
import re
from fastapi import FastAPI, Request
import os
from dotenv import load_dotenv
from github import Github, Auth
import anthropic
from pinecone import Pinecone
import requests
from requests.auth import HTTPBasicAuth
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.core import StorageContext, VectorStoreIndex

from models import ReviewResult

from langfuse import Langfuse, observe, get_client as get_langfuse_client
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# Hidden marker embedded in each posted PR comment so a later reply can be
# traced back to the exact Langfuse trace/observation that produced it.
FEEDBACK_MARKER_RE = re.compile(
    r'<!-- langfuse-feedback trace_id="([^"]+)" observation_id="([^"]+)" -->'
)

load_dotenv()

app = FastAPI()

langfuse = Langfuse()

# Initialize clients once at module level (reuse across requests)
github_client = Github(auth=Auth.Token(os.getenv("GITHUB_TOKEN")))
anthropic_client = anthropic.Anthropic()

# Initialize embedding model (must match the model used in indexer.py)
embed_model = HuggingFaceEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

# Initialize Pinecone once (cloud-hosted, works the same locally and on Railway)
pinecone_client = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
pinecone_index = pinecone_client.Index(os.getenv("PINECONE_INDEX_NAME", "codebase-index"))
vector_store = PineconeVectorStore(pinecone_index=pinecone_index)
storage_context = StorageContext.from_defaults(vector_store=vector_store)


def get_pr_diff(repo_name: str, pr_number: int) -> str:
    repo = github_client.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    diff_text = ""
    for file in pr.get_files():
        diff_text += f"\n### File: {file.filename}\n"
        diff_text += file.patch or "(binary file, skipped)"
    return diff_text

@observe()
def review_diff(diff: str, rules: str, context: str):
    # Call Claude with diff + context + rules (no need to regenerate query/context)
    schema = ReviewResult.model_json_schema()

    message = anthropic_client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": f"""You are a senior software engineer performing a code review.

                TEAM RULES (enforce these strictly):
                {rules}

                RELATED CODEBASE CONTEXT (files that may be affected by this change):
                {context}

                DIFF TO REVIEW:
                {diff}

                Return ONLY a JSON object matching this schema. No prose outside the JSON.
                Every field in the schema is required for every comment object, including
                "confidence" - never omit it.
                Schema: {json.dumps(schema)}"""
            }
        ]
    )

    raw = message.content[0].text
    result = ReviewResult.model_validate_json(raw)

    # Create one child observation per comment, so a reply to that specific
    # GitHub comment later can be scored against exactly this comment - not
    # just the review as a whole.
    langfuse_client = get_langfuse_client()
    trace_id = langfuse_client.get_current_trace_id()
    observation_ids = []
    for c in result.comments:
        span = langfuse_client.start_observation(
            name=f"comment:{c.file}:{c.line}",
            as_type="span",
            input=c.message,
            metadata={"category": c.category, "severity": c.severity, "file": c.file, "line": c.line},
        )
        span.end()
        observation_ids.append(span.id)

    return result, trace_id, observation_ids


def _valid_diff_lines(patch: str) -> set[int]:
    """Return the set of new-file line numbers actually visible in a diff hunk.
    GitHub only allows PR review comments on lines shown in the diff context,
    not just any line that exists in the file."""
    if not patch:
        return set()

    valid_lines = set()
    current_line = None
    for line in patch.splitlines():
        if line.startswith("@@"):
            # e.g. "@@ -10,7 +12,8 @@" -> new file starts at line 12
            try:
                plus_part = line.split("+")[1].split(" ")[0]
                current_line = int(plus_part.split(",")[0])
            except (IndexError, ValueError):
                current_line = None
            continue
        if current_line is None:
            continue
        if line.startswith("\\"):
            pass  # "\ No newline at end of file" marker - not a real line
        elif line.startswith("+"):
            valid_lines.add(current_line)
            current_line += 1
        elif line.startswith("-"):
            pass  # removed lines don't exist in the new file
        else:
            valid_lines.add(current_line)  # context line
            current_line += 1
    return valid_lines


def post_review_comments(repo_name: str, pr_number: int, result: ReviewResult, head_sha: str, trace_id: str, observation_ids: list[str]):
    repo = github_client.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    # Build a map of filename -> valid commentable line numbers from the real diff
    valid_lines_by_file = {
        file.filename: _valid_diff_lines(file.patch)
        for file in pr.get_files()
    }

    # Post line-level comments (only where the line actually appears in the diff)
    comments = []
    skipped = []
    for c, observation_id in zip(result.comments, observation_ids):
        valid_lines = valid_lines_by_file.get(c.file)
        if valid_lines is None:
            skipped.append(f"- `{c.file}` (not part of this PR's diff): {c.message}")
            continue
        if c.line not in valid_lines:
            skipped.append(f"- `{c.file}:{c.line}` (outside diff context): {c.message}")
            continue

        label = f"**[{c.severity}]** `{c.category}` (confidence: {int(c.confidence * 100)}%)"
        body = f"{label}\n\n{c.message}"
        if c.suggestion:
            body += f"\n\n**Suggested fix:**\n```\n{c.suggestion}\n```"
        # Hidden marker (invisible on GitHub) so a reply to this comment can be
        # traced back to the exact Langfuse trace/observation that produced it.
        body += f'\n\n<!-- langfuse-feedback trace_id="{trace_id}" observation_id="{observation_id}" -->'
        comments.append({
            "path": c.file,
            "line": c.line,
            "body": body
        })

    # Post overall summary as a top-level comment, including anything we couldn't attach inline
    summary_body = f"## AI Review Summary\n\n{result.summary}"
    if skipped:
        summary_body += "\n\n**Additional notes (couldn't attach to a specific line):**\n" + "\n".join(skipped)
    pr.create_issue_comment(summary_body)

    # Post line comments as a review (only if there's at least one valid comment)
    if comments:
        pr.create_review(
            commit=repo.get_commit(head_sha),
            body="",
            event="COMMENT",
            comments=comments
        )


def get_rules(repo_name: str) -> str:
    repo = github_client.get_repo(repo_name)
    try:
        content = repo.get_contents("RULES.md")
        return content.decoded_content.decode("utf-8")
    except:
        return "No RULES.md found. Apply general best practices only."


@observe(as_type="retriever")
def retrieve_context(query: str, top_k: int = 5) -> dict:
    """Retrieves the top-k most similar codebase chunks for a query. Instrumented
    as a Langfuse retriever span - the query is auto-captured as input, and the
    returned dict (with per-chunk similarity scores) as output, so retrieval
    quality is inspectable per-review instead of being a black box."""
    langfuse_client = get_langfuse_client()

    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_context, embed_model=embed_model
    )
    retriever = index.as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(query)

    chunks = []
    context = ""
    for node in nodes:
        file_name = node.metadata.get("file_name", "unknown")
        score = float(node.score) if node.score is not None else None
        chunks.append({"file": file_name, "score": score, "snippet": node.text[:500]})
        header = f"\n### {file_name}" + (f" (similarity: {score:.3f})" if score is not None else "") + "\n"
        context += header + node.text

    return {
        "context": context,
        "chunks": chunks,
        "observation_id": langfuse_client.get_current_observation_id(),
    }


def generate_retrieval_query(diff: str) -> str:
    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Given this code diff, write a short search query (1-2 sentences)
                            that would help find related functions, classes, or files in the codebase that
                            this change might affect.

            DIFF:
            {diff[:2000]}

            Query:"""
        }]
    )
    return message.content[0].text.strip()


def classify_retrieval_relevance(diff: str, chunks: list[dict]) -> tuple[float, str]:
    """LLM-as-judge: score how relevant the retrieved codebase chunks actually
    are to the diff being reviewed. Runs automatically on every review, giving a
    continuous quality signal for RAG retrieval with no human input needed."""
    if not chunks:
        return 0.0, "No chunks were retrieved."

    chunks_summary = "\n\n".join(
        f"File: {c['file']} (similarity: {c['score']:.3f})\n{c['snippet']}"
        for c in chunks
    )

    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""You are evaluating a RAG retrieval step for an AI code reviewer.

Given the code diff being reviewed and the codebase chunks that were retrieved as
"related context", judge how relevant and useful the retrieved chunks actually are
for reviewing this specific diff.

DIFF:
{diff[:2000]}

RETRIEVED CHUNKS:
{chunks_summary}

Respond in EXACTLY this format, two lines, no other text:
SCORE: <a number from 0.0 (irrelevant) to 1.0 (highly relevant and useful)>
REASON: <one sentence explaining why>"""
        }]
    )

    text = message.content[0].text.strip()
    score, reason = 0.5, ""
    for line in text.splitlines():
        if line.upper().startswith("SCORE:"):
            try:
                score = max(0.0, min(1.0, float(line.split(":", 1)[1].strip())))
            except ValueError:
                pass
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return score, reason


@observe()
def run_review_pipeline(diff: str, rules: str):
    """Runs query generation, retrieval, and the Claude review as one Langfuse
    trace (query gen inline, retrieval and review as nested spans) so retrieval
    quality can be correlated with the review it actually produced - without this,
    retrieval and review would show up as two disconnected, uncorrelated traces."""
    langfuse_client = get_langfuse_client()
    trace_id = langfuse_client.get_current_trace_id()

    query = generate_retrieval_query(diff)
    retrieval = retrieve_context(query, top_k=5)

    result, _, observation_ids = review_diff(diff, rules, retrieval["context"])

    # Automated RAG eval: attach an LLM-judged relevance score directly to the
    # retrieval span, so you can see retrieval quality per-review in Langfuse
    # without waiting on human feedback.
    relevance_score, relevance_reason = classify_retrieval_relevance(diff, retrieval["chunks"])
    submit_langfuse_score(
        trace_id, retrieval["observation_id"], relevance_score, relevance_reason,
        name="retrieval_relevance", data_type="NUMERIC"
    )

    return result, trace_id, observation_ids, query, retrieval


@app.post("/review")
async def review(request: Request):
    payload = await request.json()
    print(f"1. Received payload: {payload}")

    try:
        diff = get_pr_diff(payload["repo"], payload["pr_number"])
        print(f"2. Got diff: {len(diff)} chars")

        rules = get_rules(payload["repo"])
        print(f"3. Got rules: {len(rules)} chars")

        feedback, trace_id, observation_ids, query, retrieval = run_review_pipeline(diff, rules)
        print(f"4. Generated retrieval query: {query}")
        print(f"5. Retrieved {len(retrieval['chunks'])} chunks: {[c['file'] for c in retrieval['chunks']]}")

        print(f"6. Got feedback: {feedback}")

        post_review_comments(payload["repo"], payload["pr_number"], feedback, payload["head_sha"], trace_id, observation_ids)
        print("7. Posted comment successfully")

        return {
            "status": "success",
            "pr_number": payload["pr_number"],
            "trace_id": trace_id
        }
    
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
    
@app.post("/reindex")
async def reindex(request: Request):
    """Re-index the codebase when it changes."""
    payload = await request.json()
    repo_name = payload.get("repo")

    try:
        print(f"Starting re-index for {repo_name}...")
        # Reads directly via the GitHub API, so this works the same
        # locally and in production (no local clone required)
        from indexer import build_index_from_github
        build_index_from_github(repo_name, github_client)

        print("Re-index completed successfully")
        return {"status": "success", "message": "Index rebuilt"}
    except Exception as e:
        print(f"Re-index error: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


def submit_langfuse_score(trace_id: str, observation_id: str | None, value, comment: str = "", name: str = "helpful", data_type: str = "BOOLEAN") -> bool:
    """Submit a score to Langfuse via its REST API. `value` is a bool for BOOLEAN
    scores (e.g. human 'helpful' feedback) or a float for NUMERIC scores (e.g.
    automated retrieval relevance). Returns True if accepted, never raises."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    base_url = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

    if not public_key or not secret_key:
        print("  Langfuse credentials not configured, skipping score")
        return False

    score_value = (1 if value else 0) if data_type == "BOOLEAN" else value

    score_payload = {
        "traceId": trace_id,
        "observationId": observation_id,
        "name": name,
        "value": score_value,
        "dataType": data_type,
        "comment": comment
    }
    response = requests.post(
        f"{base_url}/api/public/scores",
        json=score_payload,
        auth=HTTPBasicAuth(public_key, secret_key),
        headers={"Content-Type": "application/json"}
    )

    if response.status_code in [200, 201]:
        print(f"  Score '{name}' submitted to Langfuse successfully: {response.json()}")
        return True
    print(f"  Langfuse API error: {response.status_code} - {response.text}")
    return False


def classify_feedback_helpfulness(reply_text: str) -> bool:
    """Use Claude to classify a free-text reply as indicating the original
    review comment was helpful or not."""
    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": f"""A user replied to an AI code review comment on a pull request.
Classify whether their reply indicates the original comment was HELPFUL or NOT HELPFUL.
Respond with exactly one word, lowercase: "true" if helpful, "false" if not helpful.
If genuinely ambiguous, default to "true".

Reply: \"\"\"{reply_text}\"\"\"

Answer:"""
        }]
    )
    answer = message.content[0].text.strip().lower()
    return answer.startswith("true")


@app.post("/feedback")
async def feedback(request: Request):
    """Log feedback on AI review comments (helpful or not helpful). Manual/testing entry point -
    for real usage see /github-comment, which is triggered automatically by replies on GitHub."""
    payload = await request.json()
    pr_number = payload.get("pr_number")
    trace_id = payload.get("comment_id")  # This is the trace ID from Langfuse
    observation_id = payload.get("observation_id")
    helpful = payload.get("helpful")  # True or False

    print(f"Feedback endpoint called - PR: {pr_number}, Trace ID: {trace_id}, Helpful: {helpful}")
    submit_langfuse_score(trace_id, observation_id, helpful, payload.get("comment", ""))
    return {"status": "success", "message": "Feedback recorded"}


@app.post("/github-comment")
async def github_comment(request: Request):
    """Receives every new PR review comment (via a GitHub Actions webhook in the
    sandbox repo). If the comment is a reply to one of our AI review comments,
    classify the reply's sentiment with Claude and log it as a Langfuse score -
    fully automatic, no manual /feedback calls needed."""
    payload = await request.json()
    repo_name = payload.get("repo")
    pr_number = payload.get("pr_number")
    in_reply_to_id = payload.get("in_reply_to_id")
    comment_body = payload.get("comment_body", "")
    comment_user = payload.get("comment_user")

    print(f"GitHub comment webhook: repo={repo_name}, pr={pr_number}, in_reply_to={in_reply_to_id}, user={comment_user}")

    if not in_reply_to_id:
        # The service only ever posts top-level review comments, never replies,
        # so requiring in_reply_to_id is sufficient to avoid reacting to its own output.
        return {"status": "skipped", "message": "Not a reply, ignoring"}

    try:
        repo = github_client.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        parent_comment = pr.get_review_comment(int(in_reply_to_id))

        match = FEEDBACK_MARKER_RE.search(parent_comment.body or "")
        if not match:
            return {"status": "skipped", "message": "Parent comment has no Langfuse marker (not an AI review comment)"}

        trace_id, observation_id = match.group(1), match.group(2)
        helpful = classify_feedback_helpfulness(comment_body)
        print(f"  Classified reply as helpful={helpful}: {comment_body[:100]!r}")

        submit_langfuse_score(trace_id, observation_id, helpful, comment_body)
        return {"status": "success", "helpful": helpful}
    except Exception as e:
        print(f"GitHub comment webhook error: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


