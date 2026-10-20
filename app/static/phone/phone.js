/**
 * Focus Lab Live — phone client for the streaming host.
 *
 * Wire protocol: docs/streaming-plan.md ("Wire protocol"); event shapes:
 * src/stream/contracts.py. Cards are built with PostRenderer.renderPost
 * (/js/post-renderer.js) and then enhanced here for touch (carousel dots,
 * tap-to-play video, curator "why" pill, Comments button).
 *
 * Vanilla JS, no build step. Exposes window.PhoneClient for debugging/tests.
 */
(function () {
'use strict';

const BUFFER_TARGET = 15;
const DEMAND_INTERVAL_MS = 1000;   // steady-state demand throttle
const NEAR_BOTTOM_PX = 1500;       // "near bottom" → demand immediately
const COMMENTS_TIMEOUT_MS = 30000;
const LIVE_TIMEOUT_MS = 15000;
const LS_TOKEN = 'focuslab:stream:token';
const LS_PLATFORMS = 'focuslab:stream:platforms';
const FRAME_TAG_JPEG = 0x01;

const PR = window.PostRenderer;
const $ = (id) => document.getElementById(id);

// --- PostRenderer extensions (see report: upstream these) -------------------
if (PR && PR.PLATFORM_LINKS && !PR.PLATFORM_LINKS.linkedin) {
    // PLATFORM_LINKS is the same object renderPost closes over, so adding a
    // key here takes effect for mention/hashtag links.
    PR.PLATFORM_LINKS.linkedin = {
        mention: h => `https://www.linkedin.com/in/${h}`,
        hashtag: h => `https://www.linkedin.com/feed/hashtag/${h}`,
    };
}

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function rid(prefix) {
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}
function domId(id) { return 'p' + String(id).replace(/[^A-Za-z0-9_-]/g, '_'); }
const DISPLAY = { x: 'X', threads: 'Threads', instagram: 'Instagram', youtube: 'YouTube', linkedin: 'LinkedIn' };
function displayName(p) { return DISPLAY[p] || cap(p); }
function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }

const ICON_COMMENTS = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
const ICON_EYE_OFF = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 8.5 4.5 9.5 6-.4.6-1.3 1.9-2.7 3.1M6.6 7.6C4.6 8.9 3.2 10.8 2.5 12c1 1.5 4.5 6 9.5 6 1.6 0 3-.4 4.3-1.1M9.9 9.9a3 3 0 0 0 4.2 4.2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
const ICON_PLAY = '<span><svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M8 5v14l11-7z" fill="#fff"/></svg></span>';

// --- Config ----------------------------------------------------------------
function readConfig() {
    const q = new URLSearchParams(location.search);
    let token = q.get('token');
    let platforms = q.get('platforms');
    try {
        if (token) localStorage.setItem(LS_TOKEN, token);
        else token = localStorage.getItem(LS_TOKEN) || '';
        if (platforms != null) localStorage.setItem(LS_PLATFORMS, platforms);
        else platforms = localStorage.getItem(LS_PLATFORMS) || '';
    } catch (_) { token = token || ''; platforms = platforms || ''; }
    if (q.has('token')) {
        // Keep the pairing token out of the visible URL / history once saved.
        q.delete('token');
        const rest = q.toString();
        history.replaceState(null, '', location.pathname + (rest ? '?' + rest : '') + location.hash);
    }
    return {
        token,
        // Empty list = "whatever the host has enabled".
        platforms: platforms.split(',').map(s => s.trim()).filter(Boolean),
    };
}

const cfg = readConfig();

// --- State -----------------------------------------------------------------
const state = {
    ws: null,
    conn: 'connecting',
    retry: 0,
    retryTimer: null,
    sessionId: null,
    seen: new Set(),            // post ids already rendered (dedupe across reconnects)
    posts: new Map(),           // post id → post.curated event
    platforms: new Map(),       // platform → PlatformStatus-ish
    statusMeta: {},
    host: { mode: 'server', loginHint: '' },
    pending: new Map(),         // request_id → {kind, post_id, platform, sent}
    threads: new Map(),         // post_id → thread.curated event (cache)
    sheet: { requestId: null, postId: null, timer: null },
    live: { platform: null, requestId: null, timer: null, meta: null, decoding: false, nextFrame: null, gotFrame: false },
    demand: { lastAt: 0, timer: null, lastKey: '', lastKeyAt: 0 },
    received: 0,
};

// --- WebSocket ---------------------------------------------------------------
function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/stream?token=${encodeURIComponent(cfg.token)}`;
}

function send(msg) {
    const ws = state.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
}

function connect() {
    clearTimeout(state.retryTimer);
    state.retryTimer = null;
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) return;
    setConn('connecting');
    let ws;
    try { ws = new WebSocket(wsUrl()); } catch (e) { scheduleReconnect(); return; }
    ws.binaryType = 'arraybuffer';
    state.ws = ws;

    ws.onopen = () => {
        state.retry = 0;
        setConn('live');
        const d = computeDemand();
        send({
            type: 'hello',
            platforms: cfg.platforms,
            buffer_target: BUFFER_TARGET,
            // Resume hints (extra fields; host may ignore).
            session_id: state.sessionId,
            last_seen_post_id: d.last_seen_post_id,
        });
        sendDemand(true);
        // Re-issue requests that were in flight when the socket dropped.
        for (const [id, p] of state.pending) {
            if (p.kind === 'comments') send({ type: 'open_comments', request_id: id, post_id: p.post_id });
        }
        if (state.live.platform && !$('live').hidden) {
            send({ type: 'live.start', platform: state.live.platform, request_id: state.live.requestId });
        }
    };
    ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
            let m;
            try { m = JSON.parse(ev.data); } catch (_) { return; }
            try { handleMessage(m); } catch (e) { console.warn('phone: bad message', m && m.type, e); }
        } else {
            handleBinary(ev.data);
        }
    };
    ws.onclose = () => {
        if (state.ws !== ws) return;
        state.ws = null;
        setConn('offline');
        scheduleReconnect();
    };
    ws.onerror = () => { /* onclose follows and handles reconnect */ };
}

function scheduleReconnect() {
    if (state.retryTimer) return;
    const base = Math.min(15000, 500 * Math.pow(2, state.retry));
    const delay = Math.round(base * (0.7 + Math.random() * 0.6));
    state.retry++;
    $('tail-label').textContent = `Disconnected — retrying in ${Math.max(1, Math.round(delay / 1000))}s…`;
    state.retryTimer = setTimeout(() => { state.retryTimer = null; connect(); }, delay);
}

function reconnectNow() {
    if (state.ws) return;
    clearTimeout(state.retryTimer);
    state.retryTimer = null;
    connect();
}

function setConn(s) {
    state.conn = s;
    const el = $('conn');
    el.dataset.state = s;
    $('conn-label').textContent = s;
    if (s === 'live') $('tail-label').textContent = 'Your agent is curating…';
    if (s === 'connecting') $('tail-label').textContent = 'Connecting to your host…';
    renderMeta();
}

// --- Message dispatch --------------------------------------------------------
function handleMessage(m) {
    switch (m.type) {
        case 'session': return onSession(m);
        case 'post.curated': return onPost(m);
        case 'thread.curated': return onThread(m);
        case 'status': return onStatus(m);
        case 'live.meta': state.live.meta = m; return;
        case 'error': return onError(m);
        default: return; // forward-compatible: ignore unknown types
    }
}

function onSession(m) {
    state.sessionId = m.session_id || state.sessionId;
    if (m.host) state.host = { mode: m.host.mode || 'server', loginHint: m.host.login_hint || '' };
    for (const p of (m.platforms || [])) {
        if (typeof p === 'string') { upsertPlatform({ platform: p, state: 'ready' }); continue; }
        upsertPlatform({ platform: p.platform || p.name, state: p.status || p.state, detail: p.detail });
    }
    renderPlatforms();
}

function onStatus(m) {
    let list = m.platforms;
    if (list && !Array.isArray(list)) list = Object.entries(list).map(([k, v]) => Object.assign({ platform: k }, v));
    if (!list && m.platform) list = [m];
    for (const p of (list || [])) upsertPlatform(p);
    for (const k of ['buffer_depth', 'curator_latency_ms', 'curator']) if (k in m) state.statusMeta[k] = m[k];
    renderPlatforms();
}

function upsertPlatform(p) {
    if (!p || !p.platform) return;
    const prev = state.platforms.get(p.platform) || {};
    const next = Object.assign({}, prev, p);
    if (!next.state && p.status) next.state = p.status;
    state.platforms.set(p.platform, next);
}

// --- Feed --------------------------------------------------------------------
function onPost(ev) {
    const post = ev.post || {};
    const id = String(post.id || ev.event_id || '');
    if (!id || state.seen.has(id)) return;
    const verdict = ev.verdict || {};
    if (verdict.action === 'drop') return; // only sent in host debug mode
    state.seen.add(id);
    state.posts.set(id, ev);
    state.received++;

    const card = buildCard(ev, id);
    const feed = $('feed');
    if (verdict.action === 'boost') {
        // Boosts may jump the queue, but never into (or above) what's on screen.
        const anchor = firstCardBelowViewport();
        if (anchor) feed.insertBefore(card, anchor); else feed.appendChild(card);
    } else {
        feed.appendChild(card);
    }
    enhanceCard(card);
    renderMeta();
    scheduleDemand();
}

function buildCard(ev, id) {
    const post = Object.assign({}, ev.post);
    const platform = ev.platform || post.platform || 'x';
    const v = ev.verdict || {};
    const category = v.category || 'neutral';
    const action = v.action || 'keep';
    const boosted = action === 'boost';

    const headerExtras =
        (boosted ? '<span class="boost-badge" title="Boosted by your agent">▲ boost</span>' : '') +
        `<button class="why-pill cat-${esc(category)}" data-act="why" aria-expanded="false" title="Why am I seeing this?">${esc(category)}</button>`;
    const statsExtras =
        `<button class="comments-btn" data-act="comments" aria-label="Open comments">${ICON_COMMENTS}<span>Comments</span></button>`;

    const flags = (v.flags || []).filter(Boolean);
    const cur = ev.curator || {};
    const metaBits = [];
    if (typeof v.score === 'number') metaBits.push(`score ${v.score}`);
    if (flags.length) metaBits.push(flags.join(', '));
    if (v.stage) metaBits.push(v.stage);
    if (cur.backend || cur.model) metaBits.push([cur.backend, cur.model].filter(Boolean).join(' · '));
    if (typeof cur.latency_ms === 'number') metaBits.push(`${Math.round(cur.latency_ms)}ms`);
    const afterStats = `<div class="why-box" hidden>
        <div>${esc(v.reason || (boosted ? 'Boosted by your agent.' : 'Kept by your agent.'))}</div>
        ${metaBits.length ? `<div class="meta">${esc(metaBits.join(' · '))}</div>` : ''}
    </div>`;

    const html = PR.renderPost(post, {
        platform,
        postId: domId(id),
        headerExtras,
        statsExtras,
        afterStats,
        dataAttrs: { category, action, platform },
    });
    const tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    const el = tpl.content.firstElementChild;
    el.dataset.id = id;
    return el;
}

const videoObserver = ('IntersectionObserver' in window) ? new IntersectionObserver((entries) => {
    for (const e of entries) if (!e.isIntersecting && !e.target.paused) e.target.pause();
}, { threshold: 0.2 }) : null;

function enhanceCard(card) {
    // Images: lazy + async decode (renderer already sets loading=lazy).
    card.querySelectorAll('img').forEach(img => {
        img.loading = 'lazy';
        img.decoding = 'async';
        img.alt = img.alt || '';
    });

    // Video: tap-to-play instead of native controls (better on small screens).
    card.querySelectorAll('video').forEach(v => {
        v.removeAttribute('controls');
        v.setAttribute('playsinline', '');
        v.preload = 'metadata';
        const wrap = document.createElement('div');
        wrap.className = 'video-wrap';
        v.parentNode.insertBefore(wrap, v);
        wrap.appendChild(v);
        const btn = document.createElement('button');
        btn.className = 'video-play';
        btn.setAttribute('aria-label', 'Play video');
        btn.dataset.act = 'video';
        btn.innerHTML = ICON_PLAY;
        wrap.appendChild(btn);
        v.addEventListener('play', () => wrap.classList.add('playing'));
        v.addEventListener('pause', () => wrap.classList.remove('playing'));
        v.addEventListener('click', () => toggleVideo(v));
        if (videoObserver) videoObserver.observe(v);
    });

    // Carousel: native touch scroll + snap; add dots; mouse drag via renderer.
    card.querySelectorAll('.post-carousel').forEach(c => {
        const n = c.children.length;
        if (n < 2) return;
        const dots = document.createElement('div');
        dots.className = 'carousel-dots';
        dots.innerHTML = Array.from({ length: n }, (_, i) => `<i class="${i === 0 ? 'on' : ''}"></i>`).join('');
        c.after(dots);
        let raf = 0;
        c.addEventListener('scroll', () => {
            if (raf) return;
            raf = requestAnimationFrame(() => {
                raf = 0;
                const idx = Math.round(c.scrollLeft / Math.max(1, c.clientWidth));
                dots.querySelectorAll('i').forEach((d, i) => d.classList.toggle('on', i === idx));
            });
        }, { passive: true });
    });
    if (card.querySelector('.viewer-carousel')) PR.setupCarouselDrag('.viewer-carousel');
}

function toggleVideo(v) {
    if (v.paused) {
        v.muted = false;
        v.play().catch(() => { v.muted = true; v.play().catch(() => {}); });
    } else {
        v.pause();
    }
}

function cards() { return $('feed').children; }

function firstCardBelowViewport() {
    const vh = window.innerHeight;
    const list = cards();
    // Cards are in visual order; scan from the end (new content lives there).
    let found = null;
    for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].getBoundingClientRect().top >= vh) found = list[i];
        else break;
    }
    return found;
}

// --- Demand / backpressure ---------------------------------------------------
function computeDemand() {
    const vh = window.innerHeight;
    const list = cards();
    let unseen = 0, lastSeen = null;
    for (let i = list.length - 1; i >= 0; i--) {
        const top = list[i].getBoundingClientRect().top;
        if (top >= vh) { unseen++; continue; }
        lastSeen = list[i].dataset.id;
        break;
    }
    return { buffer_depth: unseen, last_seen_post_id: lastSeen };
}

function sendDemand(force) {
    const d = computeDemand();
    const key = `${d.buffer_depth}|${d.last_seen_post_id}`;
    const now = Date.now();
    if (!force && key === state.demand.lastKey && now - state.demand.lastKeyAt < 3000) return;
    if (send(Object.assign({ type: 'demand' }, d))) {
        state.demand.lastAt = now;
        state.demand.lastKey = key;
        state.demand.lastKeyAt = now;
    }
}

function nearBottom() {
    const doc = document.documentElement;
    return window.scrollY + window.innerHeight >= doc.scrollHeight - NEAR_BOTTOM_PX;
}

function scheduleDemand() {
    const now = Date.now();
    const since = now - state.demand.lastAt;
    const urgent = nearBottom() && since > 250;
    if (urgent || since >= DEMAND_INTERVAL_MS) {
        clearTimeout(state.demand.timer);
        state.demand.timer = null;
        sendDemand(urgent);
        return;
    }
    if (!state.demand.timer) {
        state.demand.timer = setTimeout(() => {
            state.demand.timer = null;
            sendDemand(false);
        }, DEMAND_INTERVAL_MS - since);
    }
}

// --- Status strip / login banner ---------------------------------------------
function renderPlatforms() {
    const chips = [];
    const needLogin = [];
    for (const [name, p] of state.platforms) {
        const st = p.state || 'starting';
        if (st === 'needs_login') needLogin.push(name);
        const label = st === 'needs_login' ? 'needs login' : st;
        const counts = (p.kept || p.dropped)
            ? ` · <span class="counts"><b>${p.kept || 0}</b> kept <span class="drop">· ${p.dropped || 0} cut</span></span>` : '';
        chips.push(`<span class="chip" data-platform="${esc(name)}" data-state="${esc(st)}" title="${esc(p.detail || '')}"><i class="dot"></i>${esc(displayName(name))} · ${esc(label)}${counts}</span>`);
    }
    $('chips').innerHTML = chips.join('');

    const banner = $('login-banner');
    if (needLogin.length) {
        const names = needLogin.map(displayName);
        const who = names.length === 1 ? names[0] : names.slice(0, -1).join(', ') + ' & ' + names[names.length - 1];
        if (state.host.mode === 'laptop') {
            // The feed browser is a visible window on the laptop: log in there,
            // the host notices within a few seconds and starts scrolling.
            const hint = state.host.loginHint || 'Log in in the browser window on your laptop.';
            banner.innerHTML =
                `<div class="login-txt"><b>${esc(who)}</b> ${names.length === 1 ? 'needs' : 'need'} you to log in. ${esc(hint)}</div>`;
            banner.hidden = false;
            renderMeta();
            return;
        }
        banner.innerHTML =
            `<div class="login-txt"><b>${esc(who)}</b> ${names.length === 1 ? 'needs' : 'need'} you to log in on the host.</div>` +
            `<div class="login-btns">${needLogin.map(n =>
                `<button class="pill-btn sm" data-act="login" data-platform="${esc(n)}">${needLogin.length === 1 ? 'Log in on host' : 'Log in · ' + esc(displayName(n))}</button>`).join('')}</div>`;
        banner.hidden = false;
    } else {
        banner.hidden = true;
        banner.innerHTML = '';
    }
    renderMeta();
}

function renderMeta() {
    const bits = [];
    if (state.conn !== 'live') bits.push(state.conn === 'offline' ? 'Offline — reconnecting' : 'Connecting…');
    bits.push(`${state.received} curated`);
    let kept = 0, dropped = 0;
    for (const p of state.platforms.values()) { kept += p.kept || 0; dropped += p.dropped || 0; }
    if (dropped) bits.push(`${dropped} filtered out`);
    if (typeof state.statusMeta.curator_latency_ms === 'number') bits.push(`agent ${Math.round(state.statusMeta.curator_latency_ms)}ms`);
    $('feed-meta').textContent = bits.join(' · ');
}

// --- Comments sheet ----------------------------------------------------------
function openComments(postId) {
    const ev = state.posts.get(postId);
    const post = (ev && ev.post) || {};
    const sheet = $('sheet');
    $('sheet-title').textContent = post.author_name ? `Comments · ${post.author_name}` : 'Comments';
    $('sheet-backdrop').hidden = false;
    sheet.hidden = false;
    sheet.style.transform = '';
    document.body.classList.add('locked');
    state.sheet.postId = postId;

    const cached = state.threads.get(postId);
    if (cached) { renderThread(cached); return; }
    requestThread(postId);
}

function requestThread(postId) {
    const ev = state.posts.get(postId);
    const post = (ev && ev.post) || {};
    const requestId = rid('c');
    clearTimeout(state.sheet.timer);
    state.sheet.requestId = requestId;
    state.pending.set(requestId, { kind: 'comments', post_id: postId, sent: Date.now() });
    const ctx = post.text ? `<div class="sheet-context">${esc(post.text)}</div>` : '';
    $('sheet-body').innerHTML = `${ctx}<div class="sheet-loading"><div class="spinner"></div><div>Opening the post on your host…</div></div>`;
    if (!send({ type: 'open_comments', request_id: requestId, post_id: postId })) {
        $('sheet-body').querySelector('.sheet-loading div:last-child').textContent = 'Waiting for connection…';
    }
    state.sheet.timer = setTimeout(() => {
        if (state.sheet.requestId !== requestId || !state.pending.has(requestId)) return;
        state.pending.delete(requestId);
        showSheetError('The host took too long to fetch replies.');
    }, COMMENTS_TIMEOUT_MS);
}

function closeSheet() {
    $('sheet').hidden = true;
    $('sheet-backdrop').hidden = true;
    document.body.classList.remove('locked');
    clearTimeout(state.sheet.timer);
    state.sheet.requestId = null;
    state.sheet.postId = null;
}

function showSheetError(message) {
    if ($('sheet').hidden) return;
    $('sheet-body').innerHTML = `<div class="inline-error" role="alert"><span>${esc(message)}</span><button class="pill-btn ghost" data-act="retry-comments">Retry</button></div>`;
}

function onThread(m) {
    if (m.request_id) state.pending.delete(m.request_id);
    if (m.post_id) state.threads.set(String(m.post_id), m);
    const current = state.sheet.requestId && m.request_id === state.sheet.requestId;
    const samePost = !$('sheet').hidden && String(m.post_id) === state.sheet.postId;
    if (current || samePost) {
        clearTimeout(state.sheet.timer);
        renderThread(m);
    }
}

function replyHtml(r, platform, extraClass) {
    const name = esc(r.author_name || r.author_handle || 'Unknown');
    const handle = r.author_handle ? `@${esc(r.author_handle)}` : '';
    const when = r.created_at ? esc(PR.timeAgo(r.created_at)) : '';
    const text = PR.formatText(r.text || '', platform);
    const media = (r.media_urls && r.media_urls.length) || (r.video_urls && r.video_urls.length)
        ? PR.renderMedia({ media_urls: r.media_urls || [], video_urls: r.video_urls || [] }, platform, null) : '';
    const likes = r.likes ? `<div class="reply-meta">likes ${PR.fmtNum(r.likes)}</div>` : '';
    return `<div class="reply ${extraClass || ''}" data-id="${esc(r.id || '')}">
        <div class="reply-head"><span class="reply-author">${name}</span><span class="reply-handle">${handle}</span><span class="reply-time">${when}</span></div>
        <div class="reply-text">${text}</div>${media}${likes}
    </div>`;
}

function renderThread(m) {
    const ev = state.posts.get(String(m.post_id));
    const platform = (ev && (ev.platform || (ev.post && ev.post.platform))) || 'x';
    const replies = m.replies || [];
    let html = '';
    const hidden = [];
    let visible = 0;
    replies.forEach((cr, i) => {
        const r = cr.reply || cr;
        const action = cr.action || 'show';
        if (action === 'hide') { hidden.push(cr); return; }
        visible++;
        if (action === 'collapse') {
            const why = cr.reason || (cr.flags || []).join(', ') || 'low quality';
            html += `<div class="reply-collapsed" data-idx="${i}">
                <button class="reply-reveal" data-act="reveal">${ICON_EYE_OFF}<span>Hidden by your agent: <em>${esc(why)}</em> — tap to show</span></button>
                <div class="reply-inner" hidden>${replyHtml(r, platform)}</div>
            </div>`;
        } else {
            html += replyHtml(r, platform);
        }
    });
    if (!visible && !hidden.length) html = '<div class="empty-replies">No replies yet.</div>';
    else if (!visible) html = '<div class="empty-replies">Your agent hid every reply on this post.</div>';
    if (hidden.length) {
        html += `<button class="hidden-footer" data-act="show-hidden">${hidden.length} hidden by your agent · <u>show anyway</u></button>`;
        html += `<div class="hidden-replies" hidden>${hidden.map(cr => replyHtml(cr.reply || cr, platform, 'was-hidden')).join('')}</div>`;
    }
    $('sheet-body').innerHTML = html;
    $('sheet-body').scrollTop = 0;
}

// Swipe the sheet header down to dismiss.
function setupSheetDrag() {
    const head = $('sheet-head');
    const sheet = $('sheet');
    let startY = null, dy = 0;
    head.addEventListener('touchstart', (e) => { startY = e.touches[0].clientY; dy = 0; sheet.style.transition = 'none'; }, { passive: true });
    head.addEventListener('touchmove', (e) => {
        if (startY == null) return;
        dy = Math.max(0, e.touches[0].clientY - startY);
        sheet.style.transform = `translateY(${dy}px)`;
    }, { passive: true });
    head.addEventListener('touchend', () => {
        sheet.style.transition = '';
        if (dy > 90) closeSheet();
        sheet.style.transform = '';
        startY = null;
    });
}

// --- Errors ------------------------------------------------------------------
function isNotImplemented(m) {
    return m.code === 'not_implemented' || /not[_ ]implemented/i.test(m.message || '');
}

function onError(m) {
    const id = m.request_id;
    const pending = id ? state.pending.get(id) : null;
    if (pending) state.pending.delete(id);

    if (pending && pending.kind === 'comments') {
        if (id === state.sheet.requestId) {
            clearTimeout(state.sheet.timer);
            showSheetError(isNotImplemented(m)
                ? 'Comments aren\'t supported for this platform on your host yet.'
                : (m.message || 'Could not load replies.'));
        }
        return;
    }
    const liveOpen = !$('live').hidden;
    if (liveOpen && (id === state.live.requestId || (!id && isNotImplemented(m)) || (pending && pending.kind === 'live'))) {
        clearTimeout(state.live.timer);
        if (isNotImplemented(m)) {
            showLiveMsg(`<h3>Live view is coming soon</h3><p>Your host can't stream its browser yet. For now, log in to <b>${esc(state.live.platform ? displayName(state.live.platform) : 'this platform')}</b> directly on the host machine — your feed will resume automatically.</p><button class="pill-btn" data-act="live-close">Got it</button>`);
        } else {
            showLiveMsg(`<h3>Live view unavailable</h3><p>${esc(m.message || 'Unknown error')}</p><button class="pill-btn" data-act="live-close">Close</button>`);
        }
        return;
    }
    toast(m.message || 'Something went wrong on the host.');
}

