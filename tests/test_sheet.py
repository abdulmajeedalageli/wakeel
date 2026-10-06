"""Sheet parsing, using the sample sheet (tests/fixtures/sample_properties.json,
generated from test_data/sample_properties.xlsx). Each yellow TEST row has its own test."""

import copy

import pytest

from app.sheet import SheetConfig, SheetFormatError, parse_rows


@pytest.fixture
def parsed(sample_rows, sheet_config, locations):
    return parse_rows(sample_rows, sheet_config, locations)


def by_id(result, property_id):
    return next(p for p in result.properties if p.property_id == property_id)


# --- overall ---

def test_sample_sheet_counts(parsed):
    # 35 data rows: 2 skipped (missing ID, invalid price) -> 33 properties.
    assert len(parsed.properties) == 33
    assert len(parsed.skipped) == 2
    # Not available: P-1004 and P-1018 (reserved), P-1010 (sold).
    assert sum(p.is_available for p in parsed.properties) == 30


def test_normal_row_is_parsed_fully(parsed):
    p = by_id(parsed, "P-1001")
    assert (p.type, p.deal, p.city, p.district, p.district_key) == (
        "villa", "sale", "Riyadh", "Al-Narjis", "Al-Narjis"
    )
    assert (p.price_sar, p.area_sqm, p.bedrooms, p.bathrooms) == (3450000, 375, 5, 6)
    assert p.features == "corner, driver room, maid room, elevator"
    assert p.status == "available"
    assert p.ad_license_no == "TEST-7200000001"
    assert p.photos_link == "https://example.com/photos/P-1001"
    assert p.notes == "New build, facing north"
    assert p.internal_notes is None
    assert p.row_number == 2


def test_internal_notes_are_parsed_separately_from_public_notes(parsed):
    p = by_id(parsed, "P-1004")
    assert p.notes is None
    assert p.internal_notes == "Deposit paid"
    assert by_id(parsed, "P-1002").internal_notes == "Owner will accept 3.9M minimum"
    assert by_id(parsed, "P-1012").notes == "Annual rent"  # public note kept
    # The yellow rows' test descriptions are private, not customer-facing.
    assert by_id(parsed, "P-1035").notes is None
    assert by_id(parsed, "P-1035").internal_notes.startswith("TEST:")


def test_rent_price_is_kept_as_annual_rent(parsed):
    assert by_id(parsed, "P-1005").price_sar == 180000


def test_land_and_offices_have_no_bedrooms(parsed):
    assert by_id(parsed, "P-1019").bedrooms is None  # land
    assert by_id(parsed, "P-1023").bedrooms is None  # office
    assert by_id(parsed, "P-1023").bathrooms == 2


def test_empty_features_is_none(parsed):
    assert by_id(parsed, "P-1028").features is None


def test_known_districts_produce_no_warning(parsed):
    assert not any("District(s) not in" in w for w in parsed.warnings)


# --- yellow TEST rows ---

def test_P1029_arabic_district_maps_to_al_narjis(parsed):
    p = by_id(parsed, "P-1029")
    assert p.district == "حي النرجس"  # original text kept for display
    assert p.district_key == "Al-Narjis"
    assert p.features == "مدخلين، غرفة سائق، ملحق"


def test_P1030_lowercase_no_hyphen_district_maps_to_al_narjis(parsed):
    assert by_id(parsed, "P-1030").district_key == "Al-Narjis"


def test_P1031_district_without_al_maps_to_al_yasmin(parsed):
    assert by_id(parsed, "P-1031").district_key == "Al-Yasmin"


def test_row_with_missing_property_id_is_skipped_and_reported(parsed):
    assert parsed.skipped[0].row_number == 33
    assert parsed.skipped[0].reason == "missing property_id"
    assert not any(p.photos_link == "https://example.com/photos/missing" for p in parsed.properties)


def test_P1033_invalid_price_is_skipped_and_reported(parsed):
    assert parsed.skipped[1].row_number == 34
    assert "invalid price_sar 'call us'" in parsed.skipped[1].reason
    assert "P-1033" in parsed.skipped[1].reason
    assert not any(p.property_id == "P-1033" for p in parsed.properties)


def test_P1034_messy_status_is_treated_as_available(parsed):
    p = by_id(parsed, "P-1034")
    assert p.status == "available"
    assert p.is_available


def test_P1035_price_with_comma_is_parsed(parsed):
    assert by_id(parsed, "P-1035").price_sar == 950000


# --- other validation ---

