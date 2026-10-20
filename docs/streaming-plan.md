# Streaming Vibe Scrolling — Plan

Branch: `feature/streaming-host` (worktree `vibe-scrolling-stream`), based on `main` + `fix/browser-automation-signals`.

## Goal

Replace the async batch loop (collect → agent curates → repackage → phone) with a
live pipeline. A **host** (Linux box with Xvfb now, Mac mini later) keeps logged-in
browsers alive. A **phone** connects; everything it sees is curated in-line by an
agent; interactions (open comments, load more, take over) run on the real hosted
browser and stream back.

## Core decisions

| Decision | Choice | Why |
|---|---|---|
| What streams to the phone | **Structured posts** (intercepted JSON → `Post` → curated event) by default | Pixels can't be curated; native render, low bandwidth, offline buffer |
| Escape hatch | **Live view**: CDP `Page.startScreencast` JPEG frames over the same WS, touch/wheel input back via `Input.dispatch*` | Login/2FA, captchas, unsupported UI, "show me the real thing" |
| Scrolling | **Pull-driven** by phone buffer (low/high watermark), human pacing, session caps | Avoids 24/7 bot-like scrolling; no wasted curation spend |
| Browsers | One `launch_persistent_context(user_data_dir)` per platform, headed, `channel="chrome"` when present, automation signals stripped | Persistent cookies auto-refresh; headed is far less detectable |
| Curation | Pluggable `Curator`. v1: heuristic pre-filter → Claude Haiku 4.5 micro-batches (≤8 posts / 1.5s), structured output, cached goals prompt. Fallback: heuristic. Offline: existing skill | ~1s to first verdict, ~$0.06 / 100 posts |
| Transport | One WebSocket per phone (`/ws/stream`), JSON messages; binary frames for live view | Bidirectional — needed for interactions and backpressure |
| Phone client | PWA served by the host (`/phone/`), reuses `PostRenderer` | Fast iteration; native later if push is needed |
| Remote access | LAN first; then `tailscale serve` (HTTPS, private). Pairing token via QR | No public exposure |
| Persistence | Append every event to `<workspace>/streams/<session>/events.jsonl`; writer rebuilds `posts.filtered.json` | Existing Curated/Data tabs keep working |

## Architecture

```
            ┌──────────────────────── Host (FastAPI, one process) ──────────────────────────┐
 Phone PWA  │                                                                              │
  ─ WS ───► │ StreamHub ──► per-platform FeedProducer ──► BrowserPool (persistent ctx)    │
  ◄─ events │    │              │  page.on(response) → adapter.parse_body → Post         │
            │    │              ▼                                                          │
            │    │        prefilter (dedupe/ads/seen) → CurationQueue → Curator           │
            │    │                                              │ verdicts (streamed)      │
            │    ◄──────────── ready buffer (reorder by score) ◄┘                          │
            │    │                                                                          │
            │    ├─ actions: open_comments → adapter.fetch_comments(ctx, post) → curator   │
            │    └─ live view: CDP screencast ⇄ Input.dispatch*                            │
            │ EventLog (events.jsonl) → posts.filtered.json writer                          │
            └──────────────────────────────────────────────────────────────────────────────┘
```

Package layout (new code under `src/stream/`):

- `contracts.py` — event + message dataclasses, `PlatformAdapter`, `Curator` protocols (**source of truth**)
- `browser_pool.py` — persistent contexts per platform, Xvfb on Linux, profile import from existing `storage_state`
- `adapters/` — per-platform streaming adapters wrapping existing interceptors/replies (`x`, `threads`, `instagram`, `youtube`, `linkedin`, `fake`)
- `producer.py` — pull-driven scroll driver per platform
- `curator/` — `heuristic.py`, `claude_api.py`, `skill_cli.py` (offline)
- `hub.py` — session orchestration, buffers, backpressure, fan-out, event log
- `live_view.py` — CDP screencast + input injection
- `app/api/stream.py` — WebSocket endpoint + pairing
- `app/static/phone/` — phone PWA
- `tests/stream/` — unit + end-to-end against a local **fake feed site** (no real logins needed)

## Wire protocol (WS `/ws/stream?token=…`)

Client → host:

| type | fields | meaning |
|---|---|---|
| `hello` | `platforms[]`, `buffer_target` | start/resume session |
| `demand` | `buffer_depth`, `last_seen_post_id` | backpressure signal (send on scroll) |
| `open_comments` | `request_id`, `post_id` | fetch + curate thread on real browser |
| `live.start` / `live.stop` | `platform`, `post_id?` | start pixel stream (optionally on a post) |
| `live.input` | `kind: tap/swipe/wheel/text/key`, coords normalized 0..1 | input injection |
| `feedback` | `post_id`, `signal: more/less/hide` | v2 tuning |

Host → client:

| type | fields |
|---|---|
| `session` | `session_id`, `platforms[]` with status (`ready`/`needs_login`/`error`) |
| `post.curated` | see `contracts.CuratedPost` (action keep/boost; drops are logged, not sent unless `debug`) |
| `thread.curated` | `request_id`, `post_id`, `replies[]` each with `action: show/collapse/hide` |
| `status` | per-platform producer state, buffer depth, curator latency |
| `live.frame` | binary: 1-byte tag + JPEG; plus JSON `live.meta` (`device_width/height`) |
| `error` | `request_id?`, `message` |

## Milestones

**M1 — Streaming spine (fake platform, end-to-end)**
contracts, hub, producer, browser pool, fake feed site + adapter, heuristic curator, WS endpoint, phone PWA showing posts streaming in. E2E test: fake site → WS client receives curated posts; demand pauses/resumes scrolling.

**M2 — Real platforms**
Incremental per-response parse for X and Threads (move parse into `handle_response`), emit hooks for IG/YT/LI. Persistent profiles + import of existing `session/*.json`. Recorded-fixture tests per platform.

**M3 — Claude curation in-line**
Haiku 4.5 micro-batch curator with streaming verdict parse, cached goals prompt, 4s timeout → heuristic fallback; thread curation; `posts.filtered.json` writer from event log; goals.md drop-threshold implemented.

**M4 — Streaming interactions**
`open_comments` on the real browser (X `TweetDetail` intercept; Threads/IG reuse existing fetchers) → curated thread back. Live view: screencast + input, used for login on the phone and "open real post".

**M5 — Remote & host ops**
Pairing token + QR, `tailscale serve`, Mac mini runbook (App Nap off, caffeinate, auto-login, dummy display), session caps/quiet hours, desktop-app tab showing host status.

## Risks

- **Account restrictions** (esp. LinkedIn, Instagram): pull-driven pacing, caps, per-platform opt-in, headed Chrome, one stable IP.
- **CDN media hotlink/expiry**: host proxies media (`/api/stream/media?u=`) with a small disk cache.
- **Mobile layouts differ** when emulating a phone for live view: keep feed contexts desktop-sized; live view uses a separate mobile-emulated page in the same profile.
- **Curator latency spikes**: buffer of 15–20, heuristic fallback, boost reorder only within ready buffer.
- **Existing test suite is stale** (imports pre-refactor APIs) — new code gets its own `tests/stream/`; fixing the old suite is a separate task.
