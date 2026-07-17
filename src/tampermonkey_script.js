// ==UserScript==
// @name         YT Music RPC Bridge
// @namespace    local.ytmusic-discord-bridge
// @version      1.4
// @description  Sends now-playing info from YouTube Music to a local Python bridge for RPC
// @match        https://music.youtube.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==
(function () {
    'use strict';

    const BRIDGE_URL = 'http://127.0.0.1:8765/update';
    const POLL_INTERVAL_MS = 3000; // safety-net poll for anything the listeners below miss
    const HEARTBEAT_MS = 5000; // force a resync at least this often -- combined with the bridge's
                                 // own STALE_AFTER_SECONDS watchdog, this is what clears presence
                                 // after the tab closes (there's no explicit close signal)
    const MIN_SEND_INTERVAL_MS = 2000; // mirrors the bridge's own SET_ACTIVITY rate limit

    // Must match SHARED_SECRET in ytmusic_bridge.py.
    const SHARED_SECRET = "YTBridge_M68QHRbRP0Tx3i$k7ro#C$7V@D5C^v9kKBsSq5$LNiQ=";

    let lastSent = { title: null, artist: null, album: null, paused: null };
    let lastSentAt = 0; // last time a request actually went out -- throttle + heartbeat reference
    let pendingTimer = null;

    function pickBestArtwork(artworkList) {
        if (!artworkList || !artworkList.length) return null;
        let best = null;
        let bestArea = -1;
        for (const art of artworkList) {
            const match = /(\d+)x(\d+)/.exec(art.sizes || '');
            const area = match ? parseInt(match[1], 10) * parseInt(match[2], 10) : 0;
            if (area >= bestArea) {
                bestArea = area;
                best = art;
            }
        }
        return best ? best.src : null;
    }

    function post(payload) {
        const headers = { 'Content-Type': 'application/json' };
        if (SHARED_SECRET) {
            headers['X-Bridge-Token'] = SHARED_SECRET;
        }
        GM_xmlhttpRequest({
            method: 'POST',
            url: BRIDGE_URL,
            headers,
            data: JSON.stringify(payload),
            timeout: 3000,
            onerror: () => {}, // bridge probably isn't running; ignore
            ontimeout: () => {},
        });
    }

    // Single source of truth for reading the current track off the page. Used by both
    // evaluate() (change detection) and buildPayload() (outgoing data) so the two can't
    // drift apart -- e.g. differing "Unknown title" fallbacks.
    function readMetadata() {
        const video = document.querySelector('video');
        const metadata = navigator.mediaSession && navigator.mediaSession.metadata;
        if (!video || !metadata) return null;
        return {
            video,
            title: metadata.title || 'Unknown title',
            artist: metadata.artist || 'Unknown artist',
            album: metadata.album || null,
            artwork: pickBestArtwork(metadata.artwork),
        };
    }

    // Builds the payload from live state. Called at the moment a request actually goes
    // out (never earlier), so a send that got deferred by the throttle below still
    // reports where playback really is *now*, not where it was when the triggering
    // event first fired.
    function buildPayload() {
        const state = readMetadata();
        if (!state) return null;
        return {
            title: state.title,
            artist: state.artist,
            album: state.album,
            artwork: state.artwork,
            paused: state.video.paused,
            currentTime: state.video.currentTime || 0,
            // video.duration is Infinity or NaN while YT Music's MediaSource-backed
            // player hasn't resolved the real track length yet (normal for the first
            // moment or two after a track change). `|| 0` doesn't catch Infinity since
            // Infinity is truthy -- send 0 (bridge/Discord already treat 0 as "unknown")
            // instead of a non-finite number.
            duration: Number.isFinite(state.video.duration) ? state.video.duration : 0,
        };
    }

    function sendNow() {
        const payload = buildPayload();
        if (!payload) {
            // Video/metadata weren't available at the moment the throttle window opened
            // (e.g. mid track-swap). Reset lastSent so the next evaluate() treats this as
            // a fresh change and retries, instead of silently dropping the update.
            lastSent = { title: null, artist: null, album: null, paused: null };
            return;
        }
        lastSentAt = Date.now();
        post(payload);
    }

    // Throttles outgoing requests to at most one per MIN_SEND_INTERVAL_MS. Anything that
    // arrives inside an open window is coalesced into a single pending timer rather than
    // dropped -- when it fires, sendNow() reads fresh state, so a burst (e.g. dragging the
    // seek bar) collapses into one request carrying the final position, not the first one.
    function requestSend() {
        const now = Date.now();
        const elapsed = now - lastSentAt;
        if (elapsed >= MIN_SEND_INTERVAL_MS && pendingTimer === null) {
            sendNow();
            return;
        }
        if (pendingTimer === null) {
            const delay = Math.max(0, MIN_SEND_INTERVAL_MS - elapsed);
            pendingTimer = setTimeout(() => {
                pendingTimer = null;
                sendNow();
            }, delay);
        }
    }

    // Decides whether current state is worth sending. force=true skips the change check --
    // used for seeks, where currentTime moves but title/artist/album/paused all stay
    // identical, so the normal check would otherwise miss it until the next heartbeat.
    function evaluate(force) {
        const state = readMetadata();
        if (!state) return;

        const paused = state.video.paused;
        const changed = state.title !== lastSent.title || state.artist !== lastSent.artist ||
                        state.album !== lastSent.album || paused !== lastSent.paused;
        const dueForHeartbeat = Date.now() - lastSentAt > HEARTBEAT_MS;

        if (!changed && !force && !dueForHeartbeat) return;

        lastSent = { title: state.title, artist: state.artist, album: state.album, paused };
        requestSend();
    }

    // Delegated on document with capture:true. 'seeking'/'play'/'pause'/'loadedmetadata'
    // don't bubble, but capture-phase listeners still see them on the way down to their
    // target regardless -- so this keeps working even when YT Music swaps in a new
    // <video> element on track change, with nothing to re-attach.
    document.addEventListener('seeking', () => evaluate(true), true);
    document.addEventListener('play', () => evaluate(false), true);
    document.addEventListener('pause', () => evaluate(false), true);
    document.addEventListener('loadedmetadata', () => evaluate(false), true);
    // MediaSource-backed playback (what YT Music uses) commonly reports duration as
    // Infinity/NaN right after a track change, then corrects it once enough of the
    // stream has buffered -- 'durationchange' fires exactly then. force=true because
    // title/artist/album/paused haven't changed at that point, so the normal check
    // would otherwise miss it and leave Discord showing no length until the next
    // heartbeat (up to 15s later) -- that's the "length doesn't show" bug.
    document.addEventListener('durationchange', () => evaluate(true), true);

    // Catch whatever's already playing at inject time (e.g. script loads mid-song on a
    // page refresh) instead of waiting up to POLL_INTERVAL_MS for the first tick.
    evaluate(false);
    setInterval(() => evaluate(false), POLL_INTERVAL_MS);

    // No explicit tab-close signal: pagehide/beforeunload-based detection (sendBeacon,
    // GM_xmlhttpRequest) proved unreliable in practice, and beforeunload specifically
    // disables Firefox's back-forward cache for every page load, not just the one being
    // closed. The bridge's own STALE_AFTER_SECONDS watchdog already clears a stale
    // presence once updates stop arriving, so closing the tab is handled there instead
    // -- at the cost of a delay up to STALE_AFTER_SECONDS after the last heartbeat,
    // rather than an instant signal.

    // If the page is restored from the back-forward cache (navigated away and back,
    // rather than actually closed), no updates were sent while it was cached, so the
    // bridge's watchdog may have cleared the presence in the meantime. Force a fresh
    // resync so playback comes back instead of staying cleared until the next heartbeat.
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) evaluate(true);
    });
})();
