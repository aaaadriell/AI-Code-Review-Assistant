import requests
import os
from dotenv import load_dotenv
from github import Github, Auth

load_dotenv()

# Set up GitHub client
auth = Auth.Token(os.getenv("GITHUB_TOKEN"))
g = Github(auth=auth)

# Get the sandbox repo
repo = g.get_repo("aaaadriell/AI-Code-Review-Sandbox")

# Get all open PRs
open_prs = repo.get_pulls(state="open")

print(f"Found {open_prs.totalCount} open PR(s). Triggering review for each...\n")

# Send a review request for each open PR
for pr in open_prs:
    payload = {
        "repo": repo.full_name,
        "pr_number": pr.number,
        "base_sha": pr.base.sha,
        "head_sha": pr.head.sha
    }

    print(f"Triggering review for PR #{pr.number}: {pr.title}")
    print(f"  base_sha: {pr.base.sha}")
    print(f"  head_sha: {pr.head.sha}")

    try:
        response = requests.post("http://localhost:8000/review", json=payload)
        print(f"  Response: {response.status_code} - {response.json()}")
    except Exception as e:
        print(f"  Error: {e}")

    print()