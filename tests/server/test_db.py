"""Tests for the database layer using in-memory SQLite.

Search tests are behavioral: given specific papers in the DB, does the
query find them? This avoids coupling to internal query string formatting.
"""

import sqlite3

import pytest

from biorxiv_mcp.server import db


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


# -- upsert -------------------------------------------------------------------


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
    assert db.get_paper(conn, p1["doi"])["title"] == "V2"


def test_upsert_replaces_existing(conn):
    db.upsert_papers(conn, [_make_paper(title="Old")])
    db.upsert_papers(conn, [_make_paper(title="New")])
    assert db.get_paper(conn, "10.1101/2024.01.01.000001")["title"] == "New"


# -- search: basic matching ---------------------------------------------------


def test_search_finds_by_title(conn):
    db.upsert_papers(conn, [_make_paper(title="CRISPR gene editing in neurons")])
    assert len(db.search(conn, "CRISPR")) == 1


def test_search_finds_by_abstract(conn):
    db.upsert_papers(conn, [_make_paper(abstract="Novel approach to RNA sequencing")])
    assert len(db.search(conn, "RNA sequencing")) == 1


def test_search_no_results(conn):
    db.upsert_papers(conn, [_make_paper(title="Something else")])
    assert db.search(conn, "nonexistent_term_xyz") == []


def test_search_empty_query(conn):
    db.upsert_papers(conn, [_make_paper(title="Anything")])
    assert db.search(conn, "()") == []


# -- search: hyphenated terms -------------------------------------------------


def test_search_hyphenated_query(conn):
    db.upsert_papers(conn, [_make_paper(title="mRNA-seq analysis of neurons")])
    assert len(db.search(conn, "mRNA-seq")) == 1


def test_search_hyphenated_does_not_match_partial(conn):
    """'mRNA-seq' should not match a paper that only mentions 'seq' in isolation."""
    db.upsert_papers(conn, [_make_paper(title="DNA seq protocol")])
    assert db.search(conn, "mRNA-seq") == []


# -- search: MeSH synonym expansion ------------------------------------------


def test_search_finds_synonym(conn):
    """'cancer' should find a paper about 'tumor' via MeSH synonym expansion."""
    db.upsert_papers(conn, [_make_paper(title="Tumor suppressor genes in breast tissue")])
    results = db.search(conn, "cancer")
    assert len(results) == 1


def test_search_synonym_bidirectional(conn):
    """'tumor' should find 'cancer' and vice versa."""
    db.upsert_papers(conn, [_make_paper(title="Breast cancer genomics")])
    assert len(db.search(conn, "tumor")) >= 1


def test_search_no_false_synonym_match(conn):
    """MeSH expansion shouldn't match completely unrelated papers."""
    db.upsert_papers(conn, [_make_paper(title="Machine learning for weather prediction")])
    assert db.search(conn, "heart attack") == []


# -- search: AND behavior (implicit) -----------------------------------------


def test_search_implicit_and(conn):
    """Multiple terms require all to match (like PubMed)."""
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="CRISPR gene editing"),
            _make_paper(doi="10.1101/002", title="Cancer treatment options"),
            _make_paper(doi="10.1101/003", title="CRISPR for cancer therapy"),
        ],
    )
    results = db.search(conn, "CRISPR cancer")
    # Only the paper with both terms should match.
    assert len(results) == 1
    assert "CRISPR" in results[0]["title"] and "cancer" in results[0]["title"].lower()


def test_search_explicit_and(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="CRISPR editing"),
            _make_paper(doi="10.1101/002", title="CRISPR and cancer"),
        ],
    )
    results = db.search(conn, "CRISPR AND cancer")
    assert len(results) == 1
    assert "cancer" in results[0]["title"]


def test_search_explicit_or(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="CRISPR editing"),
            _make_paper(doi="10.1101/002", title="Cancer treatment"),
        ],
    )
    results = db.search(conn, "CRISPR OR cancer")
    assert len(results) == 2


def test_search_explicit_not(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="CRISPR gene editing"),
            _make_paper(doi="10.1101/002", title="CRISPR cancer therapy"),
        ],
    )
    results = db.search(conn, "CRISPR NOT cancer")
    assert len(results) == 1
    assert "cancer" not in results[0]["title"].lower()


def test_search_quoted_phrase(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="Single cell RNA sequencing"),
            _make_paper(doi="10.1101/002", title="RNA from a single source"),
        ],
    )
    results = db.search(conn, '"single cell"')
    assert len(results) == 1
    assert "Single cell" in results[0]["title"]


def test_search_explicit_wildcard(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="CRISPR gene editing"),
            _make_paper(doi="10.1101/002", title="CRISPRi silencing approach"),
        ],
    )
    results = db.search(conn, "CRISPR*")
    assert len(results) == 2


# -- search: filters ----------------------------------------------------------


def test_search_with_category_filter(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="Neuro paper", category="neuroscience"),
            _make_paper(doi="10.1101/002", title="Neuro genomics", category="genomics"),
        ],
    )
    results = db.search(conn, "Neuro", category="neuroscience")
    assert len(results) == 1
    assert results[0]["category"] == "neuroscience"


def test_search_with_date_filter(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="Old paper", date="2020-01-01"),
            _make_paper(doi="10.1101/002", title="New paper", date="2024-06-01"),
        ],
    )
    results = db.search(conn, "paper", after="2024-01-01")
    assert len(results) == 1
    assert results[0]["date"] == "2024-06-01"


def test_search_respects_limit(conn):
    papers = [_make_paper(doi=f"10.1101/{i:03d}", title="Common term") for i in range(10)]
    db.upsert_papers(conn, papers)
    assert len(db.search(conn, "Common", limit=3)) == 3


def test_search_compact_vs_detail(conn):
    db.upsert_papers(conn, [_make_paper(title="Detail test", abstract="Full abstract here")])
    compact = db.search(conn, "Detail")[0]
    detailed = db.search(conn, "Detail", detail=True)[0]
    assert "abstract" not in compact
    assert detailed["abstract"] == "Full abstract here"


def test_search_date_sort(conn):
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="Alpha paper", date="2020-01-01"),
            _make_paper(doi="10.1101/002", title="Alpha recent", date="2024-06-01"),
        ],
    )
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
    db.upsert_papers(
        conn,
        [
            _make_paper(doi="10.1101/001", title="A", category="neuroscience"),
            _make_paper(doi="10.1101/002", title="B", category="neuroscience"),
            _make_paper(doi="10.1101/003", title="C", category="genomics"),
        ],
    )
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


# -- connection context manager ------------------------------------------------


def test_connection_context_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._initialized_ids.clear()
    if hasattr(db._thread_local, "conn"):
        del db._thread_local.conn
    with db.connection() as conn:
        assert conn is not None
        db.get_paper_count(conn)


def test_connection_reuses_per_thread_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._initialized_ids.clear()
    if hasattr(db._thread_local, "conn"):
        del db._thread_local.conn
    with db.connection() as c1:
        pass
    with db.connection() as c2:
        assert c1 is c2
