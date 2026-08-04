"""Automate Swiss grocery shopping from recipes."""

from .browser import BrowserSession
from .errors import BotWall, GroceryError, NotLoggedIn
from .models import ListItem, Product, ProductDetail, ShoppingList
from .stores.base import Capabilities, Store
from .stores.coop import CoopClient, CoopSession, SearchFilters

__all__ = [
    "BotWall",
    "BrowserSession",
    "Capabilities",
    "CoopClient",
    "CoopSession",
    "GroceryError",
    "ListItem",
    "NotLoggedIn",
    "Product",
    "ProductDetail",
    "SearchFilters",
    "ShoppingList",
    "Store",
]
