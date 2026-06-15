"""Tests for consolidator normalization + group branding.

These are the most important tests in the project. If the consolidator is
wrong, duplicates leak to users. If it's too aggressive, distinct channels
collapse into one. The boundary is in normalize(); everything else hangs
off of it.
"""
from engine.consolidator import (
    normalize,
    is_same_channel,
    group_slug,
    group_brand_name,
    candidate_matches,
)


class TestNormalize:
    def test_basic_lowercase_and_strip(self):
        assert normalize("  CNN  ") == "cnn"

    def test_drops_punctuation(self):
        assert normalize("BBC-News") == "bbc news"
        assert normalize("ESPN: Sports") == "espn sports"

    def test_collapses_whitespace(self):
        assert normalize("BBC    News") == "bbc news"
        assert normalize("CNN\t\tNews") == "cnn news"

    def test_drops_hd_suffix(self):
        assert normalize("BBC News HD") == "bbc news"
        assert normalize("BBC News FHD") == "bbc news"
        assert normalize("CNN UHD") == "cnn"

    def test_drops_plus_one(self):
        assert normalize("Discovery+1") == "discovery"
        assert normalize("ESPN+1") == "espn"

    def test_drops_directional(self):
        assert normalize("CBS East") == "cbs"
        assert normalize("NBC West") == "nbc"

    def test_drops_24_7(self):
        assert normalize("Fox Weather 24/7") == "fox weather"
        assert normalize("CNN 247") == "cnn"

    def test_drops_country_codes(self):
        # Edge case: "Fox News USA" → "fox news" (USA stripped),
        # but "USA Network" → "usa network" (USA is a real word here).
        # We tolerate this; admin can fix in the UI.
        assert normalize("Fox News USA") == "fox news"
        assert normalize("BBC News UK") == "bbc news"

    def test_keeps_espn_2(self):
        # Numeric suffixes that are part of the channel identity must stay.
        assert normalize("ESPN 2") == "espn 2"
        assert normalize("ESPN2") == "espn2"

    def test_empty_and_none_like(self):
        assert normalize("") == ""
        assert normalize("   ") == ""
        assert normalize("...") == ""

    def test_accent_stripping(self):
        assert normalize("Café") == "cafe"
        assert normalize("TéléMéxico") == "telemexico"

    def test_repeated_suffix_stripping(self):
        assert normalize("BBC News HD Backup") == "bbc news"

    def test_punctuation_does_not_become_word_boundary_noise(self):
        # "A&E" and "AE" should not collapse to the same key.
        # Punctuation is dropped, so they normalize to different keys.
        assert normalize("A&E") != normalize("AE")
        # The exact form is implementation-defined; both are valid as
        # long as they're distinct.
        assert normalize("A&E") in ("a", "a e")
        assert normalize("AE") == "ae"

    def test_case_insensitive_idempotence(self):
        a = normalize("BBC News HD")
        b = normalize("bbc news hd")
        c = normalize("BBC-NEWS-hd")
        assert a == b == c == "bbc news"


class TestIsSameChannel:
    def test_obvious_match(self):
        assert is_same_channel("BBC News", "BBC News HD")
        assert is_same_channel("CNN", "cnn")
        assert is_same_channel("ESPN2", "ESPN 2") is False  # espn2 vs espn 2

    def test_obvious_mismatch(self):
        assert not is_same_channel("CNN", "BBC")
        assert not is_same_channel("", "")

    def test_via_normalize(self):
        # Anything that equal-normalizes is a match.
        assert is_same_channel("Fox Weather", "FOX WEATHER 24/7")


class TestGroupSlug:
    def test_basic(self):
        assert group_slug("News") == "news"
        assert group_slug("Sports & Outdoors") == "sports-outdoors"

    def test_empty(self):
        assert group_slug("") == "other"
        assert group_slug(None) == "other"  # type: ignore[arg-type]

    def test_unicode(self):
        assert group_slug("Café") == "cafe"


class TestGroupBrandName:
    def test_renames_with_prefix(self):
        assert group_brand_name("News") == "🐸 Tree Frog Free | News"
        assert group_brand_name("Kids") == "🐸 Tree Frog Free | Kids"

    def test_unknown_falls_to_other(self):
        assert group_brand_name("") == "🐸 Tree Frog Free | Other"
        assert group_brand_name(None) == "🐸 Tree Frog Free | Other"  # type: ignore[arg-type]


class TestCandidateMatches:
    def test_exact_match_scores_higher_than_containment(self):
        results = candidate_matches("CNN", ["CNN HD", "CNN", "CNBC News", "BBC"])
        # CNN (exact) should beat CNN HD (containment) and CNBC News (no match).
        assert results[0][0] == "CNN"
        assert ("CNN HD", 100) in results or results[0][0] == "CNN"

    def test_filters_zero_scores(self):
        results = candidate_matches("CNN", ["BBC", "Fox News"])
        assert results == []

    def test_limit(self):
        cands = [f"CNN HD {i}" for i in range(50)]
        results = candidate_matches("CNN", cands, limit=5)
        assert len(results) == 5
