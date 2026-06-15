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


# ── Category canonicalization ──────────────────────────────────────────
# Free M3U sources have wildly inconsistent group titles. "Animation",
# "Animation Classics", "Animation Kids", "Animación", "Cartoons", and
# "Kids" all mean roughly the same thing. Without consolidation, the
# public site shows 81 categories, half with 1-2 channels — useless
# for navigation.
#
# This map collapses known variants to a canonical display name. The
# key match is a normalized substring; e.g. "animation classics" maps
# to "Animation". The list is intentionally conservative — anything
# we don't recognize falls through to its raw group_title (so a new
# M3U source can still surface new categories).
#
# To add a consolidation, append a tuple (needle, canonical) where
# `needle` is a substring of normalize(group_title). The first
# matching needle wins; keep more specific needles above general ones.
_CATEGORY_CANONICAL: tuple[tuple[str, str], ...] = (
    # ── Animation & kids ──
    ("animacion", "Animation"),
    ("animation", "Animation"),
    ("cartoon", "Animation"),
    ("kids", "Kids"),
    ("children", "Kids"),
    ("preschool", "Kids"),
    # ── News ──
    ("news", "News"),
    ("noticias", "News"),
    # ── Sports ──
    ("sport", "Sports"),
    ("deportes", "Sports"),
    # ── Movies & series ──
    ("movie", "Movies"),
    ("peliculas", "Movies"),
    ("cinema", "Movies"),
    ("series", "Series"),
    ("tv show", "Series"),
    # ── Music ──
    ("music", "Music"),
    ("musica", "Music"),
    ("radio", "Music"),
    # ── Documentary & education ──
    ("documentary", "Documentary"),
    ("documental", "Documentary"),
    ("education", "Education"),
    ("educacion", "Education"),
    # ── Religious ──
    ("religion", "Religious"),
    ("religious", "Religious"),
    ("cristiana", "Religious"),
    ("islam", "Religious"),
    # ── Lifestyle & cooking ──
    ("cooking", "Lifestyle"),
    ("food", "Lifestyle"),
    ("travel", "Lifestyle"),
    ("lifestyle", "Lifestyle"),
    ("fashion", "Lifestyle"),
    # ── Nature & science ──
    ("nature", "Nature"),
    ("science", "Science"),
    ("naturaleza", "Nature"),
    # ── Weather ──
    ("weather", "Weather"),
    ("clima", "Weather"),
    # ── General entertainment ──
    ("entertainment", "Entertainment"),
    ("entretenimiento", "Entertainment"),
    ("variety", "Entertainment"),
    # ── Classic TV ──
    ("classic", "Classic TV"),
    ("retro", "Classic TV"),
    # ── Shop / infomercial ──
    ("shop", "Shopping"),
    ("shopping", "Shopping"),
    ("infomercial", "Shopping"),
    # ── Local / regional ──
    ("local", "Local"),
    ("regional", "Local"),
)


def canonical_category(group_title: str) -> str:
    """Map a raw M3U group_title to a canonical display name.

    Returns the input (stripped) if no rule matches. The slug
    helper downstream handles the URL-safe form.
    """
    g = (group_title or "").strip()
    if not g:
        return "Other"
    n = normalize(g)
    if not n:
        return "Other"
    for needle, canonical in _CATEGORY_CANONICAL:
        if needle in n:
            return canonical
    # No rule matched — return the original (capitalized for display).
    return g


# ── Manual multi-region channel consolidation ──────────────────────────
# Some channels are region-flavored variants of a single logical
# network. Examples:
#   - "PBS Kids Alaska" + "PBS Kids East"  →  "PBS Kids"
#   - "BBC One London" + "BBC One Scotland" →  "BBC One"  (probably)
#   - "ESPN East" + "ESPN West"             →  "ESPN"
#
# These can't be caught by a generic suffix-stripper because
# "alaska" is a real word, not a quality tag. Operators maintain
# this list manually — the M3U importer's `is_same_channel()`
# consults it.
#
# The map is normalized -> canonical normalized form. After lookup,
# the importer uses the canonical form to look up an existing
# channels row, so all region variants collide on the same id.
_MULTI_REGION_OVERRIDE: dict[str, str] = {
    "pbs kids alaska": "pbs kids",
    "pbs kids east":   "pbs kids",
    "pbs kids west":   "pbs kids",
    "pbs kids hawaii": "pbs kids",
    # Add more here as the operator notices regional splits.
}


def canonical_channel_name(name: str) -> str:
    """Operator override: return the canonical normalized form of a
    channel name. If a multi-region rule applies, return the
    target; otherwise pass through to the regular normalizer.

    Used by the M3U importer so 'PBS Kids Alaska' and 'PBS Kids East'
    both hash to the same channels.normalized_name and merge into
    one row with multiple stream URLs (failover).

    Match strategy: a normalized name is collapsed to the override
    target if it either equals an override key or starts with one
    followed by a word boundary (whitespace). "PBS Kids Alaska" →
    "pbs kids"; "PBS Kids Alaska HD" → "pbs kids" (the trailing
    'hd' is a quality suffix the normalizer would have stripped,
    but if it didn't, we still want the override to win).
    """
    n = normalize(name)
    if not n:
        return n
    if n in _MULTI_REGION_OVERRIDE:
        return _MULTI_REGION_OVERRIDE[n]
    # Prefix match: 'pbs kids alaska edition' starts with
    # 'pbs kids alaska' → collapse to 'pbs kids'.
    for key, target in _MULTI_REGION_OVERRIDE.items():
        if n.startswith(key + " "):
            return target
    return n


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
