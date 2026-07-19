# AI Code Review Assistant — Project Roadmap

## Overview of Phases

| Phase | What You Build | Goal |
|---|---|---|
| 0 | Environment setup | Everything installed and connected |
| 1 | Dumb end-to-end pipe | PR opens → Claude comments on it |
| 2 | Structured output + RULES.md | Comments are formatted, line-level, and rule-aware |
| 3 | RAG over codebase | LLM gets broader repo context, not just the diff |
| 4 | Observability | Every LLM call tracked for cost, latency, feedback |
| 5 | Polish & deploy | Live URL, clean README, demo video |

Estimated total time: **4–6 weeks** building part-time (~2hrs/day).

---

## Key Packages & Why We Use Them

**FastAPI** — Web framework for your review service. Lightweight, modern, and perfect for building the HTTP endpoint that GitHub Actions will call.

**PyGithub** — Python library for GitHub API. Handles fetching PRs, diffs, and posting comments without dealing with raw HTTP requests.

**anthropic** — Official Anthropic SDK. Calls Claude for code review analysis and retrieval query generation.

**pydantic** — Schema validation for structured output. Ensures Claude's JSON responses match your expected format, catching errors early.

**llama-index** — Orchestration framework for RAG (Retrieval-Augmented Generation). Handles loading documents, managing embeddings, and querying vector stores without reinventing the wheel.

**chromadb** — Vector database for storing code embeddings locally. Persists to disk so your index survives restarts, and enables semantic search over your codebase.

**tree-sitter** + **tree-sitter-python** — AST parser for Python code. Critical for smart chunking: splits code by functions/classes (semantic units) instead of character count, so retrieval returns whole functions instead of fragments.

**sentence-transformers** (via llama-index) — Generates embeddings (vector representations) of code for semantic search. HuggingFace's `BAAI/bge-small-en-v1.5` is small, fast, and open-source.

---

## Phase 0 — Environment Setup
**Goal: Everything installed, credentials working, test repo ready.**
**Estimated time: 1–2 days**

### Steps

**0.1 — Create a test GitHub repository**
- Create a new repo (e.g. `ai-review-sandbox`) — this is the repo your bot will review PRs on
- Add some realistic Python files to it (a simple FastAPI app with intentional bugs works well)
- Create a `main` branch and protect it (Settings → Branches → Add rule → Require PR before merging)

**0.2 — Create the bot's project repository**
- Create a second repo (e.g. `ai-code-reviewer`) — this is where your Review Service code lives
- Set up a Python virtual environment:
  ```bash
  python -m venv venv
  Windows: venv\Scripts\activate # Mac/Linux: source venv/bin/activate 
  ```
- Install initial dependencies:
  ```bash
  pip install fastapi uvicorn anthropic PyGithub python-dotenv requests
  ```

**0.3 — Set up credentials**
- Get your **Anthropic API key** from console.anthropic.com
- Create a **GitHub Personal Access Token**: GitHub → Settings → Developer Settings →
  Personal Access Tokens → Fine-grained token → give it `repo` and `pull_requests` read/write access
- Create a `.env` file in your project root:
  ```
  ANTHROPIC_API_KEY=sk-ant-...
  GITHUB_TOKEN=github_pat_...
  ```
- Add `.env` to `.gitignore` immediately

**0.4 — Verify API access**
- Write a small test script that calls Claude and prints a response
- Write a small test script that lists open PRs on your sandbox repo via PyGitHub
- Both should work before proceeding

---

## Phase 1 — Dumb End-to-End Pipe
**Goal: PR opens → GitHub Action fires → diff fetched → Claude called → comment posted. No structure, no rules — just prove the pipe works.**
**Estimated time: 4–5 days**

### Steps

**1.1 — Build the Review Service skeleton**

Create `main.py`:
```python
from fastapi import FastAPI, Request
import hmac, hashlib, os

app = FastAPI()

@app.post("/review")
async def review(request: Request):
    payload = await request.json()
    # We'll fill this in next
    return {"status": "received"}
```

Run it locally:
```bash
uvicorn main:app --reload
```

**1.2 — Trigger the service locally (no tunnel needed)**

You don't need your service to be reachable from the internet to build and test
most of the pipeline — only the real GitHub Actions → your machine hop needs that.
Everything downstream (fetch diff, call Claude, post PR comment) just needs your
machine to reach GitHub's API, which it already can.

