"""Session management — login, save/load cookies for LinkedIn."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from src.browser import launch_browser

SESSION_DIR = Path("session")
SESSION_FILE = SESSION_DIR / "linkedin_state.json"


async def login_and_save_session():
    """Open browser for manual LinkedIn login, then save session state."""
    SESSION_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://www.linkedin.com/login")
        print("[auth:linkedin] Browser opened to LinkedIn login page.")
        print("[auth:linkedin] Please log in.")
        print("[auth:linkedin] Once you see your feed, press Enter here to save the session...")
        await asyncio.get_event_loop().run_in_executor(None, input)

        await context.storage_state(path=str(SESSION_FILE))
        print(f"[auth:linkedin] Session saved to {SESSION_FILE}")

        await browser.close()
        print("[auth:linkedin] Browser closed. You can now run the collector.")


async def load_session(playwright, session_file: str | None = None):
    """Launch browser with saved session state. Returns (browser, context, page)."""
    session_path = Path(session_file) if session_file else SESSION_FILE

    if not session_path.exists():
        raise FileNotFoundError(
            f"No saved session at {session_path}. "
            "Run 'python3 -m src.platforms.linkedin.auth' to log in first."
        )

    try:
        json.loads(session_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"Session file at {session_path} is corrupted: {e}. "
            "Run 'python3 -m src.platforms.linkedin.auth' to re-authenticate."
        )

    browser = await launch_browser(playwright)
    context = await browser.new_context(storage_state=str(session_path))
    page = await context.new_page()

    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    if "/login" in page.url or "/checkpoint" in page.url or "/uas/login" in page.url:
        await browser.close()
        raise RuntimeError(
            "Session expired or invalid. "
            "Run 'python3 -m src.platforms.linkedin.auth' to re-authenticate."
        )

    print(f"[auth:linkedin] Session loaded successfully. Current URL: {page.url}")
    return browser, context, page


if __name__ == "__main__":
    asyncio.run(login_and_save_session())
