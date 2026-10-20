"""Append-only event log per stream session + posts.filtered.json writer.

Layout: `<workspace>/streams/<session_id>/events.jsonl`. Each line is one
wire event (post.curated incl. drops, thread.curated, status, feedback...).
Dropped posts are logged with the post body reduced to identifying fields.

`write_filtered(session_dir)` rebuilds a `posts.filtered.json` that follows
the skills/focus-lab-curator/SKILL.md output contract, so the existing
Curated tab can read a stream session like a collection job.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DROP_POST_FIELDS = ("id", "platform", "author_handle", "url")
CATEGORIES = ("goal", "joy", "adjacent", "drain", "neutral")


class EventLog:
    def __init__(self, session_dir: Path | str):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "events.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")
        self.count = 0

    def append(self, event: dict) -> None:
        if event.get("type") == "post.curated" and (event.get("verdict") or {}).get("action") == "drop":
            post = event.get("post") or {}
            event = {**event, "post": {k: post.get(k) for k in DROP_POST_FIELDS}}
        self._fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def read_events(session_dir: Path | str) -> Iterator[dict]:
    path = Path(session_dir) / "events.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def write_filtered(
    session_dir: Path | str,
    *,
    workspace: Optional[Path | str] = None,
    goals_text: str = "",
    drop_rule: str = "category=drain OR score<=19 (stream policy)",
) -> Path:
    session_dir = Path(session_dir)
    kept: list[tuple[int, dict]] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for idx, ev in enumerate(read_events(session_dir)):
        if ev.get("type") != "post.curated":
            continue
        post = ev.get("post") or {}
        verdict = ev.get("verdict") or {}
        key = f"{post.get('platform')}:{post.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        score = int(verdict.get("score") or 0)
        category = verdict.get("category") or "neutral"
        reason = verdict.get("reason") or ""
        if verdict.get("action") == "drop":
            dropped.append(
                {
                    "id": post.get("id"),
                    "score": score,
                    "category": category,
                    "filter_reason": reason,
                    "platform": post.get("platform") or ev.get("platform"),
                }
            )
        else:
            kept.append(
                (idx, {**post, "score": score, "filter_reason": reason, "category": category})
            )
    kept.sort(key=lambda t: (-t[1]["score"], t[0]))  # stable within score
    posts = [p for _, p in kept]
    scores = [p["score"] for p in posts]
    category_counts = {c: 0 for c in CATEGORIES}
    platform_counts: dict[str, int] = {}
    for p in posts:
        category_counts[p["category"]] = category_counts.get(p["category"], 0) + 1
        platform_counts[p.get("platform", "")] = platform_counts.get(p.get("platform", ""), 0) + 1
    platforms = sorted(set(platform_counts) | {d["platform"] for d in dropped if d["platform"]})
    out = {
        "filter_metadata": {
            "filtered_at": datetime.now(timezone.utc).isoformat(),
            "workspace": str(Path(workspace).resolve()) if workspace else "",
            "job": f"streams/{session_dir.name}",
            "platforms": platforms,
            "goals_snapshot": goals_text,
            "source_posts": len(posts) + len(dropped),
            "kept_posts": len(posts),
            "dropped_count": len(dropped),
            "drop_rule": drop_rule,
            "median_score": int(statistics.median(scores)) if scores else 0,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "category_counts": category_counts,
            "platform_counts": platform_counts,
            "dropped": dropped,
            "notes": "Built from a live stream session event log.",
        },
        "posts": posts,
    }
    out_path = session_dir / "posts.filtered.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return out_path
