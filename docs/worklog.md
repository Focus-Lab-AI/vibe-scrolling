# Work Log

## 2026-07-12 - Fix login rejection: shared browser launch without automation signals

Platforms were refusing logins in the automated browser. Playwright's default
`chromium.launch()` passes `--enable-automation`, which sets
`navigator.webdriver=true` — the first thing every bot check reads.

- New `src/browser.py` with `launch_browser(playwright, headless=False)`: strips `--enable-automation` via `ignore_default_args`, adds `--disable-blink-features=AutomationControlled`, and prefers the branded Chrome build (`channel="chrome"`) since Google's sign-in treats unbranded Chromium as insecure — falling back to bundled Chromium when no system Chrome exists.
- Routed all 11 launch sites through it: `app/tasks/auth_task.py` (login flow + `_verify_session`) and `src/platforms/{x,threads,instagram,linkedin,youtube}/auth.py` (`login_and_save_session` + `load_session`). Collectors get it for free — they all obtain their browser from `load_session`.
- `focus-lab.spec`: added `src.browser`; fixed stale `src.platforms.twitter.*` hiddenimports left over from the twitter→x rename (would have broken the bundled app) and added the missing `src.platforms.linkedin.*` entries.

Verified: side-by-side launch probe — old default reports `navigator.webdriver=True`, `launch_browser` reports `False`; Chrome-channel fallback to bundled Chromium exercised (no system Chrome on this box).

Found-but-not-fixed: `tests/` is stale from the multi-platform refactor — all 6 modules fail at collection importing `src.scroller` / `save_tweets`, which no longer exist. Pre-existing on a clean tree; separate cleanup.

## 2026-07-12 - Dev environment setup on Linux (Steam Deck)

- Created `.venv` (Python 3.13.5, system pip absent on SteamOS) and installed `requirements.txt` + `requirements-app.txt`.
- Launched the app with `.venv/bin/python -m app.main --no-gui` — server mode on port 8741; pywebview native window untested on this machine.
- Installed Chromium through the app's own `POST /api/setup/install` endpoint (lands in `~/.cache/focus-lab-feed-collector/playwright/chromium-1228/`).
- Note: that install provides the full headed Chromium only, not Playwright's `chrome-headless-shell` — headless Playwright scripts on this box need `executable_path=.../chrome-linux64/chrome`.
- Verified: `/` serves the UI, `/api/config` and `/api/setup/status` respond, onboarding welcome screen renders in Chromium (screenshot checked). No code changes.

## 2026-05-02 - Rename twitter → x

Twitter has been "X" for years; the codebase finally catches up.

- Renamed `src/platforms/twitter/` → `src/platforms/x/` (`git mv`); all imports updated.
- Slug `"twitter"` → `"x"` everywhere it appeared as an identifier: PLATFORMS lists in `app/api/collection.py` + `src/platforms/__init__.py`, choices in `src/collect.py`, PLATFORM_LOGIN_URLS + PLATFORM_VERIFY in `app/tasks/auth_task.py`, `app/paths.py` default config block, `config.json`, frontend `platform-icons.js` (PATHS / NAMES key, display name "X"), `post-renderer.js`, `viewer.js`, `collection.js`, CSS variables (`--color-x`, `.badge-x`, `.platform-icon-x`), and `skills/focus-lab-curator/SKILL.md` examples.
- Login URL switched from `twitter.com/login` → `x.com/login`. Existing `still_on_login` patterns (`/login`, `/i/flow/login`) stay valid at x.com.
- Session file renamed: `session/twitter_state.json` → `session/x_state.json` (in both repo `session/` and `~/Library/Application Support/Focus Lab Feed Collector/session/`).
- Migrated active workspace data: `~/Documents/vibe-scrolling-data/data/2026-05-02/job_021736/twitter/` → `…/x/`, rewrote `posts.json` (`platform: "twitter"` → `"x"`, `…/twitter/media/…` → `…/x/media/…`), `run_log.json`, and `posts.filtered.json` (including `filter_metadata.platforms` and `platform_counts` keys).
- Comments referencing the old slug in legacy migration code (`src/storage.py`, `app/api/data.py`) intentionally left — they describe the historical flat-format migration and dropping them would lose context.
- Old `feed_data/` and the prior workspace's data left untouched per the earlier "leave it" decision.

