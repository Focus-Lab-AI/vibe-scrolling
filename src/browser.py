"""Shared browser launch for auth and collection.

Playwright's default launch passes --enable-automation, which sets
navigator.webdriver=true and shows the "controlled by automated software"
infobar. Login pages (Google's sign-in especially) refuse browsers carrying
these signals, so every launch goes through here with them stripped — the
user is logging into their own account in a real, headed browser.
"""

from playwright.async_api import Browser, Error, Playwright

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
IGNORE_DEFAULT_ARGS = ["--enable-automation"]


async def launch_browser(playwright: Playwright, *, headless: bool = False) -> Browser:
    """Launch Chromium, preferring the branded Chrome build when installed.

    Google treats unbranded Chromium sign-ins as insecure; falls back to the
    bundled Chromium when no system Chrome exists.
    """
    try:
        return await playwright.chromium.launch(
            channel="chrome",
            headless=headless,
            args=LAUNCH_ARGS,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
        )
    except Error:
        return await playwright.chromium.launch(
            headless=headless,
            args=LAUNCH_ARGS,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
        )
