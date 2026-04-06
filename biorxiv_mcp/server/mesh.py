"""MeSH synonym expansion for search queries.

Loads a synonym table from the NLM MeSH descriptor XML and expands
query tokens against it at search time. This gives PubMed-like
behavior where "heart attack" also finds "myocardial infarction".

The synonym table is built once (at first use) and cached in memory.
The raw MeSH XML is downloaded on first build and cached on disk.
"""

from __future__ import annotations

import gzip
import logging
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .db import DB_DIR

logger = logging.getLogger(__name__)

MESH_URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.gz"
MESH_CACHE_FILE = DB_DIR / "mesh_synonyms.gz"

# Lowercase term -> set of lowercase synonyms (including the canonical name).
_synonyms: dict[str, set[str]] | None = None


def _download_mesh_xml(dest: Path) -> Path:
    """Download the MeSH descriptor XML if not already cached."""
    if dest.exists():
        return dest
    logger.info("Downloading MeSH descriptors from %s", MESH_URL)
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(MESH_URL, str(dest))
    logger.info("Saved MeSH XML to %s", dest)
    return dest


def _build_synonym_table(xml_path: Path) -> dict[str, set[str]]:
    """Parse MeSH XML and build a bidirectional synonym map.

    Every term in a descriptor's concept list maps to every other term
    in the same descriptor. All keys and values are lowercased.
    """
    table: dict[str, set[str]] = {}

    opener = gzip.open if str(xml_path).endswith(".gz") else open
    with opener(xml_path, "rb") as f:
        for event, elem in ET.iterparse(f, events=["end"]):
            if elem.tag != "DescriptorRecord":
                continue
            # Collect all terms across all concepts in this descriptor.
            terms: set[str] = set()
            for term_el in elem.findall(".//Term/String"):
                if term_el.text:
                    terms.add(term_el.text.lower())
            # Map each term to all other terms in the group.
            if len(terms) > 1:
                for term in terms:
                    if term not in table:
                        table[term] = set()
                    table[term].update(terms - {term})
            elem.clear()

    logger.info("Built MeSH synonym table: %d terms", len(table))
    return table


def _load() -> dict[str, set[str]]:
    global _synonyms
    if _synonyms is not None:
        return _synonyms
    try:
        xml_path = _download_mesh_xml(MESH_CACHE_FILE)
        _synonyms = _build_synonym_table(xml_path)
    except Exception:
        logger.warning("Failed to load MeSH synonyms; query expansion disabled", exc_info=True)
        _synonyms = {}
    return _synonyms


def is_term(text: str) -> bool:
    """True if ``text`` is a known MeSH term (exact, case-insensitive)."""
    return text.lower() in _load()


def find_phrases(words: list[str], max_phrase_len: int = 3) -> list[str | list[str]]:
    """Group consecutive words into MeSH phrases using greedy longest-match.

    Scans ``words`` with a sliding window (longest first). When a window
    matches a MeSH term, those words are grouped as a single phrase and
    the scan skips ahead.

    Returns a list where each element is either:
    - A ``str`` (single unmatched word)
    - A ``list[str]`` (words that form a MeSH phrase)

    Example::

        >>> find_phrases(["ribonucleic", "acid", "cancer"])
        [["ribonucleic", "acid"], "cancer"]
    """
    table = _load()
    result: list[str | list[str]] = []
    i = 0
    while i < len(words):
        matched = False
        for window in range(min(max_phrase_len, len(words) - i), 1, -1):
            candidate = " ".join(words[i : i + window]).lower()
            if candidate in table:
                result.append(words[i : i + window])
                i += window
                matched = True
                break
        if not matched:
            result.append(words[i])
            i += 1
    return result


def expand(term: str, max_synonyms: int = 5) -> list[str]:
    """Return up to ``max_synonyms`` MeSH synonyms for a term.

    Returns an empty list if no synonyms are found. The input term
    itself is not included in the result.

    Only expands exact multi-word or single-word matches (lowercased).
    Short synonyms (< 3 chars) and very long chemical names (> 60 chars)
    are filtered out.
    """
    table = _load()
    syns = table.get(term.lower())
    if not syns:
        return []
    # Filter out chemical formulas and very short/long terms.
    filtered = [s for s in syns if 3 <= len(s) <= 60]
    # Prefer shorter, more readable synonyms.
    filtered.sort(key=len)
    return filtered[:max_synonyms]
