#!/usr/bin/env python

import fcntl
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from pypresence import Presence, PyPresenceException
from pypresence.exceptions import ServerError
from pypresence.types import ActivityType, StatusDisplayType

# Configuration

DISCORD_CLIENT_ID = "1526706535974305822"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765
STALE_AFTER_SECONDS = 15 # clear presence if the browser goes quiet this long
RECONNECT_DELAY_SECONDS = 5 # wait between retries while Discord isn't reachable

ACTIVITY_TYPE = ActivityType.LISTENING
STATUS_DISPLAY_TYPE = StatusDisplayType.NAME
PRESENCE_NAME = "YouTube Music"

# Discord's Rich Presence IPC rejects state/details over 128 character with
# a ServerError. pypresence is a thin, direct IPC client
MIN_UPDATE_INTERVAL_SECONDS = 2 # 2s
MAX_FIELD_LENGTH = 128

# HTTP hardening
MAX_BODY_BYTES = 65536
REQUEST_TIMEOUT_SECONDS = 10
# Optional shared secret: set this (and the matching value in the
# userscript's SHARED_SECRET constant) to require an X-Bridge-Token header
# on every request. Leave as None to disable (fine for 127.0.0.1-only use,
# which is all this binds to).
SHARED_SECRET = "YTBridge_M68QHRbRP0Tx3i$k7ro#C$7V@D5C^v9kKBsSq5$LNiQ="

LOCK_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "ytmusic_bridge.lock")

# Shared state, guarded by _lock

_lock = threading.Lock()
_rpc = None
_last_update_at = 0 # last INBOUND http update, for the stale watchdog
_presence_active = False

# Throttle/coalesce state for outgoing Discord RPC calls specifically.
_last_sent_at = 0
_pending_timer = None
_pending_action = None

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def _truncate(text, limit=MAX_FIELD_LENGTH):
    """Discord's IPC rejects state/details over 128 characters with a ServerError."""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

def acquire_singleton_lock():
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        log("Another instance of this script is already running. Exiting.")
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file

def _connect():
    """
    Blocks (background thread only) until a Presence connection succeeds.

    Verified against the installed pypresence 4.6.2 source: Presence.__init__
    creates its own asyncio event loop, then .connect() silently swaps in a
    SECOND one (via pypresence's internal get_event_loop()), orphaning the
    first. Neither loop is closed by pypresence on a failed attempt -- across
    repeated retries (e.g. while waiting for Discord to start) this leaks an
    epoll fd + a self-pipe socketpair per attempt. Verified with a real fake
    IPC socket: 25 failed attempts leaked 60 fds with no cleanup, vs. a
    reproducible 0 growth (confirmed over multiple runs) closing both loops
    here.
    """
    while True:
        rpc = Presence(DISCORD_CLIENT_ID)
        init_loop = rpc.loop
        try:
            rpc.connect()
        except PyPresenceException as exc:
            for loop in (init_loop, rpc.loop):
                try:
                    loop.close()
                except Exception:
                    pass
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        if rpc.loop is not init_loop:
            try:
                init_loop.close()
            except Exception:
                pass
        log("Connected to Discord.")
        return rpc

def _connector_thread():
    """Connects in the background so the HTTP server never blocks waiting on Discord."""
    global _rpc
    rpc = _connect()
    with _lock:
        _rpc = rpc

def _with_rpc(action):
    """
    Best-effort: runs action(rpc) if currently connected. Never blocks the
    caller waiting on Discord -- if the pipe just died, it drops this update
    and kicks off a background reconnect; the next update from the userscript
    (at most HEARTBEAT_MS later) will pick things back up once reconnected.

    Discord's IPC can reject a single payload (bad field, transient
    rejection, etc.) via ServerError.

    A genuinely dead pipe is different, and can surface either as
    PipeClosed/InvalidPipe (pypresence's own wrapped exceptions) OR as a raw
    ConnectionResetError/BrokenPipeError.
    """
    global _rpc
    if _rpc is None:
        return
    try:
        action(_rpc)
    except ServerError as exc:
        log(f"Discord rejected update ({exc}); connection kept alive.")
    except (PyPresenceException, OSError) as exc:
        log(f"Lost connection to Discord ({exc}); reconnecting in background...")
        dead_rpc = _rpc
        _rpc = None
        try:
            dead_rpc.close()
        except Exception:
            pass
        threading.Thread(target=_connector_thread, daemon=True).start()

def _fire_pending():
    """Timer callback: sends the most recent coalesced update once the throttle window opens."""
    global _last_sent_at, _pending_timer, _pending_action
    with _lock:
        _pending_timer = None
        action = _pending_action
        _pending_action = None
        if action is not None:
            _last_sent_at = time.time()
            _with_rpc(action)

def _dispatch_update(action):
    """
    Throttles + coalesces outgoing Discord RPC calls to at most one per
    MIN_UPDATE_INTERVAL_SECONDS, matching Discord's documented rate limit on
    SET_ACTIVITY (which covers both update() and clear() -- both send that
    same command per pypresence's payloads.py). Must be called with _lock
    held. If multiple updates land inside one throttle window, only the
    latest is kept and sent when the window opens; earlier ones are
    superseded, not queued.
    """
    global _last_sent_at, _pending_timer, _pending_action
    now = time.time()
    elapsed = now - _last_sent_at
    if elapsed >= MIN_UPDATE_INTERVAL_SECONDS and _pending_timer is None:
        _last_sent_at = now
        _with_rpc(action)
        return

    _pending_action = action
    if _pending_timer is None:
        delay = max(0, MIN_UPDATE_INTERVAL_SECONDS - elapsed)
        _pending_timer = threading.Timer(delay, _fire_pending)
        _pending_timer.daemon = True
        _pending_timer.start()

