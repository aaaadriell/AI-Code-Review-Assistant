import requests

requests.post("http://localhost:8000/review", json={
    "repo": "aaaadriell/AI-Code-Review-Sandbox",
    "pr_number": 2,
    "base_sha": "98551ec1400f3bfe7898e4cdf590862c52040301",
    "head_sha": "9b357ed86bde209e3319428b8d272b550465c6f1"
})