let toastTimer = null;
function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

// --- Live view (M4) ----------------------------------------------------------
function startLive(platform) {
    const live = state.live;
    live.platform = platform;
    live.requestId = rid('l');
    live.gotFrame = false;
    live.meta = null;
    state.pending.set(live.requestId, { kind: 'live', platform, sent: Date.now() });
    $('live-title').textContent = `Live view · ${displayName(platform)}`;
    $('live').hidden = false;
    document.body.classList.add('locked');
    const c = $('live-canvas');
    c.getContext('2d').clearRect(0, 0, c.width, c.height);
    showLiveMsg(`<div class="spinner"></div><div>Opening ${esc(displayName(platform))} on your host…</div>`);
    send({ type: 'live.start', platform, request_id: live.requestId });
    clearTimeout(live.timer);
    live.timer = setTimeout(() => {
        if (live.gotFrame || $('live').hidden) return;
        showLiveMsg(`<h3>No picture yet</h3><p>The host hasn't sent any frames. Check that it's running, or log in directly on the host machine.</p><button class="pill-btn" data-act="live-close">Close</button>`);
    }, LIVE_TIMEOUT_MS);
}

function stopLive() {
    const live = state.live;
    if (live.platform) send({ type: 'live.stop', platform: live.platform });
    if (live.requestId) state.pending.delete(live.requestId);
    clearTimeout(live.timer);
    live.platform = null;
    live.requestId = null;
    $('live').hidden = true;
    if ($('sheet').hidden) document.body.classList.remove('locked');
}

