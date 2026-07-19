import json
from unittest import result
from fastapi import FastAPI, Request
import os
from dotenv import load_dotenv
from github import Github, Auth
import anthropic

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


@app.post("/review")
async def review(request: Request):
    payload = await request.json()
    print(f"1. Received payload: {payload}")
    
    try:
        diff = get_pr_diff(payload["repo"], payload["pr_number"])
        print(f"2. Got diff: {len(diff)} chars")
        
        feedback = review_diff(diff)
        print(f"3. Got feedback: {feedback}")
        
        post_review_comments(payload["repo"], payload["pr_number"], feedback, payload["head_sha"])
        print("4. Posted comment successfully")
        
        return {"status": "success"}
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

