"""Tests for the database layer using in-memory SQLite."""

import sqlite3

import pytest

from biorxiv_mcp import db


@pytest.fixture()
def conn():
    """In-memory SQLite connection with schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db._initialized_ids.discard(id(c))
    db.init_db(c)
    yield c
    db._initialized_ids.discard(id(c))
    c.close()


def _make_paper(**overrides):
    defaults = {f: "" for f in db.PAPER_FIELDS}
    defaults.update(doi="10.1101/2024.01.01.000001", title="Test Paper", server="biorxiv")
    defaults.update(overrides)
    return defaults


# -- upsert_papers ------------------------------------------------------------


def test_upsert_and_retrieve(conn):
    paper = _make_paper()
    count = db.upsert_papers(conn, [paper])
    assert count == 1
    result = db.get_paper(conn, paper["doi"])
    assert result is not None
    assert result["title"] == "Test Paper"


def test_upsert_empty_list(conn):
    assert db.upsert_papers(conn, []) == 0


def test_upsert_deduplicates_by_version(conn):
    p1 = _make_paper(version="1", title="V1")
    p2 = _make_paper(version="2", title="V2")
    count = db.upsert_papers(conn, [p1, p2])
    assert count == 1
    result = db.get_paper(conn, p1["doi"])
    assert result["title"] == "V2"


def test_upsert_replaces_existing(conn):
    db.upsert_papers(conn, [_make_paper(title="Old")])
    db.upsert_papers(conn, [_make_paper(title="New")])
    result = db.get_paper(conn, "10.1101/2024.01.01.000001")
    assert result["title"] == "New"


# -- search -------------------------------------------------------------------


def test_search_finds_by_title(conn):
    db.upsert_papers(conn, [_make_paper(title="CRISPR gene editing in neurons")])
    results = db.search(conn, "CRISPR")
    assert len(results) == 1
    assert "CRISPR" in results[0]["title"]


def test_search_finds_by_abstract(conn):
    db.upsert_papers(conn, [_make_paper(abstract="Novel approach to RNA sequencing")])
    results = db.search(conn, "RNA sequencing")
    assert len(results) == 1


def test_search_no_results(conn):
    db.upsert_papers(conn, [_make_paper(title="Something else")])
    results = db.search(conn, "nonexistent_term_xyz")
    assert results == []


def test_search_with_category_filter(conn):
    db.upsert_papers(conn, [
        _make_paper(doi="10.1101/001", title="Neuro paper", category="neuroscience"),
        _make_paper(doi="10.1101/002", title="Neuro genomics", category="genomics"),
    ])
    results = db.search(conn, "Neuro", category="neuroscience")
    assert len(results) == 1
    assert results[0]["category"] == "neuroscience"


def test_search_with_date_filter(conn):
    db.upsert_papers(conn, [
        _make_paper(doi="10.1101/001", title="Old paper", date="2020-01-01"),
        _make_paper(doi="10.1101/002", title="New paper", date="2024-06-01"),
    ])
    results = db.search(conn, "paper", after="2024-01-01")
    assert len(results) == 1
    assert results[0]["date"] == "2024-06-01"


def test_search_respects_limit(conn):
    papers = [_make_paper(doi=f"10.1101/{i:03d}", title="Common term") for i in range(10)]
    db.upsert_papers(conn, papers)
    results = db.search(conn, "Common", limit=3)
    assert len(results) == 3


def test_search_compact_vs_detail(conn):
    db.upsert_papers(conn, [_make_paper(title="Detail test", abstract="Full abstract here")])
    compact = db.search(conn, "Detail")[0]
    detailed = db.search(conn, "Detail", detail=True)[0]
    assert "abstract" not in compact
    assert detailed["abstract"] == "Full abstract here"


def test_search_date_sort(conn):
    db.upsert_papers(conn, [
        _make_paper(doi="10.1101/001", title="Alpha paper", date="2020-01-01"),
        _make_paper(doi="10.1101/002", title="Alpha recent", date="2024-06-01"),
    ])
    results = db.search(conn, "Alpha", sort="date")
    assert results[0]["date"] == "2024-06-01"


# -- search_count -------------------------------------------------------------


def test_search_count(conn):
    papers = [_make_paper(doi=f"10.1101/{i:03d}", title="Countable term") for i in range(5)]
    db.upsert_papers(conn, papers)
    assert db.search_count(conn, "Countable") == 5


# -- get_paper ----------------------------------------------------------------


def test_get_paper_missing(conn):
    assert db.get_paper(conn, "10.1101/nonexistent") is None


# -- get_categories -----------------------------------------------------------


def test_get_categories(conn):
    db.upsert_papers(conn, [
        _make_paper(doi="10.1101/001", title="A", category="neuroscience"),
        _make_paper(doi="10.1101/002", title="B", category="neuroscience"),
        _make_paper(doi="10.1101/003", title="C", category="genomics"),
    ])
    cats = db.get_categories(conn)
    assert cats[0]["category"] == "neuroscience"
    assert cats[0]["count"] == 2
    assert cats[1]["category"] == "genomics"


# -- metadata -----------------------------------------------------------------


def test_paper_count(conn):
    assert db.get_paper_count(conn) == 0
    db.upsert_papers(conn, [_make_paper()])
    assert db.get_paper_count(conn) == 1


def test_sync_date_roundtrip(conn):
    assert db.get_last_sync_date(conn) is None
    db.set_last_sync_date(conn, "2024-01-01")
    assert db.get_last_sync_date(conn) == "2024-01-01"


def test_bulk_sync_cursor_roundtrip(conn):
    assert db.get_bulk_sync_cursor(conn) is None
    db.set_bulk_sync_cursor(conn, "2024-03-01")
    assert db.get_bulk_sync_cursor(conn) == "2024-03-01"
    db.clear_bulk_sync_cursor(conn)
    assert db.get_bulk_sync_cursor(conn) is None


# -- prefix matching ----------------------------------------------------------


def test_prefix_matching_appends_star():
    assert db._add_prefix_matching("CRISPR") == "CRISPR*"


def test_prefix_matching_skips_short_tokens():
    # "the" is 3 chars so it gets a *, only "in" (2 chars) is skipped
    assert db._add_prefix_matching("in the brain") == "in the* brain*"


def test_prefix_matching_skips_operators():
    assert db._add_prefix_matching("CRISPR AND cancer") == "CRISPR* AND cancer*"


def test_prefix_matching_skips_quoted():
    assert db._add_prefix_matching('"exact phrase"') == '"exact phrase"'


def test_prefix_matching_skips_already_prefixed():
    assert db._add_prefix_matching("CRISPR*") == "CRISPR*"


def test_prefix_matching_strips_punctuation():
    # Hyphens, parens, colons get stripped; tokens re-split.
    assert db._add_prefix_matching("mRNA-seq") == "mRNA* seq*"
    assert db._add_prefix_matching("(CRISPR)") == "CRISPR*"
    assert db._add_prefix_matching("foo:bar") == "foo* bar*"


def test_search_handles_punctuation(conn):
    db.upsert_papers(conn, [_make_paper(title="mRNA sequencing analysis")])
    # Previously this would raise sqlite3.OperationalError.
    results = db.search(conn, "mRNA-seq")
    assert len(results) == 1


def test_search_empty_query_returns_no_results(conn):
    db.upsert_papers(conn, [_make_paper(title="Anything")])
    assert db.search(conn, "()") == []


# -- connection context manager ------------------------------------------------


def test_connection_context_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    if hasattr(db._thread_local, "conn"):
        del db._thread_local.conn
    with db.connection() as conn:
        assert conn is not None
        db.get_paper_count(conn)  # should work


def test_connection_reuses_per_thread_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    if hasattr(db._thread_local, "conn"):
        del db._thread_local.conn
    with db.connection() as c1:
        pass
    with db.connection() as c2:
        assert c1 is c2
