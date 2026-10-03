"""Pytest fixtures: import the MCP server module from src/onprem_mcp (no install needed)."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "onprem_mcp"
sys.path.insert(0, str(SRC))

import main as server  # noqa: E402


@pytest.fixture()
def m():
    return server


@pytest.fixture(autouse=True)
def temp_db(m, tmp_path, monkeypatch):
    """Every test works on its own throw-away SQLite file seeded with the demo data."""
    db = tmp_path / "erp.db"
    monkeypatch.setattr(m, "DB_PATH", str(db))
    m.init_db(str(db))
    return db
