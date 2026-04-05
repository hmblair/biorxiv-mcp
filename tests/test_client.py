"""Tests for the MCP tool wrappers (client side)."""

from unittest.mock import MagicMock, patch

import pytest

from biorxiv_mcp.client.api import ApiError, BiorxivApi
from biorxiv_mcp.client import tools


@pytest.fixture(autouse=True)
def _mock_api(monkeypatch):
    """Patch _api() to return a mock."""
    mock = MagicMock(spec=BiorxivApi)
    monkeypatch.setattr(tools, "_api", lambda: mock)
    yield mock


def test_search_returns_results(_mock_api):
    _mock_api.search.return_value = [{"doi": "10.1101/1", "title": "Paper"}]
    result = tools.search_biorxiv("test")
    assert isinstance(result, list)
    assert result[0]["doi"] == "10.1101/1"


def test_search_empty_db_message(_mock_api):
    _mock_api.search.return_value = []
    _mock_api.status.return_value = {"paper_count": 0}
    result = tools.search_biorxiv("test")
    assert "message" in result
    assert "empty" in result["message"].lower()


def test_search_no_results_message(_mock_api):
    _mock_api.search.return_value = []
    _mock_api.status.return_value = {"paper_count": 100}
    result = tools.search_biorxiv("test")
    assert "No results" in result["message"]


def test_api_error_surfaces_in_tool(_mock_api):
    _mock_api.search.side_effect = ApiError(403, "invalid token")
    result = tools.search_biorxiv("test")
    assert "error" in result
    assert "403" in result["error"]


def test_connection_error_surfaces(_mock_api):
    _mock_api.search.side_effect = ConnectionError("unreachable")
    result = tools.search_biorxiv("test")
    assert "error" in result
    assert "unreachable" in result["error"]


def test_get_paper(_mock_api):
    _mock_api.get_paper.return_value = {"doi": "10.1101/1", "title": "Paper"}
    assert tools.get_paper("10.1101/1")["title"] == "Paper"


def test_categories(_mock_api):
    _mock_api.categories.return_value = [{"category": "genomics", "count": 5}]
    assert tools.biorxiv_categories()[0]["category"] == "genomics"


def test_status(_mock_api):
    _mock_api.status.return_value = {"paper_count": 100}
    assert tools.biorxiv_status()["paper_count"] == 100


def test_download_writes_file(_mock_api, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "PAPERS_DIR", tmp_path)
    _mock_api.download_pdf.return_value = b"%PDF-fake-content"
    result = tools.download_paper("10.1101/2024.01.01.000001")
    assert "path" in result
    assert (tmp_path / "10.1101_2024.01.01.000001.pdf").read_bytes() == b"%PDF-fake-content"