Write a small local script, `test_trigger.py`, that POSTs the same JSON payload
the GitHub Action would send, straight to your local service:

```python
import requests

requests.post("http://localhost:8000/review", json={
    "repo": "your-username/ai-review-sandbox",
    "pr_number": 1,
    "base_sha": "...",
    "head_sha": "..."
})
```

Fill in real values from an open PR in your sandbox repo. Run `uvicorn main:app --reload`
in one terminal and `python test_trigger.py` in another — this exercises the entire
pipeline end-to-end (diff fetch, Claude call, structured output, comment posted to the
real PR) with zero tunneling.

Later in Phase 5 you'll deploy to Railway and get a permanent public URL to replace this.

**1.3 — Write the GitHub Actions workflow**

In your **sandbox repo** (not the reviewer repo), create `.github/workflows/ai-review.yml`:
```yaml
name: AI Code Review

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Review Service
        run: |
          curl -X POST ${{ secrets.REVIEW_SERVICE_URL }}/review \
            -H "Content-Type: application/json" \
            -d '{
              "repo": "${{ github.repository }}",
              "pr_number": ${{ github.event.pull_request.number }},
              "base_sha": "${{ github.event.pull_request.base.sha }}",
              "head_sha": "${{ github.event.pull_request.head.sha }}"
            }'
```

Add `REVIEW_SERVICE_URL` as a secret in your sandbox repo (Settings → Secrets → Actions). (Not now though)

**1.4 — Fetch the diff in the Review Service**

```python
from github import Github

def get_pr_diff(repo_name: str, pr_number: int) -> str:
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    diff_text = ""
    for file in pr.get_files():
        diff_text += f"\n### File: {file.filename}\n"
        diff_text += file.patch or "(binary file, skipped)"
    return diff_text
```

**1.5 — Call Claude with the diff**

```python
import anthropic

def review_diff(diff: str) -> str:
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-8",
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
```

**1.6 — Post the response as a PR comment**

```python
def post_pr_comment(repo_name: str, pr_number: int, body: str):
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    pr.create_issue_comment(body)
```

**1.7 — Wire it all together and test**

Open a PR in your sandbox repo with an intentional bug (e.g. a SQL string concatenation).
Run `test_trigger.py` with that PR's number/SHAs, and watch your service receive the
request, Claude respond, and a comment appear on the PR. The entire pipeline works end-to-end.

**✅ Phase 1 checkpoint: A comment appears on your PR. Content doesn't matter yet — the pipe works.**

---

## Phase 2 — Structured Output + RULES.md
**Goal: Comments are line-level, categorised, severity-labelled, and rule-aware.**
**Estimated time: 4–5 days**

### Steps

**2.1 — Add RULES.md to your sandbox repo**

Create `RULES.md` in the root of the sandbox repo:
```markdown
# Code Review Rules

## Security
- All DB queries must use parameterised inputs. Never concatenate user input into SQL.
- Never log request bodies that may contain PII.

## Error Handling
- All external API calls must have explicit timeout values.
- Never catch bare Exception silently.

## Style
- All functions must have a docstring.
```

**2.2 — Fetch RULES.md in the Review Service**

```python
def get_rules(repo_name: str) -> str:
    g = Github(os.getenv("GITHUB_TOKEN"))
    repo = g.get_repo(repo_name)
    try:
        content = repo.get_contents("RULES.md")
        return content.decoded_content.decode("utf-8")
    except:
        return "No RULES.md found. Apply general best practices only."
```

**2.3 — Define a structured output schema**

Create `models.py`:
```python
from pydantic import BaseModel
from typing import List, Literal

class ReviewComment(BaseModel):
    file: str
    line: int
    severity: Literal["BLOCKING", "WARNING", "SUGGESTION"]
    category: Literal["bug", "security", "style", "error_handling"]
    confidence: float  # 0.0 to 1.0
    message: str
    suggestion: str | None = None

class ReviewResult(BaseModel):
    comments: List[ReviewComment]
    summary: str
```

**2.4 — Update the Claude prompt for structured output**

```python
import json

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
```

**2.5 — Post line-level comments**

GitHub supports comments attached to specific lines in a diff. Update your posting logic:

```python
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
```

**2.6 — Test with intentional violations**

