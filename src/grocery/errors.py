"""Exceptions shared by every store backend."""

from __future__ import annotations


class GroceryError(RuntimeError):
    """Something went wrong talking to a store."""


class NotLoggedIn(GroceryError):
    """The store needs a signed-in session and there isn't one.

    Always actionable by a human and never by the tool: log in to the store in the
    Chrome window this package opens, and the session persists in its profile.
    """


class BotWall(GroceryError):
    """A bot-protection interstitial was served instead of real content."""
