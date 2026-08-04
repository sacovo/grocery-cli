"""Tabular exports of lists and product details, for feeding other systems.

CSV here is deliberately flat and one-row-per-thing: the point is importing into a
spreadsheet or an inventory tool, not round-tripping. Use ``--format json`` when full
fidelity matters (nested nutrition, the untouched ``info`` block).
"""

from __future__ import annotations

import csv
from typing import IO

from .models import ListItem, ProductDetail

LIST_COLUMNS = [
    "product_id",
    "name",
    "quantity",
    "price",
    "line_total",
    "entry_id",
]

# Scalar product columns. Nutrition and leftover info sections are appended as
# dynamically-named columns after these, since which ones exist varies per product.
PRODUCT_COLUMNS = [
    "id",
    "name",
    "brand",
    "price",
    "gtin",
    "ingredients",
    "allergens",
    "origin",
    "production_country",
    "storage",
    "nutrition_base",
    "description",
    "url",
]

NUTRITION_PREFIX = "nutrition:"
INFO_PREFIX = "info:"


def list_rows(items: list[ListItem]) -> tuple[list[str], list[dict]]:
    """Flatten a shopping list, with a line total per row."""
    rows = []
    for item in items:
        total = round(item.price * item.quantity, 2) if item.price is not None else None
        rows.append(
            {
                "product_id": item.product_id,
                "name": item.name,
                "quantity": item.quantity,
                "price": item.price,
                "line_total": total,
                "entry_id": item.entry_id,
            }
        )
    return LIST_COLUMNS, rows


def _nutrition_map(detail: ProductDetail) -> dict[str, str]:
    """Nutrition as label -> value, disambiguating repeated labels.

    Coop prints two rows both labelled "Energie" (kJ then kcal) with no unit on either.
    Rather than guessing which is which, the second becomes "Energie (2)" so no data is
    silently dropped when flattening to columns.
    """
    seen: dict[str, int] = {}
    out: dict[str, str] = {}
    for label, value in detail.nutrition:
        seen[label] = seen.get(label, 0) + 1
        key = label if seen[label] == 1 else f"{label} ({seen[label]})"
        out[f"{NUTRITION_PREFIX}{key}"] = value
    return out


def product_rows(details: list[ProductDetail]) -> tuple[list[str], list[dict]]:
    """One row per product, with a union of the nutrition and info columns seen."""
    rows: list[dict] = []
    extra: list[str] = []

    for detail in details:
        row: dict[str, object] = {
            "id": detail.id,
            "name": detail.name,
            "brand": detail.brand,
            "price": detail.price,
            "gtin": detail.gtin,
            "ingredients": detail.ingredients,
            "allergens": ", ".join(detail.allergens),
            "origin": detail.origin,
            "production_country": detail.production_country,
            "storage": detail.storage,
            "nutrition_base": detail.nutrition_base,
            "description": detail.description,
            "url": f"https://www.coop.ch{detail.url}" if detail.url else None,
        }
        row.update(_nutrition_map(detail))
        row.update({f"{INFO_PREFIX}{k}": v for k, v in detail.info.items()})
        # Preserve first-seen order rather than sorting, so related columns stay together.
        for key in row:
            if key not in PRODUCT_COLUMNS and key not in extra:
                extra.append(key)
        rows.append(row)

    return PRODUCT_COLUMNS + extra, rows


def write_csv(columns: list[str], rows: list[dict], stream: IO[str]) -> None:
    """Write rows as CSV; missing keys become empty cells rather than errors."""
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
