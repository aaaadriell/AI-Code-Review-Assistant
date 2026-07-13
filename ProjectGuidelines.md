## What You Need to Begin

### Accounts & API Keys
- [ ] Anthropic account — get a Claude API key at console.anthropic.com
- [ ] GitHub account with a test repository
- [ ] Langfuse account (free tier) — langfuse.com
- [ ] Pinecone account (free tier) — for when you move off local ChromaDB

### Local Environment
- [ ] Python 3.11+
- [ ] Docker Desktop
- [ ] A GitHub Personal Access Token with `repo` and `pull_requests` scopes

### Knowledge Prerequisites
Before starting, you should be comfortable with:
- [ ] FastAPI basics (routing, request/response models, async)
- [ ] GitHub Actions YAML syntax (triggers, jobs, steps)
- [ ] Basic RAG concepts (embedding, vector search, retrieval) — complete the
      DeepLearning.ai RAG short course first if not already done
- [ ] Calling the Anthropic API and requesting structured JSON output

### Suggested Build Order
1. **GitHub Action → diff fetch → raw Claude call → hardcoded comment posted to PR**
   Get the end-to-end pipe working before adding complexity.
2. **Add RULES.md parsing** into the prompt.
3. **Add structured JSON output** and proper line-level comment posting.
4. **Add RAG service** — index a sample repo, wire retrieval into the prompt.
5. **Add Langfuse observability.**
6. **Add feedback endpoint** (thumbs up/down on comments).
7. **Dockerise and deploy** to Railway.

---

## What Makes This Portfolio-Grade

- **RAG over a codebase** (not just documents) — shows you understand retrieval beyond the tutorial use case
- **Structured LLM output with schema enforcement** — shows production thinking
- **Observability from day one** — most candidates skip this entirely
- **RULES.md as a configuration layer** — shows you thought about the real user (the senior engineer), not just the demo
- **Honest scope** — you can articulate clearly what it does and doesn't do, and why