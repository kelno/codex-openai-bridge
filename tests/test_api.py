"""API tests against a fake `codex app-server` (tests/fake_app_server.py)."""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import codex as codex_module
from app import main
from app.config import settings

FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")
FAKE_COMMAND = [sys.executable, str(FAKE_SERVER)]

SCHEMA = {
    "type": "object",
    "properties": {"echo": {"type": "string"}},
    "required": ["echo"],
    "additionalProperties": False,
}


@pytest.fixture
def backend_log(monkeypatch, tmp_path):
    """Point the Codex client at the fake server; return a reader for its request log."""

    log_path = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_APP_SERVER_LOG", str(log_path))
    monkeypatch.setattr(settings, "codex_workdir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "timeout_seconds", 10)
    monkeypatch.setattr(settings, "idle_threads", 2)  # the pool tests count thread starts
    main._REJECTED_SCHEMAS.clear()
    monkeypatch.setattr(main.codex, "_command_factory", lambda: FAKE_COMMAND)

    def requests(method):
        if not log_path.exists():
            return []
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        return [e["params"] for e in entries if e["method"] == method]

    return requests


@pytest.fixture
def client(backend_log):
    with TestClient(main.app) as c:
        yield c


def _chat(client, content, **body):
    payload = {"model": "gpt-5", "messages": [{"role": "user", "content": content}], **body}
    return client.post("/v1/chat/completions", json=payload)


def _sse_chunks(text):
    return [
        json.loads(line[len("data: "):])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def test_non_stream_returns_unique_ids_and_real_usage(client, backend_log):
    body = {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "You are a blacksmith."},
            {"role": "user", "content": "hello"},
        ],
    }
    first = client.post("/v1/chat/completions", json=body).json()
    second = client.post("/v1/chat/completions", json=body).json()

    assert first["choices"][0]["message"]["content"] == "echo:hello"
    assert first["id"].startswith("chatcmpl-") and first["id"] != second["id"]
    assert first["object"] == "chat.completion"
    assert first["model"] == "gpt-5"
    assert first["usage"] == {
        "prompt_tokens": 105,
        "completion_tokens": len("echo:hello"),
        "total_tokens": 105 + len("echo:hello"),
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }

    thread = backend_log("thread/start")[0]
    assert thread["baseInstructions"] == "You are a blacksmith."
    assert thread["ephemeral"] is True
    assert thread["approvalPolicy"] == "never"
    assert thread["sandbox"] == "read-only"
    assert "model" not in thread  # chosen per turn, so spare threads work for any model
    assert backend_log("turn/start")[0]["model"] == "gpt-5"


def test_usage_is_omitted_when_not_reported(client):
    resp = _chat(client, "NOUSAGE please").json()
    assert resp["choices"][0]["message"]["content"] == "echo:NOUSAGE please"
    assert "usage" not in resp


def test_stream_chunks_share_id_and_usage_is_opt_in(client):
    chunks = _sse_chunks(_chat(client, "stream me", stream=True).text)
    ids = {c["id"] for c in chunks}
    assert len(ids) == 1 and ids.pop().startswith("chatcmpl-")
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "echo:stream me"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert not any("usage" in c for c in chunks)

    chunks = _sse_chunks(_chat(client, "stream me", stream=True, stream_options={"include_usage": True}).text)
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["completion_tokens"] == len("echo:stream me")


def test_json_schema_response_format_is_forwarded(client, backend_log):
    resp = _chat(
        client,
        "npc line",
        model="gpt-5 low",
        response_format={"type": "json_schema", "json_schema": {"name": "npc", "schema": SCHEMA, "strict": True}},
    ).json()

    assert json.loads(resp["choices"][0]["message"]["content"]) == {"echo": "npc line"}
    assert resp["model"] == "gpt-5 low"
    turn = backend_log("turn/start")[0]
    assert turn["outputSchema"] == SCHEMA
    assert turn["effort"] == "low"


LOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "place": {"type": "string"}},
                "required": ["name"],
            },
        }
    },
    "required": ["items"],
}


