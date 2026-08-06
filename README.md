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
A developer opens or updates a Pull Request on the target repository (the codebase being
reviewed). A GitHub Actions workflow in *that* repo fires on `pull_request` events and calls
the Review Service's `/review` endpoint. (This repo only contains the workflow that keeps the
RAG index fresh — see Step 5; the PR-trigger workflow lives in each reviewed repo.)

### Step 1 — Fetch PR Context
The Review Service calls the GitHub API to retrieve:
- The full diff (what changed)
- A list of all files modified in the PR
- The `RULES.md` file from the root of the repository

### Step 2 — RAG: Retrieve Relevant Repo Context
The diff alone is insufficient. A function changed in one file may break callers in three
other files not in the diff. To handle this, the system maintains a vector index of the
entire codebase.

At review time:
- Changed files are identified
- The RAG service retrieves the most semantically relevant files — files that import changed
  modules, files in the same package, files with related function signatures
- These are appended to the prompt as supporting context

This is the core technical differentiator of the project.

### Step 3 — LLM Review
The orchestrator constructs a structured prompt containing:
- The PR diff
- Retrieved repo context from RAG
- The team's `RULES.md`
- A strict output schema (JSON) specifying how comments must be formatted

Claude reviews the diff against:
- **Its own training knowledge** for bug risks (null pointer risks, race conditions, logic
  errors) and security issues (injection, auth bypass, exposed secrets, OWASP Top 10)
- **RULES.md** for team-specific standards (naming conventions, required patterns, internal
  library usage, company security policies)

### Step 4 — Post Comments to PR
The structured JSON response is parsed and each comment is posted to the PR via the GitHub
API — attached to the specific line in the diff it refers to, with a severity label
(BLOCKING / WARNING / SUGGESTION) and a confidence score.

### Step 5 — Observability & Feedback
- Every LLM call is logged to Langfuse: latency, token count, cost per PR
- Each posted comment has a hidden HTML marker embedding the Langfuse trace ID and the
  observation ID for that specific comment
- When an engineer replies to an AI comment on GitHub, a webhook fires to `/github-comment`,
  which reads the marker off the parent comment, uses Claude (Haiku) to classify whether the
  reply indicates the comment was helpful, and logs that as a boolean score on the exact trace
  in Langfuse — fully automatic, no manual voting UI required
- A `/feedback` endpoint also exists for submitting the same kind of score directly (used for
  testing, or a future UI)
- The codebase is kept current for RAG retrieval via a separate `/reindex` endpoint, triggered
  automatically by a GitHub Actions workflow whenever the target repo's `main` branch changes

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        GitHub                               │
│   PR Opened/Updated ──► GitHub Actions Workflow             │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP POST (diff, repo, PR metadata)
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                   Review Service (FastAPI)                   │
│                                                             │
│  1. Fetch diff + RULES.md via GitHub API                    │
│  2. Call RAG Service for relevant repo context              │
│  3. Build prompt, call Claude API                           │
│  4. Parse structured JSON response                          │
│  5. Post line comments back to GitHub PR                    │
│  6. Log to Langfuse                                         │
└────────┬─────────────────────────────────┬──────────────────┘
         │                                 │
         ▼                                 ▼
┌─────────────────────┐       ┌────────────────────────────┐
│    RAG Service      │       │         Claude API          │
│                     │       │      (Anthropic)            │
│  - Vector DB        │       └────────────────────────────┘
│    (Pinecone)       │
│  - Codebase index   │       ┌────────────────────────────┐
│  - Retriever        │       │         Langfuse            │
└─────────────────────┘       │   (Observability & Logs)    │
                               └────────────────────────────┘
```

### Services

| Service | Responsibility | Tech |
|---|---|---|
| **Review Service** | Orchestrates the full review pipeline | FastAPI, Python |
| **RAG Service** | Indexes codebase, retrieves relevant context at review time | LlamaIndex, Pinecone |
| **GitHub Actions** | Triggers the pipeline on PR events | YAML workflow |
| **Observability** | Logs latency, cost, token usage, feedback | Langfuse |

This is intentionally a **lean two-service architecture** rather than over-engineered
microservices. The Review Service is the core; the RAG Service is the technically
interesting component you can speak to in interviews.

---

## Tools & Technologies

| Category | Tool | Why |
|---|---|---|
| **LLM** | Claude API (Anthropic) | Strong code understanding, structured output support |
| **RAG Framework** | LlamaIndex | Better suited for codebase indexing than LangChain |
| **Vector DB** | Pinecone | Cloud-hosted, so the index persists identically whether indexing runs locally or on Railway — no local-disk persistence problem to solve |
| **Code Chunking** | Tree-sitter | Splits code by AST (functions, classes) rather than arbitrary characters — far better for retrieval. Currently supports Python, TypeScript, and TSX |
| **API Framework** | FastAPI | Async, fast, easy to document |
| **GitHub Integration** | PyGitHub | Python wrapper for GitHub REST API |
| **CI Trigger** | GitHub Actions | Native, no extra infra needed |
| **Observability** | Langfuse | Tracks cost, latency, and feedback per PR |
| **Containerisation** | Docker + Docker Compose | Packages Review Service + ChromaDB together |
| **Deployment** | Railway or Render | Free-tier friendly for a portfolio project |

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