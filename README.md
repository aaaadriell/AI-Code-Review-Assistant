# AI Code Review Assistant

An AI system that automatically reviews GitHub Pull Requests and posts structured, line-level
comments covering bug risks, security vulnerabilities, and style violations — built for teams
where senior engineer review time is a bottleneck. It handles the mechanical layer of review
(enforcing rules, catching obvious bugs, flagging security issues) so senior engineers can spend
their time on architecture and judgment calls instead.

In larger teams, PRs sit unreviewed for hours or days, junior engineers get inconsistent
feedback depending on who happens to review them, and team-specific conventions (internal
libraries, security policies, naming rules) rarely get enforced consistently across reviewers.
This system exists to close that gap automatically, on every single PR.

What follows is a walkthrough of exactly what happens, in order, from the moment a developer
opens a Pull Request to the moment feedback closes the loop.

---

## The Journey of a Pull Request

### It starts in the sandbox repo, not this one

The codebase being reviewed lives in a separate "sandbox" repository. This repo — the one
you're reading right now — is just the Review Service: a single FastAPI application that does
all the actual work. Three GitHub Actions workflows live in the sandbox repo and call this
service over HTTP whenever something interesting happens there. The first of those,
`ai-review.yml`, fires the moment a PR is opened or pushed to, and POSTs the repo name, PR
number, and base/head SHAs to the service's `/review` endpoint. Everything below happens inside
that one request.

### Step 1 — Gathering context

