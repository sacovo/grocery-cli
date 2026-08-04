"""Migros session: a browser session that also carries an OAuth bearer token.

migros.ch is not bot-protected, but its personal endpoints (shopping list, profile)
require `Authorization: Bearer <jwt>`. The SPA obtains that token through a silent
OAuth redirect and keeps it in memory - it is not in localStorage, cookies or anywhere
else we could read directly. What we can do is watch the requests the page itself
makes and reuse the header it sends.
"""

from __future__ import annotations

from ...browser import BrowserSession
from ...errors import NotLoggedIn

BASE = "https://www.migros.ch"

# Any page that triggers the silent login and then calls an authenticated endpoint.
_WARMUP = f"{BASE}/de"


class MigrosSession(BrowserSession):
    """Browser session that harvests the SPA's bearer token."""

    def __init__(self, *args, locale: str = "de", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.locale = locale
        self._bearer: str | None = None

    def start(self) -> MigrosSession:
        super().start()
        self.page.on("request", self._capture_token)
        return self

    def _capture_token(self, request) -> None:
        if self._bearer or "migros.ch" not in request.url:
            return
        for key, value in request.headers.items():
            if key.lower() == "authorization" and value.lower().startswith("bearer "):
                self._bearer = value
                return

    @property
    def bearer(self) -> str | None:
        return self._bearer

    def authenticate(self, timeout: float = 25000) -> str:
        """Load a page until the SPA reveals a bearer token, and return it.

        Raises `NotLoggedIn` if none appears, which means exactly one thing: nobody is
        signed in to migros.ch in this Chrome profile.
        """
        if self._bearer:
            return self._bearer

        self.goto(_WARMUP)
        waited = 0.0
        while waited < timeout and not self._bearer:
            self.page.wait_for_timeout(500)
            waited += 500

        if not self._bearer:
            raise NotLoggedIn(
                "Not logged in to migros.ch. Sign in in the Chrome window this tool "
                "opened (the session then persists in its profile), and retry."
            )
        return self._bearer
