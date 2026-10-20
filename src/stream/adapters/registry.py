"""Platform name -> streaming adapter.

Real platforms only; the fake adapter (fake.py) is owned by the spine and can
be registered by callers via `register()`.
"""

from __future__ import annotations

from src.stream.contracts import PlatformAdapter

from .instagram import InstagramAdapter
from .linkedin import LinkedInAdapter
from .threads import ThreadsAdapter
from .x import XAdapter
from .youtube import YouTubeAdapter

ADAPTERS: dict[str, PlatformAdapter] = {
    a.platform: a
    for a in (XAdapter(), ThreadsAdapter(), InstagramAdapter(), YouTubeAdapter(), LinkedInAdapter())
}

ALIASES = {"twitter": "x"}

HOME_URLS: dict[str, str] = {name: a.home_url for name, a in ADAPTERS.items()}


def _canonical(platform: str) -> str:
    p = (platform or "").strip().lower()
    return ALIASES.get(p, p)


def get_adapter(platform: str) -> PlatformAdapter:
    try:
        return ADAPTERS[_canonical(platform)]
    except KeyError:
        raise KeyError(
            f"No streaming adapter for platform {platform!r}; known: {sorted(ADAPTERS)}"
        ) from None


def home_url(platform: str) -> str:
    return get_adapter(platform).home_url


def register(adapter: PlatformAdapter) -> None:
    """Add/replace an adapter (e.g. the fake adapter in tests/E2E)."""
    ADAPTERS[adapter.platform] = adapter
    HOME_URLS[adapter.platform] = adapter.home_url
