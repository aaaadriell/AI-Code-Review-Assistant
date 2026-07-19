import json
from unittest import result
from fastapi import FastAPI, Request
import os
from dotenv import load_dotenv
from github import Github, Auth
import anthropic
import chromadb
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core import StorageContext, VectorStoreIndex

from models import ReviewResult

load_dotenv()

app = FastAPI()


def get_pr_diff(repo_name: str, pr_number: int) -> str:
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    diff_text = ""
    for file in pr.get_files():
        diff_text += f"\n### File: {file.filename}\n"
        diff_text += file.patch or "(binary file, skipped)"
    return diff_text


def review_diff(diff: str, rules: str) -> ReviewResult:
    client = anthropic.Anthropic()

    # Step 1: generate retrieval query
    query = generate_retrieval_query(diff)
    print(f"Generated retrieval query: {query}")

    # Step 2: retrieve relevant context
    context = retrieve_context(query, top_k=5)
    print(f"Retrieved context: {len(context)} chars")

    # Step 3: call Claude with diff + context + rules
    schema = ReviewResult.model_json_schema()

    message = client.messages.create(
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
                Schema: {json.dumps(schema)}"""
            }
        ]
    )

    raw = message.content[0].text
    return ReviewResult.model_validate_json(raw)


def post_review_comments(repo_name: str, pr_number: int, result: ReviewResult, head_sha: str):
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    # Post line-level comments
    comments = []
    for c in result.comments:
        label = f"**[{c.severity}]** `{c.category}` (confidence: {int(c.confidence * 100)}%)"
        body = f"{label}\n\n{c.message}"
        if c.suggestion:
            body += f"\n\n**Suggested fix:**\n```\n{c.suggestion}\n```"
        comments.append({
            "path": c.file,
            "line": c.line,
            "body": body
        })

    # Post overall summary as a top-level comment
    pr.create_issue_comment(f"## AI Review Summary\n\n{result.summary}")

    # Post line comments as a review
    pr.create_review(
        commit=repo.get_commit(head_sha),
        body="",
        event="COMMENT",
        comments=comments
    )


def get_rules(repo_name: str) -> str:
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    try:
        content = repo.get_contents("RULES.md")
        return content.decoded_content.decode("utf-8")
    except:
        return "No RULES.md found. Apply general best practices only."


def retrieve_context(query: str, top_k: int = 5) -> str:
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    chroma_collection = chroma_client.get_or_create_collection("codebase")
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_context
    )
    retriever = index.as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(query)

    context = ""
    for node in nodes:
        context += f"\n### {node.metadata.get('file_name', 'unknown')}\n"
        context += node.text
    return context


def generate_retrieval_query(diff: str) -> str:
    client = anthropic.Anthropic()
    message = client.messages.create(
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
        print("5. Posted comment successfully")

        return {"status": "success"}
    
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


