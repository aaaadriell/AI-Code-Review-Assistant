from fastapi import FastAPI, Request
import os
from dotenv import load_dotenv
from github import Github, Auth
import anthropic

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

def review_diff(diff: str) -> str:
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": f"""You are a senior software engineer reviewing a pull request.
Review the following diff and identify bugs, security issues, and style problems.

DIFF:
{diff}

Provide your feedback as a list of issues."""
            }
        ]
    )
    return message.content[0].text

def post_pr_comment(repo_name: str, pr_number: int, body: str):
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    pr.create_issue_comment(body)


@app.post("/review")
async def review(request: Request):
    payload = await request.json()
    print(f"1. Received payload: {payload}")
    
    try:
        diff = get_pr_diff(payload["repo"], payload["pr_number"])
        print(f"2. Got diff: {len(diff)} chars")
        
        feedback = review_diff(diff)
        print(f"3. Got feedback: {feedback}")
        
        post_pr_comment(payload["repo"], payload["pr_number"], feedback)
        print("4. Posted comment successfully")
        
        return {"status": "success"}
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

