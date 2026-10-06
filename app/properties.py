"""The in-memory copy of your listings, and the two search tools the agent will use.

Listings come from your Excel file (sent into the Telegram group, saved as
data/listings.xlsx) or, if there's no such file, from the Google Sheet.

- refresh() reloads the listings. If loading fails, the last good copy is kept.
- search() backs the agent's `search_properties` tool: available properties only, max 5.
- get() backs the agent's `get_property_details` tool: any status, so the agent can
  honestly say a property is reserved or sold.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.db import utc_now
from app.excel_listings import ExcelConfig, read_listings
from app.locations import Locations, place_key
from app.normalize import id_key
from app.sheet import (
    GoogleSheetLoader,
    ParseResult,
    Property,
    SheetConfig,
    SkippedRow,
    parse_rows,
)

logger = logging.getLogger(__name__)

MAX_RESULTS = 5

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SHEET_CONFIG_FILE = CONFIG_DIR / "sheet.json"
LOCATIONS_FILE = CONFIG_DIR / "locations.json"
EXCEL_CONFIG_FILE = CONFIG_DIR / "excel.json"


def listings_file_for(db_path: str) -> Path:
    """Where your uploaded Excel listings are kept: next to the database (data/listings.xlsx)."""
    return Path(db_path).with_name("listings.xlsx")


def build_property_store(settings: Settings) -> "PropertyStore":
    """Create the store from settings + the files in config/."""
    loader = None
    if settings.google_sheet_id:
        key_json = settings.google_service_account_json
        loader = GoogleSheetLoader(
            settings.google_service_account_file,
            settings.google_sheet_id,
            settings.sheet_tab_name,
            service_account_json=key_json.get_secret_value() if key_json else None,
        )
    store = PropertyStore(loader, SheetConfig.load(SHEET_CONFIG_FILE), Locations.load(LOCATIONS_FILE))
    excel_path = listings_file_for(settings.db_path)
    if excel_path.exists():
        store.use_excel(excel_path)  # your uploaded Excel file wins over the Google Sheet
    elif loader is None:
        logger.warning("No listings yet: send your Excel file in the Telegram group (or set GOOGLE_SHEET_ID)")
    return store


@dataclass(frozen=True)
class SearchResult:
    properties: list[Property]  # best matches first, at most MAX_RESULTS
    total_matches: int  # how many matched in total (may be more than shown)


class PropertyStore:
    def __init__(
        self,
        loader: Callable[[], list[list[Any]]] | None,
        config: SheetConfig,
        locations: Locations,
    ):
        """`loader` returns the raw sheet rows; None means no sheet is configured."""
        self._loader = loader
        self.config = config
        self.locations = locations
        # (list, index) replaced together in one assignment, so readers never see a half-update.
        self._data: tuple[list[Property], dict[str, Property]] = ([], {})
        self.last_refresh_at: str | None = None
        self.last_error: str | None = None
        self.skipped_rows: list[SkippedRow] = []
        self._last_warnings: list[str] = []
        self.excel_path: Path | None = None  # set when listings come from your Excel file
        self._excel_config: ExcelConfig | None = None

    @property
    def configured(self) -> bool:
        return self._loader is not None or self.excel_path is not None

    @property
    def source(self) -> str | None:
        """"excel", "google", or None (no listings configured)."""
        if self.excel_path is not None:
            return "excel"
        return "google" if self._loader is not None else None

    @property
    def excel_config(self) -> ExcelConfig:
        if self._excel_config is None:
            self._excel_config = ExcelConfig.load(EXCEL_CONFIG_FILE)
        return self._excel_config

    def use_excel(self, path: Path) -> None:
        """From now on, read the listings from this Excel file."""
        self.excel_path = path

    def _parse(self) -> ParseResult:
        if self.excel_path is not None:
            return read_listings(self.excel_path.read_bytes(), self.excel_config, self.locations)
        return parse_rows(self._loader(), self.config, self.locations)

    @property
    def properties(self) -> list[Property]:
        return self._data[0]

    # --- loading ---

    def refresh(self) -> bool:
        """Reload the listings. Returns True on success; on failure keeps the old data."""
        if not self.configured:
            return False
        try:
            result = self._parse()
            if not result.properties:
                raise ValueError("no valid property rows found")
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Listings refresh failed; keeping the last good copy (%s properties). %s",
                len(self.properties), self.last_error,
            )
            return False
        self._install(result)
        return True

    def replace_with_excel(self, content: bytes, path: Path) -> ParseResult:
        """Your new Excel file (from Telegram): check it, save it, and use it right away.

        Raises ExcelFormatError or ValueError (the old listings stay) if the file can't be used.
        """
        result = read_listings(content, self.excel_config, self.locations)
        if not result.properties:
            raise ValueError("no listings found in the file")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)  # a half-written file is never used
        self.use_excel(path)
        self._install(result)
        return result

    def _install(self, result: ParseResult) -> None:
        self._log_problems(result)
        self._data = (result.properties, {id_key(p.property_id): p for p in result.properties})
        self.skipped_rows = result.skipped
        self.last_refresh_at = utc_now()
        self.last_error = None
        available = sum(p.is_available for p in result.properties)
        logger.info(
            "%s loaded: %s properties (%s available), %s rows skipped",
            "Excel listings" if self.source == "excel" else "Sheet",
            len(result.properties), available, len(result.skipped),
        )

    def _log_problems(self, result: ParseResult) -> None:
        # Only repeat the details when they change, to keep the log readable
        # (the sheet is reloaded every few minutes).
        if self.last_refresh_at and (result.skipped, result.warnings) == (
            self.skipped_rows, self._last_warnings
        ):
            return
        for row in result.skipped:
            logger.warning("Sheet row %s skipped: %s", row.row_number, row.reason)
        for warning in result.warnings:
            logger.warning("Sheet: %s", warning)
        self._last_warnings = result.warnings

    # --- agent tools ---

    def get(self, property_id: str) -> Property | None:
        """Full details of one property (any status). "p1023" finds "P-1023"."""
        return self._data[1].get(id_key(property_id))

    def search(
        self,
        deal: str | None = None,
        type: str | None = None,
        city: str | None = None,
        district: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        min_bedrooms: int | None = None,
        min_area: float | None = None,
    ) -> SearchResult:
        """Find AVAILABLE properties matching all given filters (empty filters are ignored).

        A listing with an unknown price or area is never excluded by a budget or area filter
        (the team tells customers the price). Prices are only compared here, never shown.

        Order ("closest matches first"):
          1. price closest to the customer's budget (the middle of min/max, or
             whichever one was given); if no budget, the sheet's own order
          2. then bedrooms closest to what was asked for
        """
        deal = self.config.standard_value("deal", deal)
        type = self.config.standard_value("type", type)
        city_key = self.locations.cities.match(city)[0] if city else None
        district_key = self.locations.districts.match(district)[0] if district else None
        if min_price is not None and max_price is not None and min_price > max_price:
            min_price, max_price = max_price, min_price  # swapped by mistake

        wanted_place = place_key(district) if district else ""

        def in_district(p: Property) -> bool:
            # Exact match via config/locations.json, or the district's name appears inside a
            # descriptive cell like "الصحنة - الشارع العام".
            return p.district_key == district_key or bool(wanted_place and wanted_place in place_key(p.district))

        matches = [
            p for p in self.properties
            if p.is_available
            and (deal is None or p.deal == deal)
            and (type is None or p.type == type)
            and (city_key is None or p.city == city_key)
            and (district_key is None or in_district(p))
            and (min_price is None or p.price_sar is None or p.price_sar >= min_price)
            and (max_price is None or p.price_sar is None or p.price_sar <= max_price)
            and (min_bedrooms is None or (p.bedrooms is not None and p.bedrooms >= min_bedrooms))
            and (min_area is None or p.area_sqm is None or p.area_sqm >= min_area)
        ]

        if min_price is not None and max_price is not None:
            target = (min_price + max_price) / 2
        else:
            target = max_price if max_price is not None else min_price

        def closeness(p: Property) -> tuple:
            if not target:
                price_gap = 0
            elif p.price_sar is None:
                price_gap = 1  # unknown price: after the close matches
            else:
                price_gap = abs(p.price_sar - target) / target
            bedroom_gap = (p.bedrooms - min_bedrooms) if min_bedrooms is not None else 0
            return (price_gap, bedroom_gap)

        matches.sort(key=closeness)  # stable sort: ties keep the sheet's order
        return SearchResult(properties=matches[:MAX_RESULTS], total_matches=len(matches))
