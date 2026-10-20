"""Per-platform streaming adapters.

`build_adapter(platform, **kwargs)` resolves `src.stream.adapters.<platform>`
lazily, so a platform module that isn't written yet (or fails to import)
only disables that platform. A platform module may expose either a
`make_adapter(**kwargs)` factory or a class named `*Adapter` whose
`platform` attribute matches.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Optional

from src.stream.contracts import PlatformAdapter

KNOWN_PLATFORMS = ["x", "threads", "instagram", "youtube", "linkedin", "fake"]


def build_adapter(platform: str, **kwargs) -> Optional[PlatformAdapter]:
    if platform == "fake":
        from src.stream.adapters.fake import FakeAdapter

        base_url = kwargs.get("base_url")
        if not base_url:
            return None
        return FakeAdapter(base_url)
    try:
        mod = importlib.import_module(f"src.stream.adapters.{platform}")
    except Exception as e:  # noqa: BLE001
        print(f"[stream] no adapter for {platform}: {e}")
        return None
    factory = getattr(mod, "make_adapter", None)
    if callable(factory):
        return factory(**kwargs)
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if (
            name.endswith("Adapter")
            and obj.__module__ == mod.__name__
            and getattr(obj, "platform", None) == platform
        ):
            try:
                return obj(**kwargs)
            except TypeError:
                return obj()
    print(f"[stream] adapter module for {platform} exposes no adapter")
    return None
