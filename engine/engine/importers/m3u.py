"""M3U/M3U8 parser — streams entries as an async iterator.

M3U grammar (relevant subset):
    #EXTM3U
    #EXTINF:-1 tvg-id="ch1" tvg-name="Channel 1" tvg-logo="http://..." group-title="News",Channel 1
    http://provider.example/live/ch1.m3u8
    #EXTINF:-1 ...,Channel 2
    http://provider.example/live/ch2.m3u8

We stream the file so even a 50 MB M3U doesn't blow the 1 GB container's RSS.
Tested in tests/test_m3u_parser.py.
"""
from __future__ import annotations

import re
import logging
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import aiohttp

from ..models import M3UEntry

log = logging.getLogger("treefrog.m3u")

# An attribute line looks like:
#   #EXTINF:-1 tvg-id="abc" tvg-name="Foo" tvg-logo="http://..." group-title="News",Foo HD
_EXTINF_RE = re.compile(
    r'^#EXTINF:(?P<duration>-?\d+)\s*'
    r'(?P<attrs>(?:[a-zA-Z-]+="[^"]*"\s*)*),'
    r'(?P<title>.+)$'
)

_ATTR_RE = re.compile(r'([a-zA-Z-]+)="([^"]*)"')


def _parse_attrs(attr_string: str) -> dict[str, str]:
    """Pull tvg-id / tvg-name / tvg-logo / group-title out of the attr blob."""
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(attr_string)}


def _is_http_url(s: str) -> bool:
    """Cheap URL check: must start with http:// or https://."""
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def _parse_extinf(line: str) -> Optional[M3UEntry]:
    """Parse one #EXTINF line. Returns None if the line is malformed."""
    m = _EXTINF_RE.match(line)
    if not m:
        return None
    attrs = _parse_attrs(m.group("attrs"))
    title = m.group("title").strip()
    return M3UEntry(
        name=title,
        url="",  # filled in by the caller from the next line
        tvg_id=attrs.get("tvg-id") or None,
        tvg_name=attrs.get("tvg-name") or None,
        tvg_logo=attrs.get("tvg-logo") or None,
        group_title=attrs.get("group-title") or "Other",
    )


async def _stream_lines(text_stream: aiohttp.StreamReader) -> AsyncIterator[str]:
    """Yield lines one at a time without buffering the whole file.

    aiohttp's iter_chunked yields bytes; we decode each chunk as latin-1
    to preserve byte boundaries (we only care about ASCII line breaks
    and the #EXTM3U / #EXTINF markers anyway).
    """
    buf: list[str] = []
    async for chunk in text_stream.iter_chunked(64 * 1024):
        if not chunk:
            continue
        # bytes → str without splitting multi-byte UTF-8 sequences.
        # Latin-1 is a 1:1 byte-to-codepoint mapping so chunk boundaries
        # stay clean. Non-ASCII content (rare in M3U) just becomes garbage
        # we don't care about.
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            text = chunk.decode("utf-8", errors="replace")
        for ch in text:
            if ch == "\n":
                yield "".join(buf).rstrip("\r")
                buf = []
            else:
                buf.append(ch)
    if buf:
        yield "".join(buf).rstrip("\r")


async def parse_url(url: str, *, session: aiohttp.ClientSession) -> AsyncIterator[M3UEntry]:
    """Stream-parse an M3U from a URL.

    Use a 30s connect timeout, no overall read timeout (some M3U files are
    served slowly). The 64KB chunk size keeps memory bounded.

    allow_redirects=True: aiohttp defaults to False, but most real-world
    M3U URLs come from shorteners (tinyurl, bit.ly, etc.) that 302 to
    the actual file. Without following redirects, the importer gets
    back a 302 and the parse fails before the first line.
    """
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
    async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
        resp.raise_for_status()
        # Make sure we're getting text. Some servers mislabel M3U as octet-stream.
        ctype = resp.headers.get("Content-Type", "").lower()
        if ctype and "html" in ctype:
            raise ValueError(f"URL returned HTML, not M3U: {url}")
        async for entry in _parse_stream(resp.content):
            yield entry


async def parse_file(path: str) -> AsyncIterator[M3UEntry]:
    """Stream-parse an M3U from a local file path."""
    # aiofiles would be nice, but the stdlib + a small read loop is enough.
    loop = __import__("asyncio").get_event_loop()

    def _open() -> any:
        return open(path, "r", encoding="utf-8", errors="replace")

    # Run blocking open in default thread pool.
    f = await loop.run_in_executor(None, _open)
    try:
        pending: Optional[M3UEntry] = None
        while True:
            line = await loop.run_in_executor(None, f.readline)
            if not line:
                break
            line = line.rstrip("\r\n")
            if not line:
                continue
            if line.startswith("#EXTM3U"):
                continue
            if line.startswith("#EXTINF:"):
                pending = _parse_extinf(line)
                continue
            if line.startswith("#"):
                # Other metadata lines (e.g., #EXTVLCOPT) — ignore.
                continue
            if _is_http_url(line):
                if pending is None:
                    # URL with no preceding #EXTINF — synthesize a bare entry.
                    pending = M3UEntry(name=line.strip(), url=line.strip())
                else:
                    pending.url = line.strip()
                yield pending
                pending = None
    finally:
        await loop.run_in_executor(None, f.close)


async def _parse_stream(text_stream: aiohttp.StreamReader) -> AsyncIterator[M3UEntry]:
    """Shared streaming logic used by parse_url."""
    pending: Optional[M3UEntry] = None
    saw_header = False
    async for line in _stream_lines(text_stream):
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            saw_header = True
            continue
        if line.startswith("#EXTINF:"):
            pending = _parse_extinf(line)
            continue
        if line.startswith("#"):
            continue
        if _is_http_url(line):
            if not saw_header:
                # Tolerant: some sources omit #EXTM3U. Don't reject.
                saw_header = True
            if pending is None:
                pending = M3UEntry(name=line.strip(), url=line.strip())
            else:
                pending.url = line.strip()
            yield pending
            pending = None


def looks_like_url(s: str) -> bool:
    """Public helper for callers deciding between parse_url and parse_file."""
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False