Verified: `/api/auth/status` returns `x` (connected), `/api/curated/jobs` shows the curated job with `platform_counts.x: 59` and `platforms: [..., 'x', 'youtube']`, `/api/data/runs` lists `2026-05-02/job_021736/x` with 63 posts.

Found-but-not-fixed: media downloader has a race condition when multiple platforms collect in parallel (`_current_run_dir` is a module global). Some X posts in the existing job ended up with media saved into `linkedin/media/` instead of `x/media/`. Pre-existing bug, separate fix.

## 2026-05-02 - Workspace IS the data dir + per-job curation, drop auto-export

Followup to the workspace-as-data-dir refactor. Three things ship together:

- **Curator skill rewritten for per-job, cross-platform curation** (`skills/focus-lab-curator/`, v1.0.0 → v2.0.0). `curate.py` now operates from the workspace root (auto-detects via walking up to find `data/` or `goals.md`), picks the latest job under `data/` (or `--job 2026-05-02/job_HHMMSS`), reads every platform's `posts.json` together, scores them all in one ranking against `goals.md`, and writes a single `posts.filtered.json` at `<workspace>/data/<date>/<job>/`. Drops the orphan-media trim and `--keep-media` / `--drop-videos` flags — data is in place, not packed. SKILL.md rewritten end-to-end (no more "pack" vocabulary, goals are workspace-level only, output contract documents the new shape with `platforms` + `platform_counts` + per-dropped `platform`).
- **Curated API + UI flipped from packs to jobs.** `/api/curated/packs` → `/api/curated/jobs`; lists every job under `data/` that has `posts.filtered.json`, returns full filter_metadata. `app/static/js/curated.js` now selects by job (date · job_id · platforms label), shows per-platform kept counts in the header ribbon, and serves media via the existing `/feed_data/` route since `local_media_paths` are already relative to the data root. `curation.js` (Curate-with-AI tab) lost its pack picker — agent-launch step now always points at the workspace root and the prompt says "curate the latest feed".
- **Auto-export ripped out entirely.** `app/api/export.py` deleted; `_maybe_auto_export()` and the `export_curation` plumbing in `collection.py` deleted; `/api/workspace/auto-export` GET/POST deleted; `auto_export` config key dropped; auto-export toggle removed from Settings + Onboarding; `pack_count` / `recent_packs` / `exports_dir` dropped from `/api/workspace` response. Curation reads directly from `data/`, so the duplicate-then-curate dance was buying nothing.

Verified end-to-end on a real job: workspace at `~/Documents/vibe-scrolling-data`, 265 posts across twitter / threads / instagram / linkedin / youtube. `python3 skills/focus-lab-curator/curate.py` ran 14 batches of 20 in 4-way parallel against `claude --print` with Sonnet 4.6, took ~2 min, produced 245 kept / 20 dropped, top 8 results mix all 5 platforms (NASA tweet 73, instagram cinnamoroll 65, threads + linkedin all interleaved). `/api/curated/jobs` lists the job; `/api/curated/jobs/2026-05-02/job_021736` returns 245 posts; `/feed_data/<path>` serves a 12 MB linkedin video correctly.

## 2026-05-02 - LinkedIn collector

