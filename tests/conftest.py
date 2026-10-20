import pytest


@pytest.fixture(autouse=True)
def _isolated_codex_home(monkeypatch, tmp_path_factory):
    """Never read the developer's real ~/.codex (AGENTS.md, config.toml) in tests."""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex_home")))
