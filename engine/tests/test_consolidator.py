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

    def test_unknown_category_falls_through_to_other(self):
        # Categories we don't recognize collapse to "Other" rather
        # than create a one-off pill. Operators extend
        # _CATEGORY_CANONICAL when a new category surfaces in volume.
        assert canonical_category("Astrology") == "Other"
        assert canonical_category("Quantum Physics") == "Other"
        assert canonical_category("Origami") == "Other"

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

    # ── Long-tail consolidation: the 114-category problem ──
    # The M3U sources from the BuddyChewChew ecosystem and apsatt
    # use wildly inconsistent group_titles across 7 languages. The
    # canonical_category helper is the single place we collapse
    # them into ~30 useful pills. These tests pin the mapping for
    # the variants that showed up in the production catalog after
    # the first bulk import (June 2026).

    def test_undefined_and_bare_source_names_go_to_other(self):
        # The screenshot showed 175 channels with group_title="Undefined"
        # and several with bare source names. All collapse to "Other"
        # so the public site doesn't show 5+ "DistroTV", "TCL TOP 15",
        # "TV Favorites" pills with 1-7 channels each.
        assert canonical_category("Undefined") == "Other"
        assert canonical_category("DistroTV") == "Other"
        assert canonical_category("TCL TOP 15") == "Other"
        assert canonical_category("TV Favorites") == "Other"

    def test_language_tags_go_to_other(self):
        # "En Español", "Latino", "Latinoamérica" etc. are LANGUAGE
        # tags, not categories. The public UI can add a language
        # filter separately; the category list should not include
        # 41 Spanish-language channels as a pseudo-category.
        assert canonical_category("En Español") == "Other"
        assert canonical_category("Español") == "Other"
        assert canonical_category("Latino") == "Other"
        assert canonical_category("En Espa\xf1ol") == "Other"

    def test_pipe_separated_group_titles_take_last_segment(self):
        # The BuddyChewChew DistroTV generator packs source-flavor
        # prefixes into the group_title, e.g. "DistroTV | US | Business".
        # Only the LAST segment is the real category. Source/region
        # prefixes must not leak into the public category list.
        assert canonical_category("DistroTV | US | Business") == "News"
        # "Sport" comes through the Sport needle.
        assert canonical_category("Plex | US | Sport") == "Sports"
        # Source-only trailing → Other.
        assert canonical_category("Plex | US") == "Other"
        assert canonical_category("DistroTV |") == "Other"

    def test_slash_separated_group_titles_take_last_segment(self):
        # Xumo uses "Region/Category" form.
        assert canonical_category("US/News") == "News"
        assert canonical_category("US/Movies") == "Movies"

    def test_jeunesse_collapses_to_kids(self):
        # French for "Youth" — the French Pluto/Plex feed uses this.
        assert canonical_category("Jeunesse") == "Kids"

    def test_intrattenimento_collapses_to_entertainment(self):
        # Italian for "Entertainment".
        assert canonical_category("Intrattenimento") == "Entertainment"

    def test_divertissement_collapses_to_entertainment(self):
        # French for "Entertainment".
        assert canonical_category("Divertissement") == "Entertainment"

    def test_general_collapses_to_entertainment(self):
        # "General" (219 channels) is too broad to be its own pill;
        # it covers variety + talk shows + uncategorized entertainment
        # feeds.
        assert canonical_category("General") == "Entertainment"

    def test_serien_and_seriale_collapses_to_series(self):
        # German + Italian plurals.
        assert canonical_category("Serien") == "Series"
        assert canonical_category("Seriale") == "Series"

    def test_cucina_viaggi_collapses_to_lifestyle(self):
        # Italian "Cooking & Travel" — a single source-flavored pill
        # that doesn't make sense as its own category.
        assert canonical_category("Cucina & Viaggi") == "Lifestyle"

    def test_viajes_y_cocina_collapses_to_lifestyle(self):
        # Spanish "Travel & Cooking".
        assert canonical_category("Viajes y Cocina") == "Lifestyle"

    def test_voyages_et_gastronomie_collapses_to_lifestyle(self):
        # French "Travel & Gastronomy".
        assert canonical_category("Voyages et Gastronomie") == "Lifestyle"

    def test_faith_and_family_collapses_to_religious(self):
        # "Faith & Family" is a TV genre block from faith-based
        # distributors; route to Religious (a Faith-only pill would
        # have ~1 channel and not be useful as a filter).
        assert canonical_category("Faith & Family") == "Religious"

    def test_true_crime_collapses_to_crime(self):
        # 20 channels; standalone pill too small to be useful.
        assert canonical_category("True Crime") == "Crime"

    def test_westerns_collapses_to_classic_tv(self):
        # "Westerns" + "Westerns & Country" → "Classic TV".
        assert canonical_category("Westerns") == "Classic TV"
        assert canonical_category("Westerns & Country") == "Classic TV"

    def test_telenovela_collapses_to_series(self):
        # Spanish-language soap operas — categories are series.
        assert canonical_category("Telenovela") == "Series"

    def test_crime_drama_goes_to_crime(self):
        # Order matters: "crimen drama" should hit Crime first
        # (because the "crimen" needle is more specific), not Drama.
        assert canonical_category("Crimen Drama") == "Crime"

    def test_comedia_drama_goes_to_comedy(self):
        # "Comedia + Drama" — Comedy wins because the "comedia"
        # needle is in the substring before "drama".
        assert canonical_category("Comedia + Drama") == "Comedy"

    def test_binge_worthy_collapses_to_movies(self):
        # Tubi's marketing label for "long-form movie content".
        assert canonical_category("Binge-worthy") == "Movies"

    def test_heating_up_competition_collapses_to_reality(self):
        # Sports reality-TV subgenre; reality is the closer fit.
        assert canonical_category("Heating up the Competition") == "Reality"

    def test_anime_does_not_match_animation(self):
        # "anime" is a substring of "animation" — order matters.
        # "anime" needle comes first, so anime channels go to Anime
        # (their own pill) and animation channels go to Animation.
        assert canonical_category("Anime") == "Anime"
        assert canonical_category("Animation") == "Animation"
        # Plural is fine too.
        assert canonical_category("Animation") == "Animation"

    def test_max_canonical_count_is_bounded(self):
        # Sanity: count how many distinct canonical names the
        # production-shaped data produces. We allow up to 35 (30
        # meaningful + a few "Other"-adjacent) so the operator has
        # headroom for edge cases without going back to the 114-
        # category problem.
        from engine.consolidator import _CATEGORY_CANONICAL
        canon_names = {c for _, c in _CATEGORY_CANONICAL} | {"Other"}
        assert len(canon_names) <= 35, (
            f"canonical category set grew to {len(canon_names)}: {sorted(canon_names)}"
        )


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
