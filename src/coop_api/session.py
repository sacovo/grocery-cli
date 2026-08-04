"""Browser session management.

coop.ch is protected by DataDome, which rejects browsers started with Playwright's
automation flags. So we do not let Playwright launch anything: we start an ordinary
Chrome ourselves and attach to its debug port. That is a genuine browser running the
user's own logged-in session - no fingerprint spoofing involved.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import socket
import subprocess  # nosec B404 - launching Chrome is this module's whole purpose
import time
import urllib.request

from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import extractors as js

log = logging.getLogger(__name__)

DEFAULT_PORT = 9222
DEFAULT_PROFILE = pathlib.Path.home() / ".coop-api" / "chrome-profile"
CHROME_CANDIDATES = ("google-chrome-stable", "google-chrome", "chromium", "chrome")

BASE = "https://www.coop.ch"


class CoopError(RuntimeError):
    pass


class NotLoggedIn(CoopError):
    pass


class BotWall(CoopError):
    """coop.ch served the DataDome interstitial instead of real content."""


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _debug_ready(port: int) -> bool:
    """True if a Chrome DevTools endpoint answers on ``port``."""
    # Fixed scheme and loopback host, with an int port - the URL cannot be steered
    # elsewhere by a caller, so the "audit url open" warning does not apply here.
    url = f"http://127.0.0.1:{int(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1):  # nosec B310 - literal http://127.0.0.1
            return True
    except OSError:
        # urllib.error.URLError subclasses OSError, so this covers both.
        return False


def find_chrome() -> str:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise CoopError(
        "No Chrome/Chromium binary found. Install Chrome, or pass chrome_path=... explicitly."
    )


def launch_chrome(
    port: int = DEFAULT_PORT,
    profile: pathlib.Path = DEFAULT_PROFILE,
    chrome_path: str | None = None,
    timeout: float = 30.0,
) -> subprocess.Popen | None:
    """Start a normal Chrome with remote debugging enabled.

    Returns ``None`` if something is already listening on ``port`` (we attach to that
    instead). The profile directory is dedicated to this tool so it never fights with
    the user's day-to-day Chrome over a profile lock.
    """
    if _debug_ready(port):
        return None
    if _port_open(port):
        raise CoopError(f"Port {port} is in use but is not a Chrome debug endpoint.")

    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        chrome_path or find_chrome(),
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        BASE,
    ]
    # No shell, fixed argument list, and the executable is either resolved from a
    # known-name allowlist via shutil.which or supplied explicitly by the caller.
    proc = subprocess.Popen(  # nosec B603 - shell=False with a fixed argv
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _debug_ready(port):
            return proc
        if proc.poll() is not None:
            raise CoopError("Chrome exited before the debug port became available.")
        time.sleep(0.3)
    raise CoopError(f"Chrome debug port {port} did not come up within {timeout:g}s.")


class CoopSession:
    """Attaches to a running Chrome and hands out a page pointed at coop.ch."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        profile: pathlib.Path = DEFAULT_PROFILE,
        chrome_path: str | None = None,
        autostart: bool = True,
    ) -> None:
        self.port = port
        self.profile = profile
        self.chrome_path = chrome_path
        self.autostart = autostart
        self._pw = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._proc: subprocess.Popen | None = None
        self._owns_chrome = False

    def __enter__(self) -> CoopSession:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> CoopSession:
        if not _debug_ready(self.port):
            if not self.autostart:
                raise CoopError(
                    f"No Chrome debug endpoint on port {self.port}. Start Chrome with "
                    f"--remote-debugging-port={self.port}, or use autostart=True."
                )
            self._proc = launch_chrome(self.port, self.profile, self.chrome_path)
            self._owns_chrome = self._proc is not None

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.port}"
        )
        if not self._browser.contexts:
            raise CoopError("Chrome exposed no browser context.")
        ctx = self._browser.contexts[0]
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return self

    @property
    def page(self) -> Page:
        if self._page is None:
            raise CoopError("Session not started - use `with CoopSession() as s:`.")
        return self._page

    def goto(
        self, url: str, *, wait_for: str | None = None, timeout: float = 30000
    ) -> None:
        """Navigate and fail loudly if DataDome intercepted the request."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if self.is_blocked():
            raise BotWall(
                "coop.ch served its bot-protection page. Interact with the Chrome window "
                "once (scroll/click) to re-establish the session, then retry."
            )
        if wait_for:
            try:
                self.page.wait_for_selector(wait_for, timeout=timeout)
            except PlaywrightTimeoutError as exc:
                # Surface as a CoopError so callers never have to catch Playwright's
                # exceptions to tell "this page is not what we expected" from a crash.
                raise CoopError(
                    f"{url} did not render {wait_for!r} within {timeout / 1000:g}s."
                ) from exc

    def is_blocked(self) -> bool:
        return bool(self.page.evaluate(js.IS_BLOCKED))

    def is_logged_in(self) -> bool:
        return bool(self.page.evaluate(js.IS_LOGGED_IN))

    def require_login(self) -> None:
        if not self.is_logged_in():
            raise NotLoggedIn(
                "Not logged in to coop.ch. Log in via Supercard in the Chrome window that "
                "this tool opened (the session then persists in its profile), and retry."
            )

    def close(self) -> None:
        """Detach. Chrome keeps running unless we started it ourselves.

        Teardown failures are logged rather than raised: `close` runs from `__exit__`,
        where letting a detach error propagate would mask the real exception.
        """
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                log.debug(
                    "Ignoring error while closing the browser connection: %s", exc
                )
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as exc:
                log.debug("Ignoring error while stopping Playwright: %s", exc)
            self._pw = None
        self._page = None

    def quit_chrome(self) -> None:
        """Terminate the Chrome instance we launched, if any."""
        self.close()
        if self._owns_chrome and self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            self._owns_chrome = False
