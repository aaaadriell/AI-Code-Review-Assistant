import json
from fastapi import FastAPI, Request
import os
from dotenv import load_dotenv
from github import Github, Auth
import anthropic
import chromadb
import requests
from requests.auth import HTTPBasicAuth
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core import StorageContext, VectorStoreIndex

from models import ReviewResult

from langfuse import Langfuse, observe
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

load_dotenv()

app = FastAPI()

langfuse = Langfuse()

# Initialize clients once at module level (reuse across requests)
github_client = Github(auth=Auth.Token(os.getenv("GITHUB_TOKEN")))
anthropic_client = anthropic.Anthropic()

# Initialize embedding model (same as indexer.py)
embed_model = HuggingFaceEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

# Initialize ChromaDB once (reuse across requests)
chroma_client = chromadb.PersistentClient(path="./chroma_db")
chroma_collection = chroma_client.get_or_create_collection("codebase")
vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
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
def review_diff(diff: str, rules: str, context: str) -> ReviewResult:
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
    return ReviewResult.model_validate_json(raw)


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


def post_review_comments(repo_name: str, pr_number: int, result: ReviewResult, head_sha: str):
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
    for c in result.comments:
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


def retrieve_context(query: str, top_k: int = 5) -> str:
    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_context, embed_model=embed_model
    )
    retriever = index.as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(query)

    context = ""
    for node in nodes:
        context += f"\n### {node.metadata.get('file_name', 'unknown')}\n"
        context += node.text
    return context


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


@app.post("/review")
async def review(request: Request):
    payload = await request.json()
    print(f"1. Received payload: {payload}")

    try:
        diff = get_pr_diff(payload["repo"], payload["pr_number"])
        print(f"2. Got diff: {len(diff)} chars")

        rules = get_rules(payload["repo"])
        print(f"3. Got rules: {len(rules)} chars")

        query = generate_retrieval_query(diff)
        print(f"4. Generated retrieval query: {query}")

        context = retrieve_context(query, top_k=5)
        print(f"5. Retrieved context: {len(context)} chars")

        feedback = review_diff(diff, rules, context)
        print(f"6. Got feedback: {feedback}")

        post_review_comments(payload["repo"], payload["pr_number"], feedback, payload["head_sha"])
        print("7. Posted comment successfully")

        return {
            "status": "success",
            "pr_number": payload["pr_number"],
            "message": "Check Langfuse dashboard for trace ID to use with /feedback endpoint"
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
        # Import your build_index function from indexer
        from indexer import build_index

        # For now, re-index your sandbox repo
        build_index("path-to-your-sandbox-repo")

        print("Re-index completed successfully")
        return {"status": "success", "message": "Index rebuilt"}
    except Exception as e:
        print(f"Re-index error: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


@app.post("/feedback")
async def feedback(request: Request):
    """Log feedback on AI review comments (helpful or not helpful)."""
    payload = await request.json()
    pr_number = payload.get("pr_number")
    trace_id = payload.get("comment_id")  # This is the trace ID from Langfuse
    observation_id = payload.get("observation_id")  # Optional, if you want to attach to a specific observation
    helpful = payload.get("helpful")  # True or False

    try:
        print(f"Feedback endpoint called - PR: {pr_number}, Trace ID: {trace_id}, Helpful: {helpful}")

        # Get Langfuse API credentials and base URL
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        base_url = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            print("  Langfuse credentials not configured, skipping score")
            return {"status": "success", "message": "Feedback recorded (Langfuse not configured)"}

        # Call Langfuse REST API to submit score
        langfuse_url = f"{base_url}/api/public/scores"

        score_payload = {
            "traceId": trace_id,
            "name": "helpful",
            "value": 1 if helpful else 0,
            "dataType": "BOOLEAN",
            "comment": payload.get("comment", ""),
            # observationId is optional - Langfuse will attach to the trace root if not provided
            "observationId": observation_id
        }

        response = requests.post(
            langfuse_url,
            json=score_payload,
            auth=HTTPBasicAuth(public_key, secret_key),
            headers={"Content-Type": "application/json"}
        )

        if response.status_code in [200, 201]:
            print(f"  Score submitted to Langfuse successfully: {response.json()}")
        else:
            print(f"  Langfuse API error: {response.status_code} - {response.text}")

        print(f"Feedback logged for PR #{pr_number}: helpful={helpful}")
        return {"status": "success", "message": "Feedback recorded"}
    except Exception as e:
        print(f"Feedback error: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


