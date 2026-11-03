import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    """A fresh database and the example fork (unless a fork/ folder exists) for every test."""
    if not os.path.isdir(os.path.join(ROOT, "fork")):
        monkeypatch.setenv("FORK_DIR", os.path.join(ROOT, "example"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEBUG_DIR", str(tmp_path / "debug"))
    from adwatch import config, db, fetcher, sites
    monkeypatch.setattr(fetcher, "DEBUG_DIR", str(tmp_path / "debug"))
    config.reset(); sites.reset(); db.close(); fetcher._workers.clear()
    yield tmp_path
    db.close(); config.reset(); sites.reset(); fetcher._workers.clear()