- Added `src/platforms/linkedin/` (auth, interceptor, collector) following the same shape as threads/youtube.
- **Switched from DOM scraping to Voyager API parsing.** Initial DOM approach captured 59 posts but with empty author names and broken counts. The Voyager response shape is actually easy to parse: each response has a flat `included` array of typed entities (`Update`, `Profile`, `SocialActivityCounts`, `VideoPlayMetadata`, ...) keyed by `entityUrn`. Build an in-memory store from `included`, walk every `Update`, and resolve `*foo` URN references (e.g. `*socialDetail` → `SocialDetail` → `*totalSocialActivityCounts` → `SocialActivityCounts`) to get author, text, counts, and media. DOM extraction stays as a fallback if the API ever yields nothing.
- Author handle extracted from `actor.navigationContext.actionTarget` (handles `/in/`, `/company/`, `/school/` URL forms). Time text from `actor.subDescription.text`. Media URLs built by concatenating `vectorImage.rootUrl` + the largest artifact's `fileIdentifyingUrlPathSegment`. Native videos resolved via `*videoPlayMetadata` → `VideoPlayMetadata.progressiveStreams` (highest-quality progressive stream). Document slides pull the first cover page; article previews pull the headline image. Voyager responses are archived to `raw/voyager_*.json`.
- Verified end-to-end: 55 posts captured in ~55s with 0 empty authors / 0 empty text / 100% time text / 54/55 with engagement counts / 49/55 with remote media URLs / 72 local media files downloaded.
- Wired LinkedIn through the rest of the system: `src/platforms/__init__.py` registry, `src/collect.py` CLI choices, `app/api/collection.py` PLATFORMS, `app/tasks/auth_task.py` PLATFORM_LOGIN_URLS + PLATFORM_VERIFY (login URL + still-on-login patterns including `/uas/login` and `/checkpoint`), `app/static/js/{platforms,settings,onboarding,collection}.js`, `post-renderer.js` mention/hashtag links, `app.css` (`--color-linkedin: #0a66c2` + `.badge-linkedin`), and `config.json` defaults.
- Captured fields: id (`urn:li:activity:*`), text, author name + headline + url, time-ago text (LinkedIn doesn't expose absolute dates in the DOM), likes, comments (mapped to `Post.replies`), reposts, image URLs, video URLs, repost detection with quoted_post, sponsored/promoted flag mapped to `is_ad`.

## 2026-04-22 - Vibe Scrolling rebrand + gated onboarding

- Renamed app to **Focus Lab — Vibe Scrolling** (browser title, Dock/bundle name, macOS window title, FastAPI title, sidebar logo)
- Sidebar logo now two-line: "Focus Lab" / "Vibe Scrolling" (small-caps treatment in CSS)
- New multi-step onboarding (`app/static/js/onboarding.js`) gates the main app until the user has: installed Chromium (if missing), connected ≥1 platform, and confirmed a workspace folder. Steps: welcome → setup → connect → workspace → done. Includes emoji pipeline walkthrough on the welcome step (🔐 → 🌀 → 📦 → 🤖) with staggered fade-in animation.
- Workspace step defaults to `~/Focus Lab Feed`, supports the native folder picker, and exposes the auto-export toggle with a disk-space note. With auto-export off, the step shows a hint pointing to the Export tab for per-day pulls.
- Boot logic refactored: `App.init()` decides onboarding vs `App.bootApp()` based on `/setup/status` + `/workspace` state. Returning users (workspace set up, Chromium installed) bypass onboarding entirely.
- Renamed the "Instructions" tab to **Curate with AI** and rewrote `curation.js` as a focused 4-section workflow: goals editor → pack check → agent picker + copy-paste commands → link to AI Curation tab. Removed now-redundant connect/collect prose now covered by onboarding and dedicated tabs.
- Export page: added subtitle clarifying "pick a day and the platforms you want", clearer "Choose day and platform" card heading, and a floating bottom-right "Open folder" FAB that reveals the exports dir in Finder.
- CSS: new `.ob-*` design tokens for onboarding (progress dots, pipeline stages, platform grid, folder/auto-export boxes), `.curate-section-*` for the new Curate with AI numbered sections, `.fab-open-folder` FAB.

## 2026-03-23 - Desktop app with macOS .app bundle

- Built full desktop app in `app/` directory (FastAPI + vanilla JS SPA + PyWebView)
- Backend: FastAPI server with REST API for auth, collection, config, data, export, setup
- Frontend: 4-page SPA — Platforms (connect/disconnect), Collect (trigger/monitor), Viewer (feed display), Export (JSON/CSV/Focus Lab)
- Auth flow: Playwright browser opens for manual login, asyncio.Event replaces stdin input(), browser disconnect detection, login verification (navigate to login page → check redirect)
- Collection: background asyncio tasks wrapping existing platform `run()` functions, status polling, cancellation
- Centralized paths (`app/paths.py`): dev mode uses project root, bundled mode uses `~/Library/Application Support/` and `~/Library/Caches/`
- First-launch onboarding: detects missing Chromium, shows setup UI, installs via Playwright's node CLI driver
- PyInstaller bundling: `focus-lab.spec` with hidden imports, macOS BUNDLE config, `scripts/build-macos.sh` for .app + .dmg
- Result: 64MB .dmg, ~159MB .app, Chromium downloaded on first launch (~150MB one-time)
- Fixed auth issues: stuck tasks on browser close, cancel not killing Playwright, can't reconnect after failure
- Fixed login validation: flipped check (go to login page, verify redirect away) works for IG/Threads which don't require login to browse
- Files created: app/{__init__,main,server,paths,setup}.py, app/api/{__init__,auth,collection,config,data,export,setup}.py, app/tasks/{__init__,manager,auth_task}.py, app/static/{index.html,css/app.css,js/{app,platforms,collection,viewer,export}.js}, focus-lab.spec, scripts/build-macos.sh, requirements-app.txt
- Existing CLI (`python3 src/collect.py`) unchanged and still works independently

## 2026-02-22 - Project created

- Initial project setup with TST structure
- Co-founder discussion established technical approach:
  - Playwright + API interception as primary strategy
  - DOM scraping via data-testid as fallback
  - gallery-dl + yt-dlp for media download
  - Twitter/X first, other platforms later
- Key reference projects identified: fa0311/twitter-openapi, proxidize/x-scraper
- Discussion summary saved to HQ

## 2026-02-22 - VP Planning Session (Fiona Feed)

- Sharpened vision into one-liner: "An AI agent scrolls your social media so you never have to"
- Broke Milestone 1 into two concrete sprints (5 tasks each)
- Sprint 1: Scaffolding, session management, GraphQL interception, tweet parsing, data output
- Sprint 2: Scroll automation, stop conditions, deduplication, image download, run summary
- Populated prd.json with 10 user stories (S1.1-S1.5, S2.1-S2.5) with acceptance criteria
- Technical decisions locked: Python 3.11+, JSON files for storage, manual login with saved storage_state
- Updated CLAUDE.md with file structure, technical notes, and key patterns
- Updated roadmap.md with detailed sprint breakdown
- Updated next-tasks.md with concrete task definitions and done criteria
- Defined branch name for Ralph loop: `milestone-1-twitter-collection`
- Researched reference projects (proxidize/x-scraper uses same Playwright + cookie pattern, 2-5s scroll delays)

## 2026-02-22 - Ralph Loop launched

- Initiated autonomous work session via `/tst:project-work`
- Stories to complete: 10
- Starting with: Project Scaffolding (S1.1)

## 2026-02-22 - S1.1 Project Scaffolding complete

- Created all 8 source files in `src/` with initial implementations
- Created `requirements.txt` (playwright, aiohttp), `config.json`, `.gitignore`
- Verified `python3 src/collector.py` runs cleanly and models instantiate correctly
- Branch: `milestone-1-twitter-collection`

## 2026-02-22 - S1.3 GraphQL Response Interception complete

- Wired up `collector.py` with `auth.load_session()` and `interceptor.ResponseInterceptor`
- End-to-end flow: load session → attach page.on("response") → reload page → capture GraphQL responses → save raw JSON
- Interceptor pattern matches `*/i/api/graphql/*/Home*` (HomeTimeline, HomeLatestTimeline)
- Each response logged with: endpoint name, HTTP status, response size, entry count
- Raw responses saved to `feed_data/YYYY-MM-DD/raw/{endpoint}_{timestamp}.json`
- Added microsecond precision to raw filenames to prevent collisions
- Added `sys.path` fix in collector.py so `python3 src/collector.py` works with `from src.` imports
- Graceful error handling for missing/expired sessions
- Files changed: src/collector.py, src/interceptor.py

## 2026-02-22 - S1.4 Tweet Data Parsing complete

- Added `parse_all_tweets()`, `_parse_entry()`, `_parse_tweet_result()`, `_extract_author()`, `_extract_media_urls()` to `ResponseInterceptor`
- Parser navigates Twitter's nested JSON: data.home.home_timeline_urt.instructions[].entries[].content.itemContent.tweet_results.result
- Handles `TweetWithVisibilityResults` wrapper (unwraps to inner tweet)
- Retweets detected via `retweeted_status_result` — original author tracked, inner tweet content used
- Promoted/ad tweets detected via `promotedMetadata` — skipped by default
- Missing fields handled gracefully (empty strings, zero counts, empty lists)
- Cursor entries and non-tweet items filtered out
- Deduplication by tweet ID across multiple responses
- Wired parser into `collector.py` — parse + save after interception
- Created test suite with 12 tests covering all acceptance criteria
- Added pytest to requirements.txt
- Files changed: src/interceptor.py, src/collector.py, requirements.txt, tests/test_parser.py

## 2026-02-22 - S1.5 Structured Data Output complete

- Enhanced `storage.py` with collection metadata (run_timestamp, tweet_count, collection_duration_seconds)
- Used `dataclasses.asdict()` for proper serialization instead of `__dict__`
- Added `load_tweets_from_file()` for loading from arbitrary paths and `load_metadata()` helper
- Updated `collector.py` to track collection timing with `time.monotonic()` and pass duration to save_tweets
- JSON output is pretty-printed with `indent=2` and `ensure_ascii=False`
- Created `tests/test_storage.py` with 12 tests: save, pretty-print, metadata, round-trip, all fields
- All 24 tests pass (12 parser + 12 storage)
- Files changed: src/storage.py, src/collector.py, tests/test_storage.py

## 2026-02-22 - S2.1 Scroll Automation complete

- Enhanced `src/scroller.py` with `scroll_loop()` function that orchestrates scrolling with the interceptor
- `scroll_feed()` scrolls one viewport height down with random delay between `scroll_delay_min` and `scroll_delay_max`
- `scroll_loop()` scrolls continuously, parsing tweets after each scroll via interceptor
- Stale detection: stops after N consecutive scrolls with no new tweets (default stale_limit=3)
- Max tweets stop condition: stops when enough tweets collected
- Detailed logging per scroll: scroll number, new tweets, total tweets, delay used
- Updated `collector.py` to use `scroll_loop()` after initial page load, passing config values
- Final summary includes scroll count, total tweets, and stop reason
- Created `tests/test_scroller.py` with 9 async tests covering: single scroll, max_tweets stop, stale detection, stale reset, zero tweets, delay range, dict keys
- Added `pytest-asyncio` to requirements.txt for async test support
- All 33 tests pass (12 parser + 9 scroller + 12 storage)
- Files changed: src/scroller.py, src/collector.py, requirements.txt, tests/test_scroller.py

## 2026-02-22 - S2.2 Configurable Stop Conditions complete

- Added `max_minutes` and `oldest_tweet_date` stop conditions to `scroll_loop()` in `src/scroller.py`
- Stop conditions: max_tweets, max_minutes (time limit), oldest_tweet_date (YYYY-MM-DD), stale detection — whichever fires first
- Added `_parse_twitter_date()` and `_has_tweet_older_than()` helper functions
- Updated `collector.py` to pass `max_minutes` and `oldest_tweet_date` from config to scroll_loop
- Added `oldest_tweet_date: null` to `config.json` (max_minutes was already present)
- Default: 50 tweets or 5 minutes, whichever comes first
- Stop reason is clearly logged for every condition
- Added 11 new tests: max_minutes stop, max_minutes not triggered, time-before-tweets priority, oldest_tweet_date stop, date not triggered, date on initial load, date parsing, invalid dates, has_tweet_older_than true/false/unparseable
- All 44 tests pass (12 parser + 20 scroller + 12 storage)
- Files changed: src/scroller.py, src/collector.py, config.json, tests/test_scroller.py, docs/status.md, docs/worklog.md

## 2026-02-22 - S2.3 Tweet Deduplication complete

- Added `deduplicate_tweets()` to `src/storage.py` — loads existing tweets from today's file, merges with new, reports duplicates
- Updated `src/collector.py` to call `deduplicate_tweets()` before saving, dedup count shown in final summary
- Enhanced `interceptor.py` `parse_all_tweets()` to track and report within-run duplicate count
- Cross-run dedup: existing tweets loaded from `tweets.json`, new tweets matched by ID, duplicates skipped
- Within-run dedup: same tweet appearing in multiple GraphQL responses stored only once (was already working, now reports count)
- Created `tests/test_dedup.py` with 9 tests: no existing file, all duplicates, partial overlap, order preservation, ID-based dedup, empty input, multi-run accumulation, within-run across responses, within-run same response
- All 53 tests pass (12 parser + 9 dedup + 20 scroller + 12 storage)
- Files changed: src/storage.py, src/interceptor.py, src/collector.py, tests/test_dedup.py, docs/status.md, docs/worklog.md

## 2026-02-22 - S2.4 Image Download complete

- Enhanced `src/media_downloader.py` with `download_tweet_images()` and `_image_download_url()` helper
- `download_tweet_images()` iterates over all tweets, downloads images via aiohttp, updates `local_media_paths` in-place
- Images downloaded with `?format=jpg&name=large` suffix for best quality
- Images saved to `feed_data/YYYY-MM-DD/media/{tweet_id}_{index}.jpg`
- Reuses a single aiohttp session for all downloads (efficient)
- Failed downloads logged but don't crash — returns (downloaded, failed) counts
- Progress printed for each image: "X of Y images processed"
- Updated `src/collector.py` to call `download_tweet_images()` after dedup, before save
- Collector summary now includes image download stats
- `download_image()` refactored to accept session parameter (no session-per-image overhead)
- Created `tests/test_media_downloader.py` with 12 tests: URL formatting, success, dirs, HTTP errors, exceptions, no-media, paths, local_media_paths, failures, mixed results, suffix verification
- All 65 tests pass (12 parser + 9 dedup + 20 scroller + 12 storage + 12 media)
- Files changed: src/media_downloader.py, src/collector.py, tests/test_media_downloader.py, docs/status.md, docs/worklog.md

## 2026-03-22 - Multi-platform collection + viewer

### Twitter improvements
- Fixed author extraction: Twitter moved `screen_name`/`name` from `user_results.result.legacy` to `user_results.result.core` — need to check both paths with fallback
- Added video download: `extended_entities.media[].video_info.variants[]` has mp4 URLs at different bitrates, pick highest
- Changed scrolling from `scrollBy(0, innerHeight)` to `scrollTo(0, document.body.scrollHeight)` — the old approach only moved one viewport and stalled after 3 scrolls, the new one reaches the actual bottom and triggers infinite scroll loading
- Changed storage from per-day to per-run: each collection gets `feed_data/YYYY-MM-DD_HHMMSS_{platform}/` since social media feeds serve unique content each time
- Added reply collection via parallel browser tabs: open 4 tabs at once, each navigates to tweet detail URL, intercepts `TweetDetail` GraphQL response

### Multi-platform architecture
- Restructured to `src/platforms/{platform}/` with shared `models.py`, `storage.py`, `media_downloader.py`
- Unified `Post` model replaces `Tweet` — added `platform`, `url`, `reposts`, `is_repost`, `platform_data` fields
- Unified CLI: `python3 src/collect.py --platform twitter` or omit for all enabled
- Config: nested per-platform settings under `platforms` key with `enabled` flags

### Threads collector
- **API pattern:** `threads.com/graphql/query` — responses contain `data.feedData.edges[].node.text_post_app_thread.thread_items[].post`
- **Data location:** Feed data comes from GraphQL responses during page load and scroll (not embedded in HTML like Instagram)
- **Key fields:** `post.user.username`, `post.caption.text`, `post.like_count`, `post.text_post_app_info.{direct_reply_count, repost_count, quote_count}`
- **Media:** `post.image_versions2.candidates[]` for images, `post.video_versions[]` for video, `post.carousel_media[]` for multi-image
- **Timestamps:** Unix epoch in `post.taken_at`
- **Gotcha:** Some edges have `text_post_app_thread: null` (suggested users) — must null-check
- **Replies:** Threads doesn't expose reply data in GraphQL or HTML script tags. Had to use DOM scraping: find `<a role="link" href="/@username">` elements, walk up the DOM tree to find associated text spans. Skip the OP and logged-in user's profile link.
- **Media download:** Meta CDN URLs don't support Twitter's `?format=jpg&name=large` suffix — returns 403. Must use URLs directly.

### Instagram collector
- **API pattern:** `instagram.com/graphql/query` with key `xdt_api__v1__feed__timeline__connection`
- **Data location:** Initial feed NOT in GraphQL (returns empty media fields). Feed data comes from:
  1. Embedded JSON in `<script type="application/json">` tags on first load (path: `require[0][3][0].__bbox.require[0][3][1].__bbox.result.data.xdt_api__v1__feed__timeline__connection`)
  2. GraphQL responses after scrolling (same connection key, but media is in `explore_story.media` not `media` directly)
- **Key fields:** `media.user.username` (NOT `media.owner.username` — owner only has ID), `media.caption.text`, `media.like_count`, `media.comment_count`
- **Media:** Same structure as Threads (`image_versions2.candidates[]`, `video_versions[]`, `carousel_media[]`)
- **Ad detection:** Check `node.ad`, `media.ad_id`, `media.dr_ad_type`, `media.is_paid_partnership`
- **Comments:** Found in post detail page HTML script tags as `XDTCommentDict` objects under `xdt_api__v1__media__media_id__comments__connection.edges[].node`. Fields: `user.username`, `text`, `comment_like_count`, `created_at`
- **Gotcha:** Instagram aggressively detects bots. Need headful mode, realistic delays (3-7s between scrolls)

### YouTube collector
- **API pattern:** No GraphQL — YouTube uses `youtubei/v1/browse` and `youtubei/v1/guide`
- **Data location:** `window.ytInitialData` JavaScript variable contains the entire home feed
- **Feed structure:** `ytInitialData.contents.twoColumnBrowseResultsRenderer.tabs[0].tabRenderer.content.richGridRenderer.contents[]`
- **Video items:** `richItemRenderer.content.lockupViewModel` — fields: `contentId` (video ID), `metadata.lockupMetadataViewModel.title.content`, channel name and views in `metadataRows`
- **Shorts:** Appear in `richSectionRenderer.content.richShelfRenderer` shelves titled "Shorts". Each short is a `shortsLockupViewModel` with video ID in `onTap.innertubeCommand.reelWatchEndpoint.videoId`
- **No media download:** Store `embed_url` (`youtube.com/embed/{videoId}`) for iframe playback in viewer
- **Scrolling:** YouTube pre-loads heavily — initial page has ~20 videos + ~18 shorts. Scroll continuation via browse API didn't trigger in testing; the initial data is sufficient
- **Gotcha:** YouTube recently changed from `videoRenderer` to `lockupViewModel` for video items

### Viewer
- Platform tabs (All | Twitter | Threads | Instagram | YouTube)
- Multi-image: horizontal scrollable carousel with drag-to-scroll, counter badges, same-height items
- Click image → fullscreen lightbox overlay
- Videos autoplay muted on scroll (IntersectionObserver at 50% threshold), pause when scrolled away
- YouTube embedded via iframe
- Collapsible replies with blue dot indicator
- Ad badge + dimmed styling
- Platform-aware links (@mentions, #hashtags link to correct platform)
- Backward compatible with old `tweets.json` format

## 2026-02-22 - S2.5 Collection Run Summary complete

- Added `save_run_summary()` to `src/storage.py` — appends summary dicts to `run_log.json` array
- Added `print_summary()` to `src/collector.py` — formatted table with all key stats
- Collector tracks warnings list (no GraphQL responses, image download failures, no tweets parsed)
- Summary includes: total tweets, new tweets, duplicates skipped, images downloaded/failed, scroll count, run time, stop reason, warnings
- Summary saved to `feed_data/YYYY-MM-DD/run_log.json` (appended, supports multiple runs per day)
- Created `tests/test_run_summary.py` with 11 tests: file creation, appending, location, field preservation, pretty-print, warnings, empty warnings, printed output, warning display, no-warnings omission, header
- All 76 tests pass (12 parser + 9 dedup + 20 scroller + 12 storage + 12 media + 11 summary)
- Files changed: src/storage.py, src/collector.py, tests/test_run_summary.py, prd.json, docs/status.md, docs/worklog.md
