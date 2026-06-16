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
# public site shows 100+ categories, half with 1-2 channels — useless
# for navigation.
#
# This map collapses known variants to a canonical display name. The
# key match is a normalized substring; e.g. "animation classics" maps
# to "Animation". Anything we don't recognize falls through to
# "Other" so the public UI never has more than ~30 distinct category
# pills (the 30 canonical names below + "Other" as the catchall).
#
# To add a consolidation, append a tuple (needle, canonical) where
# `needle` is a substring of normalize(group_title). The first
# matching needle wins; keep more specific needles above general ones.
_CATEGORY_CANONICAL: tuple[tuple[str, str], ...] = (
    # ── Anime (must precede Animation; "anime" is a substring of
    #    "animation", so without this rule, all animation channels
    #    would route to Anime by alphabetical accident) ──
    ("anime", "Anime"),
    ("manga", "Anime"),
    # ── Animation & cartoons ──
    ("animacion", "Animation"),
    ("animation", "Animation"),
    ("cartoon", "Animation"),
    ("south park", "Animation"),
    # ── Kids ──
    ("kids", "Kids"),
    ("children", "Kids"),
    ("child", "Kids"),
    ("preschool", "Kids"),
    ("jeunesse", "Kids"),
    ("ninos", "Kids"),
    ("junior", "Kids"),
    # ── News ──
    ("news", "News"),
    ("noticias", "News"),
    ("notizie", "News"),
    ("nachrichten", "News"),
    ("actualite", "News"),
    ("headline", "News"),
    ("legislative", "News"),
    ("parliament", "News"),
    ("business", "News"),
    # ── Sports ──
    ("sport", "Sports"),
    ("deporte", "Sports"),
    ("deportes", "Sports"),
    ("futbol", "Sports"),
    ("fussball", "Sports"),
    ("calcio", "Sports"),
    ("basketball", "Sports"),
    ("baseball", "Sports"),
    ("football", "Sports"),
    ("soccer", "Sports"),
    ("hockey", "Sports"),
    ("tennis", "Sports"),
    ("golf", "Sports"),
    # ── Movies ──
    ("movie", "Movies"),
    ("peliculas", "Movies"),
    ("pelicula", "Movies"),
    ("cinema", "Movies"),
    ("cine", "Movies"),
    ("film", "Movies"),
    ("filme", "Movies"),
    ("binge", "Movies"),
    # ── Series ──
    ("serie", "Series"),
    ("series", "Series"),
    ("serien", "Series"),
    ("serial", "Series"),
    ("tv show", "Series"),
    ("telenovela", "Series"),
    # ── Comedy ──
    ("comedy", "Comedy"),
    ("comedie", "Comedy"),
    ("comedia", "Comedy"),
    ("commedia", "Comedy"),
    ("humor", "Comedy"),
    # ── Crime (must come before Drama so "Crimen Drama" routes
    #    to Crime, not Drama) ──
    ("crime", "Crime"),
    ("crimen", "Crime"),
    ("crimine", "Crime"),
    ("true crime", "Crime"),
    ("criminal", "Crime"),
    ("krimi", "Crime"),
    ("mystery", "Crime"),
    # ── Drama ──
    ("drama", "Drama"),
    ("dramatique", "Drama"),
    # ── Action & Adventure ──
    ("action", "Action & Adventure"),
    ("adventure", "Action & Adventure"),
    ("aventuras", "Action & Adventure"),
    ("avventura", "Action & Adventure"),
    ("abenteuer", "Action & Adventure"),
    # ── Reality ──
    ("reality", "Reality"),
    ("realite", "Reality"),
    ("realidad", "Reality"),
    ("tele realite", "Reality"),
    ("competition", "Reality"),
    # ── Game Shows ──
    ("game show", "Game Shows"),
    ("gameshow", "Game Shows"),
    ("jeu", "Game Shows"),
    ("jeux", "Game Shows"),
    ("gioco", "Game Shows"),
    ("spiele", "Game Shows"),
    # ── Entertainment (catchall for variety/talk/general/lifestyle
    #    shows that don't fit a more specific bucket) ──
    ("entertainment", "Entertainment"),
    ("divertissement", "Entertainment"),
    ("divertimento", "Entertainment"),
    ("intrattenimento", "Entertainment"),
    ("entretenimiento", "Entertainment"),
    ("variety", "Entertainment"),
    ("talk show", "Entertainment"),
    ("general", "Entertainment"),
    ("infotainment", "Entertainment"),
    ("pop culture", "Entertainment"),
    # ── Documentary ──
    ("documentary", "Documentary"),
    ("documentales", "Documentary"),
    ("documental", "Documentary"),
    ("documentari", "Documentary"),
    ("documentaire", "Documentary"),
    ("history", "Documentary"),
    ("paranormal", "Documentary"),
    # ── Education ──
    ("education", "Education"),
    ("educacion", "Education"),
    ("educazione", "Education"),
    ("bildung", "Education"),
    ("cultura", "Education"),
    ("culture", "Education"),
    ("kultur", "Education"),
    ("learning", "Education"),
    # ── Science & Nature ──
    ("nature", "Science & Nature"),
    ("naturaleza", "Science & Nature"),
    ("natura", "Science & Nature"),
    ("science", "Science & Nature"),
    ("scienza", "Science & Nature"),
    ("wildlife", "Science & Nature"),
    ("animal", "Science & Nature"),
    ("fauna", "Science & Nature"),
    ("outdoor", "Science & Nature"),
    # ── Tech ──
    ("tech", "Tech"),
    ("technology", "Tech"),
    ("gadget", "Tech"),
    ("computer", "Tech"),
    ("computing", "Tech"),
    # ── Music ──
    ("music", "Music"),
    ("musica", "Music"),
    ("musik", "Music"),
    ("musique", "Music"),
    ("radio", "Music"),
    ("musical", "Music"),
    # ── Music Videos ──
    ("music video", "Music Videos"),
    ("videoclip", "Music Videos"),
    # ── Lifestyle (cooking, food, travel, fashion, home, design) ──
    ("cooking", "Lifestyle"),
    ("cuisine", "Lifestyle"),
    ("cucina", "Lifestyle"),
    ("food", "Lifestyle"),
    ("gourmet", "Lifestyle"),
    ("travel", "Lifestyle"),
    ("voyage", "Lifestyle"),
    ("viaggi", "Lifestyle"),
    ("viajes", "Lifestyle"),
    ("fashion", "Lifestyle"),
    ("moda", "Lifestyle"),
    ("lifestyle", "Lifestyle"),
    ("home", "Lifestyle"),
    ("design", "Lifestyle"),
    ("relax", "Lifestyle"),
    # ── Auto ──
    ("auto", "Auto"),
    ("automotive", "Auto"),
    ("automobile", "Auto"),
    ("cars", "Auto"),
    ("racing", "Auto"),
    ("motor", "Auto"),
    ("motorsport", "Auto"),
    # ── Shopping ──
    ("shop", "Shopping"),
    ("shopping", "Shopping"),
    ("tienda", "Shopping"),
    ("einkaufen", "Shopping"),
    ("infomercial", "Shopping"),
    # ── Religious ──
    ("religious", "Religious"),
    ("religion", "Religious"),
    ("faith", "Religious"),
    ("cristiana", "Religious"),
    ("cristian", "Religious"),
    ("christian", "Religious"),
    ("islam", "Religious"),
    ("muslim", "Religious"),
    ("jewish", "Religious"),
    ("judaica", "Religious"),
    ("hindu", "Religious"),
    ("buddh", "Religious"),
    ("gospel", "Religious"),
    ("devotional", "Religious"),
    ("spiritual", "Religious"),
    # ── Classic TV ──
    ("classic", "Classic TV"),
    ("retro", "Classic TV"),
    ("vintage", "Classic TV"),
    ("western", "Classic TV"),
    # ── Weather ──
    ("weather", "Weather"),
    ("clima", "Weather"),
    ("meteo", "Weather"),
    ("wetter", "Weather"),
    # ── Local ──
    ("local", "Local"),
    ("regional", "Local"),
    ("regionale", "Local"),
    ("lokal", "Local"),
    # ── Horror ──
    ("horror", "Horror"),
    ("terror", "Horror"),
    ("scary", "Horror"),
    ("halloween", "Horror"),
    # ── Sci-Fi & Fantasy ──
    ("sci fi", "Sci-Fi & Fantasy"),
    ("sci-fi", "Sci-Fi & Fantasy"),
    ("scifi", "Sci-Fi & Fantasy"),
    ("fantasy", "Sci-Fi & Fantasy"),
    ("fantasie", "Sci-Fi & Fantasy"),
    ("ciencia ficcion", "Sci-Fi & Fantasy"),
)


