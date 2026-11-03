import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    """A fresh database and the example site for every core test, also inside a fork, so a fork's
    CI catches core regressions. Fork tests (fork/tests) have their own conftest."""
    monkeypatch.setenv("FORK_DIR", os.path.join(ROOT, "example"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEBUG_DIR", str(tmp_path / "debug"))
    from adwatch import config, db, fetcher, sites
    monkeypatch.setattr(fetcher, "DEBUG_DIR", str(tmp_path / "debug"))
    config.reset(); sites.reset(); db.close(); fetcher._workers.clear()
    yield tmp_path
    db.close(); config.reset(); sites.reset(); fetcher._workers.clear()
