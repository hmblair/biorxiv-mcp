"""Tests for server tool handlers with mocked DB and HTTP."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from biorxiv_mcp import db


# We need to import the tool functions from server, but they register with
# FastMCP on import. Import them as regular functions via their module.
import server as srv


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Reset rate limiter state between tests."""
    srv._search_bucket._tokens = float(srv._search_bucket.burst)
    srv._sync_bucket._tokens = float(srv._sync_bucket.burst)


@pytest.fixture()
def mock_db(tmp_path, monkeypatch):
    """Provide an in-memory DB via the db.connection() context manager."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._initialized.discard(id(conn))
    db.init_db(conn)

    from contextlib import contextmanager

    @contextmanager
    def fake_connection():
        yield conn

    monkeypatch.setattr(db, "connection", fake_connection)
    yield conn
    conn.close()


def _make_paper(**overrides):
    defaults = {f: "" for f in db.PAPER_FIELDS}
    defaults.update(doi="10.1101/2024.01.01.000001", title="Test Paper", server="biorxiv")
    defaults.update(overrides)
    return defaults


# -- search_biorxiv -----------------------------------------------------------


def test_search_returns_results(mock_db):
    db.upsert_papers(mock_db, [_make_paper(title="CRISPR editing")])
    results = srv.search_biorxiv("CRISPR")
    assert len(results) == 1
    assert "CRISPR" in results[0]["title"]


def test_search_empty_db(mock_db):
    results = srv.search_biorxiv("anything")
    assert results[0]["message"].startswith("Database is empty")


def test_search_no_match(mock_db):
    db.upsert_papers(mock_db, [_make_paper(title="Something")])
    results = srv.search_biorxiv("nonexistent_xyz")
    assert "No results" in results[0]["message"]


def test_search_db_error(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def broken():
        raise sqlite3.OperationalError("disk I/O error")
        yield  # noqa: unreachable

    monkeypatch.setattr(db, "connection", broken)
    results = srv.search_biorxiv("test")
    assert "error" in results[0]
    assert "Database error" in results[0]["error"]


# -- search_biorxiv_count -----------------------------------------------------


def test_count_returns_count(mock_db):
    db.upsert_papers(mock_db, [_make_paper(title="Countable")])
    result = srv.search_biorxiv_count("Countable")
    assert result["count"] == 1


# -- biorxiv_categories -------------------------------------------------------


def test_categories(mock_db):
    db.upsert_papers(mock_db, [_make_paper(category="genomics")])
    result = srv.biorxiv_categories()
    assert isinstance(result, list)
    assert result[0]["category"] == "genomics"


# -- biorxiv_status -----------------------------------------------------------


def test_status(mock_db, monkeypatch):
    monkeypatch.setattr(db, "get_db_size_mb", lambda: 1.5)
    monkeypatch.setattr(db, "DB_PATH", "/tmp/test.db")
    result = srv.biorxiv_status()
    assert "paper_count" in result
    assert result["db_size_mb"] == 1.5


# -- get_paper ----------------------------------------------------------------


def test_get_paper_from_db(mock_db):
    db.upsert_papers(mock_db, [_make_paper()])
    result = srv.get_paper("10.1101/2024.01.01.000001")
    assert result["title"] == "Test Paper"


def test_get_paper_api_fallback(mock_db):
    with patch.object(srv.sync, "fetch_paper_by_doi", return_value={"title": "From API"}):
        result = srv.get_paper("10.1101/missing")
    assert result["title"] == "From API"
    assert result["_source"] == "api"


def test_get_paper_not_found(mock_db):
    with patch.object(srv.sync, "fetch_paper_by_doi", return_value=None):
        result = srv.get_paper("10.1101/missing")
    assert "error" in result


# -- rate limiting -------------------------------------------------------------


def test_search_rate_limit():
    srv._search_bucket._tokens = 0
    srv._search_bucket._last -= 0  # No time to refill
    results = srv.search_biorxiv("test")
    assert "Rate limit" in results[0]["error"]


def test_sync_rate_limit():
    srv._sync_bucket._tokens = 0

    import asyncio
    result = asyncio.get_event_loop().run_until_complete(srv.sync_biorxiv())
    assert "Rate limit" in result["error"]


def test_get_paper_rate_limit():
    srv._search_bucket._tokens = 0
    result = srv.get_paper("10.1101/test")
    assert "Rate limit" in result["error"]