function showLiveMsg(html) {
    const el = $('live-msg');
    if (html == null) { el.hidden = true; el.innerHTML = ''; return; }
    el.innerHTML = html;
    el.hidden = false;
}

function handleBinary(buf) {
    if (!(buf instanceof ArrayBuffer) || buf.byteLength < 2) return;
    const bytes = new Uint8Array(buf);
    if (bytes[0] !== FRAME_TAG_JPEG) return;
    if ($('live').hidden) return;
    const blob = new Blob([bytes.subarray(1)], { type: 'image/jpeg' });
    // Keep only the newest frame while one is decoding.
    if (state.live.decoding) { state.live.nextFrame = blob; return; }
    drawFrame(blob);
}

function drawFrame(blob) {
    const live = state.live;
    live.decoding = true;
    const done = () => {
        live.decoding = false;
        const next = live.nextFrame;
        live.nextFrame = null;
        if (next) drawFrame(next);
    };
    const paint = (src, w, h) => {
        const c = $('live-canvas');
        if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
        c.getContext('2d').drawImage(src, 0, 0);
        if (!live.gotFrame) { live.gotFrame = true; clearTimeout(live.timer); showLiveMsg(null); }
    };
    if (window.createImageBitmap) {
        createImageBitmap(blob).then(bmp => { paint(bmp, bmp.width, bmp.height); bmp.close && bmp.close(); })
            .catch(() => {}).finally(done);
    } else {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => { paint(img, img.naturalWidth, img.naturalHeight); URL.revokeObjectURL(url); done(); };
        img.onerror = () => { URL.revokeObjectURL(url); done(); };
        img.src = url;
    }
}

