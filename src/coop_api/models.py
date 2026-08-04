"""Data types returned by the Coop client."""

from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(slots=True)
class Product:
    """A single product as shown on a coop.ch listing page."""

    id: str
    name: str
    brand: str | None = None
    price: float | None = None
    """Price in CHF."""
    quantity: str | None = None
    """Pack size as printed on the tile, e.g. ``"6l"``, ``"250ml"``."""
    unit_price: str | None = None
    """Reference price, e.g. ``"1.95/1l"``."""
    url: str | None = None
    in_stock: bool = True
    orderable: bool = False
    """Whether the tile offers an add-to-cart button (i.e. deliverable online)."""
    rating_count: int | None = None

    @property
    def image_url(self) -> str:
        return f"https://www.coop.ch/img/produkte/310_310/RGB/{self.id}_001.jpg"

    @property
    def full_url(self) -> str | None:
        if self.url is None:
            return None
        return f"https://www.coop.ch{self.url}"

    def as_dict(self) -> dict:
        """Plain JSON-serialisable representation."""
        return {
            "id": self.id,
            "name": self.name,
            "brand": self.brand,
            "price": self.price,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "orderable": self.orderable,
            "in_stock": self.in_stock,
            "rating_count": self.rating_count,
            "url": self.full_url,
        }

    def __str__(self) -> str:
        price = f"CHF {self.price:.2f}" if self.price is not None else "n/a"
        qty = f" ({self.quantity})" if self.quantity else ""
        return f"[{self.id}] {self.name}{qty} - {price}"


@dataclasses.dataclass(slots=True)
class ListItem:
    """An entry on a Coop shopping list."""

    product_id: str
    entry_id: str
    """Coop's GUID for this list entry - needed to edit or remove it."""
    name: str
    quantity: int = 1
    price: float | None = None

    def __str__(self) -> str:
        price = f"CHF {self.price:.2f}" if self.price is not None else "n/a"
        return f"{self.quantity}x [{self.product_id}] {self.name} - {price}"

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "entry_id": self.entry_id,
            "name": self.name,
            "quantity": self.quantity,
            "price": self.price,
        }


@dataclasses.dataclass(slots=True)
class ShoppingList:
    """One of the user's named Coop shopping lists."""

    id: str
    name: str
    active: bool = False

    def __str__(self) -> str:
        return f"{'* ' if self.active else '  '}{self.name}  [{self.id}]"

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "active": self.active}


@dataclasses.dataclass(slots=True)
class ProductDetail:
    """Everything the product page says about an article."""

    id: str
    name: str
    brand: str | None = None
    price: float | None = None
    description: str | None = None
    gtin: str | None = None
    """Barcode (EAN), when Coop publishes one."""
    ingredients: str | None = None
    allergens: list[str] = dataclasses.field(default_factory=list)
    """Ingredients Coop highlights as allergens - it marks them in bold, we read that."""
    origin: str | None = None
    production_country: str | None = None
    storage: str | None = None
    nutrition: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    """Ordered (label, value) pairs exactly as printed, e.g. ``("Fett", "3.5g")``."""
    nutrition_base: str | None = None
    """What the nutrition values refer to, e.g. ``"100ml"``."""
    info: dict[str, str] = dataclasses.field(default_factory=dict)
    """Any other labelled section on the page, verbatim."""
    url: str | None = None

    def __str__(self) -> str:
        price = f"CHF {self.price:.2f}" if self.price is not None else "n/a"
        return f"[{self.id}] {self.name} - {price}"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "brand": self.brand,
            "price": self.price,
            "gtin": self.gtin,
            "description": self.description,
            "ingredients": self.ingredients,
            "allergens": list(self.allergens),
            "origin": self.origin,
            "production_country": self.production_country,
            "storage": self.storage,
            "nutrition": [{"label": k, "value": v} for k, v in self.nutrition],
            "nutrition_base": self.nutrition_base,
            "info": dict(self.info),
            "url": f"https://www.coop.ch{self.url}" if self.url else None,
        }


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def parse_price(raw: str | None) -> float | None:
    """Parse a Coop price string (``"11.70"``, ``"CHF 11.70"``) into a float."""
    if not raw:
        return None
    m = _NUM.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None
