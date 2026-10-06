from app.logging_setup import mask_phone


def test_mask_normal_saudi_number():
    assert mask_phone("966512341234") == "9665****1234"


def test_mask_short_number_shows_only_last_two_digits():
    assert mask_phone("12345") == "****45"


def test_mask_empty_or_none():
    assert mask_phone("") == "<none>"
    assert mask_phone(None) == "<none>"
