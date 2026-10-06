"""Read the property sheet and turn its rows into validated Property objects.

- Column headers are mapped using config/sheet.json, so your real sheet can use
  Arabic or different header names.
- Bad rows (no property_id, invalid price, duplicate ID) are skipped and reported
  with their row number, so you can fix them in the sheet.
- The Google connection uses a service account with READ-ONLY access.
"""

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.locations import Locations
from app.normalize import clean_str, id_key, normalize_text, parse_number

logger = logging.getLogger(__name__)

# Without these columns the sheet can't be used at all.
REQUIRED_FIELDS = ("property_id", "type", "deal", "district", "price_sar", "status")

# Read-only access to Google Sheets. The bot can never change your sheet.
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class SheetFormatError(Exception):
    """The sheet can't be read at all (e.g. a required column is missing)."""


@dataclass(frozen=True)
class Property:
    property_id: str
    type: str | None  # standard category: apartment / villa / land / farm / rest_house / ... (None = unknown)
    deal: str | None  # sale / rent
    city: str | None  # canonical name, e.g. "Al-Kharj"
    district: str | None  # as written in the sheet, e.g. "حي النرجس"
    district_key: str  # canonical name used for search, e.g. "Al-Narjis"
    # Sale: total price; rent: ANNUAL rent. None = unknown. PRIVATE: used only to filter by
    # budget, never sent to the AI (customers are never told prices).
    price_sar: int | float | None
    area_sqm: int | float | None
    bedrooms: int | None
    bathrooms: int | None
    features: str | None
    status: str | None  # available / reserved / sold (anything else = not available)
    ad_license_no: str | None
    photos_link: str | None
    notes: str | None  # PUBLIC notes, may be shown to customers
    internal_notes: str | None  # PRIVATE: never sent to the AI (see app/agent_tools.py)
    row_number: int  # row in the sheet, for your reference when fixing data
    # Filled from the Excel workbook (app/excel_listings.py); empty for the Google Sheet.
    type_text: str | None = None  # the type as written, e.g. "أرض سكنية" or "استراحة غرفتين"
    category: str | None = None  # residential / commercial (which tab it came from)
    facade: str | None = None  # الواجهة, e.g. "شمالية شارع 20"
    area_text: str | None = None  # the area as written, e.g. "660 للقطعة الواحدة"
    age: str | None = None  # عمر العقار, e.g. "10 سنوات"
    terms: str | None = None  # rent only: rental terms (حالة التأجير), e.g. "سنوي"
    source_tab: str | None = None  # the Excel tab, for your reference when fixing data

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    def to_dict(self) -> dict:
        """ALL fields, including internal_notes. For admin use only, never for the AI."""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class SkippedRow:
    row_number: int
    reason: str


@dataclass
class ParseResult:
    properties: list[Property] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SheetConfig:
    """Column mapping and value synonyms, loaded from config/sheet.json."""

    def __init__(self, columns: dict[str, str], values: dict[str, dict[str, list[str]]]):
        missing = [f for f in REQUIRED_FIELDS if f not in columns]
        if missing:
            raise ValueError(f"config/sheet.json is missing column mappings for: {missing}")
        self.columns = columns
        # {"status": {"متاح": "available", "available": "available", ...}, ...}
        self.values: dict[str, dict[str, str]] = {}
        for field_name, options in values.items():
            lookup = {}
            for standard, synonyms in options.items():
                for word in [standard, *synonyms]:
                    lookup[normalize_text(word)] = standard
            self.values[field_name] = lookup

    @classmethod
    def load(cls, path: Path) -> "SheetConfig":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(data["columns"], data.get("values", {}))

    def standard_value(self, field_name: str, raw) -> str | None:
        """Map a cell value to its standard word: "متاح" / "Available " -> "available"."""
        key = normalize_text(raw)
        if not key:
            return None
        return self.values.get(field_name, {}).get(key, key)