The service calls the GitHub API (via PyGithub) to pull the full diff, the list of files that
actually changed, and the repo's `RULES.md` — a file senior engineers write once, defining
team-specific standards ("use our internal `db.query()` wrapper, not raw psycopg2 calls", "all
new endpoints need rate limiting"). If a repo doesn't have one, the service just falls back to
general best practices.

### Step 2 — Why the diff alone isn't enough

A diff only shows what changed *inside* the files that were touched. But a function renamed in
one file can silently break three callers in files that never appear in the diff at all — and
a reviewer who only sees the diff has no way to catch that. So the service maintains a
searchable vector index of the *entire* codebase, and retrieves the pieces of it that are
actually relevant to this specific change before asking Claude to review anything.

Retrieval happens in three steps: Claude Haiku reads the diff and writes a short search query
("find usages of the `Customer` interface and the `fetchCustomer` API function"); that query is
embedded and used to pull the top-k most similar chunks of code out of a vector database; and
each retrieved chunk comes back with its file name and a cosine similarity score attached.

**How the codebase gets chunked, and why it changed.** The first version of this indexer used
LlamaIndex's default chunking, which splits files by raw character count — meaning a chunk could
end halfway through a function body, with the rest of that function in a completely different
chunk. Retrieval quality suffered for exactly the reason you'd expect: a query that should match
a whole function might only match half of it. The fix was switching to Tree-sitter, which parses
each file into its actual AST and chunks along function and class boundaries instead — so every
retrieved chunk is a complete, coherent unit of code, never a fragment. Python, TypeScript, and
TSX each get their own `CodeSplitter` instance, since each language needs its own grammar.

**Where the chunks live, and why that changed too.** The first version stored these vectors in
ChromaDB, which is genuinely the right choice for local development — zero setup, just a folder
on disk. It fell apart the moment this service moved to Railway: ChromaDB's `PersistentClient`
writes to local disk, and Railway's filesystem doesn't survive between deploys or restarts, so
the entire index would silently vanish on every redeploy. The fix was switching to Pinecone,
which is cloud-hosted — the index persists identically whether indexing runs on a laptop or
inside a Railway container, and `/reindex` (covered later) behaves the same way in both places.

**Why the embeddings are local, not OpenAI's.** LlamaIndex defaults to OpenAI's embedding model
if you don't specify one — which would mean depending on a second LLM provider just to turn text
into vectors. Instead, the service uses HuggingFace's `sentence-transformers/all-MiniLM-L6-v2`:
free, runs locally, needs no API key, and keeps the whole project resting on Anthropic plus
open-source tooling rather than a second paid API.

### Step 2.5 — How do you know retrieval is actually any good?

For a while, the honest answer was: you don't. Retrieval was a black box — Claude got some
"related codebase context" appended to its prompt, and there was no way to tell whether that
context was useful or noise. If a review missed an obvious cross-file issue, there was no way to
distinguish two very different failures: either retrieval never surfaced the relevant file at
all (a retrieval problem — fix chunking or the query), or it did surface the right file and
Claude just reasoned poorly about it (a prompting problem). Those need completely different
fixes, and you can't pick the right one blind.

Two things fixed this. First, retrieval is now its own instrumented Langfuse **retriever span**,
nested inside the same trace as the review it feeds — not a disconnected side-effect. Opening
any past review's trace shows exactly what query was generated, exactly which chunks came back,
and each one's file name and similarity score, sitting right next to what Claude actually said
in its review. That alone turns "why did it miss this" from a guessing game into a two-minute
lookup.

Second, similarity score on its own isn't the whole story — cosine similarity just measures how
close two pieces of text are in embedding space, and text can be geometrically similar without
being *useful for reviewing this specific diff*. So after every retrieval, Claude Haiku is
handed the diff and the retrieved chunks and asked to judge, as a reviewer would: how relevant
and useful is this context, actually, for this change? It returns a 0.0–1.0 score with a
one-sentence reason, logged to Langfuse as a `retrieval_relevance` score attached to that exact
retrieval span. This runs on every single review automatically, with no human involved, which
means there's now a continuous quality signal for retrieval you can track over time, and outlier
low scores point straight at the traces worth investigating.

It's worth being honest about the limit of this: the judge is still an LLM opinion, not ground
truth. A more rigorous next step would be a small hand-labeled eval set — a handful of diffs with
manually verified "these files should come back" answers, scored as recall@k — to periodically
check whether the automated judge's scores can actually be trusted, and to catch regressions
when the chunking strategy or embedding model changes. That hasn't been built yet; the two
mechanisms above are what exist today.

### Step 3 — The actual review

Claude Opus receives one structured prompt: the diff, the retrieved RAG context, and
`RULES.md`, with instructions to return JSON matching a strict schema
(`ReviewComment`/`ReviewResult` in `models.py`) rather than free-form prose. It reviews against
two distinct sources — its own training knowledge for general bug risk and security issues
(logic errors, broken calls, injection, exposed secrets), and `RULES.md` for whatever this
specific team has decided matters to them.

Structured output from an LLM isn't perfectly reliable in practice: Claude occasionally omits a
field — most often `confidence` — even when the prompt explicitly says every field is required.
Rather than let one missing field crash an entire review with a validation error, `confidence`
has a safe default (`0.5`) in the schema instead of being strictly required, so a single flaky
field degrades gracefully instead of failing the whole thing.

### Step 4 — Getting comments back onto the PR

Each comment gets posted to the PR via the GitHub API, attached to the specific line it refers
to, carrying a severity (BLOCKING / WARNING / SUGGESTION), a category, and a confidence score.

GitHub's review API is stricter than it looks: it only accepts line numbers that are actually
visible inside a diff hunk, not just any line that exists in the file. Claude occasionally
points at a line just outside that window, and posting a comment like that fails the entire
GitHub API call with a 422 — not just that one comment. So the service parses the real diff
hunks itself before posting anything, works out exactly which lines GitHub will actually accept,
and downgrades any comment that falls outside that range into the PR-level summary instead of
letting it take the whole review down.

One more thing happens here, invisibly: every posted comment carries a hidden HTML comment
(rendered as nothing on GitHub, but readable via the API) embedding that specific comment's
Langfuse trace ID and observation ID. That's the entire mechanism the feedback loop below runs
on — no database required.

### Step 5 — Closing the loop

This is where the second and third GitHub Actions workflows come in.

When an engineer replies to one of the AI's comments directly on GitHub, `ai-feedback.yml` fires
and forwards that reply to `/github-comment`. The service reads the hidden marker off the parent
comment to recover its trace and observation ID, sends the reply text to Claude Haiku to
classify whether it reads as "this was helpful" or "this was wrong/unhelpful", and logs that as
a boolean score against the *exact* comment being replied to — not the review as a whole. No
voting UI, no extra step for the engineer beyond just replying normally. A `/feedback` endpoint
also exists for submitting the same kind of score directly, useful for testing.

Separately, `re-index_codebase.yml` fires whenever the sandbox repo's `main` branch changes,
and calls `/reindex` to keep the vector index current. It re-fetches every source file directly
via the GitHub API rather than reading a local clone — the same trick that makes this work
identically whether it's triggered locally or on Railway, since Railway's containers don't have
a checkout of the sandbox repo sitting on disk anywhere. Every reindex wipes the Pinecone index
clean before rebuilding: documents aren't given stable IDs, so without a clean slate, re-indexing
the same repo twice would silently pile duplicate vectors on top of the old ones forever.

Every LLM call anywhere in this pipeline — query generation, the review itself, both feedback
classifiers — is traced automatically through Langfuse's `@observe()` decorator, so cost,
latency, and token usage per PR are visible without any extra instrumentation work.

---

## Real Bugs Found Along the Way

None of the decisions above were made in a vacuum — most of them came from something concrete
breaking. Documenting them honestly is more useful than pretending the system arrived fully
formed, and each one reveals a real assumption the system depends on.

**Two different embedding models for indexing vs. retrieval.** Early on, the indexer embedded
code with `BAAI/bge-small-en-v1.5` while retrieval used `sentence-transformers/all-MiniLM-L6-v2`.
Both happen to produce 384-dimensional vectors, so nothing crashed — but comparing vectors from
two different models means comparing two different, incompatible embedding spaces. A dimension
mismatch throws an error you can't miss; a *model* mismatch just quietly returns worse results
forever. Fixed by standardizing on one embedding model, used identically for both indexing and
retrieval — a constraint that has to hold for the vector search to mean anything at all.

**GitHub rejected comments with "Line could not be resolved."** GitHub's review API only accepts
line numbers visible inside a diff's hunk context, not just any line that exists in the file —
and posting even one comment outside that range fails the *entire* batched review, not just that
comment. The service's own diff parser had a second, subtler bug layered on top: Git's
`\ No newline at end of file` marker line doesn't start with `+`, `-`, or a space, so the parser's
fallback branch mistook it for a real context line, extending the "valid lines" set one line past
where the diff actually ended. Fixed by having the parser explicitly recognize and skip that
marker instead of guessing.

**Pinecone silently accumulated duplicate vectors.** Indexed `Document` objects were never given
stable IDs, so every re-index generated fresh random IDs and Pinecone just added them on top of
whatever was already there — nothing was ever overwritten. This went unnoticed until the RAG
observability work made it directly visible: the same file, same score, same content, showing up
twice in a five-result retrieval. That's exactly the kind of problem the observability work was
built to catch, and it caught one immediately. Fixed by wiping the Pinecone index clean before
every rebuild, since indexing here is always a full re-index, never incremental.

**The feedback webhook's bot-loop guard broke real feedback.** `/github-comment` originally
checked whether a comment's author matched the bot's own GitHub username, to stop the service
ever reacting to its own output. But the service authenticates as the same personal GitHub
account used for the engineer's own replies too — there's no separate bot account — so that check
silently discarded every real reply that came in. The safeguard that actually mattered was
already in place: the service never posts replies, only top-level review comments, so requiring
`in_reply_to_id` to be present is sufficient on its own. The redundant, incorrect check was
removed.

**PyGithub doesn't have the method its name suggests.** Fetching a single PR review comment by ID
isn't `Repository.get_pull_comment()` — that method doesn't exist. It's
`PullRequest.get_review_comment(id)`, which, despite living on a `PullRequest` object, doesn't
actually use the PR number in its request URL. `pr_number` is still explicitly forwarded through
the webhook payload anyway, rather than relying on that internal implementation detail holding
forever.

**A quoted environment variable broke production feedback, silently.** `LANGFUSE_BASE_URL` was
set on Railway with literal quote characters included in the value
(`"https://jp.cloud.langfuse.com"` instead of `https://jp.cloud.langfuse.com`). Locally this was
invisible, because `python-dotenv` strips surrounding quotes automatically when reading a `.env`
file — Railway's raw environment variables get no such treatment, so the service tried to POST to
a URL that literally started with a `"` character and failed with `No connection adapters were
found`. One real piece of engineer feedback was lost before this was caught and corrected.

**Railway's first deploy crashed immediately.** `ModuleNotFoundError: No module named 'fastapi'`
— there was no `requirements.txt` in the repo at all, so the build step had nothing to install
from, and no `Procfile` either, so even a successful install wouldn't have known to run
`uvicorn main:app`. Fixed by generating `requirements.txt` via `pip freeze` and adding a
`Procfile` that binds to Railway's dynamic `$PORT` instead of a hardcoded one.

---

## Architecture at a Glance

Now that you've seen the whole journey, here's the same system as a diagram. The Review Service
is a single FastAPI application — there's no separate RAG microservice; `indexer.py` is just a
shared module used both by a standalone local script and by `/reindex`. That's a deliberate
choice: the interesting engineering here is the RAG/evaluation logic, not the service count.

```
┌──────────────────────────────────────────────────────────────────┐
│                    Sandbox Repo (GitHub)                          │
│                                                                    │
│  ai-review.yml         ──► fires on PR opened/synchronize         │
│  ai-feedback.yml       ──► fires on PR review comment created     │
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
     │      Pinecone      │            │       Langfuse        │
     │  codebase vector idx│           │  traces, cost, RAG eval│
     │  (Python/TS/TSX,    │           │  scores, human feedback│
     │   Tree-sitter chunks)│          └───────────────────────┘
     └───────────────────┘
```

| Component | Responsibility | Tech |
|---|---|---|
| **Review Service** | Everything: diff fetch, RAG, review, comments, feedback, reindexing | FastAPI, Python |
| **Vector index** | Stores codebase chunks for retrieval | Pinecone + LlamaIndex |
| **GitHub Actions (sandbox repo)** | Triggers review, feedback forwarding, and reindexing | YAML workflows |
| **Observability** | Traces, cost, RAG evaluation scores, human feedback | Langfuse |
| **Deployment** | Native Python buildpack via `Procfile` + `requirements.txt` — no containerization needed | Railway |

---

## The Three GitHub Actions Workflows, in Detail

All three live in the **sandbox repo**, not this one — this repo is the service being called;
the sandbox repo is the codebase being reviewed, and it's the one that actually experiences PR
events. Each workflow does the minimum possible work itself and immediately hands off to the
Review Service via `secrets.REVIEW_SERVICE_URL`.

### `ai-review.yml` — the entry point
```yaml
on:
  pull_request:
    types: [opened, synchronize]
```
Fires whenever a PR is opened or pushed to. POSTs the repo name, PR number, and base/head SHAs
to `/review`. This is what kicks off everything described above.

### `ai-feedback.yml` — forwards reply feedback
```yaml
on:
  pull_request_review_comment:
    types: [created]
```
Fires on *every* new PR review comment, including the bot's own initial comments and human
replies — the Review Service decides server-side whether a given comment is actually worth
scoring.

This one is deliberately built with `actions/github-script` instead of a plain `curl` inside a
`run:` step. A naive version of this workflow would interpolate
`${{ github.event.comment.body }}` — arbitrary, attacker-controllable PR comment text — directly
into a shell command, which is a real
[script-injection vulnerability](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#understanding-the-risk-of-script-injections):
anyone able to comment on the PR could inject shell metacharacters. `github-script` runs in
Node.js and sends the comment body through `JSON.stringify()` inside a `fetch()` call instead,
so untrusted text never touches a shell at all.

### `re-index_codebase.yml` — keeps the index fresh
```yaml
on:
  push:
    branches:
      - main
```
Fires whenever `main` changes. POSTs the repo name to `/reindex`, which rebuilds the Pinecone
index from scratch by reading every source file straight from the GitHub API.

---

## Local Development, Without Docker

This project was built on a machine that couldn't run Docker Desktop — no hardware
virtualization support — which ruled out the originally-planned local setup of an ngrok tunnel
plus Docker Compose. Rather than fight that constraint, local development skips tunneling
entirely:

- The FastAPI service runs locally with `uvicorn main:app --reload`.
- Instead of exposing it to the public internet so GitHub's real Action could reach it,
  `test_trigger.py` simulates exactly what that Action would send: it fetches every open PR on
  the sandbox repo via the GitHub API and POSTs the same JSON payload (repo, PR number, base/head
  SHA) straight to `http://localhost:8000/review`. This exercises the *entire* pipeline — diff
  fetch, RAG retrieval, the Claude review, comment posting, Langfuse tracing — with zero public
  exposure.
- The real GitHub Actions workflows only come into play once the service is deployed to Railway
  and reachable at a public URL — which is also where they're actually meant to run in
  production, so nothing about local testing is a compromise.

Endpoints can also be exercised directly:

```bash
# Trigger a review for every open PR on the sandbox repo
python test_trigger.py

# Manually submit feedback for a specific comment (trace_id/observation_id from Langfuse)
curl -X POST http://localhost:8000/feedback \
  -H "Content-Type: application/json" \
  -d '{"pr_number": 13, "comment_id": "<trace_id>", "observation_id": "<observation_id>", "helpful": true}'

# Rebuild the vector index on demand
curl -X POST http://localhost:8000/reindex \
  -H "Content-Type: application/json" \
  -d '{"repo": "aaaadriell/AI-Code-Review-Sandbox"}'
```

---

## API Reference

| Endpoint | Method | Triggered by | Purpose |
|---|---|---|---|
| `/review` | POST | `ai-review.yml` (or `test_trigger.py` locally) | Runs the full review pipeline for one PR |
| `/feedback` | POST | Manual / testing | Directly submits a helpful/unhelpful score for a comment |
| `/github-comment` | POST | `ai-feedback.yml` | Classifies a GitHub reply and logs it as feedback, if it's a reply to an AI comment |
| `/reindex` | POST | `re-index_codebase.yml` (or manually) | Rebuilds the Pinecone index from the sandbox repo's current state |

---

## Limitations & What This Doesn't Do

Being direct about the edges of the system, rather than overselling it:

- **The RAG relevance judge is an LLM opinion, not ground truth.** It's genuinely useful as a
  continuous signal and for spotting outlier traces worth investigating, but it hasn't been
  validated against a hand-labeled dataset. Treat a `retrieval_relevance` trend as a strong hint,
  not a certified metric. A proper fix would be a small hand-labeled eval set (known diffs with
  manually verified "these files should come back" answers, scored as recall@k) to periodically
  check whether the judge's scores can be trusted — that hasn't been built yet.
- **No runtime or dynamic reasoning.** The review is entirely static analysis over a diff and
  retrieved source. It can't know how code actually behaves at runtime, so bugs that only
  manifest under specific runtime conditions are out of reach.
- **Retrieval is a single similarity search, not multi-hop reasoning.** One search query gets
  generated from the diff, and the top-k most similar chunks get pulled once. A change with a
  very indirect blast radius (three layers of indirection away from anything textually similar to
  the diff) may simply not be part of what gets retrieved.
- **Full re-index every time, not incremental.** `/reindex` rebuilds the entire vector index from
  scratch on every call. This is simple and correct, but doesn't scale gracefully to very large
  repositories — each file is fetched from the GitHub API individually, so indexing time and API
  calls grow linearly with repo size.
- **Three languages, one general-purpose embedding model.** Python, TypeScript, and TSX are
  chunked with language-aware Tree-sitter grammars, but everything is embedded with the same
  general-purpose sentence-transformer rather than anything code-specialized.

---

## Appendix: `RULES.md` Example

Senior engineers write this once, at project start:

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

## Appendix: Structured Output Example

What Claude actually returns for a PR, before it gets turned into GitHub comments:

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
