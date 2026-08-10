# AI Code Review Assistant — Project Summary

## Overview

A production-grade AI system that automatically reviews GitHub Pull Requests and posts structured,
line-level comments covering bug risks, security vulnerabilities, and style violations. Designed
for larger engineering teams where senior engineer review time is a bottleneck.

The system handles the **mechanical review layer** — enforcing rules, catching obvious bugs,
flagging security issues — so that senior engineers can focus purely on architectural decisions
and higher-order reasoning when they do review.

---

## The Problem

In larger engineering teams:
- Senior engineers spend significant time on repetitive, rule-based review work
- PRs sit unreviewed for hours or days, blocking velocity
- Junior engineers get inconsistent feedback depending on who reviews them
- Team-specific conventions (internal libraries, security policies, naming rules) are not
  consistently enforced across reviewers

---

## How the System Works

### Trigger
A developer opens or updates a Pull Request on the sandbox (target) repository. A GitHub
Actions workflow **in that repo** (`ai-review.yml`) fires on `pull_request` events and calls
the Review Service's `/review` endpoint with the repo name, PR number, and base/head SHAs.
All three CI/CD workflows that drive this system live in the sandbox repo, not this one —
see the [CI/CD](#cicd--github-actions-in-the-sandbox-repo) section below.

### Step 1 — Fetch PR Context
The Review Service calls the GitHub API (via PyGithub) to retrieve:
- The full diff (what changed, per file)
- The list of files actually modified in the PR
- The `RULES.md` file from the root of the repository (falls back to "apply general best
  practices" if the repo doesn't have one)

### Step 2 — RAG: Retrieve Relevant Repo Context
The diff alone is insufficient — a function changed in one file may break callers in files
not in the diff at all. So the service maintains a vector index of the entire codebase in
Pinecone and retrieves relevant context at review time:

1. Claude Haiku generates a short search query from the diff (e.g. "find usages of the
   `Customer` interface and the `fetchCustomer` API function")
2. That query is embedded and used to retrieve the top-k most similar code chunks from Pinecone
3. Each retrieved chunk carries its file name and cosine similarity score, both surfaced back
   into the prompt and logged for evaluation (see Observability below)

This retrieval step is instrumented as its own Langfuse **retriever** span, nested under the
same trace as the review it feeds into — not a disconnected, invisible step.

### Step 3 — LLM Review
Claude Opus receives a structured prompt containing the diff, the retrieved RAG context, and
`RULES.md`, and is required to return JSON matching a strict Pydantic schema
(`ReviewComment`/`ReviewResult` in `models.py`). It reviews against:
- **Its own training knowledge** for bug risks (logic errors, broken function calls, type
  mismatches) and security issues (injection, auth bypass, exposed secrets)
- **RULES.md** for team-specific standards

LLM structured output isn't always perfectly reliable — Claude occasionally omits a field
(most often `confidence`) even when told it's required. To keep one flaky field from crashing
an entire review, `confidence` has a safe default (`0.5`) in the schema rather than being
strictly required.

### Step 4 — Post Comments to PR
Each comment is posted via the GitHub API, attached to the specific line it refers to, with
severity (BLOCKING / WARNING / SUGGESTION), category, and confidence.

GitHub's review API only accepts line numbers that are actually visible within a diff hunk —
not just any line that exists in the file. Since Claude occasionally points at a line just
outside that window, the service parses the real diff hunks itself (`_valid_diff_lines`) and
downgrades any comment whose line can't be anchored into the PR-level summary instead of
letting the whole review fail with a GitHub 422.

Each posted comment also carries a **hidden HTML marker** (invisible when rendered on GitHub)
embedding that comment's exact Langfuse trace ID and observation ID — this is what makes the
feedback loop in Step 5 possible without a database.

### Step 5 — Observability, RAG Evaluation & Feedback
Every review is one connected Langfuse trace, not a scattered pile of disconnected logs:

- **Cost/latency tracking** — every LLM call (query generation, review, classification) is
  logged automatically via Langfuse's `@observe()` decorator: tokens, latency, cost per PR
- **RAG evaluation, tier 1 (visibility)** — the retriever span logs every retrieved chunk with
  its real similarity score, so you can inspect exactly what was retrieved for any past review
  instead of treating RAG as a black box
- **RAG evaluation, tier 2 (automated)** — after every retrieval, Claude Haiku acts as a judge,
  scoring how relevant the retrieved chunks actually were to the diff (0.0–1.0), logged as a
  Langfuse score attached directly to the retrieval span. This runs on every single review with
  no human input required
- **Human feedback (automatic)** — when an engineer replies to an AI comment on GitHub, the
  `ai-feedback.yml` workflow forwards it to `/github-comment`, which reads the hidden marker
  off the parent comment, uses Claude Haiku to classify whether the reply indicates the comment
  was helpful, and logs a boolean score against the *exact* comment (not just the review as a
  whole) — fully automatic, no voting UI needed
- **Human feedback (manual)** — `/feedback` accepts the same kind of score directly, used for
  testing or a future UI
- **Index freshness** — the codebase index is kept current via `/reindex`, triggered by the
  `re-index_codebase.yml` workflow whenever the sandbox repo's `main` branch changes. Re-indexing
  always wipes the Pinecone index before rebuilding, since documents aren't given stable IDs and
  re-indexing without a clean slate silently accumulates duplicate vectors over time

---

## System Architecture

The Review Service is a **single FastAPI application** (`main.py`) — it fetches diffs, retrieves
RAG context, calls Claude, posts comments, handles feedback, and re-indexes, all in one process.
There is no separate RAG microservice; `indexer.py` is a shared module used both by a standalone
local script and by the `/reindex` endpoint. This is intentionally lean rather than
over-engineered — the RAG/evaluation logic is what's technically interesting, not the service
count.

```
┌──────────────────────────────────────────────────────────────────┐
│                    Sandbox Repo (GitHub)                         │
│                                                                    │
│  ai-review.yml    ──► fires on PR opened/synchronize              │
│  ai-feedback.yml  ──► fires on PR review comment created          │
│  re-index_codebase.yml ──► fires on push to main                  │
└──────┬──────────────────────┬──────────────────────┬──────────────┘
       │ /review              │ /github-comment       │ /reindex
       ▼                      ▼                       ▼
┌───────────────────────────────────────────────────────────────────┐
│                  Review Service (FastAPI, Railway)                 │
│                                                                     │
│  get_pr_diff / get_rules ──► PyGithub ──► GitHub REST API          │
│  generate_retrieval_query ──► Claude Haiku                         │
│  retrieve_context ──► Pinecone (embeddings via HuggingFace model)  │
│  review_diff ──► Claude Opus ──► structured JSON (Pydantic)        │
│  post_review_comments ──► PyGithub (line-validated, hidden markers)│
│  classify_feedback_helpfulness / classify_retrieval_relevance      │
│    ──► Claude Haiku (LLM-as-judge)                                 │
│  every call traced via @observe() ──► Langfuse                     │
└──────────────┬───────────────────────────────┬─────────────────────┘
               ▼                                ▼
     ┌───────────────────┐            ┌───────────────────────┐
     │      Pinecone       │            │       Langfuse         │
     │  codebase vector idx│            │  traces, cost, RAG eval│
     │  (Python/TS/TSX,     │            │  scores, human feedback│
     │   Tree-sitter chunks)│            └───────────────────────┘
     └───────────────────┘
```

### Components

| Component | Responsibility | Tech |
|---|---|---|
| **Review Service** | Everything: diff fetch, RAG, review, comments, feedback, reindexing | FastAPI, Python |
| **Vector index** | Stores codebase chunks for retrieval | Pinecone + LlamaIndex |
| **GitHub Actions (sandbox repo)** | Triggers review, feedback forwarding, and reindexing | YAML workflows |
| **Observability** | Traces, cost, RAG evaluation scores, human feedback | Langfuse |

---

## Tools & Technologies

| Category | Tool | Why |
|---|---|---|
| **LLM (review)** | Claude Opus | Strong code understanding, structured JSON output |
| **LLM (cheap tasks)** | Claude Haiku | Retrieval query generation, RAG-relevance judging, and reply-sentiment classification are all cheap, fast tasks that don't need Opus |
| **RAG Framework** | LlamaIndex | Better suited for codebase indexing than LangChain |
| **Vector DB** | Pinecone (previously ChromaDB) | Started with ChromaDB for local development since it's zero-setup and file-based. Switched to Pinecone once deploying to Railway, because ChromaDB's `PersistentClient` writes to local disk — which doesn't survive Railway's ephemeral filesystem between deploys/restarts. Pinecone is cloud-hosted, so the index persists identically whether indexing runs locally or in production, and `/reindex` works the same way in both places |
| **Embeddings** | HuggingFace `sentence-transformers/all-MiniLM-L6-v2` | LlamaIndex defaults to OpenAI embeddings, which would mean depending on a second LLM provider just for embeddings. This model is free, runs locally, and needs no API key, keeping the project entirely on Anthropic + open-source tooling |
| **Code Chunking** | Tree-sitter (`CodeSplitter`), previously LlamaIndex's default chunker | Started with LlamaIndex's default character-count chunking, which splits functions and classes mid-way — a chunk could end halfway through a function body, badly hurting retrieval relevance. Switched to Tree-sitter, which parses each file's AST and chunks along function/class boundaries instead, so retrieved context is always a complete, coherent unit. One `CodeSplitter` per language (Python, TypeScript, TSX), since each needs its own grammar |
| **API Framework** | FastAPI | Async, fast, easy to document |
| **GitHub Integration** | PyGitHub | Python wrapper for GitHub REST API |
| **CI/CD** | GitHub Actions (in the sandbox repo) | Native, no extra infra needed — see below |
| **Observability & RAG eval** | Langfuse | Tracks cost, latency, retrieval quality, and both automated and human feedback per PR |
| **Deployment** | Railway | Native Python buildpack via `Procfile` + `requirements.txt` — no containerization was needed |

---

## CI/CD — GitHub Actions in the Sandbox Repo

All three workflows that drive this system live in the **sandbox repo**
(`AI-Code-Review-Sandbox`), not this repo — this repo is the service being called, the
sandbox repo is the codebase being reviewed, and it's the one that experiences PR events.
Each workflow does the minimum possible work itself and immediately hands off to the Review
Service via `secrets.REVIEW_SERVICE_URL`.

### `ai-review.yml` — triggers a review
```yaml
on:
  pull_request:
    types: [opened, synchronize]
```
Fires whenever a PR is opened or pushed to. POSTs the repo name, PR number, and base/head
SHAs to `/review`. This is the main entry point for the whole system.

### `ai-feedback.yml` — forwards reply feedback
```yaml
on:
  pull_request_review_comment:
    types: [created]
```
Fires on *every* new PR review comment — including the bot's own initial comments and human
replies. It forwards the comment to `/github-comment`, which decides server-side whether it's
a reply worth scoring (see Step 5 above).

This one is built with `actions/github-script` rather than a plain `curl` in a `run:` step,
deliberately. A naive workflow would interpolate `${{ github.event.comment.body }}` — arbitrary,
attacker-controllable PR comment text — directly into a shell command, which is a real
[script-injection vulnerability](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#understanding-the-risk-of-script-injections):
anyone who can comment on the PR could inject shell metacharacters. `github-script` runs in
Node.js and sends the body through `JSON.stringify()` inside a `fetch()` call, so untrusted text
never touches a shell at all.

### `re-index_codebase.yml` — keeps the RAG index fresh
```yaml
on:
  push:
    branches:
      - main
```
Fires whenever `main` changes. POSTs the repo name to `/reindex`, which re-fetches every
source file **directly via the GitHub API** (not a local clone) and rebuilds the Pinecone
index from scratch. Reading from the GitHub API rather than disk is what makes this work
identically whether `/reindex` runs locally or on Railway — Railway's containers don't have
a checkout of the sandbox repo sitting on disk.

---

## RULES.md — What It Looks Like

Senior engineers define this once at project start. Example:

```markdown
# Code Review Rules

## Security
- All database queries must use parameterised inputs. Never use string concatenation in SQL.
- Never log request bodies or response payloads that may contain PII.
- All new API endpoints must include rate limiting middleware.

## Error Handling
- All external API calls must have explicit timeout and retry logic.
- Never catch generic Exception silently — always log with context.

## Style
- Use the internal `db.query()` wrapper, not raw psycopg2 calls.
- All new functions must have a docstring.
- Background tasks must be registered in `tasks/registry.py`.
```

The LLM treats this as a hard ruleset to enforce, separate from its general knowledge.

---

## Output Format (Structured JSON from LLM)

```json
{
  "comments": [
    {
      "file": "src/api/users.py",
      "line": 42,
      "severity": "BLOCKING",
      "category": "security",
      "confidence": 0.95,
      "message": "SQL query is built via string concatenation on line 42. This is vulnerable to SQL injection. Use parameterised queries instead.",
      "suggestion": "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
    },
    {
      "file": "src/services/payment.py",
      "line": 17,
      "severity": "WARNING",
      "category": "error_handling",
      "confidence": 0.80,
      "message": "External Stripe API call has no timeout set. Under network degradation this will block indefinitely.",
      "suggestion": "Add timeout=30 to the requests.post() call."
    }
  ],
  "summary": "2 blocking issues, 1 warning. PR should not be merged until security issues are resolved."
}
```

---