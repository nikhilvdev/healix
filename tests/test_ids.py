import pytest

from healix.ids import normalize_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("user-4471", "user-{n}"),
        ("login-form", "login-form"),
        ("pt1:r1:0:soc1::content", "pt{n}:r{n}:{n}:soc{n}::content"),
        ("row-123e4567-e89b-12d3-a456-426614174000", "row-{uuid}"),
        ("btn_a3f9c2d81b", "btn_{hex}"),
        ("item-20240101123", "item-{n}"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_id(raw, expected):
    assert normalize_id(raw) == expected


def test_normalization_is_stable_across_runs():
    assert normalize_id("user-4471") == normalize_id("user-9032")
    assert normalize_id("pt1:r1:0:x") == normalize_id("pt2:r7:3:x")


def test_short_words_are_not_treated_as_hex():
    assert normalize_id("facade-button") == "facade-button"