def parse_rows(rows: list[list[Any]], config: SheetConfig, locations: Locations) -> ParseResult:
    """Turn raw sheet rows (first row = headers) into validated properties.

    Raises SheetFormatError if the sheet is empty or a required column is missing.
    """
    if not rows:
        raise SheetFormatError("The sheet is empty (no header row)")

    headers = [normalize_text(h) for h in rows[0]]
    col_index: dict[str, int] = {}
    missing = []
    for field_name, header_text in config.columns.items():
        key = normalize_text(header_text)
        if key in headers:
            col_index[field_name] = headers.index(key)
        elif field_name in REQUIRED_FIELDS:
            missing.append(header_text)
    if missing:
        raise SheetFormatError(f"Required column(s) not found in the header row: {missing}")

    result = ParseResult()
    optional_missing = [f for f in config.columns if f not in col_index]
    if optional_missing:
        result.warnings.append(f"Optional column(s) not found, left empty: {optional_missing}")

    seen_ids: set[str] = set()
    unknown_districts: set[str] = set()

    for row_number, row in enumerate(rows[1:], start=2):
        if all(clean_str(cell) is None for cell in row):
            continue  # completely empty row

        def cell(field_name: str):
            i = col_index.get(field_name)
            return row[i] if i is not None and i < len(row) else None

        def skip(reason: str) -> None:
            result.skipped.append(SkippedRow(row_number, reason))

        property_id = clean_str(cell("property_id"))
        if not property_id:
            skip("missing property_id")
            continue
        if id_key(property_id) in seen_ids:
            skip(f"duplicate property_id {property_id!r} (kept the first one)")
            continue

        price = parse_number(cell("price_sar"))
        if not price:  # None (not a number) or 0
            skip(f"invalid price_sar {cell('price_sar')!r} for {property_id}")
            continue

        district_raw = clean_str(cell("district"))
        district_key, known = locations.districts.match(district_raw)
        if district_raw and not known:
            unknown_districts.add(district_raw)

        city_raw = clean_str(cell("city"))
        city = locations.cities.match(city_raw)[0] if city_raw else None

        status = config.standard_value("status", cell("status"))
        if status not in ("available", "reserved", "sold"):
            result.warnings.append(
                f"Row {row_number} ({property_id}): unknown status {cell('status')!r}, "
                "treated as NOT available"
            )

        seen_ids.add(id_key(property_id))
        result.properties.append(
            Property(
                property_id=property_id,
                type=config.standard_value("type", cell("type")),
                deal=config.standard_value("deal", cell("deal")),
                city=city,
                district=district_raw,
                district_key=district_key,
                price_sar=price,
                area_sqm=_optional_number(cell("area_sqm"), "area_sqm", row_number, result),
                bedrooms=_optional_int(cell("bedrooms"), "bedrooms", row_number, result),
                bathrooms=_optional_int(cell("bathrooms"), "bathrooms", row_number, result),
                features=clean_str(cell("features")),
                status=status,
                ad_license_no=clean_str(cell("ad_license_no")),
                photos_link=clean_str(cell("photos_link")),
                notes=clean_str(cell("notes")),
                internal_notes=clean_str(cell("internal_notes")),
                row_number=row_number,
            )
        )

    if unknown_districts:
        result.warnings.append(
            f"District(s) not in config/locations.json (add them for better matching): "
            f"{sorted(unknown_districts)}"
        )
    return result


def _optional_number(value, name: str, row_number: int, result: ParseResult):
    """Numbers that may be empty (area, bedrooms...). Invalid values become empty + a warning."""
    if clean_str(value) is None:
        return None
    number = parse_number(value)
    if number is None:
        result.warnings.append(f"Row {row_number}: {name} {value!r} is not a number, ignored")
    return number


def _optional_int(value, name: str, row_number: int, result: ParseResult) -> int | None:
    number = _optional_number(value, name, row_number, result)
    if number is not None and not isinstance(number, int):
        result.warnings.append(f"Row {row_number}: {name} {value!r} is not a whole number, ignored")
        return None
    return number


class GoogleSheetLoader:
    """Downloads all cells of one tab of the Google Sheet.

    The service account key comes either from a file, or (on hosts without files,
    like Railway) from its JSON content in GOOGLE_SERVICE_ACCOUNT_JSON.
    """

    def __init__(
        self,
        service_account_file: str,
        sheet_id: str,
        tab_name: str,
        service_account_json: str | None = None,
    ):
        self.service_account_file = service_account_file
        self.service_account_json = service_account_json
        self.sheet_id = sheet_id
        self.tab_name = tab_name

    def _client(self):
        import gspread  # imported here so tests never need Google libraries configured

        if self.service_account_json:
            raw = self.service_account_json.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]  # some hosts keep the quotes from a pasted .env line
            try:
                info = json.loads(raw)
            except ValueError:
                # Never include the value itself in the error: it's a private key.
                raise SheetFormatError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from None
            return gspread.service_account_from_dict(info, scopes=[SHEETS_READONLY_SCOPE])
        return gspread.service_account(filename=self.service_account_file, scopes=[SHEETS_READONLY_SCOPE])

    def __call__(self) -> list[list[Any]]:
        from gspread.utils import ValueRenderOption

        client = self._client()
        client.set_timeout(30)  # gspread waits forever by default
        worksheet = client.open_by_key(self.sheet_id).worksheet(self.tab_name)
        # Unformatted = real numbers (3450000), not display text ("3,450,000").
        return worksheet.get_all_values(value_render_option=ValueRenderOption.unformatted)
