"""Read your listings from an Excel workbook (.xlsx), e.g. the one you keep on OneDrive.

You send the file into your Telegram group; app/owner_console.py passes it here.

Privacy rules:
- Only the tabs listed in config/excel.json are read. Every other tab (customer requests,
  finances...) is never opened.
- Only the columns listed there are read. Owner names, phone numbers, the responsible
  employee and notes are never even loaded.
- The price is loaded only to filter by budget. It is never sent to the AI
  (app/agent_tools.py builds what the AI sees from an allowlist that has no price).
"""

import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.locations import Locations
from app.normalize import clean_str, normalize_text, parse_amount, remove_phone_numbers
from app.sheet import ParseResult, Property, SkippedRow

logger = logging.getLogger(__name__)


class ExcelFormatError(Exception):
    """The file can't be used (not an Excel file, or none of the listing tabs are in it)."""


@dataclass(frozen=True)
class ListingTab:
    name: str
    deal: str  # sale / rent
    category: str  # residential / commercial
    id_prefix: str  # makes IDs unique across tabs: "بيع سكني 12"


class ExcelConfig:
    """Which tabs and columns to read, loaded from config/excel.json."""

    def __init__(self, data: dict):
        self.city = data.get("city")
        self.tabs = [ListingTab(**tab) for tab in data["tabs"]]
        self.columns: dict[str, list[str]] = data["columns"]
        self.unavailable = [normalize_text(w) for w in data.get("unavailable", [])]
        # [("farm", ["مزرعه", ...]), ...] in the file's order: the first match wins.
        self.types = [(std, [normalize_text(w) for w in words]) for std, words in data.get("types", {}).items()]

    @classmethod
    def load(cls, path: Path) -> "ExcelConfig":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def standard_type(self, text: str | None) -> str | None:
        """ "أرض سكنية" -> "land", "استراحة غرفتين" -> "rest_house"; None if unknown."""
        key = normalize_text(text)
        if not key:
            return None
        for standard, words in self.types:
            if any(word and word in key for word in words):
                return standard
        return None


def _tab_key(name: str) -> str:
    """Tab names compared without spaces: "عروض البيع (تجاري+ زراعي)" == "عروض البيع (تجاري + زراعي)"."""
    return normalize_text(name).replace(" ", "")


def _text(value) -> str | None:
    """A cell as clean text, with any phone number removed (belt and braces)."""
    return remove_phone_numbers(clean_str(value))


def read_listings(content: bytes, config: ExcelConfig, locations: Locations) -> ParseResult:
    """Turn the workbook's listing tabs into properties. Raises ExcelFormatError if unusable."""
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ExcelFormatError(f"not a readable Excel file ({type(exc).__name__})") from None

    sheets = {_tab_key(ws.title): ws for ws in workbook.worksheets}
    result = ParseResult()
    found = 0
    for tab in config.tabs:
        worksheet = sheets.get(_tab_key(tab.name))
        if worksheet is None:
            result.warnings.append(f"Tab not found: {tab.name}")
            continue
        found += 1
        _read_tab(worksheet, tab, config, locations, result)
    workbook.close()
    if not found:
        raise ExcelFormatError("none of the listing tabs from config/excel.json are in the file")
    return result


def _read_tab(worksheet, tab: ListingTab, config: ExcelConfig, locations: Locations, result: ParseResult) -> None:
    rows = worksheet.iter_rows(values_only=True)
    header = next(rows, None) or ()
    headers = [normalize_text(h) for h in header]
    # Column index for each field we're allowed to read; everything else is never touched.
    index: dict[str, int] = {}
    for field, spellings in config.columns.items():
        for spelling in spellings:
            key = normalize_text(spelling)
            if key in headers:
                index[field] = headers.index(key)
                break

    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        def cell(field: str):
            i = index.get(field)
            return row[i] if i is not None and i < len(row) else None

        type_text, district = _text(cell("type")), _text(cell("district"))
        if not type_text and not district:
            if any(clean_str(c) for c in row):
                result.skipped.append(SkippedRow(row_number, f"{tab.name}: بدون نوع ولا حي"))
            continue  # empty or unusable row

        number = clean_str(cell("number")) or f"ص{row_number}"
        if isinstance(cell("number"), float) and cell("number").is_integer():
            number = str(int(cell("number")))
        property_id = f"{tab.id_prefix} {number}"
        if property_id in seen:
            property_id = f"{tab.id_prefix} {number}-{row_number}"  # a repeated number in the tab
        seen.add(property_id)

        price_text = clean_str(cell("price"))
        price = parse_amount(price_text, million_suffix=True)
        if price is not None and tab.deal == "rent" and price_text and "شهري" in normalize_text(price_text):
            price *= 12  # "400 شهري": compare budgets per year like every other rent

        status_text = normalize_text(cell("status"))
        gone = any(word and word in status_text for word in config.unavailable)
        area_text = _text(cell("area"))
        district_key, known = locations.districts.match(district) if district else ("", False)
        if district and not known:
            result.warnings.append(f"{tab.name} row {row_number}: district not in config/locations.json: {district}")

        result.properties.append(
            Property(
                property_id=property_id,
                type=config.standard_type(type_text),
                deal=tab.deal,
                city=config.city,
                district=district,
                district_key=district_key,
                price_sar=price,
                area_sqm=parse_amount(area_text),
                bedrooms=None,
                bathrooms=None,
                features=None,
                status=("sold" if tab.deal == "sale" else "rented") if gone else "available",
                ad_license_no=None,
                photos_link=None,
                notes=None,  # notes may contain names or numbers: never loaded
                internal_notes=None,
                row_number=row_number,
                type_text=type_text,
                category=tab.category,
                facade=_text(cell("facade")),
                area_text=area_text,
                age=_text(cell("age")),
                terms=_text(cell("terms")) if tab.deal == "rent" else None,
                source_tab=tab.name,
            )
        )
