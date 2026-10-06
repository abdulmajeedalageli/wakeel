"""Admin helper: load your Google Sheet and try searches, without WhatsApp or the AI.

Good for checking the Google connection and how the sheet is read.

Usage (from the project folder, with the virtual environment active):
    python -m scripts.try_search                                  # load + summary
    python -m scripts.try_search --district النرجس --deal sale
    python -m scripts.try_search --type apartment --deal rent --max-price 60000
    python -m scripts.try_search --id P-1023                      # one property
"""

import argparse
import logging
import sys

from app.config import get_settings
from app.properties import build_property_store


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # so Arabic prints correctly on Windows
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", help="show one property by ID")
    parser.add_argument("--deal")
    parser.add_argument("--type")
    parser.add_argument("--city")
    parser.add_argument("--district")
    parser.add_argument("--min-price", type=float)
    parser.add_argument("--max-price", type=float)
    parser.add_argument("--min-bedrooms", type=int)
    parser.add_argument("--min-area", type=float)
    args = parser.parse_args()

    store = build_property_store(get_settings())
    if not store.refresh():
        print(f"Could not load the sheet: {store.last_error or 'GOOGLE_SHEET_ID is not set'}")
        return 1
    available = sum(p.is_available for p in store.properties)
    print(f"Loaded {len(store.properties)} properties ({available} available), "
          f"{len(store.skipped_rows)} rows skipped.\n")

    if args.id:
        prop = store.get(args.id)
        if not prop:
            print(f"No property with ID {args.id!r}.")
            return 1
        for key, value in prop.to_dict().items():
            print(f"  {key:14} {value}")
        return 0

    filters = {k: v for k, v in vars(args).items() if k != "id" and v is not None}
    if not filters:
        return 0
    result = store.search(**filters)
    print(f"{result.total_matches} match(es) for {filters}; showing {len(result.properties)}:")
    for p in result.properties:
        bedrooms = "-" if p.bedrooms is None else p.bedrooms
        print(f"  {p.property_id}  {p.type} for {p.deal}  {p.district}  "
              f"{p.price_sar:,} SAR  {p.area_sqm} m²  {bedrooms} bd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
