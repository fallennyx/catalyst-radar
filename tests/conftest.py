"""Shared pytest fixtures."""

import os
import tempfile

import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    """Point storage at a throwaway SQLite file for the duration of one test."""
    from radar import config, storage

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    monkeypatch.setattr(config, "DB_PATH", path)
    storage.init_db(path)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
