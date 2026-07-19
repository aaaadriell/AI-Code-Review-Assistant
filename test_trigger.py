import requests

requests.post("http://localhost:8000/review", json={
    "repo": "aaaadriell/AI-Code-Review-Sandbox",
    "pr_number": 6,
    "base_sha": "67c5a9f453e7d56647283673b49a86843e71f211",
    "head_sha": "61fe874eff3ddd9c9b2ac782551d9d198abcf9da"
})