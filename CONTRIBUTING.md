# Contributing

## Development

```bash
uv sync                                     # Python 3.12 + dependencies into .venv
uv run pytest                               # tests (use a fake app-server, no model calls)
uv run uvicorn app.main:app --reload        # local server on :8000
uv run python scripts/bench_latency.py      # latency against a running server
```

## Guidelines

- Keep the OpenAI-compatible surface stable: `/v1/models`, `/v1/chat/completions` (streaming, `response_format`, `usage`) and `/v1/responses`.
- Changes to request parsing, prompt mapping, the lean app-server profile or anything security-related (sandbox, approvals, image inputs) need tests in `tests/`.
- Document new environment variables in `docs/ENV.md` and `.env.example`.
- Commit messages: short and imperative, with a `feat:`, `fix:`, `docs:` or `chore:` prefix.
