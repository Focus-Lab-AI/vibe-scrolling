/**
 * PostRenderer — shared HTML builder for Feed (raw) and AI Curation (filtered).
 *
 * Both pages render the same card shape: header · body · media · quoted · stats.
 * They differ in where posts come from and what extras they layer on top
 * (Feed adds replies toggle; Curated adds score pill + category + "why"
 * toggle). This module owns the structure; callers pass `opts` for
 * page-specific extras and a `mediaResolver` for URL prefixing.
 *
 * Load order: post-renderer.js first, then viewer.js / curated.js.
 */
(function () {
'use strict';

const PLATFORM_LINKS = {
    x:         { mention: h => `https://x.com/${h}`,                  hashtag: h => `https://x.com/hashtag/${h}` },
    threads:   { mention: h => `https://threads.net/@${h}`,           hashtag: h => `https://threads.net/search?q=%23${h}` },
    instagram: { mention: h => `https://instagram.com/${h}`,          hashtag: h => `https://instagram.com/explore/tags/${h}` },
    youtube:   { mention: h => `https://youtube.com/@${h}`,           hashtag: h => `https://youtube.com/results?search_query=%23${h}` },
};

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatText(text, platform) {
    const links = PLATFORM_LINKS[platform] || PLATFORM_LINKS.x;
    return (text || '')
        .replace(/&amp;/g, '&')
        .replace(/(https?:\/\/t\.co\/\S+)/g, '')
        .replace(/(https?:\/\/\S+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
        .replace(/@(\w+)/g, (_, h) => `<a href="${links.mention(h)}" target="_blank" rel="noopener">@${h}</a>`)
        .replace(/#(\w+)/g, (_, h) => `<a href="${links.hashtag(h)}" target="_blank" rel="noopener">#${h}</a>`)
        .trim();
}

function timeAgo(s) {
    if (!s) return '';
    if (/\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago/i.test(s)) return s;
    if (/ago$/i.test(s)) return s;
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    const diff = (Date.now() - d) / 1000;
    if (diff < 0)     return 'now';  // host/platform clock skew
    if (diff < 60)   return `${Math.floor(diff)}s`;
    if (diff < 3600)  return `${Math.floor(diff/60)}m`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtNum(n) {
    n = n || 0;
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
    return String(n);
}

// Body with read-more toggle (300-char threshold).
//
// Markup notes: pre-wrap lives on `.post-text` (inline span) not on
// `.post-body`, so the literal whitespace between sibling tags in this
// template doesn't render as blank lines. Read-more buttons are siblings,
// NOT inside the pre-wrap span, so trailing newlines in the post text
// don't push them to a new line.
function renderPostBody(text, platform, postId) {
    const TRUNCATE = 300;
    const full = formatText(text, platform);
    if (!text || text.length <= TRUNCATE) {
        return `<div class="post-body"><span class="post-text">${full}</span></div>`;
    }
    const short = formatText(text.substring(0, TRUNCATE).replace(/\s+\S*$/, ''), platform);
    const onclick = `onclick="PostRenderer.toggleReadMore('${postId}')"`;
    // Single-line template → no whitespace leaks when pre-wrap is active.
    // The space between </span> and <button> collapses to one via normal
    // HTML whitespace handling on the outer .post-body (which is NOT pre-wrap).
    return `<div class="post-body"><span class="post-text" id="short-${postId}">${short}…</span> <button class="read-more-btn" id="more-${postId}" ${onclick}>Read more</button><span class="post-text hidden" id="full-${postId}">${full}</span> <button class="read-more-btn hidden" id="less-${postId}" ${onclick}>Show less</button></div>`;
}

function toggleReadMore(postId) {
    // Flip all four elements — the short/full spans and their own buttons.
    for (const prefix of ['short', 'more', 'full', 'less']) {
        const el = document.getElementById(`${prefix}-${postId}`);
        if (el) el.classList.toggle('hidden');
    }
}

function renderQuoted(q, platform) {
    if (!q) return '';
    const links = PLATFORM_LINKS[platform] || PLATFORM_LINKS.x;
    const name = esc(q.author_name || q.author_handle || 'Unknown');
    const handle = q.author_handle
        ? `<span class="post-handle text-xs"><a href="${links.mention(q.author_handle)}" target="_blank" rel="noopener">@${esc(q.author_handle)}</a></span>`
        : '';
    const text = q.text ? `<div class="text-sm" style="line-height:1.4;margin-top:4px">${formatText(q.text, platform)}</div>` : '';
    return `<div class="quoted-post">
        <div class="quoted-post-header">
            <span class="font-semibold text-sm">${name}</span>${handle}
        </div>
        ${text}
    </div>`;
}

// Render media. For YouTube: iframe embed via platform_data.embed_url.
// Otherwise: resolver(path) → URL for each local_media_path; single image/video
// or carousel. Fallback: remote URLs from media_urls / video_urls.
function renderMedia(post, platform, resolver) {
    const pd = post.platform_data || {};
    if (platform === 'youtube' && pd.embed_url) {
        // enablejsapi=1 → YT IFrame API can play/pause via JS on hover.
        // mute=1       → autoplay is allowed without a user gesture.
        // playsinline  → mobile Safari doesn't steal the screen.
        const sep = pd.embed_url.includes('?') ? '&' : '?';
        const src = `${pd.embed_url}${sep}enablejsapi=1&mute=1&playsinline=1`;
        return `<div class="post-embed yt-hover-embed"><iframe src="${esc(src)}" allowfullscreen loading="lazy" frameborder="0"></iframe></div>`;
    }

    const localPaths = post.local_media_paths || [];
    const fallbackUrls = [...(post.media_urls || []), ...(post.video_urls || [])];

    const resolve = (path, fallback) => {
        if (path && resolver) return resolver(path);
        return fallback || path || '';
    };

    const mediaElement = (src, hint) => {
        if (!src) return '<div class="empty-state">Media unavailable</div>';
        const lower = (hint || src).toLowerCase();
        const isVideo = /\.(mp4|mov|m4v|webm)(\?|$)/.test(lower) || lower.includes('_v0') || lower.includes('_v1');
        if (isVideo) {
            return `<video src="${esc(src)}" muted loop preload="metadata" controls></video>`;
        }
        return `<img src="${esc(src)}" loading="lazy" onclick="PostRenderer.openLightbox(this.src)">`;
    };

    // Prefer local paths (resolver-aware); fall back to remote URLs.
    const useLocals = localPaths.length > 0;
    const sources = useLocals ? localPaths : fallbackUrls;
    const resolvedSources = useLocals
        ? localPaths.map((p, i) => ({ src: resolve(p, fallbackUrls[i]), hint: p }))
        : fallbackUrls.map(u => ({ src: u, hint: u }));

    if (resolvedSources.length === 0) return '';
    if (resolvedSources.length === 1) {
        return `<div class="post-media">${mediaElement(resolvedSources[0].src, resolvedSources[0].hint)}</div>`;
    }
    const slides = resolvedSources.map((s, i) =>
        `<div class="post-carousel-item">${mediaElement(s.src, s.hint)}<span class="post-carousel-badge">${i + 1}/${resolvedSources.length}</span></div>`
    );
    return `<div class="post-carousel viewer-carousel">${slides.join('')}</div>`;
}

/**
 * Build the full .post card HTML.
 *
 * opts (all optional):
 *   platform:          override post.platform (defaults to post.platform || 'x')
 *   postId:            used for toggle ids (defaults to post.id or random)
 *   showPlatformBadge: whether to render a platform-color badge in header (default true)
 *   mediaResolver:     (path) => URL  — resolves local_media_paths to fetchable URLs
 *   headerExtras:      HTML appended inside .post-time span (e.g. score pill, category badge)
 *   statsExtras:       HTML appended inside .post-stats (e.g. "why" toggle)
 *   afterStats:        HTML rendered after .post-stats (e.g. replies section, filter reason body)
 *   dataAttrs:         object — flattened to data-* on .post root (e.g. { category: 'goal' })
 */
function renderPost(post, opts) {
    opts = opts || {};
    const platform = opts.platform || post.platform || 'x';
    const postId = opts.postId || post.id || Math.random().toString(36).slice(2);
    const showPlatformBadge = opts.showPlatformBadge !== false;
    const resolver = opts.mediaResolver;
    const links = PLATFORM_LINKS[platform] || PLATFORM_LINKS.x;

    const authorDisplay = esc(post.author_name || post.author_handle || 'Unknown');
    const handleHtml = post.author_handle
        ? `<span class="post-handle"><a href="${links.mention(post.author_handle)}" target="_blank" rel="noopener">@${esc(post.author_handle)}</a></span>`
        : '';

    const pd = post.platform_data || {};
    const shortBadge = pd.type === 'short' ? '<span class="badge-short">Short</span>' : '';
    const adBadge = post.is_ad ? '<span class="badge-ad">Ad</span>' : '';
    const platformBadge = showPlatformBadge
        ? `<span class="badge badge-${platform} mr-2">${platform}</span>`
        : '';

    const repostLabel = platform === 'x' ? 'Retweeted' : 'Reposted';
    const repostStatLabel = platform === 'x' ? 'RT' : 'reposts';
    const repostBadge = post.is_repost
        ? `<div class="repost-label">${repostLabel} by @${esc(post.original_author || 'unknown')}</div>` : '';

    const bodyHtml = renderPostBody(post.text || '', platform, postId);
    const mediaHtml = renderMedia(post, platform, resolver);
    const quotedHtml = renderQuoted(post.quoted_post, platform);

    const timeStr = timeAgo(post.created_at);
    const timeDisplay = post.url
        ? `<a href="${esc(post.url)}" target="_blank" rel="noopener">${timeStr}</a>`
        : timeStr;

    const dataAttrs = opts.dataAttrs || {};
    const dataAttrStr = Object.entries(dataAttrs)
        .map(([k, v]) => `data-${esc(k)}="${esc(v)}"`).join(' ');

    const stats = `<div class="post-stats">
        <span>replies ${fmtNum(post.replies)}</span>
        <span>${repostStatLabel} ${fmtNum(post.reposts)}</span>
        <span>likes ${fmtNum(post.likes)}</span>
        ${post.quotes ? `<span>quotes ${fmtNum(post.quotes)}</span>` : ''}
        ${opts.statsExtras || ''}
    </div>`;

    return `<div class="post" data-id="${esc(post.id || '')}" ${dataAttrStr}>
        ${repostBadge}
        <div class="post-header">
            <div>
                ${adBadge}
                ${platformBadge}
                <span class="post-author">${authorDisplay}</span>
                ${handleHtml}${shortBadge}
            </div>
            <span class="post-time">
                ${opts.headerExtras || ''}
                ${timeDisplay}
            </span>
        </div>
        ${bodyHtml}
        ${mediaHtml}
        ${quotedHtml}
        ${stats}
        ${opts.afterStats || ''}
    </div>`;
}

// Observers — bound to a feed container so multiple pages can own their own.
function setupVideoAutoplay(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return null;
    const videos = container.querySelectorAll('video');
    if (videos.length === 0) return null;
    const io = new IntersectionObserver(entries => {
        for (const e of entries) {
            if (e.isIntersecting) e.target.play().catch(() => {});
            else                  e.target.pause();
        }
    }, { threshold: 0.5 });
    videos.forEach(v => io.observe(v));
    return io;
}

function setupCarouselDrag(rootSelector) {
    document.querySelectorAll(rootSelector || '.viewer-carousel').forEach(carousel => {
        // Skip if we've already wired this carousel (re-renders call us again).
        if (carousel.dataset.dragBound === '1') return;
        carousel.dataset.dragBound = '1';

        let down = false;
        let startX = 0, startScroll = 0, dragged = false;

        const endDrag = () => {
            if (!down) return;
            down = false;
            // Re-enable scroll-snap a tick later so the momentum-rest position
            // can snap without fighting the final mouseup frame.
            setTimeout(() => carousel.classList.remove('dragging'), 50);
        };

        carousel.addEventListener('mousedown', e => {
            down = true; dragged = false;
            // Use clientX (viewport-relative) + current scroll as the anchor —
            // simpler and unaffected by the carousel's offsetLeft changes
            // during layout (which caused the prior "jump").
            startX = e.clientX;
            startScroll = carousel.scrollLeft;
            carousel.classList.add('dragging');
            e.preventDefault();
        });
        carousel.addEventListener('mouseleave', endDrag);
        carousel.addEventListener('mouseup', endDrag);
        window.addEventListener('mouseup', endDrag);  // catch release outside the element
        carousel.addEventListener('mousemove', e => {
            if (!down) return;
            e.preventDefault();
            const delta = e.clientX - startX;
            // 1:1 tracking — drag 100px right, scroll 100px left.
            carousel.scrollLeft = startScroll - delta;
            // 10px of slop so a normal click's jitter isn't read as a drag —
            // below this the click still falls through to openLightbox.
            if (Math.abs(delta) > 10) dragged = true;
        });
        // Swallow the click that would otherwise fire after a drag.
        carousel.addEventListener('click', e => {
            if (dragged) { e.stopPropagation(); e.preventDefault(); }
        }, true);
    });
}

function openLightbox(src) {
    const lb = document.createElement('div');
    lb.className = 'lightbox';
    lb.innerHTML = `<button class="lightbox-close">&times;</button><img src="${esc(src)}">`;
    lb.onclick = () => lb.remove();
    document.body.appendChild(lb);
}

// Wire YouTube embeds to play-on-hover / pause-on-leave, just like
// YouTube's own homepage feed. Loads the YT IFrame API lazily (only if
// there's at least one YouTube post on screen) and binds a YT.Player to
// each .yt-hover-embed iframe so we can call playVideo / pauseVideo.
let _ytApiPromise = null;
function _loadYTApi() {
    if (_ytApiPromise) return _ytApiPromise;
    _ytApiPromise = new Promise(resolve => {
        if (window.YT && window.YT.Player) { resolve(); return; }
        // YT fires a single global when it's ready. Chain onto any prior one.
        const prev = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = () => {
            if (prev) { try { prev(); } catch (_) {} }
            resolve();
        };
        if (!document.querySelector('script[data-yt-iframe-api]')) {
            const tag = document.createElement('script');
            tag.src = 'https://www.youtube.com/iframe_api';
            tag.setAttribute('data-yt-iframe-api', '1');
            document.head.appendChild(tag);
        }
    });
    return _ytApiPromise;
}

function setupYoutubeHover(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const wrappers = container.querySelectorAll('.yt-hover-embed');
    if (wrappers.length === 0) return;

    _loadYTApi().then(() => {
        wrappers.forEach(wrap => {
            // Re-entrance guard — renderPosts can run many times as the user
            // changes sorts / platforms; only bind once per iframe.
            if (wrap.dataset.ytHoverBound === '1') return;
            wrap.dataset.ytHoverBound = '1';

            const iframe = wrap.querySelector('iframe');
            if (!iframe) return;
            if (!iframe.id) iframe.id = 'yt-' + Math.random().toString(36).slice(2);

            let player;
            try {
                player = new YT.Player(iframe.id, {
                    events: {
                        onReady: (e) => { try { e.target.mute(); } catch (_) {} },
                    },
                });
            } catch (e) {
                return;
            }

            wrap.addEventListener('mouseenter', () => {
                try { player.playVideo && player.playVideo(); } catch (_) {}
            });
            wrap.addEventListener('mouseleave', () => {
                try { player.pauseVideo && player.pauseVideo(); } catch (_) {}
            });
        });
    });
}

window.PostRenderer = {
    renderPost,
    renderPostBody,
    renderMedia,
    renderQuoted,
    formatText,
    timeAgo,
    fmtNum,
    toggleReadMore,
    setupVideoAutoplay,
    setupCarouselDrag,
    setupYoutubeHover,
    openLightbox,
    PLATFORM_LINKS,
};

})();
