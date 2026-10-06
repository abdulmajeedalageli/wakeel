"""PropertyStore: refresh behaviour, search_properties and get_property_details."""

import logging

from app.properties import MAX_RESULTS, PropertyStore


def ids(result):
    return [p.property_id for p in result.properties]


# --- refresh ---

def test_refresh_loads_sample(store):
    assert len(store.properties) == 33
    assert store.last_refresh_at is not None
    assert store.last_error is None
    assert [r.row_number for r in store.skipped_rows] == [33, 34]


def test_failed_refresh_keeps_last_good_copy(sample_rows, sheet_config, locations, caplog):
    calls = {"n": 0}

    def flaky_loader():
        calls["n"] += 1
        if calls["n"] > 1:
            raise ConnectionError("Google is down")
        return sample_rows

    s = PropertyStore(flaky_loader, sheet_config, locations)
    assert s.refresh() is True
    first_refresh = s.last_refresh_at

    with caplog.at_level(logging.WARNING):
        assert s.refresh() is False

    assert len(s.properties) == 33  # still serving the old data
    assert s.last_refresh_at == first_refresh
    assert "Google is down" in s.last_error
    assert "keeping the last good copy" in caplog.text


def test_broken_header_keeps_last_good_copy(sample_rows, sheet_config, locations):
    rows = {"data": sample_rows}
    s = PropertyStore(lambda: rows["data"], sheet_config, locations)
    s.refresh()
    rows["data"] = [["something", "else"]]
    assert s.refresh() is False
    assert len(s.properties) == 33
    assert "SheetFormatError" in s.last_error


def test_sheet_with_no_valid_rows_keeps_last_good_copy(sample_rows, sheet_config, locations):
    rows = {"data": sample_rows}
    s = PropertyStore(lambda: rows["data"], sheet_config, locations)
    s.refresh()
    rows["data"] = [sample_rows[0]]  # header only, e.g. rows deleted by accident
    assert s.refresh() is False
    assert len(s.properties) == 33


def test_successful_refresh_after_failure_clears_error(sample_rows, sheet_config, locations):
    state = {"fail": True}

    def loader():
        if state["fail"]:
            raise ConnectionError("down")
        return sample_rows

    s = PropertyStore(loader, sheet_config, locations)
    assert s.refresh() is False
    assert s.properties == []
    state["fail"] = False
    assert s.refresh() is True
    assert s.last_error is None


def test_skipped_rows_are_logged_once_not_every_refresh(store, caplog):
    with caplog.at_level(logging.WARNING):
        store.refresh()
    assert "Sheet row 33 skipped" not in caplog.text  # unchanged since the first load


def test_store_without_sheet_is_not_configured(sheet_config, locations):
    s = PropertyStore(None, sheet_config, locations)
    assert not s.configured
    assert s.refresh() is False
    assert s.search().total_matches == 0


# --- get_property_details ---

def test_get_by_exact_id(store):
    assert store.get("P-1023").type == "office"


def test_get_is_forgiving_about_id_format(store):
    assert store.get("p1023").property_id == "P-1023"
    assert store.get(" P 1023 ").property_id == "P-1023"


def test_get_returns_reserved_and_sold_so_agent_can_be_honest(store):
    assert store.get("P-1004").status == "reserved"
    assert store.get("P-1010").status == "sold"


def test_get_unknown_or_skipped_id_returns_none(store):
    assert store.get("P-9999") is None
    assert store.get("P-1033") is None  # skipped (invalid price)


# --- search_properties: filters ---

def test_search_returns_only_available(store):
    result = store.search(type="villa", deal="sale", district="Hittin")
    assert ids(result) == []  # P-1004 is reserved
    result = store.search(type="floor", deal="sale", district="Qurtubah")
    assert ids(result) == []  # P-1018 is reserved
    result = store.search(district="Al-Sahafa", type="apartment", deal="sale")
    assert ids(result) == []  # P-1010 is sold


def test_search_never_returns_more_than_five(store):
    result = store.search()
    assert len(result.properties) == MAX_RESULTS
    assert result.total_matches == 30


def test_search_by_deal_and_type(store):
    result = store.search(deal="rent", type="apartment")
    assert result.total_matches == 6
    assert all(p.deal == "rent" and p.type == "apartment" for p in result.properties)


def test_search_accepts_arabic_type_and_deal(store):
    assert store.search(deal="إيجار", type="شقة").total_matches == 6
    assert store.search(deal="للبيع", type="أرض").total_matches == 4