# Source-name fragments that aren't real categories. If a M3U
# source ships its own name as the group_title (e.g. "DistroTV",
# "TCL TOP 15", "TV Favorites") we route to "Other" rather than
# create a one-off category pill for the operator to wade through.
_SOURCE_NAME_FRAGMENTS: frozenset[str] = frozenset({
    # Bare source names
    "distrotv", "pluto", "pluto tv", "plex", "plex tv", "tubi",
    "xumo", "samsung", "samsung tv", "samsung tv plus", "tcl",
    "tcl tv", "airy", "local now", "iptv", "iptv org",
    "iptvorg",
    # Source-flavored top-N lists
    "tcl top 15", "tv favorites",
    # Language tags — not categories
    "espanol", "latino", "en espanol", "allemand", "aleman",
})


def canonical_category(group_title: str) -> str:
    """Map a raw M3U group_title to a canonical display name.

    Returns "Other" if no rule matches. The slug helper downstream
    handles the URL-safe form.

    Source-flavored group_titles (e.g. "DistroTV | US | Business")
    are split on "|" or "/" — only the LAST segment is considered
    for canonicalization, so source/region prefixes don't leak into
    the public category list.
    """
    g = (group_title or "").strip()
    if not g:
        return "Other"

    # Some BuddyChewChew generators pack source-flavor prefixes into
    # the group_title, e.g. "DistroTV | US | Business". Only the
    # last segment is the real category. The "/" rule covers Xumo's
    # "Region/Category" form.
    if "|" in g:
        g = g.rsplit("|", 1)[-1].strip()
    if "/" in g:
        g = g.rsplit("/", 1)[-1].strip()

    n = normalize(g)
    if not n:
        return "Other"

    # Empty or generic group_titles — "Undefined" (175 channels
    # in the wild), "Other", "N/A", bare source names, language
    # tags — all collapse to "Other".
    if n in ("undefined", "n a", "na", "default", "other",
             "uncategorized", "general purpose", "misc",
             "miscellaneous"):
        return "Other"
    if n in _SOURCE_NAME_FRAGMENTS:
        return "Other"

    for needle, canonical in _CATEGORY_CANONICAL:
        if needle in n:
            return canonical
    # No rule matched — fall through to "Other" rather than create
    # a new one-off pill. Operators can extend _CATEGORY_CANONICAL
    # when a new category surfaces in volume.
    return "Other"


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
