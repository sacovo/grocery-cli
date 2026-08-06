"""Shared browser session: a real Chrome, attached over its debug port.

Store-agnostic. Two independent reasons the stores need a genuine browser rather than
an HTTP client: coop.ch is behind DataDome, which rejects browsers started with
Playwright's automation flags; and migros.ch issues an OAuth bearer token that only a
logged-in browser session obtains. So Playwright launches nothing - we start an ordinary
Chrome ourselves and attach to it. No fingerprint spoofing is involved.
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

from .errors import BotWall, GroceryError

log = logging.getLogger(__name__)

DEFAULT_PORT = 9222
_LEGACY_PROFILE = pathlib.Path.home() / ".coop-api" / "chrome-profile"
_PROFILE = pathlib.Path.home() / ".grocery" / "chrome-profile"

# The package used to be called coop-api. Keep using the old profile if it is the one
# that exists: it holds the store logins, and silently starting a fresh profile would
# look like "you have been logged out of everything" for no visible reason.
DEFAULT_PROFILE = (
    _LEGACY_PROFILE if _LEGACY_PROFILE.exists() and not _PROFILE.exists() else _PROFILE
)
CHROME_CANDIDATES = ("google-chrome-stable", "google-chrome", "chromium", "chrome")


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
    raise GroceryError(
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
        raise GroceryError(f"Port {port} is in use but is not a Chrome debug endpoint.")

    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        chrome_path or find_chrome(),
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
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
            raise GroceryError("Chrome exited before the debug port became available.")
        time.sleep(0.3)
    raise GroceryError(f"Chrome debug port {port} did not come up within {timeout:g}s.")


class BrowserSession:
    """Attaches to a running Chrome and hands out a page to drive."""

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

    def __enter__(self) -> BrowserSession:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> BrowserSession:
        if not _debug_ready(self.port):
            if not self.autostart:
                raise GroceryError(
                    f"No Chrome debug endpoint on port {self.port}. Start Chrome with "
                    f"--remote-debugging-port={self.port}, or use autostart=True."
                )
            self._proc = launch_chrome(self.port, self.profile, self.chrome_path)
            self._owns_chrome = self._proc is not None

        self._pw = sync_playwright().start()
        # A half-started session must not outlive a failure here: in a long-running
        # process a leaked sync Playwright keeps its event loop parked in the thread,
        # which poisons every later attempt to start one there.
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.port}"
            )
            if not self._browser.contexts:
                raise GroceryError("Chrome exposed no browser context.")
            ctx = self._browser.contexts[0]
            self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        except BaseException:
            self.close()
            raise
        return self

    @property
    def page(self) -> Page:
        if self._page is None:
            raise GroceryError(
                "Session not started - use `with BrowserSession() as s:`."
            )
        return self._page

    def is_blocked(self) -> bool:
        """True if a bot-protection interstitial was served instead of the page.

        Detects DataDome, which fronts coop.ch. Migros never trips it, so the check is
        simply always false there - cheap enough to run for every navigation.
        """
        return bool(
            self.page.evaluate(
                "() => !!document.querySelector('script[src*=\"captcha-delivery\"]')"
            )
        )

    def goto(
        self, url: str, *, wait_for: str | None = None, timeout: float = 30000
    ) -> None:
        """Navigate, failing loudly if bot protection intercepted the request."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if self.is_blocked():
            raise BotWall(
                f"{url} served a bot-protection page. Interact with the Chrome window "
                "once (scroll/click) to re-establish the session, then retry."
            )
        if wait_for:
            try:
                self.page.wait_for_selector(wait_for, timeout=timeout)
            except PlaywrightTimeoutError as exc:
                # Surface as a GroceryError so callers never have to catch Playwright's
                # exceptions to tell "this page is not what we expected" from a crash.
                raise GroceryError(
                    f"{url} did not render {wait_for!r} within {timeout / 1000:g}s."
                ) from exc

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
