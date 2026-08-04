"""Coop-specific session details layered on the shared browser session."""

from __future__ import annotations

from ...browser import BrowserSession
from ...errors import NotLoggedIn
from . import extractors as js

BASE = "https://www.coop.ch"


class CoopSession(BrowserSession):
    """A browser session that knows how to tell whether Coop considers us signed in."""

    def is_logged_in(self) -> bool:
        return bool(self.page.evaluate(js.IS_LOGGED_IN))

    def require_login(self) -> None:
        if not self.is_logged_in():
            raise NotLoggedIn(
                "Not logged in to coop.ch. Log in via Supercard in the Chrome window "
                "that this tool opened (the session then persists in its profile), "
                "and retry."
            )
