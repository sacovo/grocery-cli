"""coop.ch backend: scraped HTML behind DataDome, driven through a real browser."""

from .client import CoopClient
from .filters import SearchFilters
from .session import BASE, CoopSession

__all__ = ["BASE", "CoopClient", "CoopSession", "SearchFilters"]
