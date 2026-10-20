from pathlib import Path

from app import config


def test_default_workdir_posix(monkeypatch):
    """POSIX keeps /workspace to match the Docker image layout."""

    monkeypatch.setattr(config.os, "name", "posix")
    assert config._default_codex_workdir() == "/workspace"


def test_default_workdir_windows_uses_localappdata(monkeypatch, tmp_path):
    """Windows must not default to a drive-relative path like E:/workspace."""

    monkeypatch.setattr(config.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert Path(config._default_codex_workdir()) == tmp_path / "codex-wrapper" / "workspace"


def test_env_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_WORKDIR", str(tmp_path))
    assert config.Settings(_env_file=None).codex_workdir == str(tmp_path)