function setupLiveInput() {
    const c = $('live-canvas');
    let start = null, lastY = 0, pendingDy = 0, flushTimer = null;
    const norm = (e) => {
        const r = c.getBoundingClientRect();
        return {
            x: Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width))),
            y: Math.min(1, Math.max(0, (e.clientY - r.top) / Math.max(1, r.height))),
        };
    };
    // Scale screen px → frame px so a swipe scrolls the same visual distance.
    const scale = () => c.width / Math.max(1, c.getBoundingClientRect().width);
    const flush = (pos) => {
        flushTimer = null;
        if (!pendingDy) return;
        send(Object.assign({ type: 'live.input', kind: 'wheel', dy: Math.round(pendingDy) }, pos || {}));
        pendingDy = 0;
    };
    c.addEventListener('pointerdown', (e) => {
        start = { x: e.clientX, y: e.clientY, moved: false, pos: norm(e) };
        lastY = e.clientY;
        c.setPointerCapture && c.setPointerCapture(e.pointerId);
    });
    c.addEventListener('pointermove', (e) => {
        if (!start) return;
        if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10) start.moved = true;
        if (!start.moved) return;
        pendingDy += (lastY - e.clientY) * scale(); // finger up → scroll down (positive dy)
        lastY = e.clientY;
        if (!flushTimer) { const pos = start.pos; flushTimer = setTimeout(() => flush(pos), 60); }
    });
    const end = (e) => {
        if (!start) return;
        if (!start.moved) {
            const p = norm(e);
            send({ type: 'live.input', kind: 'tap', x: +p.x.toFixed(4), y: +p.y.toFixed(4) });
        } else {
            clearTimeout(flushTimer);
            flush(start.pos);
        }
        start = null;
    };
    c.addEventListener('pointerup', end);
    c.addEventListener('pointercancel', () => { start = null; });
    c.addEventListener('wheel', (e) => {
        e.preventDefault();
        const p = norm(e);
        send({ type: 'live.input', kind: 'wheel', dy: Math.round(e.deltaY), x: +p.x.toFixed(4), y: +p.y.toFixed(4) });
    }, { passive: false });
}

