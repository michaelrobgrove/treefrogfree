"""Channel name normalization + match helpers.

The consolidator is the *heart* of the system per plan.md §3. The function
is pure (no DB, no I/O) so it can be tested exhaustively and called from
import, merge, and admin search contexts.

Normalization: lowercase → strip → drop punctuation → collapse whitespace
→ drop common suffixes (hd, fhd, uhd, +1, east, west, 24/7, usa, etc.).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# Suffixes that almost never distinguish logical channels.
# Split into two sets because they need different boundary handling:
#   - WORD_SUFFIXES are pure-alphanumeric; need a word-boundary lookbehind
#     so "east" inside "feast" doesn't get stripped.
#   - PUNCT_SUFFIXES contain their own boundary (a non-alphanumeric character
#     like "+" or "/") so they can match without a separate boundary check.
_WORD_SUFFIXES: tuple[str, ...] = (
    "hd", "fhd", "uhd", "sd", "4k", "8k",
    "east", "west", "north", "south", "central", "pacific", "mountain",
    "247",
    "usa", "us", "uk", "ca",
    "intl", "international",
    "feed", "backup", "alt",
    "v2", "v3", "v4",
    "e", "w",  # when wrapped in parens by the regex
)
_PUNCT_SUFFIXES: tuple[str, ...] = (
    "+1",
    "24/7",
)

# Suffixes are stripped repeatedly, so "BBC News HD Backup" → "bbc news".
# Keep `+` and `/` in the alnum set so punctuation-containing suffixes
# (+1, 24/7) survive to be matched and stripped. The suffix regex below
# is what actually drops them.
_NON_ALNUM = re.compile(r"[^a-z0-9\s+/]")
_WS = re.compile(r"\s+")

# Build a single regex that matches any of the suffixes at end-of-string.
# Two alternatives:
#   1. Punctuation-containing suffix anywhere at end (the punctuation IS
#      the boundary).
#   2. Word-only suffix at end, preceded by a non-word character or start.
_PAREN_RE = re.compile(r"\((e|w)\)$", re.IGNORECASE)
_SUFFIX_RE = re.compile(
    r"(?:"
    + r"|".join(re.escape(s) for s in _PUNCT_SUFFIXES)
    + r"|(?:^|[^a-z0-9])(?:"
    + "|".join(re.escape(s) for s in _WORD_SUFFIXES)
    + r")"
    + r")$",
    flags=re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
    """NFKD then drop combining marks. 'Café' → 'cafe'."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Return the canonical match key for a channel name.

    Examples (verified in tests):
        "BBC News HD"        → "bbc news"
        "BBC NEWS"           → "bbc news"
        "BBC-News"           → "bbc news"
        "  CNN  "            → "cnn"
        "ESPN 2"             → "espn 2"
        "Discovery+1"        → "discovery"
        "Fox Weather 24/7"   → "fox weather"
    """
    if not name:
        return ""
    s = name.strip()
    s = _strip_accents(s)
    s = s.lower()
    s = _NON_ALNUM.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    if not s:
        return ""

    # Drop suffixes in a loop. Each pass trims at most one suffix.
    for _ in range(3):
        new = _SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


def is_same_channel(a: str, b: str) -> bool:
    """True if two raw channel names consolidate to the same key."""
    return normalize(a) == normalize(b) and bool(normalize(a))


def group_slug(group: str) -> str:
    """Stable, URL-safe slug for a group title (lowercase, hyphenated)."""
    if not group:
        return "other"
    s = _strip_accents(group).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "other"


def group_brand_name(group: str) -> str:
    """Apply the §3.4 group rename: 'News' → '🐸 Tree Frog Free | News'."""
    g = (group or "Other").strip() or "Other"
    return f"🐸 Tree Frog Free | {g}"


def candidate_matches(
    query: str, candidates: Iterable[str], limit: int = 20
) -> list[tuple[str, int]]:
    """Return (candidate, score) pairs sorted by score desc.

    Used by admin search. Pure function; we don't depend on rapidfuzz in v1
    to keep the dependency surface small. A future fuzzy stage can plug in
    here without touching the call sites.
    """
    qn = normalize(query)
    scored: list[tuple[str, int]] = []
    for c in candidates:
        cn = normalize(c)
        if not cn:
            continue
        if qn == cn:
            scored.append((c, 100))
        elif qn and (qn in cn or cn in qn):
            # Containment is a decent proxy; scale by length ratio.
            ratio = min(len(qn), len(cn)) / max(len(qn), len(cn))
            scored.append((c, int(70 + 30 * ratio)))
        else:
            scored.append((c, 0))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [s for s in scored if s[1] > 0][:limit]
