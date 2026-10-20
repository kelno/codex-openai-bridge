# Repository Guidelines

## Project Structure
- `app/` is the FastAPI service. `main.py` holds the endpoints (`/v1/models`, `/v1/chat/completions`, `/v1/responses`); `codex.py` runs one persistent `codex app-server` process and multiplexes requests over it as ephemeral threads; `models.py`, `prompt.py`, `images.py`, `schemas.py`, `config.py` and `deps.py` handle model names, message mapping, image inputs, API models, settings and auth/rate limiting.
- `tests/` contains pytest suites; `tests/fake_app_server.py` stands in for `codex app-server`.
- `scripts/bench_latency.py` measures time to first token and total latency against a running server.
- `docs/ENV.md` is the environment variable reference.

## Commands
- `uv sync` installs dependencies (Python 3.12, see `.python-version`).
- `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` runs the server.
- `uv run pytest` runs the tests.

## Coding Style
- PEP 8, 4-space indentation, snake_case functions, PascalCase Pydantic models, type hints.
- Keep endpoints async and delegate Codex interaction to `app.codex`.
- Add short docstrings where behaviour is security-sensitive (sandbox, approvals, image inputs).

## Testing
- Cover streaming and non-streaming flows; use the fake app-server, never a real model.
- Add regression tests when changing request parsing, prompt mapping, the lean profile or `response_format` handling.

## Commits
- Concise, imperative messages with `feat:`, `fix:`, `docs:` or `chore:` prefixes.
- Document new environment variables in `docs/ENV.md` and `.env.example`.
