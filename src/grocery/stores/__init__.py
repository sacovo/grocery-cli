"""Store backends. Each one adapts a retailer to the `Store` protocol in `base`."""

from .base import Capabilities, Store

__all__ = ["Capabilities", "Store"]
