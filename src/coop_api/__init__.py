"""A small wrapper around coop.ch for automating grocery shopping."""

from .client import CoopClient
from .filters import ALL_PROPERTIES, DIETARY, SearchFilters
from .models import ListItem, Product, ProductDetail, ShoppingList
from .session import BotWall, CoopError, CoopSession, NotLoggedIn

__all__ = [
    "ALL_PROPERTIES",
    "DIETARY",
    "BotWall",
    "CoopClient",
    "CoopError",
    "CoopSession",
    "ListItem",
    "NotLoggedIn",
    "Product",
    "ProductDetail",
    "SearchFilters",
    "ShoppingList",
]