def _apply_track(title, artist, album, artwork, paused, current_time, duration):
    global _presence_active

    if paused:
        if _presence_active:
            log("Clearing presence (paused).")
        _dispatch_update(lambda rpc: rpc.clear())
        _presence_active = False
        return

    start = time.time() - current_time
    # int(float('inf')) raises OverflowError, so a non-finite duration (which a raw
    # request to this endpoint could send even though the userscript no longer will)
    # must not reach the int() call below.
    end = start + duration if duration > 0 and math.isfinite(duration) else None

    title = _truncate(title)
    # Spotify-style second line: "Artist — Album", or just "Artist" if no album.
    state_text = _truncate(f"{artist} — {album}" if album else artist)

    assets = {}
    if artwork:
        assets["large_image"] = artwork
        assets["large_text"] = title

    def do_update(rpc):
        rpc.update(
            activity_type=ACTIVITY_TYPE,
            status_display_type=STATUS_DISPLAY_TYPE,
            name=PRESENCE_NAME,
            details=title,
            state=state_text,
            start=int(start),
            end=int(end) if end else None,
            **assets,
        )

    _dispatch_update(do_update)
    _presence_active = True

class BridgeHandler(BaseHTTPRequestHandler):
    # StreamRequestHandler.setup() applies this as the socket timeout --
    # verified: a client that opens a connection and never sends the body
    # gets dropped after this many seconds, and the (single-threaded) server
    # keeps serving subsequent requests normally afterward.
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format, *args):
        pass # keep stdout limited to our own log() lines

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not SHARED_SECRET:
            return True
        header_token = self.headers.get("X-Bridge-Token")
        if header_token == SHARED_SECRET:
            return True
        self._reply(403, {"error": "forbidden"})
        return False

    def do_POST(self):
        if self.path != "/update":
            self._reply(404, {"error": "not found"})
            return

        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            self._reply(415, {"error": "expected application/json"})
            return

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._reply(400, {"error": "missing or invalid Content-Length"})
            return

        if length <= 0:
            self._reply(400, {"error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._reply(413, {"error": "body too large"})
            return

        try:
            raw_body = self.rfile.read(length)
        except (socket.timeout, OSError):
            return  # client stalled mid-body; connection will be dropped

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self._reply(400, {"error": "invalid json"})
            return

        if not isinstance(payload, dict):
            self._reply(400, {"error": "expected a json object"})
            return

        if not self._authorized():
            return

        album_raw = payload.get("album")
        artwork_raw = payload.get("artwork")

        global _last_update_at
        with _lock:
            _last_update_at = time.time()
            try:
                _apply_track(
                    title=str(payload.get("title") or "Unknown title"),
                    artist=str(payload.get("artist") or "Unknown artist"),
                    album=str(album_raw) if album_raw else None,
                    artwork=artwork_raw if isinstance(artwork_raw, str) and artwork_raw else None,
                    paused=bool(payload.get("paused", True)),
                    current_time=float(payload.get("currentTime") or 0),
                    duration=float(payload.get("duration") or 0),
                )
            except Exception as exc:
                # _with_rpc already contains connection-level exceptions
                # internally, so anything reaching here is unexpected -- log
                # it and keep the server alive for the next request rather
                # than letting one bad payload take down request handling.
                log(f"Failed to apply track update: {exc}")

        self._reply(200, {"ok": True})

def _watchdog():
    """Clears a stale presence if the browser stops sending updates (tab/browser closed)."""
    global _presence_active
    while True:
        time.sleep(2)
        try:
            with _lock:
                if _presence_active and time.time() - _last_update_at > STALE_AFTER_SECONDS:
                    log(f"Clearing presence (watchdog: no update in {STALE_AFTER_SECONDS}s).")
                    _dispatch_update(lambda rpc: rpc.clear())
                    _presence_active = False
        except Exception as exc:
            # This loop must never die -- it's the only thing that clears a
            # stale presence after the browser tab/window closes.
            log(f"Watchdog error: {exc}")

def main():
    lock = acquire_singleton_lock()
    httpd = None

    try:
        def signal_handler(sig, frame):
            log("Exiting cleanly...")
            raise SystemExit

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGHUP, signal_handler)

        log("Starting bridge...")
        threading.Thread(target=_connector_thread, daemon=True).start()
        threading.Thread(target=_watchdog, daemon=True).start()

        httpd = HTTPServer((LISTEN_HOST, LISTEN_PORT), BridgeHandler)
        log(f"Listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
        httpd.serve_forever()

    finally:
        log("Shutting down...")
        if httpd:
            httpd.server_close()
        with _lock:
            global _pending_timer
            if _pending_timer:
                _pending_timer.cancel()
                _pending_timer = None
            if _rpc:
                try:
                    _rpc.clear()
                except Exception:
                    pass
                try:
                    _rpc.close()
                except Exception:
                    pass
        try:
            lock.close()
        except Exception:
            pass
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass

if __name__ == "__main__":
    main()