def test_search_by_city_in_arabic(store):
    assert store.search(city="الرياض").total_matches == 30
    assert store.search(city="Jeddah").total_matches == 0


def test_search_price_range(store):
    result = store.search(deal="sale", type="apartment", min_price=700000, max_price=900000)
    assert set(ids(result)) == {"P-1007", "P-1008", "P-1031"}
    assert all(700000 <= p.price_sar <= 900000 for p in result.properties)


def test_search_swapped_price_range_still_works(store):
    a = store.search(deal="sale", type="apartment", min_price=700000, max_price=900000)
    b = store.search(deal="sale", type="apartment", min_price=900000, max_price=700000)
    assert ids(a) == ids(b)


def test_search_min_bedrooms_excludes_unknown_bedrooms(store):
    result = store.search(min_bedrooms=5)
    assert result.total_matches > 0
    assert all(p.bedrooms is not None and p.bedrooms >= 5 for p in result.properties)
    # Offices/shops/land have no bedrooms, so never match a bedroom filter.
    assert store.search(type="office", min_bedrooms=1).total_matches == 0


def test_search_min_area(store):
    result = store.search(type="land", min_area=800)
    assert set(ids(result)) == {"P-1020", "P-1022"}


def test_search_unknown_district_finds_nothing(store):
    assert store.search(district="Al-Shifa").total_matches == 0


# --- search_properties: the yellow TEST rows ---

def test_search_al_narjis_finds_all_spellings(store):
    result = store.search(district="Al-Narjis")
    # P-1001, P-1007, P-1017, P-1029 (حي النرجس), P-1030 (al narjis), P-1035 (950,000)
    assert result.total_matches == 6


def test_search_in_arabic_finds_english_rows(store):
    result = store.search(district="النرجس", deal="sale", type="apartment")
    assert set(ids(result)) == {"P-1007", "P-1035"}


def test_search_english_finds_arabic_row(store):
    result = store.search(district="Narjis", type="villa")
    assert set(ids(result)) == {"P-1001", "P-1029"}


def test_search_al_narjis_rent_finds_lowercase_row(store):
    result = store.search(district="Al-Narjis", deal="rent", type="apartment")
    assert ids(result) == ["P-1030"]


def test_search_al_yasmin_finds_row_written_without_al(store):
    result = store.search(district="Al-Yasmin", deal="sale", type="apartment")
    assert set(ids(result)) == {"P-1008", "P-1031"}


def test_search_finds_messy_status_row(store):
    result = store.search(district="Hittin", deal="rent", type="villa")
    assert ids(result) == ["P-1034"]


def test_search_price_filter_uses_parsed_comma_price(store):
    result = store.search(district="Al-Narjis", deal="sale", type="apartment", min_price=900000)
    assert ids(result) == ["P-1035"]


def test_search_never_returns_skipped_rows(store):
    everything = store.search(district="Al-Malqa", deal="sale", type="apartment")
    assert "P-1033" not in ids(everything)
    everything = store.search(district="Al-Aqiq", deal="sale", type="villa")
    assert ids(everything) == []  # the only one there had no property_id


# --- search_properties: ordering ---

def test_closest_to_budget_comes_first(store):
    result = store.search(deal="sale", type="villa", max_price=3500000)
    # Budget up to 3.5M: 3.45M, 3.1M, 2.95M, closest to 3.5M first.
    assert ids(result) == ["P-1001", "P-1029", "P-1003"]


def test_closest_to_middle_of_range_comes_first(store):
    result = store.search(deal="rent", type="apartment", min_price=40000, max_price=60000)
    # Middle = 50,000 -> 48k, 55k, 42k, 38k is out of range.
    assert ids(result) == ["P-1013", "P-1011", "P-1014"]


def test_closest_bedrooms_first_when_no_budget(store):
    result = store.search(deal="sale", district="Al-Yasmin", min_bedrooms=3)
    # Sheet order is P-1003 (5 bd), P-1008 (3), P-1016 (4), P-1031 (3).
    # Asked for 3+: exact 3s first (sheet order between them), then 4, then 5.
    assert ids(result) == ["P-1008", "P-1031", "P-1016", "P-1003"]


def test_no_budget_keeps_sheet_order(store):
    result = store.search(deal="sale", type="land")
    assert ids(result) == ["P-1019", "P-1020", "P-1021", "P-1022"]
