"""Deterministic, instant keyword curator. Used as the realtime fallback for
ClaudeCurator and in tests. No I/O, no randomness: same goals + same post ->
same verdict."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import AsyncIterator

from src.models import Post
from src.stream.contracts import CuratorCaps, CuratorContext, Verdict

from . import apply_policy, post_to_dict, prefilter_verdict

_STOPWORDS = frozenset("""
a an and are as at be but by for from get i im i'm in into is it its just like me more my
none not of on or our out so some stuff than that the their them things this to up us want
we what when with you your yet etc also really very about any anything else other others
especially lot lots kind kinds sort being getting see content people posts post
year years month months week weeks day days time better good great new make making work working toward
help helps learn learning stuff things thing way ways next right now
""".split())

# Section headings -> bucket. Matched as lowercase substrings of the heading.
_SECTION_BUCKETS = (
    ("avoid", "avoid"),
    ("joy", "joy"),
    ("see more", "goal"),
    ("more of", "goal"),
    ("working toward", "goal"),
    ("goal", "goal"),
)

RAGE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\byou won'?t believe\b",
    r"\bratio(?:'?d)?\b",
    r"\b(?:destroy(?:s|ed)?|slams?|obliterate[sd]?|eviscerate[sd]?|owned|humiliate[sd]?)\b",
    r"\b(?:disgust(?:ing|ed)|disgrace(?:ful)?|outrage(?:ous|d)?|shameful|pathetic)\b",
    r"\b(?:idiots?|morons?|clowns?|sheeple|libtards?|snowflakes?)\b",
    r"\b(?:dumbest|stupidest)\b",
    r"\b(?:absolutely|so|completely) (?:furious|livid|enraged)\b",
    r"\bshould be ashamed\b",
    r"\bwake up,? (?:people|sheeple|america)\b",
    r"\bthis is why (?:we can'?t have nice things|everyone hates)\b",
)]

BAIT_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:like|rt|retweet|repost|share)\s*(?:and|&|\+|/)\s*(?:like|rt|retweet|repost|share|follow)\b.*\bif\b",
    r"\b(?:like|rt|retweet|repost|share) (?:this )?if you\b",
    r"\bcomment ['\"]?\w+['\"]? (?:below|and I'?ll|for)\b",
    r"\btag (?:a friend|someone|\d+ (?:friends|people))\b",
    r"\bfollow (?:me )?for (?:more|part \d)\b",
    r"\bwait (?:for|till|until) the end\b",
    r"\b(?:only|just) \d+% (?:of people )?(?:can|will)\b",
    r"\bagree\?\s*$",
    r"\bthread 🧵|🧵👇|👇👇",
    r"\b(?:link in bio|dm me ['\"]?\w+['\"]?)\b",
)]


def _is_all_caps_outrage(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.7 and ("!" in text or "?!" in text)


def _phrase_words(phrase: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+#.'-]*", phrase.lower())
    return [w.strip(".'-") for w in words if w.strip(".'-") and w not in _STOPWORDS and len(w.strip(".'-")) >= 2]


def _word_regex(word: str) -> re.Pattern:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]  # "tips" -> "tip", "cats" -> "cat"
    w = re.escape(word)
    # Short words (ai, ml, art, cat) must match whole-word (+plural); longer
    # ones match as prefix so "climb" hits "climbing".
    return re.compile(rf"\b{w}(?:s|es)?\b" if len(word) <= 3 else rf"\b{w}", re.IGNORECASE)


@dataclass
class GoalsKeywords:
    goal: list[list[re.Pattern]] = field(default_factory=list)
    joy: list[list[re.Pattern]] = field(default_factory=list)
    avoid: list[list[re.Pattern]] = field(default_factory=list)


def parse_goals_keywords(goals_text: str) -> GoalsKeywords:
    """Extract keyword phrases per bucket from goals.md bullet/prose lines."""
    out = GoalsKeywords()
    text = re.sub(r"<!--.*?-->", "", goals_text or "", flags=re.DOTALL)
    bucket = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            head = line.lstrip("#").strip().lower()
            bucket = None
            for needle, b in _SECTION_BUCKETS:
                if needle in head:
                    bucket = b
                    break
            continue
        if not bucket or not line:
            continue
        line = line.lstrip("-*• ").strip()
        if line.lower().startswith("(none"):
            continue
        for chunk in re.split(r"[,;/()]|\band\b|\bor\b|—|–|:", line, flags=re.IGNORECASE):
            words = _phrase_words(chunk)
            if not words or len(words) > 4:
                # long prose chunk: fall back to individual significant words
                for w in words:
                    if len(w) >= 3:
                        getattr(out, bucket).append([_word_regex(w)])
                continue
            getattr(out, bucket).append([_word_regex(w) for w in words])
    return out


def _hits(phrases: list[list[re.Pattern]], text: str) -> int:
    """A phrase hits when at least half (rounded up) of its words appear."""
    n = 0
    for ph in phrases:
        need = (len(ph) + 1) // 2
        if sum(1 for p in ph if p.search(text)) >= need:
            n += 1
    return n


def _clip_words(s: str, n: int = 15) -> str:
    return " ".join(s.split()[:n])


class HeuristicCurator:
    name = "heuristic"

    def __init__(self, max_batch: int = 64):
        self.caps = CuratorCaps(max_batch=max_batch, streaming=True, latency_class="realtime")
        self._kw_cache: dict[str, GoalsKeywords] = {}
        self.last_meta: dict = {}

    def _keywords(self, ctx: CuratorContext) -> GoalsKeywords:
        kw = self._kw_cache.get(ctx.goals_hash)
        if kw is None:
            if len(self._kw_cache) > 8:
                self._kw_cache.clear()
            kw = self._kw_cache[ctx.goals_hash] = parse_goals_keywords(ctx.goals_text)
        return kw

    # -- scoring (sync, pure) ---------------------------------------------

    def _score_text(self, pid: str, text: str, ctx: CuratorContext, *, is_ad: bool = False,
                    has_media: bool = False, base: int = 40) -> Verdict:
        kw = self._keywords(ctx)
        flags: list[str] = []
        goal, joy, avoid = _hits(kw.goal, text), _hits(kw.joy, text), _hits(kw.avoid, text)
        rage = any(p.search(text) for p in RAGE_PATTERNS) or _is_all_caps_outrage(text)
        bait = any(p.search(text) for p in BAIT_PATTERNS)

        score, category, reason, conf = base, "neutral", "No clear match to goals.", 0.3
        if goal:
            score, category, reason, conf = min(92, 66 + 8 * (goal - 1) + 4 * bool(joy)), "goal", "Matches goal keywords.", 0.55
        elif joy:
            score, category, reason, conf = min(90, 64 + 8 * (joy - 1)), "joy", "Matches joy keywords.", 0.55
        if avoid and avoid >= goal + joy:
            score, category, reason, conf = 12, "drain", "Matches avoid-list keywords.", 0.6
        if rage:
            flags.append("rage_bait")
            score, category, reason, conf = min(score, 10), "drain", "Rage-bait phrasing.", 0.6
        if bait:
            flags.append("engagement_bait")
            score = min(score, 15)
            if category != "drain":
                reason, conf = "Engagement-bait phrasing.", 0.5
        if is_ad:
            flags.append("ad")
            score = min(score, 30 if goal else 10)
            if category != "drain":
                reason = "Ad matching goals." if goal else "Ad."
        stripped = text.strip()
        if not goal and not joy and not has_media and len(stripped) < 15:
            flags.append("low_info")
            score = min(score, 25)
            if category == "neutral":
                reason = "Very little content."
        return Verdict(id=pid, score=max(0, min(100, score)), category=category,
                       reason=_clip_words(reason), confidence=conf, flags=flags,
                       stage="model")

    def score_post(self, post: Post | dict, ctx: CuratorContext) -> Verdict:
        """Policy-free verdict for one post (stage='model'; callers relabel)."""
        p = post_to_dict(post)
        parts = [p.get("text") or "", p.get("author_handle") or "", p.get("author_name") or ""]
        qp = p.get("quoted_post")
        if isinstance(qp, dict):
            parts += [qp.get("text") or "", qp.get("author_handle") or ""]
        has_media = bool(p.get("media_urls") or p.get("video_urls") or p.get("local_media_paths"))
        return self._score_text(str(p.get("id", "")), "\n".join(parts), ctx,
                                is_ad=bool(p.get("is_ad")), has_media=has_media)

    def score_reply(self, reply: dict, parent: Post | dict | None, ctx: CuratorContext) -> Verdict:
        text = "\n".join([reply.get("text") or "", reply.get("author_handle") or ""])
        return self._score_text(str(reply.get("id", "")), text, ctx, base=45)

    # -- Curator protocol ----------------------------------------------------

    async def curate(self, posts: list[Post], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        for post in posts:
            v = prefilter_verdict(post) or self.score_post(post, ctx)
            yield apply_policy(v, ctx)

    async def curate_replies(self, parent: Post, replies: list[dict], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        for reply in replies:
            yield apply_policy(self.score_reply(reply, parent, ctx), ctx)
