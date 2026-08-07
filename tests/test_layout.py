"""Terminal column arithmetic.

These are the sums every printed table depends on, so they are tested
directly rather than only through the renderers that use them.
"""

from mail_triage.layout import cell, char_width, clip, display_width, pad


def test_ascii_is_one_column_per_character():
    assert display_width("hello") == 5
    assert display_width("") == 0


def test_emoji_count_as_two_columns():
    assert char_width("🎉") == 2
    assert display_width("a🎉b") == 4


def test_east_asian_wide_characters_count_as_two_columns():
    assert display_width("日本") == 4
    assert display_width("a日") == 3


def test_short_text_is_returned_unchanged_by_clip():
    assert clip("short", 20) == "short"


def test_clip_truncates_with_an_ellipsis_inside_the_budget():
    clipped = clip("a very long subject line indeed", 10)
    assert clipped.endswith("…")
    assert display_width(clipped) <= 10


def test_clip_counts_the_ellipsis_against_a_wide_string():
    """The ellipsis costs a column, so a run of emoji must stop a column short."""
    clipped = clip("🎉🎉🎉🎉🎉", 5)
    assert display_width(clipped) <= 5


def test_clip_collapses_newlines():
    assert clip("two\nlines", 20) == "two lines"


def test_clip_strips_surrounding_whitespace():
    assert clip("  padded  ", 20) == "padded"


def test_pad_fills_to_the_requested_width():
    assert pad("ab", 5) == "ab   "


def test_pad_measures_in_columns_not_characters():
    """An emoji already occupies two columns, so it needs two fewer spaces."""
    assert display_width(pad("🎉", 5)) == 5


def test_pad_can_right_align():
    assert pad("ab", 5, right=True) == "   ab"


def test_pad_never_truncates_or_returns_negative_padding():
    assert pad("far too long", 4) == "far too long"


def test_cell_clips_and_pads_to_exactly_the_width():
    assert display_width(cell("a very long subject indeed", 10)) == 10
    assert display_width(cell("ab", 10)) == 10


def test_cell_right_aligns_when_asked():
    assert cell("7", 4, right=True) == "   7"