// --- Wiring ------------------------------------------------------------------
function onClick(e) {
    const t = e.target.closest('[data-act]');
    if (!t) return;
    const act = t.dataset.act;
    const card = t.closest('.post');
    switch (act) {
        case 'why': {
            e.preventDefault();
            const box = card && card.querySelector('.why-box');
            if (!box) return;
            box.hidden = !box.hidden;
            t.setAttribute('aria-expanded', String(!box.hidden));
            return;
        }
        case 'comments':
            if (card) openComments(card.dataset.id);
            return;
        case 'video': {
            const v = t.parentNode.querySelector('video');
            if (v) toggleVideo(v);
            return;
        }
        case 'reveal': {
            const wrap = t.closest('.reply-collapsed');
            const inner = wrap.querySelector('.reply-inner');
            inner.hidden = !inner.hidden;
            wrap.classList.toggle('open', !inner.hidden);
            return;
        }
        case 'show-hidden': {
            const list = $('sheet-body').querySelector('.hidden-replies');
            if (list) { list.hidden = !list.hidden; }
            return;
        }
        case 'retry-comments':
            if (state.sheet.postId) requestThread(state.sheet.postId);
            return;
        case 'login':
            startLive(t.dataset.platform);
            return;
        case 'live-close':
            stopLive();
            return;
    }
}

function init() {
    if (!PR) {
        document.body.innerHTML = '<p style="padding:24px">Could not load post renderer.</p>';
        return;
    }
    document.addEventListener('click', onClick);
    $('sheet-close').addEventListener('click', closeSheet);
    $('sheet-backdrop').addEventListener('click', closeSheet);
    $('live-close').addEventListener('click', stopLive);
    setupSheetDrag();
    setupLiveInput();

    window.addEventListener('scroll', scheduleDemand, { passive: true });
    window.addEventListener('resize', scheduleDemand, { passive: true });
    window.addEventListener('online', reconnectNow);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') { reconnectNow(); scheduleDemand(); }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (!$('live').hidden) stopLive(); else if (!$('sheet').hidden) closeSheet();
    });
    renderMeta();
    connect();
}

window.PhoneClient = { state, computeDemand, connect, openComments, startLive, stopLive };

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
})();
