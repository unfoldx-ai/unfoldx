"""Fixtures: a real-HTTP fake API and an in-process `unfoldx --json` runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fake_platform import serve  # noqa: E402


@pytest.fixture
def platform():
    with serve() as fake:
        yield fake


@pytest.fixture
def cli(platform, tmp_path, monkeypatch, capsys):
    """Run `unfoldx …` in-process in machine mode; returns (exit code, stdout JSON, stderr)."""
    monkeypatch.setenv("UNFOLDX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("UNFOLDX_NO_KEYRING", "1")
    monkeypatch.setenv("UNFOLDX_API_BASE", platform.base + "/api")
    monkeypatch.delenv("UNFOLDX_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    from unfoldx_cli import main

    def run(*argv):
        code = main.main(["--json", *argv])
        out, err = capsys.readouterr()
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, out
        return code, json.loads(lines[0]), err

    return run