Add code to your sandbox repo that violates specific RULES.md rules. Verify that:
- The comment appears on the correct line
- Severity and category are correct
- RULES.md violations are caught separately from general bugs

**✅ Phase 2 checkpoint: Line-level, structured, rule-aware comments on the PR.**

---

## Phase 3 — RAG Over the Codebase
**Goal: LLM receives relevant repo context beyond just the diff, enabling cross-file reasoning.**
**Estimated time: 1–1.5 weeks**

### Steps

**3.1 — Install RAG dependencies**

```bash
pip install llama-index chromadb tree-sitter tree-sitter-python llama-index-vector-stores-chroma llama-index-embeddings-huggingface
```

**3.2 — Clone and index the target repo**

Create `indexer.py`. Uses HuggingFace embeddings (free, no API key needed):

```python
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core import StorageContext
from llama_index.legacy.embeddings.huggingface import HuggingFaceEmbedding
import chromadb

def build_index(repo_path: str):
    # Load all Python files from the repo
    documents = SimpleDirectoryReader(
        repo_path,
        required_exts=[".py"],
        recursive=True
    ).load_data()

    # Use HuggingFace embeddings (free, no API key required)
    embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")

    # Set up ChromaDB
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    chroma_collection = chroma_client.get_or_create_collection("codebase")
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # Build and persist the index
    index = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context,
        embed_model=embed_model
    )
    print(f"Indexed {len(documents)} files.")
    return index
```

Run this once to build the index from your sandbox repo. HuggingFace embeddings are open-source and free — no additional API keys needed.

**3.3 — Use Tree-sitter for smarter chunking (important)**

By default, LlamaIndex chunks by character count, which splits functions mid-way.
Tree-sitter chunks by AST — whole functions and classes only. This makes retrieval
far more useful. Add this to your `build_index()` function in `indexer.py`:

```python
from llama_index.core.node_parser import CodeSplitter

# Inside build_index(), after loading documents:
splitter = CodeSplitter(
    language="python",
    chunk_lines=40,
    chunk_lines_overlap=5,
    max_chars=1500,
)

# Pass to VectorStoreIndex.from_documents() as:
index = VectorStoreIndex.from_documents(
    documents,
    storage_context=storage_context,
    embed_model=embed_model,
    transformations=[splitter]
)
```

**3.4 — Build the retrieval function**

```python
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
```

**3.5 — Generate a retrieval query from the diff**

You need a good query to find relevant files. Ask Claude to generate one:

```python
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
```

**3.6 — Wire RAG context into the review prompt**

Update your main review function in `main.py`:

```python
import json
from models import ReviewResult

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
        messages=[{
            "role": "user",
            "content": f"""You are a senior software engineer performing a code review.

TEAM RULES:
{rules}

RELATED CODEBASE CONTEXT (files that may be affected by this change):
{context}

DIFF TO REVIEW:
{diff}

Return ONLY a JSON object matching this schema. No prose outside the JSON.
Schema: {json.dumps(schema)}"""
        }]
    )

    raw = message.content[0].text
    return ReviewResult.model_validate_json(raw)
```

**3.7 — Handle index freshness**

The index needs to stay current with the repo. Create `.github/workflows/re-index_codebase.yml`
in your **reviewer repo** to re-index whenever the sandbox repo changes:

```yaml
name: Re-index Codebase

on:
  push:
    branches:
      - main

jobs:
  reindex:
    runs-on: ubuntu-latest
    steps:
      - name: Re-index codebase
        run: |
          curl -X POST ${{ secrets.REVIEW_SERVICE_URL }}/reindex \
            -H "Content-Type: application/json" \
            -d "{\"repo\": \"${{ github.repository }}\"}"
```

Also add a `/reindex` endpoint to your FastAPI service in `main.py`:

```python
@app.post("/reindex")
async def reindex(request: Request):
    from indexer import build_index
    payload = await request.json()
    
    try:
        print(f"Re-indexing codebase...")
        build_index("path-to-your-sandbox-repo")
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

**✅ Phase 3 checkpoint: LLM comments reference code outside the diff (e.g. "this change will break the caller in `services/auth.py` line 88").**

---

## Phase 4 — Observability & Feedback
**Goal: Every LLM call tracked. Engineers can mark comments as helpful or not.**
**Estimated time: 3–4 days**

### Steps

**4.1 — Set up Langfuse**

- Sign up at langfuse.com (free tier)
- Get your `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`
- Add to `.env`

```bash
pip install langfuse
```

**4.2 — Instrument your LLM calls**

```python
from langfuse import Langfuse
from langfuse.decorators import observe

