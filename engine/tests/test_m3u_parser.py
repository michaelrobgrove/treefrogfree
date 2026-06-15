"""Tests for the M3U parser.

The parser must handle real-world M3U files: missing #EXTM3U headers, weird
attribute quoting, comments interleaved with entries, and bare URLs with no
preceding #EXTINF.
"""
import asyncio
import textwrap
import tempfile
from pathlib import Path

import pytest

from engine.importers.m3u import parse_file, _parse_extinf, looks_like_url


SAMPLE = textwrap.dedent("""\
    #EXTM3U
    #EXTINF:-1 tvg-id="bbc1.uk" tvg-name="BBC One" tvg-logo="http://img/bbc1.png" group-title="UK",BBC One
    http://provider.example/bbc1.m3u8
    #EXTINF:-1 tvg-id="bbc2.uk" tvg-name="BBC Two" group-title="UK",BBC Two HD
    http://provider.example/bbc2.m3u8
    #EXTVLCOPT:network-caching=1000
    #EXTINF:-1 group-title="News",CNN
    http://provider.example/cnn.m3u8
    http://provider.example/bare-url.m3u8
""")


def test_parse_extinf_basic():
    line = '#EXTINF:-1 tvg-id="x" tvg-name="X" group-title="Y",X HD'
    entry = _parse_extinf(line)
    assert entry is not None
    assert entry.name == "X HD"
    assert entry.tvg_id == "x"
    assert entry.tvg_name == "X"
    assert entry.group_title == "Y"


def test_parse_extinf_handles_missing_attrs():
    line = "#EXTINF:-1,Bare Channel"
    entry = _parse_extinf(line)
    assert entry is not None
    assert entry.name == "Bare Channel"
    assert entry.tvg_id is None
    assert entry.group_title == "Other"


def test_parse_extinf_returns_none_for_garbage():
    assert _parse_extinf("#EXTM3U") is None
    assert _parse_extinf("#EXTVLCOPT:foo=bar") is None


def test_looks_like_url():
    assert looks_like_url("http://example.com/x.m3u")
    assert looks_like_url("https://example.com/x.m3u")
    assert not looks_like_url("/local/path.m3u")
    assert not looks_like_url("ftp://example.com/x.m3u")


@pytest.mark.asyncio
async def test_parse_file_full_sample():
    with tempfile.NamedTemporaryFile("w", suffix=".m3u", delete=False, encoding="utf-8") as f:
        f.write(SAMPLE)
        path = f.name
    try:
        entries = [e async for e in parse_file(path)]
    finally:
        Path(path).unlink(missing_ok=True)

    assert len(entries) == 4

    # First two: full metadata
    assert entries[0].name == "BBC One"
    assert entries[0].tvg_id == "bbc1.uk"
    assert entries[0].tvg_logo == "http://img/bbc1.png"
    assert entries[0].group_title == "UK"
    assert entries[0].url == "http://provider.example/bbc1.m3u8"

    assert entries[1].name == "BBC Two HD"
    assert entries[1].tvg_id == "bbc2.uk"
    assert entries[1].group_title == "UK"
    assert entries[1].url == "http://provider.example/bbc2.m3u8"

    # Third: no tvg-id, no logo — should still parse
    assert entries[2].name == "CNN"
    assert entries[2].group_title == "News"
    assert entries[2].url == "http://provider.example/cnn.m3u8"

    # Fourth: bare URL with no preceding #EXTINF
    assert entries[3].name == "http://provider.example/bare-url.m3u8"
    assert entries[3].url == "http://provider.example/bare-url.m3u8"


@pytest.mark.asyncio
async def test_parse_file_skips_comments():
    src = "#EXTM3U\n# a comment\n#EXTINF:-1,Channel\nhttp://x/y.m3u8\n"
    with tempfile.NamedTemporaryFile("w", suffix=".m3u", delete=False, encoding="utf-8") as f:
        f.write(src)
        path = f.name
    try:
        entries = [e async for e in parse_file(path)]
    finally:
        Path(path).unlink(missing_ok=True)
    assert len(entries) == 1
    assert entries[0].name == "Channel"
