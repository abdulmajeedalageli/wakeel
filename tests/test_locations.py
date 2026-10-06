import pytest

from app.locations import PlaceMatcher, place_key


@pytest.mark.parametrize(
    "raw",
    [
        "Al-Narjis",
        "al narjis",  # P-1030
        "AL NARJIS",
        "Narjis",
        "حي النرجس",  # P-1029
        "النرجس",
        "نرجس",
        "Narjis district",
        "Narges",  # listed alias
        "Narjiss",  # small typo, matched by similarity
    ],
)
def test_narjis_spellings_all_match(locations, raw):
    assert locations.districts.match(raw) == ("Al-Narjis", True)


def test_yasmin_without_al_matches(locations):  # P-1031
    assert locations.districts.match("Yasmin") == ("Al-Yasmin", True)
    assert locations.districts.match("الياسمين") == ("Al-Yasmin", True)


def test_arabic_city_matches(locations):
    assert locations.cities.match("الرياض") == ("Riyadh", True)
    assert locations.cities.match("riyadh") == ("Riyadh", True)


def test_unknown_district_is_not_forced_onto_a_known_one(locations):
    key, recognised = locations.districts.match("Al-Shifa")
    assert recognised is False
    assert key == "shifa"  # cleaned, so the same unknown name still matches itself


def test_different_districts_stay_different(locations):
    assert locations.districts.match("Al-Rabi")[0] == "Al-Rabi"
    assert locations.districts.match("Al-Arid")[0] == "Al-Arid"
    assert locations.districts.match("Al-Malqa")[0] == "Al-Malqa"


def test_empty_input(locations):
    assert locations.districts.match("") == ("", False)
    assert locations.districts.match(None) == ("", False)


def test_place_key_strips_prefixes():
    assert place_key("حي العليا") == "عليا"
    assert place_key("Al-Olaya") == "olaya"
    assert place_key("Dhahrat Laban") == "dhahrat laban"


def test_custom_alias_map():
    matcher = PlaceMatcher({"Al-Wurud": ["الورود", "Wuroud"]})
    assert matcher.match("حي الورود") == ("Al-Wurud", True)
    assert matcher.match("wuroud") == ("Al-Wurud", True)
