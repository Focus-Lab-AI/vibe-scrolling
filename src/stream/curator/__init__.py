"""Curators for the streaming host.

Public surface (everything the hub needs):

    load_curator_context(workspace_dir, *, allow_template=False) -> CuratorContext
    goals_status(workspace_dir) -> "missing" | "template" | "ok"
    goals_is_untouched_template(text) -> bool
    parse_drop_threshold(goals_text) -> int | None     (KEEP_EVERYTHING = -1)
    apply_policy(verdict, ctx) -> Verdict
    prefilter_verdict(post) -> Verdict | None
    make_curator(kind, **kw) -> Curator                ("heuristic" | "claude" | "cli")

Drop-threshold encoding in CuratorContext.drop_threshold (see SKILL.md § Drop rules):

    default / no override        -> 19   (drop drain OR score <= 19)
    "## Drop threshold: N"       -> N-1  (drop drain OR score <  N)
    "## Drop threshold: none|0"  -> None (only drain is dropped)
    "## Drop threshold: keep everything" -> KEEP_EVERYTHING (-1): never drop,
                                    not even drain or prefilter drops.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from pathlib import Path
from typing import Literal, Optional

from src.models import Post
from src.stream.contracts import Curator, CuratorContext, Verdict

__all__ = [
    "DEFAULT_DROP_THRESHOLD",
    "KEEP_EVERYTHING",
    "GoalsNotConfigured",
    "apply_policy",
    "goals_is_untouched_template",
    "goals_status",
    "load_curator_context",
    "make_curator",
    "parse_drop_threshold",
    "post_to_dict",
    "prefilter_verdict",
]

DEFAULT_DROP_THRESHOLD = 19
KEEP_EVERYTHING = -1  # sentinel for "## Drop threshold: keep everything"
BOOST_MIN_SCORE = 80

# Ported from skills/focus-lab-curator/curate.py (keep in sync). Ported rather
# than imported so the host works when the skill dir isn't shipped alongside.
TEMPLATE_SENTINELS = ("- (none yet)", "(nothing yet)", "(none yet)")


class GoalsNotConfigured(ValueError):
    """goals.md is missing or still the untouched workspace template."""

    def __init__(self, path: Path, status: str):
        self.path = path
        self.status = status
        super().__init__(
            f"{path}: goals.md is {status} — run the curator Bootstrap flow "
            "(5-question interview) before curating."
        )


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def goals_is_untouched_template(goals_text: str) -> bool:
    """True if every content section still holds the template sentinel.
    Port of curate.goals_is_untouched_template."""
    if not goals_text or not goals_text.strip():
        return True
    for raw_line in _strip_comments(goals_text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if any(line == s or line.startswith(s) for s in TEMPLATE_SENTINELS):
            continue
        return False
    return True


_THRESHOLD_RE = re.compile(r"^\s*#{1,6}\s*drop\s+threshold\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_drop_threshold(goals_text: str) -> Optional[int]:
    """Parse `## Drop threshold: …` (outside HTML comments). Returns the
    inclusive drop threshold for CuratorContext (see module docstring).
    The last valid override wins; unparseable values are ignored."""
    result: Optional[int] = DEFAULT_DROP_THRESHOLD
    for m in _THRESHOLD_RE.finditer(_strip_comments(goals_text or "")):
        raw = m.group(1).split("#", 1)[0].strip().strip("`\"'").lower()
        if raw in ("keep everything", "keep-everything", "keep_everything", "all", "everything"):
            result = KEEP_EVERYTHING
        elif raw in ("none", "off", "no", "0"):
            result = None
        elif re.fullmatch(r"\d{1,3}", raw):
            n = min(int(raw), 101)
            result = None if n <= 0 else n - 1  # "drop score < N" == "score <= N-1"
    return result


def goals_status(workspace_dir: Path) -> Literal["missing", "template", "ok"]:
    path = Path(workspace_dir) / "goals.md"
    if not path.is_file():
        return "missing"
    return "template" if goals_is_untouched_template(path.read_text(encoding="utf-8")) else "ok"


def load_curator_context(workspace_dir: Path, *, allow_template: bool = False) -> CuratorContext:
    """Read <workspace>/goals.md into a CuratorContext.

    Raises GoalsNotConfigured if goals.md is missing or an untouched template
    (same refusal as curate.py), unless allow_template=True — then returns a
    context with the raw (possibly empty) text so heuristic curation can run.
    """
    path = Path(workspace_dir) / "goals.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    status = "missing" if not path.is_file() else ("template" if goals_is_untouched_template(text) else "ok")
    if status != "ok" and not allow_template:
        raise GoalsNotConfigured(path, status)
    return CuratorContext(
        goals_text=text,
        goals_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        drop_threshold=parse_drop_threshold(text),
    )


def apply_policy(verdict: Verdict, ctx: CuratorContext) -> Verdict:
    """Decide action from score/category. Never mutates the input.

    keep-everything -> keep/boost only. Prefilter drops stand. Otherwise drop if
    category == drain or score <= threshold; boost if score >= 80 and category
    in (goal, joy); else keep.
    """
    thr = ctx.drop_threshold
    boost = verdict.score >= BOOST_MIN_SCORE and verdict.category in ("goal", "joy")
    if thr is not None and thr < 0:  # KEEP_EVERYTHING
        action = "boost" if boost else "keep"
    elif verdict.stage == "prefilter" and verdict.action == "drop":
        action = "drop"
    elif verdict.category == "drain" or (thr is not None and verdict.score <= thr):
        action = "drop"
    else:
        action = "boost" if boost else "keep"
    return dataclasses.replace(verdict, action=action, flags=list(verdict.flags))


def post_to_dict(post) -> dict:
    if isinstance(post, dict):
        return post
    if dataclasses.is_dataclass(post):
        return dataclasses.asdict(post)
    return dict(vars(post))


def prefilter_verdict(post: Post | dict) -> Optional[Verdict]:
    """Cheap, model-free verdicts. Ads -> drop (stage=prefilter). Empty text
    with no media/quote -> low_info (score 10, policy decides). Else None."""
    p = post_to_dict(post)
    pid = str(p.get("id", ""))
    if p.get("is_ad"):
        return Verdict(id=pid, score=5, category="neutral", reason="Sponsored post.",
                       confidence=1.0, flags=["ad"], action="drop", stage="prefilter")
    text = (p.get("text") or "").strip()
    qp = p.get("quoted_post") or {}
    has_media = bool(p.get("media_urls") or p.get("video_urls") or p.get("local_media_paths"))
    if not text and not has_media and not (isinstance(qp, dict) and (qp.get("text") or "").strip()):
        return Verdict(id=pid, score=10, category="neutral", reason="No text or media.",
                       confidence=1.0, flags=["low_info"], action="keep", stage="prefilter")
    return None


def make_curator(kind: str, **kw) -> Curator:
    """Factory: "heuristic" | "claude" (alias "api", "haiku") | "cli" (alias "skill")."""
    k = kind.lower().strip()
    if k == "heuristic":
        from .heuristic import HeuristicCurator
        return HeuristicCurator(**kw)
    if k in ("claude", "api", "haiku", "claude_api"):
        from .claude_api import ClaudeCurator
        return ClaudeCurator(**kw)
    if k in ("cli", "skill", "skill_cli"):
        from .skill_cli import SkillCliCurator
        return SkillCliCurator(**kw)
    raise ValueError(f"unknown curator kind {kind!r} (heuristic|claude|cli)")
