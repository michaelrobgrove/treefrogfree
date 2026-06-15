"""Tests for consolidator normalization + group branding.

These are the most important tests in the project. If the consolidator is
wrong, duplicates leak to users. If it's too aggressive, distinct channels
collapse into one. The boundary is in normalize(); everything else hangs
off of it.
"""
from engine.consolidator import (
    canonical_category,
    canonical_channel_name,
    candidate_matches,
    group_brand_name,
    group_slug,
    is_same_channel,
    normalize,
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


class TestCanonicalCategory:
    """The 81-category problem: free M3U sources have wildly
    inconsistent group titles. The canonical_category helper
    collapses known variants (Animation, Animation Classics,
    Animation Kids) into a single canonical name (Animation) so
    the public site's category filter shows ~15-20 useful pills
    instead of 81 near-empty ones."""

    def test_animation_variants_collapse(self):
        assert canonical_category("Animation") == "Animation"
        assert canonical_category("Animation Classics") == "Animation"
        assert canonical_category("Animation Kids") == "Animation"
        assert canonical_category("Animación") == "Animation"  # Spanish
        assert canonical_category("Cartoons") == "Animation"

    def test_kids_is_distinct_from_animation(self):
        # "Kids" is its own canonical — we don't want every kids
        # channel under the Animation pill. But the order matters:
        # "Animation Kids" must hit Animation first, not Kids.
        assert canonical_category("Kids") == "Kids"
        assert canonical_category("Children") == "Kids"
        assert canonical_category("Preschool") == "Kids"
        assert canonical_category("Animation Kids") == "Animation"

    def test_news_variants_collapse(self):
        assert canonical_category("News") == "News"
        assert canonical_category("Noticias") == "News"
        assert canonical_category("US News") == "News"
        assert canonical_category("Local News HD") == "News"

    def test_sports_variants_collapse(self):
        assert canonical_category("Sports") == "Sports"
        assert canonical_category("Sport") == "Sports"
        assert canonical_category("Deportes") == "Sports"
        assert canonical_category("Sports HD") == "Sports"

    def test_movies_and_series_split(self):
        # Movies and series are distinct — combining them would
        # make the Movies pill too big to be useful as a filter.
        assert canonical_category("Movies") == "Movies"
        assert canonical_category("Peliculas") == "Movies"
        assert canonical_category("Series") == "Series"
        assert canonical_category("TV Shows") == "Series"

    def test_music_includes_radio(self):
        # Many M3Us lump radio streams under "Music". Combine them
        # so the Music pill surfaces audio-only streams.
        assert canonical_category("Music") == "Music"
        assert canonical_category("Música") == "Music"
        assert canonical_category("Radio") == "Music"

    def test_unknown_category_falls_through(self):
        # A new M3U source can introduce categories we don't know
        # about. The helper should return the original string (not
        # crash, not return "Other") so the UI still shows the pill.
        assert canonical_category("Astrology") == "Astrology"
        assert canonical_category("Bicycle Racing") == "Bicycle Racing"

    def test_empty_and_none_like(self):
        assert canonical_category("") == "Other"
        assert canonical_category("   ") == "Other"
        assert canonical_category(None) == "Other"  # type: ignore[arg-type]

    def test_case_insensitive(self):
        assert canonical_category("ANIMATION") == "Animation"
        assert canonical_category("animation CLASSICS") == "Animation"
        assert canonical_category("kids") == "Kids"

    def test_punctuation_dropped_before_match(self):
        # The match is against the normalized form, so punctuation
        # never blocks a canonicalization. "24/7" → "247" (digits
        # kept) so the News needle still matches.
        assert canonical_category("24/7 News") == "News"
        assert canonical_category("Action & Adventure") == "Action & Adventure"  # falls through cleanly
        # The helper never raises on weird punctuation.
        canonical_category("*** weird !!! group ???")

    def test_slugs_round_trip_through_group_slug(self):
        # The catalog uses group_slug(canonical_category(...)) for
        # the per-channel `category` field and the categories list
        # `slug` field. Make sure the pipeline yields a consistent
        # URL-safe slug.
        from engine.consolidator import group_slug
        assert group_slug(canonical_category("Animation Classics")) == "animation"
        assert group_slug(canonical_category("Local News")) == "news"


class TestCanonicalChannelName:
    """Operator override: region-flavored variants of the same network
    should collapse to one channels row with multiple stream URLs.
    Example: 'PBS Kids Alaska' + 'PBS Kids East' → 'pbs kids'.

    The base normalizer can't catch 'Alaska' because that's a real
    word, not a quality tag. The override map catches it explicitly.
    """

    def test_pbs_kids_regions_collapse(self):
        # The original ask from the operator.
        assert canonical_channel_name("PBS Kids Alaska") == "pbs kids"
        assert canonical_channel_name("PBS Kids East") == "pbs kids"
        assert canonical_channel_name("PBS Kids West") == "pbs kids"
        assert canonical_channel_name("PBS Kids") == "pbs kids"  # no-op for the canonical
        assert canonical_channel_name("PBS Kids Hawaii") == "pbs kids"

    def test_case_insensitive(self):
        # Normalizer drops case before the override lookup.
        assert canonical_channel_name("pbs kids ALASKA") == "pbs kids"
        assert canonical_channel_name("PBS KIDS East") == "pbs kids"

    def test_punctuation_dropped(self):
        assert canonical_channel_name("PBS Kids: Alaska Edition") == "pbs kids"
        assert canonical_channel_name("PBS Kids — East Feed") == "pbs kids"

    def test_unrelated_channel_passthrough(self):
        # A channel that has no override should fall through to
        # the normalizer — the override never widens the match.
        assert canonical_channel_name("BBC News") == "bbc news"
        assert canonical_channel_name("CNN") == "cnn"
        assert canonical_channel_name("ESPN 2") == "espn 2"

    def test_quality_suffixes_still_stripped(self):
        # The override is layered on top of the normalizer's suffix
        # strip. "PBS Kids East HD" — 'east' strips to 'pbs kids',
        # then the override is a no-op because we already match.
        assert canonical_channel_name("PBS Kids East HD") == "pbs kids"

    def test_empty_input(self):
        assert canonical_channel_name("") == ""
        assert canonical_channel_name("   ") == ""
