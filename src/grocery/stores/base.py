"""What a store backend has to provide, and what it may honestly claim to support.

Deliberately narrow. The two backends have almost nothing in common below the surface -
Coop is scraped HTML behind DataDome, Migros is a JSON API behind an OAuth token - so
this describes only what genuinely generalises. Anything a store cannot do is declared
in `Capabilities` and refused with a clear message, never emulated.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable

from ..models import ListItem, Product, ProductDetail


@dataclasses.dataclass(frozen=True, slots=True)
class Capabilities:
    """What a given store actually supports.

    Lets the CLI say "this store has no X" instead of pretending. `dietary_filters` in
    particular is a hard promise: a store may only claim it if the *store* filters,
    because guessing a product is vegan from its name is how you poison a guest.
    """

    lists: bool = False
    """Named, persistent shopping lists that can be listed and switched between."""
    multiple_lists: bool = False
    dietary_filters: bool = False
    categories: bool = False
    product_details: bool = False
    """Ingredients, allergens, nutrition."""


@runtime_checkable
class Store(Protocol):
    """The store surface the shared code (recipes, export, CLI) relies on."""

    name: str
    capabilities: Capabilities

    def search(self, query: str, limit: int = 30, **kwargs) -> list[Product]:
        """Products matching free text, most relevant first. Empty list if none."""
        ...

    def get_product(self, product: Product | str) -> ProductDetail:
        """Full detail for one product."""
        ...

    def get_list(self) -> list[ListItem]:
        """The active list's contents."""
        ...

    def add_to_list(self, product: Product | str, quantity: int = 1) -> bool:
        """Put a product on the list. True if added, False if already there."""
        ...

    def remove_from_list(self, product: Product | str) -> bool:
        """Take a product off the list. True if removed, False if it was not on it."""
        ...