def test_non_strict_schema_is_described_in_instructions(client, backend_log):
    # OpenAI's default is strict=false: best effort, any schema accepted.
    resp = _chat(client, "list", response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": LOOSE_SCHEMA}})
    assert resp.status_code == 200
    assert "outputSchema" not in backend_log("turn/start")[0]
    instructions = backend_log("thread/start")[0]["baseInstructions"]
    assert "matches this JSON Schema" in instructions and '"place"' in instructions


def test_strict_schema_rejected_by_backend_falls_back_and_is_remembered(client, backend_log):
    fmt = {"type": "json_schema", "json_schema": {"name": "x", "schema": LOOSE_SCHEMA, "strict": True}}
    resp = _chat(client, "list", response_format=fmt)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "echo:list"
    first, retry = backend_log("turn/start")
    assert first["outputSchema"] == LOOSE_SCHEMA and "outputSchema" not in retry
    threads = {t["_threadId"]: t for t in backend_log("thread/start")}
    assert '"place"' in threads[retry["threadId"]]["baseInstructions"]

    # The rejection is remembered: the next request goes straight to the fallback.
    assert _chat(client, "again", response_format=fmt, stream=True).status_code == 200
    assert "outputSchema" not in backend_log("turn/start")[2]
    assert len(backend_log("turn/start")) == 3


def test_strict_schema_fallback_also_works_when_streaming(client):
    fmt = {"type": "json_schema", "json_schema": {"name": "x", "schema": LOOSE_SCHEMA, "strict": True}}
    resp = _chat(client, "list", response_format=fmt, stream=True)
    assert resp.status_code == 200
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in _sse_chunks(resp.text) if c["choices"])
    assert text == "echo:list"


def test_openai_request_errors_map_to_400_with_code(client):
    resp = _chat(client, "BADREQ")
    assert resp.status_code == 400
    assert resp.json()["detail"] == {
        "message": "Something about the request is invalid.",
        "type": "invalid_request_error",
        "code": "invalid_value",
    }


def test_json_object_mode_adds_instruction(client, backend_log):
    _chat(client, "x", response_format={"type": "json_object"})
    assert "single valid JSON object" in backend_log("thread/start")[0]["baseInstructions"]


def test_reasoning_effort_field_wins_and_minimal_maps_to_lowest(client, backend_log):
    _chat(client, "x", model="gpt-5 medium", reasoning_effort="minimal")
    assert backend_log("turn/start")[0]["effort"] == "low"


def test_omitted_model_uses_codex_default(client, backend_log):
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-5"  # reported by the app-server for the new thread
    assert "model" not in backend_log("turn/start")[0]


def test_unknown_model_is_404(client):
    resp = _chat(client, "x", model="gpt-unknown")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "model_not_found"


def test_failed_turn_maps_to_http_status(client):
    assert _chat(client, "please FAIL").status_code == 429
    assert _chat(client, "please FAIL", stream=True).status_code == 429


def test_multi_turn_conversation_is_rendered_as_transcript(client, backend_log):
    client.post("/v1/chat/completions", json={
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    })
    assert backend_log("turn/start")[0]["input"][0]["text"] == "User: one\n\nAssistant: two\n\nUser: three"


