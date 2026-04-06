"""Tests for the REST API handlers."""

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from biorxiv_mcp.server import db, keys
from biorxiv_mcp.server.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Provide a test client backed by an in-memory DB with one API key."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db._initialized_ids.discard(id(conn))
    db.init_db(conn)
    raw = keys.generate(conn, label="test", unlimited=True)

    @contextmanager
    def fake_connection():
        yield conn

    monkeypatch.setattr(db, "connection", fake_connection)
    app = create_app()
    tc = TestClient(app)
    tc.headers["Authorization"] = f"Bearer {raw}"
    yield tc, conn
    conn.close()


def _make_paper(**overrides):
    defaults = {f: "" for f in db.PAPER_FIELDS}
    defaults.update(doi="10.1101/2024.01.01.000001", title="Test Paper", server="biorxiv")
    defaults.update(overrides)
    return defaults


# -- /health ------------------------------------------------------------------


def test_health(client):
    tc, _ = client
    r = tc.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# -- /api/search --------------------------------------------------------------


def test_search(client):
    tc, conn = client
    db.upsert_papers(conn, [_make_paper(title="CRISPR editing")])
    r = tc.get("/api/search", params={"q": "CRISPR"})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_search_empty(client):
    tc, _ = client
    r = tc.get("/api/search", params={"q": "nothing"})
    assert r.status_code == 200
    assert r.json() == []


def test_search_bad_date(client):
    tc, _ = client
    r = tc.get("/api/search", params={"q": "x", "after": "nope"})
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["error"]


def test_search_caps_limit(client):
    tc, conn = client
    papers = [_make_paper(doi=f"10.1101/{i:04d}", title="Common") for i in range(5)]
    db.upsert_papers(conn, papers)
    r = tc.get("/api/search", params={"q": "Common", "limit": "10000"})
    assert r.status_code == 200
    assert len(r.json()) <= 100


# -- /api/search/count --------------------------------------------------------


def test_search_count(client):
    tc, conn = client
    db.upsert_papers(conn, [_make_paper(title="Countable")])
    r = tc.get("/api/search/count", params={"q": "Countable"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


# -- /api/categories ----------------------------------------------------------


def test_categories(client):
    tc, conn = client
    db.upsert_papers(conn, [_make_paper(category="genomics")])
    r = tc.get("/api/categories")
    assert r.status_code == 200
    assert r.json()[0]["category"] == "genomics"


# -- /api/paper/{doi} ---------------------------------------------------------


def test_get_paper(client):
    tc, conn = client
    db.upsert_papers(conn, [_make_paper()])
    r = tc.get("/api/paper/10.1101/2024.01.01.000001")
    assert r.status_code == 200
    assert r.json()["title"] == "Test Paper"


def test_get_paper_not_found(client):
    tc, conn = client
    with patch("biorxiv_mcp.server.sync.fetch_paper_by_doi", return_value=None):
        r = tc.get("/api/paper/10.1101/missing")
    assert r.status_code == 404


def test_get_paper_bad_doi(client):
    tc, _ = client
    r = tc.get("/api/paper/not-a-doi")
    assert r.status_code == 400
    assert "Invalid DOI" in r.json()["error"]


# -- /api/status --------------------------------------------------------------


def test_status(client):
    tc, _ = client
    r = tc.get("/api/status")
    assert r.status_code == 200
    assert "paper_count" in r.json()


# -- /api/sync ----------------------------------------------------------------


def test_sync(client):
    tc, _ = client
    r = tc.post("/api/sync")
    assert r.status_code == 200
    assert r.json()["status"] in ("started", "already_running")