def test_formatted_values_also_work(sample_rows, sheet_config, locations):
    """If numbers arrive as display text ("3,450,000"), they still parse."""
    rows = copy.deepcopy(sample_rows)
    rows[1][5] = "3,450,000"
    rows[1][6] = "375"
    result = parse_rows(rows, sheet_config, locations)
    p = by_id(result, "P-1001")
    assert (p.price_sar, p.area_sqm) == (3450000, 375)


def test_duplicate_property_id_keeps_first(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    duplicate = list(rows[2])  # P-1002
    duplicate[0] = "p-1002 "  # same ID, different spelling
    rows.append(duplicate)
    result = parse_rows(rows, sheet_config, locations)
    assert sum(p.property_id.upper().strip() == "P-1002" for p in result.properties) == 1
    assert "duplicate property_id" in result.skipped[-1].reason


def test_zero_price_is_skipped(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows[1][5] = 0
    result = parse_rows(rows, sheet_config, locations)
    assert not any(p.property_id == "P-1001" for p in result.properties)


def test_invalid_bedrooms_is_ignored_with_warning(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows[1][7] = "five"
    result = parse_rows(rows, sheet_config, locations)
    assert by_id(result, "P-1001").bedrooms is None  # row kept, value dropped
    assert any("bedrooms 'five'" in w for w in result.warnings)


def test_unknown_status_is_not_available_and_warned(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows[1][10] = "coming soon"
    result = parse_rows(rows, sheet_config, locations)
    assert not by_id(result, "P-1001").is_available
    assert any("unknown status 'coming soon'" in w for w in result.warnings)


def test_unknown_district_is_kept_and_warned(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows[1][4] = "Al-Shifa"
    result = parse_rows(rows, sheet_config, locations)
    assert by_id(result, "P-1001").district_key == "shifa"
    assert any("Al-Shifa" in w for w in result.warnings)


def test_blank_rows_are_ignored_silently(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows.insert(5, [""] * 14)
    result = parse_rows(rows, sheet_config, locations)
    assert len(result.properties) == 33
    assert len(result.skipped) == 2


# --- column mapping ---

def test_missing_required_column_raises(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows[0][5] = "cost"  # price_sar header renamed
    with pytest.raises(SheetFormatError, match="price_sar"):
        parse_rows(rows, sheet_config, locations)


def test_empty_sheet_raises(sheet_config, locations):
    with pytest.raises(SheetFormatError):
        parse_rows([], sheet_config, locations)


def test_missing_optional_columns_are_a_warning(sample_rows, sheet_config, locations):
    rows = [row[:13] for row in sample_rows]  # drop "notes" and "internal_notes"
    result = parse_rows(rows, sheet_config, locations)
    assert len(result.properties) == 33
    assert by_id(result, "P-1001").notes is None
    assert by_id(result, "P-1004").internal_notes is None
    assert any("'notes'" in w and "'internal_notes'" in w for w in result.warnings)


def test_headers_match_regardless_of_case_and_spaces(sample_rows, sheet_config, locations):
    rows = copy.deepcopy(sample_rows)
    rows[0] = [f"  {h.upper()} " for h in rows[0]]
    assert len(parse_rows(rows, sheet_config, locations).properties) == 33


def test_arabic_headers_and_values_with_custom_mapping(locations):
    """Your real sheet may use Arabic headers and values: only config/sheet.json changes."""
    config = SheetConfig(
        columns={
            "property_id": "رقم العقار", "type": "النوع", "deal": "العملية",
            "city": "المدينة", "district": "الحي", "price_sar": "السعر",
            "area_sqm": "المساحة", "bedrooms": "غرف النوم", "status": "الحالة",
        },
        values={
            "type": {"apartment": ["شقة"]},
            "deal": {"rent": ["إيجار"]},
            "status": {"available": ["متاح"]},
        },
    )
    rows = [
        ["رقم العقار", "النوع", "العملية", "المدينة", "الحي", "السعر", "المساحة", "غرف النوم", "الحالة"],
        ["A-1", "شقة", "إيجار", "الرياض", "حي الملقا", "٦٠٬٠٠٠", 120, 2, "متاح"],
    ]
    p = parse_rows(rows, config, locations).properties[0]
    assert (p.property_id, p.type, p.deal, p.city, p.district_key, p.price_sar, p.status) == (
        "A-1", "apartment", "rent", "Riyadh", "Al-Malqa", 60000, "available"
    )


def test_config_without_required_mapping_is_rejected():
    with pytest.raises(ValueError, match="price_sar"):
        SheetConfig(columns={"property_id": "id", "type": "t", "deal": "d",
                             "district": "x", "status": "s"}, values={})
