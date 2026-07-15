from github import Github, Auth
import os
from dotenv import load_dotenv

load_dotenv()
auth = Auth.Token(os.getenv("GITHUB_TOKEN"))
g = Github(auth=auth)

repo = g.get_repo("aaaadriell/AI-Code-Review-Sandbox")
pr = repo.get_pull(2)  # Replace 1 with your PR number

print(f"base_sha: {pr.base.sha}")
print(f"head_sha: {pr.head.sha}")
