import pytest

from app.normalize import clean_str, id_key, normalize_text, parse_number


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Al-Narjis ", "al narjis"),
        ("Available ", "available"),
        ("حيّ النرجس", "حي النرجس"),  # shadda removed
        ("أرض", "ارض"),  # hamza forms unified
        ("قرطبة", "قرطبه"),  # taa marbuta
        ("price_sar", "price sar"),
        (None, ""),
        ("   ", ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (950000, 950000),
        (950000.0, 950000),
        (12.5, 12.5),
        ("950000", 950000),
        ("950,000", 950000),  # P-1035
        ("3,450,000", 3450000),  # how Google displays formatted numbers
        (" 950 000 ", 950000),
        ("٩٥٠٬٠٠٠", 950000),  # Arabic digits + Arabic thousands separator
        ("120.5", 120.5),
    ],
)
def test_parse_number_accepts_plain_numbers(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize(
    "raw", ["call us", "950k", "950000 SAR", "", None, "-5", -5, True, float("nan"), "1,2.3.4"]
)
def test_parse_number_rejects_non_numbers(raw):
    assert parse_number(raw) is None


def test_clean_str():
    assert clean_str("  P-1001 ") == "P-1001"
    assert clean_str(7200000001.0) == "7200000001"
    assert clean_str("") is None
    assert clean_str(None) is None


def test_id_key_ignores_case_spaces_and_hyphens():
    assert id_key("P-1023") == id_key("p1023") == id_key(" P 1023 ") == "p1023"
