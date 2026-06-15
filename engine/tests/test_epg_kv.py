"""Tests for the EPG now/next publisher.

The XMLTV parsing + matching is pure-Python and pure-function, so
we can drive it with a fixture file written into a tmp dir and
assert on the dict `compute_nownext` returns. No DB, no KV, no
asyncio needed for the core logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.publisher.epg_kv import (
    EpgProgram,
    _format_iso,
    _parse_xmltv_dt,
    _pick_now_next,
    _programs_for_channel,
    _strip_quality_suffix,
    compute_nownext,
)


# ---- @suffix strip ----

class TestStripQualitySuffix:
    def test_strips_hd(self):
        assert _strip_quality_suffix("WGBX-TV.us@HD") == "WGBX-TV.us"

    def test_strips_us(self):
        assert _strip_quality_suffix("BBC News@US") == "BBC News"

    def test_strips_case_insensitively(self):
        assert _strip_quality_suffix("CNN@hd") == "CNN"

    def test_strips_uk(self):
        assert _strip_quality_suffix("Channel4@UK") == "Channel4"

    def test_no_suffix_unchanged(self):
        assert _strip_quality_suffix("PBS-Kids.us") == "PBS-Kids.us"

    def test_unknown_suffix_unchanged(self):
        # `@preview` isn't in our list — leave it alone.
        assert _strip_quality_suffix("BBC@preview") == "BBC@preview"

    def test_empty_string(self):
        assert _strip_quality_suffix("") == ""


# ---- XMLTV date parser ----

class TestParseXmltvDt:
    def test_basic(self):
        dt = _parse_xmltv_dt("20260615180000 +0000")
        assert dt == datetime(2026, 6, 15, 18, 0, 0, tzinfo=timezone.utc)

    def test_no_offset(self):
        # XMLTV allows omitting the offset; default to UTC.
        dt = _parse_xmltv_dt("20260615180000")
        assert dt == datetime(2026, 6, 15, 18, 0, 0, tzinfo=timezone.utc)

    def test_iso_fallback(self):
        dt = _parse_xmltv_dt("2026-06-15T18:00:00+00:00")
        assert dt == datetime(2026, 6, 15, 18, 0, 0, tzinfo=timezone.utc)

    def test_garbage(self):
        assert _parse_xmltv_dt("") is None
        assert _parse_xmltv_dt("not a date") is None
        assert _parse_xmltv_dt("1234567890 +0000") is None


# ---- _pick_now_next (the heart of the picker) ----

def _mk(starts_at: str, stops_at: str, title: str):
    return (
        _parse_xmltv_dt(starts_at),
        _parse_xmltv_dt(stops_at),
        title,
    )


class TestPickNowNext:
    def test_empty_programs(self):
        result = _pick_now_next([], datetime(2026, 6, 15, 18, tzinfo=timezone.utc))
        assert result == {"now": None, "next": None}

    def test_current_program_picked(self):
        progs = [
            _mk("20260615170000 +0000", "20260615180000 +0000", "Evening News"),
            _mk("20260615180000 +0000", "20260615190000 +0000", "Late News"),
        ]
        result = _pick_now_next(progs, datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc))
        assert result["now"]["title"] == "Late News"
        assert result["next"] is None  # nothing after

    def test_next_program_picked(self):
        progs = [
            _mk("20260615170000 +0000", "20260615180000 +0000", "Evening News"),
            _mk("20260615180000 +0000", "20260615190000 +0000", "Late News"),
        ]
        result = _pick_now_next(progs, datetime(2026, 6, 15, 17, 30, tzinfo=timezone.utc))
        assert result["now"]["title"] == "Evening News"
        assert result["next"]["title"] == "Late News"

    def test_no_current_no_next(self):
        progs = [
            _mk("20260615170000 +0000", "20260615180000 +0000", "Evening News"),
        ]
        result = _pick_now_next(progs, datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc))
        assert result["now"] is None
        assert result["next"] is None


# ---- compute_nownext against a fixture XMLTV ----

_FIXTURE_XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="fixture">
  <channel id="WGBX-DT.us">
    <display-name>WGBX-DT Boston, MA US</display-name>
  </channel>
  <channel id="KQEDPlus.us">
    <display-name>KQED Plus San Francisco, CA US</display-name>
  </channel>
  <channel id="unmapped.us">
    <display-name>Unmapped Network</display-name>
  </channel>

  <programme channel="WGBX-DT.us" start="20260615170000 +0000" stop="20260615180000 +0000">
    <title>Evening News</title>
  </programme>
  <programme channel="WGBX-DT.us" start="20260615180000 +0000" stop="20260615190000 +0000">
    <title>Late News</title>
  </programme>
  <programme channel="KQEDPlus.us" start="20260615170000 +0000" stop="20260615180000 +0000">
    <title>PBS NewsHour</title>
  </programme>
</tv>
"""


def _write_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "fixture.xml"
    p.write_text(_FIXTURE_XMLTV, encoding="utf-8")
    return p


class TestComputeNownext:
    def test_exact_tvg_id_match(self, tmp_path):
        xml = _write_fixture(tmp_path)
        # Pin the clock between 17:30 and 18:00 UTC on 2026-06-15.
        # The "Evening News" program covers that window.
        now = datetime(2026, 6, 15, 17, 45, tzinfo=timezone.utc)
        result = compute_nownext(xml, {"WGBX-DT.us"}, now)
        assert "WGBX-DT.us" in result
        assert result["WGBX-DT.us"]["now"]["title"] == "Evening News"
        assert result["WGBX-DT.us"]["next"]["title"] == "Late News"

    def test_unmapped_tvg_id_returns_empty_entry(self, tmp_path):
        xml = _write_fixture(tmp_path)
        now = datetime(2026, 6, 15, 17, 45, tzinfo=timezone.utc)
        result = compute_nownext(xml, {"does-not-exist.us"}, now)
        # The publisher records the absence so the caller knows we
        # considered this id and found nothing.
        assert result["does-not-exist.us"] == {"now": None, "next": None}

    def test_off_air_channel(self, tmp_path):
        xml = _write_fixture(tmp_path)
        # The mapped but off-air channel still appears with empty
        # now/next so the player can render "we know about this but
        # no program is airing right now."
        now = datetime(2026, 6, 15, 19, 30, tzinfo=timezone.utc)
        result = compute_nownext(xml, {"WGBX-DT.us"}, now)
        assert result["WGBX-DT.us"] == {"now": None, "next": None}

    def test_bad_xml_returns_empty_for_all(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("<not-xml", encoding="utf-8")
        result = compute_nownext(
            bad,
            {"WGBX-DT.us", "KQEDPlus.us"},
            datetime(2026, 6, 15, 18, tzinfo=timezone.utc),
        )
        assert result == {"WGBX-DT.us": {"now": None, "next": None},
                          "KQEDPlus.us": {"now": None, "next": None}}