def test_images_data_uri_and_url_are_forwarded_file_urls_rejected(client, backend_log):
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    content = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": png}},
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
    ]
    assert _chat(client, content).status_code == 200
    items = backend_log("turn/start")[0]["input"]
    assert items[1]["type"] == "localImage" and items[1]["path"].endswith(".png")
    assert not os.path.exists(items[1]["path"]), "temporary image file should be removed"
    assert items[2] == {"type": "image", "url": "https://example.com/cat.png"}

    bad = [{"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}]
    assert _chat(client, bad).status_code == 400


def test_models_endpoint_lists_visible_models_with_effort_aliases(client):
    data = client.get("/v1/models").json()
    ids = [m["id"] for m in data["data"]]
    assert data["object"] == "list"
    assert ids == ["gpt-5", "gpt-5 low", "gpt-5 medium"]


def test_model_specific_effort_suffix_is_accepted():
    from app.codex import ModelInfo
    from app.models import parse_model

    models = {"gpt-x": ModelInfo(id="gpt-x", efforts=("low", "ultra"))}
    assert parse_model("gpt-x ultra", models) == ("gpt-x", "ultra")
    assert parse_model("gpt-x  low", models) == ("gpt-x", "low")
    assert parse_model(None, models) == (None, None)
    with pytest.raises(ValueError):
        parse_model("gpt-x turbo", models)


def test_hidden_model_can_still_be_requested(client):
    assert _chat(client, "x", model="gpt-secret").status_code == 200


def test_responses_endpoint_non_stream_and_stream(client):
    resp = client.post("/v1/responses", json={"model": "gpt-5", "input": "hi", "instructions": "be brief"}).json()
    assert resp["output"][0]["content"][0]["text"] == "echo:hi"
    assert resp["usage"]["input_tokens"] == 102

    text = client.post("/v1/responses", json={"model": "gpt-5", "input": "hi", "stream": True}).text
    assert "event: response.completed" in text and text.rstrip().endswith("data: [DONE]")


def test_concurrent_turns_run_in_parallel_without_mixing(backend_log):
    async def run():
        client = codex_module.AppServerClient(command_factory=lambda: FAKE_COMMAND)
        try:
            await client.start()

            async def one(i):
                result = codex_module.TurnResult()
                parts = [d async for d in client.run_turn(text=f"req-{i}", model="gpt-5", result=result)]
                return "".join(parts), result

            t0 = time.perf_counter()
            outcomes = await asyncio.gather(*(one(i) for i in range(8)))
            return time.perf_counter() - t0, outcomes
        finally:
            await client.close()

    wall, outcomes = asyncio.run(run())
    for i, (text, result) in enumerate(outcomes):
        assert text == f"echo:req-{i}"
        assert result.text == f"echo:req-{i}"
        assert result.usage["input_tokens"] == 100 + len(f"req-{i}")
    # Each fake turn takes ~0.25 s; eight in series would take ~2 s.
    assert wall < 1.5


def test_lean_command_disables_configured_mcp_servers(monkeypatch):
    home = Path(os.environ["CODEX_HOME"])
    (home / "config.toml").write_text(
        '[mcp_servers.node_repl]\ncommand = "x"\n[mcp_servers."odd name"]\ncommand = "y"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "lean", True)
    monkeypatch.setattr(codex_module, "_resolve_codex_executable", lambda: "codex")

    cmd = codex_module.build_app_server_command()
    assert cmd[:2] == ["codex", "app-server"]
    assert "mcp_servers.node_repl.enabled=false" in cmd
    assert 'mcp_servers."odd name".enabled=false' in cmd
    assert "notify=[]" in cmd
    assert cmd[cmd.index("shell_tool") - 1] == "--disable"


def test_warns_when_global_agents_md_not_empty(caplog):
    agents = Path(os.environ["CODEX_HOME"]) / "AGENTS.md"
    agents.write_text("   \n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="app.codex"):
        codex_module.warn_if_global_agents_md()
    assert not caplog.records

    agents.write_text("# Rules\nBe nice.\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="app.codex"):
        codex_module.warn_if_global_agents_md()
    assert any("AGENTS.md is not empty" in r.getMessage() for r in caplog.records)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _npc(client, system, content="hi", **body):
    payload = {"model": "gpt-5", "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": content}], **body}
    return client.post("/v1/chat/completions", json=payload)


def test_spare_threads_are_used_for_the_same_system_prompt(client, backend_log):
    assert _npc(client, "You are a baker.").status_code == 200
    # The first request starts its own thread, then two spares are started in the background.
    assert _wait_for(lambda: len(backend_log("thread/start")) == 3)
    spare_ids = {t["_threadId"] for t in backend_log("thread/start")[1:]}

    assert _npc(client, "You are a baker.", model="gpt-5 low").status_code == 200
    turn = backend_log("turn/start")[1]
    assert turn["threadId"] in spare_ids
    assert turn["model"] == "gpt-5" and turn["effort"] == "low"
    # The used spare is replaced; the pool stays at two.
    assert _wait_for(lambda: len(backend_log("thread/start")) == 4)


def test_spare_threads_are_not_shared_across_system_prompts(client, backend_log):
    _npc(client, "You are a baker.")
    assert _wait_for(lambda: len(backend_log("thread/start")) == 3)

    _npc(client, "You are a guard.")
    threads = {t["_threadId"]: t for t in backend_log("thread/start")}
    turn = backend_log("turn/start")[1]
    assert threads[turn["threadId"]]["baseInstructions"] == "You are a guard."


def test_idle_threads_zero_disables_the_pool(monkeypatch, client, backend_log):
    monkeypatch.setattr(settings, "idle_threads", 0)
    for _ in range(3):
        _npc(client, "You are a baker.")
    time.sleep(0.3)
    assert len(backend_log("thread/start")) == 3


def test_stale_spare_threads_are_discarded(monkeypatch, client, backend_log):
    monkeypatch.setattr(codex_module, "_SPARE_MAX_AGE", 0.0)
    _npc(client, "You are a baker.")
    assert _wait_for(lambda: len(backend_log("thread/start")) == 3)
    spare_ids = {t["_threadId"] for t in backend_log("thread/start")[1:]}

    _npc(client, "You are a baker.")
    assert backend_log("turn/start")[1]["threadId"] not in spare_ids
