// ==UserScript==
// @name         YT Music RPC Bridge
// @namespace    local.ytmusic-discord-bridge
// @version      1.2
// @description  Sends now-playing info from YouTube Music to a local Python bridge for RPC
// @match        https://music.youtube.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==
(function () {
    'use strict';
    const BRIDGE_URL = 'http://127.0.0.1:8765/update';
    const POLL_INTERVAL_MS = 3000;
    const HEARTBEAT_MS = 15000; // force a resync at least this often (catches seeks/drift)

    // Verify its most likely from our code
    const SHARED_SECRET = "YTBridge_M68QHRbRP0Tx3i$k7ro#C$7V@D5C^v9kKBsSq5$LNiQ=";

    let lastSent = { title: null, artist: null, album: null, paused: null };
    let lastSentAt = 0;

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
            onerror: () => {}, // bridge script probably isn't running; ignore
            ontimeout: () => {},
        });
    }

    function tick() {
        const video = document.querySelector('video');
        const metadata = navigator.mediaSession && navigator.mediaSession.metadata;
        if (!video || !metadata) return;
        const title = metadata.title || 'Unknown title';
        const artist = metadata.artist || 'Unknown artist';
        const album = metadata.album || null;
        const paused = video.paused;
        const changed = title !== lastSent.title || artist !== lastSent.artist ||
                        album !== lastSent.album || paused !== lastSent.paused;
        const dueForHeartbeat = Date.now() - lastSentAt > HEARTBEAT_MS;
        if (!changed && !dueForHeartbeat) return;
        post({
            title,
            artist,
            album,
            artwork: pickBestArtwork(metadata.artwork),
            paused,
            currentTime: video.currentTime || 0,
            duration: video.duration || 0,
        });
        lastSent = { title, artist, album, paused };
        lastSentAt = Date.now();
    }

    setInterval(tick, POLL_INTERVAL_MS);
})();
