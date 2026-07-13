"""Auth flow adapter — replaces stdin input() with asyncio.Event signaling."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from app.paths import SESSION_DIR
from app.tasks.manager import TrackedTask
from src.browser import launch_browser

PLATFORM_LOGIN_URLS = {
    "x": "https://x.com/login",
    "threads": "https://www.threads.net/login",
    "instagram": "https://www.instagram.com/accounts/login/",
    "youtube": "https://accounts.google.com/signin",
    "linkedin": "https://www.linkedin.com/login",
}

# Navigate to login page — if logged in, the platform redirects you away.
# "still_on_login" patterns mean the user is NOT logged in (stayed on login page).
PLATFORM_VERIFY = {
    "x": {"login_url": "https://x.com/login", "still_on_login": ["/login", "/i/flow/login"]},
    "threads": {"login_url": "https://www.threads.net/login", "still_on_login": ["/login"]},
    "instagram": {"login_url": "https://www.instagram.com/accounts/login/", "still_on_login": ["/accounts/login"]},
    "youtube": {"login_url": "https://accounts.google.com/signin", "still_on_login": ["accounts.google.com/"]},
    "linkedin": {"login_url": "https://www.linkedin.com/login", "still_on_login": ["/login", "/uas/login", "/checkpoint"]},
}


def _default_session_file(platform: str) -> Path:
    return SESSION_DIR / f"{platform}_state.json"


def get_session_file(platform: str, config: dict | None = None) -> Path:
    """Get session file path from config or defaults."""
    if config:
        pconfig = config.get("platforms", {}).get(platform, {})
        if sf := pconfig.get("session_file"):
            return Path(sf)
    return _default_session_file(platform)


def check_session_status(platform: str, config: dict | None = None) -> dict:
    """Check if a platform has a valid saved session."""
    session_path = get_session_file(platform, config)
    if not session_path.exists():
        return {"connected": False, "session_file": str(session_path)}

    try:
        json.loads(session_path.read_text())
        return {"connected": True, "session_file": str(session_path)}
    except (json.JSONDecodeError, OSError):
        return {"connected": False, "session_file": str(session_path), "error": "corrupted"}


async def _watch_browser_disconnect(browser, task: TrackedTask):
    """Monitor for browser disconnection (user closed the window).
    Sets the event with cancel flag so the main flow unblocks."""
    try:
        disconnected = asyncio.Event()
        browser.on("disconnected", lambda: disconnected.set())
        await disconnected.wait()

        if task.status == "waiting_for_login":
            task._cancel_flag = True
            task._event.set()
    except Exception:
        pass


async def _verify_session(playwright, platform: str, session_file: Path) -> tuple[bool, str]:
    """Verify a saved session by loading it and navigating to the login page.

    If the user is logged in, the login page should redirect them away
    (e.g., to the home feed). If they stay on the login page, they're not
    logged in.
    """
    verify = PLATFORM_VERIFY.get(platform)
    if not verify:
        return True, "No verification available"

    browser = None
    try:
        browser = await launch_browser(playwright)
        context = await browser.new_context(storage_state=str(session_file))
        page = await context.new_page()

        # Go to the login page — a logged-in user gets redirected away
        await page.goto(verify["login_url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        current_url = page.url
        print(f"[auth:{platform}] Verification URL after redirect: {current_url}")

        # If we're still on the login page, the user is NOT logged in
        for pattern in verify["still_on_login"]:
            if pattern in current_url:
                return False, "You don't appear to be logged in. Please try again and make sure you complete the login."

        # We got redirected away from login — user is logged in
        return True, "Login verified"

    except Exception as e:
        return False, f"Verification error: {e}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def run_auth_flow(task: TrackedTask):
    """Run the browser auth flow for a platform.

    1. Opens browser to login page
    2. Waits for user to click "Done" or close browser
    3. Saves session state
    4. Verifies login by reopening with saved state
    5. If verification fails, deletes the bad session file
    """
    platform = task.platform
    login_url = PLATFORM_LOGIN_URLS.get(platform)
    if not login_url:
        task.status = "failed"
        task.error = f"Unknown platform: {platform}"
        return

    session_file = get_session_file(platform)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    browser = None
    watcher = None
    try:
        task.status = "running"
        async with async_playwright() as p:
            # Phase 1: Open browser for login
            browser = await launch_browser(p)
            context = await browser.new_context()
            page = await context.new_page()

            watcher = asyncio.create_task(_watch_browser_disconnect(browser, task))

            await page.goto(login_url, wait_until="domcontentloaded")
            print(f"[auth:{platform}] Browser opened to {login_url}")

            task.status = "waiting_for_login"

            await task._event.wait()

            if task._cancel_flag:
                task.status = "cancelled"
                task.error = "Browser was closed or auth was cancelled"
                print(f"[auth:{platform}] Auth cancelled")
                return

            # Phase 2: Save session from current browser
            task.status = "running"
            task.progress["step"] = "saving"
            try:
                await context.storage_state(path=str(session_file))
                print(f"[auth:{platform}] Session saved to {session_file}")
            except Exception as e:
                task.status = "failed"
                task.error = f"Failed to save session: {e}"
                return

            # Close the login browser
            if watcher and not watcher.done():
                watcher.cancel()
                watcher = None
            await browser.close()
            browser = None

            # Phase 3: Verify by loading saved session in fresh browser
            task.progress["step"] = "verifying"
            verified, message = await _verify_session(p, platform, session_file)

            if verified:
                task.status = "completed"
                print(f"[auth:{platform}] Login verified successfully")
            else:
                # Delete the bad session file
                try:
                    session_file.unlink()
                except OSError:
                    pass
                task.status = "failed"
                task.error = message
                print(f"[auth:{platform}] Login verification failed: {message}")

    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        print(f"[auth:{platform}] Auth flow failed: {e}")
    finally:
        if watcher and not watcher.done():
            watcher.cancel()
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