langfuse = Langfuse()

@observe()
def review_diff(diff: str, rules: str, pr_number: int) -> ReviewResult:
    # Your existing code — Langfuse auto-captures input/output/latency
    ...
```

Tag each trace with PR metadata:
```python
langfuse.trace(name="pr-review", metadata={"pr_number": pr_number, "repo": repo_name})
```

**4.3 — Track cost per PR**

Langfuse automatically computes token cost if you pass the model name.
Build a simple cost dashboard query: average cost per PR, total monthly cost.

**4.4 — Add a feedback endpoint**

When an engineer marks a comment as unhelpful, log it:

```python
@app.post("/feedback")
async def feedback(pr_number: int, comment_id: str, helpful: bool):
    langfuse.score(
        trace_id=comment_id,
        name="helpful",
        value=1 if helpful else 0
    )
    return {"status": "logged"}
```

You can trigger this via a slash command in the PR comment (e.g. `/not-helpful`) using
a GitHub Actions workflow that listens for issue_comment events.

**✅ Phase 4 checkpoint: Langfuse dashboard shows latency, cost, and feedback scores per PR.**

---

## Phase 5 — Polish & Deploy
**Goal: Live URL, professional README, demo for portfolio.**
**Estimated time: 3–4 days**

### Steps

**5.1 — Deploy the Review Service**

Railway is the easiest option:
- Push your reviewer repo to GitHub
- Connect it to Railway (railway.app)
- Add your environment variables in the Railway dashboard
- Railway auto-deploys on push — you get a live HTTPS URL

Update `REVIEW_SERVICE_URL` in your sandbox repo secrets to the Railway URL.

**5.2 — Handle ChromaDB in production**

ChromaDB's local `PersistentClient` writes to disk — this doesn't persist on Railway's
ephemeral filesystem. Options:
- Switch to **Pinecone** (free tier) for the vector store in production
- Or mount a Railway volume (paid feature)

Pinecone swap is ~20 lines of code using LlamaIndex's Pinecone integration.

**5.3 — Write the README**

A good portfolio README covers:
- What the project does and why (the problem)
- Architecture diagram (copy from the project summary)
- How to run it locally (step by step)
- Example screenshot of a PR with AI comments
- What you'd add next (shows awareness of limitations)

**5.4 — Record a demo**

A 2-minute screen recording showing:
1. A PR being opened with intentional bugs
2. The GitHub Action firing
3. Structured AI comments appearing on specific lines
4. The Langfuse dashboard showing the trace

Upload to YouTube (unlisted) and link in the README. This is what interviewers
actually watch.

**5.5 — Write about it**

Post a short LinkedIn article: "I built an AI code reviewer — here's what I learned."
Cover the RAG over codebase decision and what surprised you. This gets you inbound
interest without applying cold.

**✅ Phase 5 checkpoint: Live URL in README, demo video recorded, LinkedIn post published.**

---

## What Makes This Portfolio-Grade

- **RAG over a codebase** (not just documents) — shows you understand retrieval beyond the tutorial use case
- **Structured LLM output with schema enforcement** — shows production thinking
- **Observability from day one** — most candidates skip this entirely
- **RULES.md as a configuration layer** — shows you thought about the real user (the senior engineer), not just the demo
- **Honest scope** — you can articulate clearly what it does and doesn't do, and why

---

## What to Say in Interviews

**"What was the hardest part?"**
The RAG chunking strategy. Naively chunking by character count split functions in the
middle, which broke retrieval quality. Switching to Tree-sitter AST-based chunking — which
chunks by whole functions and classes — significantly improved the relevance of retrieved context.

**"What doesn't it do well?"**
It can't reason about runtime behaviour — it only sees static code. It also doesn't have
full repo awareness; it retrieves the top-K most relevant chunks, which means it can miss
relevant context if the retrieval query isn't precise. A next step would be generating
multiple retrieval queries per review.

**"How would you scale this?"**
Replace the GitHub Action HTTP call with a message queue (SQS or Pub/Sub) to handle
burst traffic when many PRs open simultaneously. The Review Service becomes a worker
pool rather than a synchronous endpoint.