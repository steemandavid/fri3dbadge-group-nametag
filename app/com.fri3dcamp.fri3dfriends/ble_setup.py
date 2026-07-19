# ble_setup.py — Web-Bluetooth phone-setup GATT service for the Fri3d badge.
#
# Replaces the old WiFi web portal (web_portal.py). At Fri3d Camp the badges join
# a separate SSID/subnet, so a phone can never reach the badge's HTTP portal by
# IP. Instead a static Web-Bluetooth page (docs/setup/index.html, served from
# GitHub Pages) talks GATT straight to the badge — zero network required.
#
# Same two-part split as ble_proximity.py / contact_exchange.py:
#
#   1. PURE FUNCTIONS / CLASSES (sanitize_config, ChunkAssembler, the contacts
#      pager, AuthState, badge_id/build_info/adv builders). NO dependency on
#      bluetooth/mpos/asyncio — unit-tested off-device (tests/test_ble_setup.py).
#      Importing this module on a host must work.
#
#   2. The SetupService radio wrapper (a connectable GATT server that a phone
#      browser writes config to / reads contacts from). It imports `bluetooth`
#      LAZILY. Crucially, its GATT service is registered TOGETHER WITH the
#      contact-exchange service in ONE gatts_register_services call (NimBLE will
#      only accept that call once per power-on), via ContactExchange.ensure_radio.
#      See DESIGN.md "BLE setup" and Implementation_Plan_BLE_Setup_20260716.md.
#
# The radio half needs a phone/desktop-Chrome (or tools/setup_client.py via
# bleak) round-trip to fully verify; the pure half is fully covered off-device.

try:
    import utime as time
except ImportError:                      # host/dev
    import time

import os

# ---------------------------------------------------------------------------
# Constants — protocol
# ---------------------------------------------------------------------------

# Custom 128-bit UUIDs for the setup GATT service (same Nordic-UART-derived base
# as the exchange service, next block up: exchange is 6e4000_1x_, setup is _2x_).
SETUP_SVC    = "6e400020-b5a3-f393-e0a9-e50e24dcca9e"
AUTH_CHR     = "6e400021-b5a3-f393-e0a9-e50e24dcca9e"   # WRITE: ascii 4-digit code
INFO_CHR     = "6e400022-b5a3-f393-e0a9-e50e24dcca9e"   # READ:  info/config JSON
CFG_CHR      = "6e400023-b5a3-f393-e0a9-e50e24dcca9e"   # WRITE: chunked config JSON
STATUS_CHR   = "6e400024-b5a3-f393-e0a9-e50e24dcca9e"   # READ+NOTIFY: last-op result
CONTACTS_CHR = "6e400025-b5a3-f393-e0a9-e50e24dcca9e"   # READ:  one page of contacts.json
CTLOFF_CHR   = "6e400026-b5a3-f393-e0a9-e50e24dcca9e"   # WRITE: u16-LE offset for next read

# Handle order returned by gatts_register_services for the setup service. MUST
# match the characteristic order in SetupService.service_tuple().
SETUP_CHR_ORDER = ("auth", "info", "cfg", "status", "contacts", "ctloff")

INFO_VERSION = 1

PIN_DIGITS = 4
MAX_FAILS = 5                   # wrong codes before a lockout
LOCKOUT_MS = 60000             # lockout duration (also rotates the code)

MAX_CFG_BYTES = 2048           # cap on a reassembled config payload
CONTACTS_PAGE = 490            # bytes per contacts read page (fits one ATT read at
                               # the negotiated MTU 515; fewer round-trips = fewer
                               # chances for the link to drop mid-transfer)
CONTACTS_HEADER_OFFSET = 0xFFFF  # client writes this offset to fetch the page header

# After a save, KEEP the session alive (so the phone can reload contacts / make
# more edits) until the phone disconnects. This is only a safety cap so a badge
# whose phone vanished without a clean disconnect still hands the radio back to
# the proximity beacon eventually.
POST_SAVE_MAX_MS = 60000       # end a saved session this long after the save (safety net)
# Configured-badge setup window. The window is an *idle* timeout: any GATT
# activity (auth, config write, contacts-page request) resets it, so an active
# friends-list transfer or edit never times out mid-flight. SETUP_ABS_CAP_MS is
# an absolute wall-clock backstop so a forgotten open window still hands the
# radio back to the proximity beacon eventually.
SETUP_WINDOW_MS = 120000       # idle timeout for a configured-badge window (2 min)
SETUP_ABS_CAP_MS = 600000      # absolute cap on a window session (10 min)
SESSION_TICK_MS = 30           # setup session poll cadence (matches the app TICK_MS)
ADV_INTERVAL_US = 100000       # connectable advertising interval

# STATUS codes (ascii, read/notified on STATUS_CHR).
ST_IDLE = "idle"
ST_AUTH_OK = "auth_ok"
ST_AUTH_FAIL = "auth_fail"
ST_LOCKED = "locked"
ST_AUTH_REQUIRED = "auth_required"
ST_OK = "ok"
ST_INVALID = "invalid"
ST_TOO_LARGE = "too_large"


# ---------------------------------------------------------------------------
# small time/random helpers (host-safe: fall back off ticks_ms)
# ---------------------------------------------------------------------------

def _now():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _time_add(t, delta):
    if hasattr(time, "ticks_add"):
        return time.ticks_add(t, delta)
    return t + delta


def _elapsed(since, now=None):
    n = now if now is not None else _now()
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(n, since)
    return n - since


def _rand_int(n):
    """Return a random int in [0, n) using os.urandom (falls back to time)."""
    try:
        b = os.urandom(4)
        v = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    except Exception:
        v = int(time.ticks_ms()) if hasattr(time, "ticks_ms") else 0
    return v % n


# ---------------------------------------------------------------------------
# 1. PURE FUNCTIONS / CLASSES
# ---------------------------------------------------------------------------

def badge_id(mac):
    """Return the 4-hex-char badge id (last 2 bytes of the BLE MAC, uppercase).

    Stable per board (the fused MAC). Used as the unique advertising suffix
    `Fri3d-XXXX` so a phone can filter the chooser to exactly this badge."""
    try:
        b = bytes(mac)
        return "%02X%02X" % (b[-2], b[-1])
    except Exception:
        return "0000"


def setup_name(bid):
    """Full BLE local name for a given badge id: 'Fri3d-XXXX' (10 chars)."""
    return "Fri3d-" + bid


def _as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def sanitize_config(new, base):
    """Merge a phone-supplied config dict into a copy of `base`, coercing every
    field defensively. Pure + host-testable — the BLE equivalent of the old
    web_portal.form_to_config. `new` is whatever json.loads produced from the
    phone (arbitrary/hostile); `base` is the current on-badge config.

    A field absent from `new` keeps its `base` value (or a sane default). This
    lets the page send a full form each save while a partial payload still can't
    wipe unrelated keys."""
    cfg = dict(base) if isinstance(base, dict) else {}
    if not isinstance(new, dict):
        return cfg

    # name
    if "name" in new:
        name = new.get("name")
        cfg["name"] = (name if isinstance(name, str) else "").strip()
    else:
        cfg["name"] = (cfg.get("name") or "").strip() if isinstance(cfg.get("name"), str) else ""

    # groups: accept a list, or a comma-separated string (mirror the portal form)
    if "groups" in new:
        groups = new.get("groups")
        if isinstance(groups, str):
            groups = groups.split(",")
        if isinstance(groups, list):
            cfg["groups"] = [g.strip() for g in groups
                             if isinstance(g, str) and g.strip()]
        else:
            cfg["groups"] = []
    elif not isinstance(cfg.get("groups"), list):
        cfg["groups"] = []

    # rssi_floor
    if "rssi_floor" in new:
        cfg["rssi_floor"] = _as_int(new.get("rssi_floor"), -120)
    else:
        cfg["rssi_floor"] = _as_int(cfg.get("rssi_floor", -120), -120)

    # sound (checkbox semantics: present -> truthy, absent in `new` -> keep/default)
    if "sound" in new:
        cfg["sound"] = bool(new.get("sound"))
    else:
        cfg["sound"] = bool(cfg.get("sound", True))

    # banner_ms (a 0/negative value would hide every banner -> clamp to default)
    if "banner_ms" in new:
        bm = _as_int(new.get("banner_ms"), 5000)
    else:
        bm = _as_int(cfg.get("banner_ms", 5000), 5000)
    if bm < 500:
        bm = 5000
    cfg["banner_ms"] = bm

    # contact: free-form {str: str}
    if "contact" in new:
        contact = new.get("contact")
        clean = {}
        if isinstance(contact, dict):
            for k, v in contact.items():
                if not isinstance(k, str):
                    continue
                k = k.strip()
                if not k:
                    continue
                if v is None:
                    v = ""
                if not isinstance(v, str):
                    v = str(v)
                clean[k] = v.strip()
        cfg["contact"] = clean
    elif not isinstance(cfg.get("contact"), dict):
        cfg["contact"] = {}

    return cfg


def build_info(bid, authed, config=None):
    """Serialize the INFO characteristic value to JSON bytes.

    Pre-auth (or no config): {"v":1,"badge":"XXXX","authed":false} — the minimum
    a browser needs to confirm it reached the right badge and prompt for the
    code. Post-auth: the full editable config so the page can render the form."""
    import json
    if not authed or not isinstance(config, dict):
        return json.dumps({"v": INFO_VERSION, "badge": bid,
                           "authed": bool(authed)}).encode("utf-8")
    contact = config.get("contact")
    groups = config.get("groups")
    out = {
        "v": INFO_VERSION,
        "badge": bid,
        "authed": True,
        "name": config.get("name", "") or "",
        "groups": groups if isinstance(groups, list) else [],
        "rssi_floor": _as_int(config.get("rssi_floor", -120), -120),
        "sound": bool(config.get("sound", True)),
        "banner_ms": _as_int(config.get("banner_ms", 5000), 5000),
        "contact": contact if isinstance(contact, dict) else {},
    }
    return json.dumps(out).encode("utf-8")


def contacts_response(data, offset, page=CONTACTS_PAGE):
    """Return the bytes a CONTACTS read should yield for a given CTLOFF `offset`.

    The whole contacts.json is served as raw bytes in `page`-sized slices. The
    magic offset CONTACTS_HEADER_OFFSET returns a JSON header {"len":N,"page":P}
    so the client knows how many slices to fetch; any in-range offset returns
    that slice; an out-of-range offset returns empty (end of stream). Pure."""
    if not isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data)
        except Exception:
            data = b"[]"
    data = bytes(data)
    if offset == CONTACTS_HEADER_OFFSET:
        import json
        return json.dumps({"len": len(data), "page": page}).encode("utf-8")
    if offset < 0 or offset >= len(data):
        return b""
    return data[offset:offset + page]


class ChunkAssembler:
    """Reassemble a config payload sent as framed chunks.

    Each write is `seq:u8 | total:u8 | payload`. feed() returns the full bytes
    once every chunk 0..total-1 has arrived, else None. If the running total
    exceeds `max_bytes`, it sets .overflow (the caller answers `too_large` and
    resets). Tolerant of re-sent chunks and of a fresh stream starting with a
    different `total` (it restarts). Pure + host-testable."""

    def __init__(self, max_bytes=MAX_CFG_BYTES):
        self._max = max_bytes
        self.reset()

    def reset(self):
        self._chunks = {}
        self._total = None
        self.overflow = False

    def feed(self, data):
        if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
            return None
        data = bytes(data)
        seq = data[0]
        total = data[1]
        payload = data[2:]
        if total == 0:
            self.reset()
            return None
        if self._total is None:
            self._total = total
        elif total != self._total:
            # framing changed mid-stream -> treat as a brand-new payload
            self.reset()
            self._total = total
        if seq >= total:
            return None
        self._chunks[seq] = payload
        if sum(len(c) for c in self._chunks.values()) > self._max:
            self.overflow = True
            return None
        if len(self._chunks) == self._total:
            if all(i in self._chunks for i in range(self._total)):
                out = b"".join(self._chunks[i] for i in range(self._total))
                self.reset()
                return out
        return None


class AuthState:
    """Random 4-digit code + wrong-guess lockout, ported from web_portal's PIN
    logic. The code is shown ON THE BADGE SCREEN (prove you can see it — the
    Bluetooth/smart-TV pairing model). `rand`/`clock` are injectable for tests."""

    def __init__(self, digits=PIN_DIGITS, max_fails=MAX_FAILS,
                 lockout_ms=LOCKOUT_MS, rand=None, clock=None):
        self._digits = digits
        self._max_fails = max_fails
        self._lockout_ms = lockout_ms
        self._rand = rand or _rand_int
        self._clock = clock or _now
        self.code = None
        self._fails = 0
        self._lock_until = None

    def new_code(self):
        lo = 10 ** (self._digits - 1)
        self.code = str(lo + self._rand(9 * lo))
        self._fails = 0
        return self.code

    def locked(self):
        if self._lock_until is None:
            return False
        return _elapsed(self._lock_until, self._clock()) < 0

    def check(self, attempt):
        """Return True on a correct code. On the MAX_FAILS-th wrong guess, lock
        out for LOCKOUT_MS and rotate the code."""
        if self.locked():
            return False
        if attempt == self.code:
            self._fails = 0
            return True
        self._fails += 1
        if self._fails >= self._max_fails:
            self._fails = 0
            self._lock_until = _time_add(self._clock(), self._lockout_ms)
            self.new_code()          # rotate after a burst of guesses
        return False


def build_setup_adv(bid):
    """Connectable advertising payload for setup mode: Flags + Complete Local
    Name 'Fri3d-XXXX'. The page filters requestDevice by this exact name so the
    chooser shows exactly one entry. Returns bytes (<= 31)."""
    name = setup_name(bid).encode("utf-8")
    flags = bytes([0x02, 0x01, 0x06])                # LE General Disc + BR/EDR off
    name_ad = bytes([len(name) + 1, 0x09]) + name    # 0x09 = Complete Local Name
    return flags + name_ad


def build_setup_resp(bluetooth):
    """Scan-response payload carrying the 128-bit setup service UUID (0x07 =
    Complete list of 128-bit Service UUIDs). Best-effort; the page filters by
    NAME and lists the service under optionalServices, so this is informational.
    Returns bytes, or b"" if the UUID can't be serialized."""
    try:
        raw = bytes(bluetooth.UUID(SETUP_SVC))
        return bytes([len(raw) + 1, 0x07]) + raw
    except Exception:
        return b""


# ---------------------------------------------------------------------------
# 2. RADIO WRAPPER (lazy `import bluetooth`)
# ---------------------------------------------------------------------------

class SetupService:
    """Connectable GATT server for phone setup over Web Bluetooth.

    Registration of the setup GATT service is delegated to ContactExchange
    (NimBLE accepts gatts_register_services once per power-on, so the setup and
    exchange services share ONE call). The app creates a SetupService, attaches
    it to its ContactExchange via `exchange.attach_setup(setup)`, then drives a
    session with `await setup.run(mode, proximity, timeout_ms)`:

      - mode "configure": unconfigured badge, app foreground (Configure-me
        screen). Runs until the task is cancelled (screen change / save).
      - mode "window": configured badge, a user-opened 2-min window. Suspends
        the proximity radio for the duration, resumes on exit.

    Returns True if a config was saved during the session. Never raises except
    to propagate asyncio.CancelledError (app tearing down)."""

    def __init__(self, app_dir, exchange, on_saved=None, get_badge_id=None):
        self.dbg = []
        self._app_dir = app_dir
        self._exch = exchange
        self._on_saved = on_saved            # called (deferred) after a save
        self._get_badge_id = get_badge_id    # optional override for tests
        self._ble = None
        self._E = {}
        self._h = {}                         # name -> value handle (bound by exchange)
        self._badge_id = "0000"
        self._auth = AuthState()
        self._asm = ChunkAssembler()
        self._running = False
        self._mode = None
        self._proximity = None
        # per-connection / per-session state
        self._conn = None
        self._authed = False
        self._q = []
        self._saved = False
        self._saved_at = None
        self._idle_ms = None                 # active idle window (None = no idle limit)
        self._last_activity = 0              # ticks_ms of the last GATT event
        self._contacts_cache = None          # contacts.json bytes, cached per paging session
        self._adv = None                     # cached advertising payloads so the
        self._resp = None                    # session can RE-advertise on disconnect

    # ---- registration hooks (called by ContactExchange) ----
    def service_tuple(self, bluetooth):
        """The (uuid, characteristics) tuple for gatts_register_services. The
        characteristic order MUST match SETUP_CHR_ORDER."""
        F_READ = getattr(bluetooth, "FLAG_READ", 0x02)
        F_WRITE = getattr(bluetooth, "FLAG_WRITE", 0x08)
        F_WRITE_NR = getattr(bluetooth, "FLAG_WRITE_NO_RESPONSE", 0x04)
        F_NOTIFY = getattr(bluetooth, "FLAG_NOTIFY", 0x10)
        U = bluetooth.UUID
        return (
            U(SETUP_SVC),
            (
                (U(AUTH_CHR), F_WRITE | F_WRITE_NR),
                (U(INFO_CHR), F_READ),
                (U(CFG_CHR), F_WRITE | F_WRITE_NR),
                (U(STATUS_CHR), F_READ | F_NOTIFY),
                (U(CONTACTS_CHR), F_READ),
                (U(CTLOFF_CHR), F_WRITE | F_WRITE_NR),
            ),
        )

    def on_radio_off(self):
        """The radio was deactivated (BLE.active(False)), which clears NimBLE's
        gatts table — our handles are now stale. Drop them so a fresh
        registration (via ContactExchange.ensure_radio) rebinds them."""
        self._h = {}
        self._ble = None

    def bind_handles(self, ble, handles):
        """Receive the setup service's value handles (registration order) and
        size the read buffers. Called once by ContactExchange._ensure_services."""
        self._ble = ble
        for name, h in zip(SETUP_CHR_ORDER, handles):
            self._h[name] = h
        sizes = {"auth": 16, "info": 1024, "cfg": 600,
                 "status": 64, "contacts": 600, "ctloff": 8}
        for name, sz in sizes.items():
            h = self._h.get(name)
            if h is None:
                continue
            try:
                ble.gatts_set_buffer(h, sz, True)
            except Exception:
                pass

    # ---- badge id ----
    def current_code(self):
        return self._auth.code

    def current_badge_id(self):
        return self._badge_id

    def window_secs_left(self):
        """Seconds until the idle window closes, given the current activity. The
        window resets on GATT activity, so this counts down from SETUP_WINDOW_MS
        of *idle* time. Returns None when there is no idle limit (configure mode
        on an unconfigured badge)."""
        if not self._idle_ms:
            return None
        left = self._idle_ms - time.ticks_diff(time.ticks_ms(), self._last_activity)
        if left < 0:
            left = 0
        return left // 1000

    def _refresh_badge_id(self):
        if self._get_badge_id is not None:
            try:
                self._badge_id = self._get_badge_id()
                return
            except Exception:
                pass
        try:
            _at, mac = self._ble.config("mac")
            self._badge_id = badge_id(bytes(mac))
        except Exception:
            pass

    # ---- session ----
    async def run(self, mode, proximity=None, timeout_ms=None):
        import bluetooth
        import asyncio

        self._mode = mode
        self._proximity = proximity
        self._running = True
        self._saved = False
        self._saved_at = None
        self._conn = None
        self._authed = False
        self._q = []
        self._asm.reset()
        self._auth.new_code()
        self.dbg = ["start:%s" % mode]

        if proximity is not None:
            try:
                proximity.suspend()
            except Exception:
                pass

        try:
            self._seed_events(bluetooth)
            # ONE registration site: ContactExchange brings BLE up, sets the MTU
            # once, registers exchange+setup in one call and binds our handles.
            ble = self._exch.ensure_radio(bluetooth)
            if ble is None:
                self.dbg.append("no-ble")
                return False
            self._ble = ble
            self._refresh_badge_id()
            try:
                ble.irq(self._irq)
            except Exception as e:
                self.dbg.append("irq-exc %r" % e)
            self._prime_reads()

            self._adv = build_setup_adv(self._badge_id)
            self._resp = build_setup_resp(bluetooth)
            self._advertise()
            self.dbg.append("adv %s" % setup_name(self._badge_id))

            # timeout_ms (when given) is an IDLE window: it resets on every GATT
            # event (see _process bumping self._last_activity). abs_deadline is a
            # hard backstop so a window can't stay open indefinitely.
            self._idle_ms = timeout_ms
            self._last_activity = time.ticks_ms()
            abs_deadline = None
            if timeout_ms:
                abs_deadline = time.ticks_add(
                    time.ticks_ms(), max(timeout_ms, SETUP_ABS_CAP_MS))

            while self._running:
                self._process()
                now = time.ticks_ms()
                if self._idle_ms and \
                        time.ticks_diff(now, self._last_activity) >= self._idle_ms:
                    self.dbg.append("idle-timeout")
                    break
                if abs_deadline is not None and \
                        time.ticks_diff(abs_deadline, now) <= 0:
                    self.dbg.append("abs-cap")
                    break
                # After a save we DON'T tear down — the phone stays connected so
                # it can reload contacts / make more edits. The session ends when
                # the phone disconnects (handled in _process) or, as a safety net
                # if it vanished without a clean disconnect, after POST_SAVE_MAX_MS.
                if self._saved_at is not None and \
                        time.ticks_diff(time.ticks_ms(), self._saved_at) >= POST_SAVE_MAX_MS:
                    self.dbg.append("post-save-max")
                    break
                await asyncio.sleep_ms(SESSION_TICK_MS)
        except asyncio.CancelledError:
            self.dbg.append("CANCELLED")
            raise
        except Exception as e:
            self.dbg.append("EXC %r" % e)
        finally:
            self._teardown()
            if proximity is not None:
                try:
                    proximity.resume()
                except Exception:
                    pass
        self.dbg.append("saved=%s" % self._saved)
        return self._saved

    def request_stop(self):
        self._running = False

    def _advertise(self):
        # NimBLE stops advertising while a central is connected and does NOT
        # auto-resume on disconnect, so the session must re-issue this itself —
        # otherwise the badge goes invisible after the first phone leaves.
        if self._ble is None or self._adv is None:
            return
        try:
            if self._resp:
                self._ble.gap_advertise(ADV_INTERVAL_US, adv_data=self._adv,
                                        resp_data=self._resp, connectable=True)
            else:
                self._ble.gap_advertise(ADV_INTERVAL_US, adv_data=self._adv,
                                        connectable=True)
        except Exception:
            try:
                self._ble.gap_advertise(ADV_INTERVAL_US, adv_data=self._adv,
                                        connectable=True)
            except Exception as e:
                self.dbg.append("adv-exc %r" % e)

    # ---- IRQ: capture-only; read chunk values here (as the exchange does for
    # THEIRS) so no chunk is lost when writes arrive faster than the loop drains.
    def _irq(self, event, data):
        try:
            E = self._E
            if event == E["central_connect"]:
                conn, _at, _addr = data
                self._conn = conn
                self._authed = False
                self._q.append(("connect", b""))
            elif event == E["central_disconnect"]:
                self._conn = None
                self._authed = False
                self._q.append(("disconnect", b""))
            elif event == E["gatts_write"]:
                conn, vh = data
                if self._ble is None:
                    return
                try:
                    val = bytes(self._ble.gatts_read(vh))
                except Exception:
                    val = b""
                if vh == self._h.get("auth"):
                    self._q.append(("auth", val))
                elif vh == self._h.get("cfg"):
                    self._q.append(("cfg", val))
                elif vh == self._h.get("ctloff"):
                    # Serve the requested contacts page SYNCHRONOUSLY here, not on
                    # the loop: the phone's writeValueWithResponse(offset) resolves
                    # the instant NimBLE ACKs this write, and a fast client (a
                    # browser) reads the contacts characteristic before the loop's
                    # _process() would run — so a queued update hands back the
                    # PREVIOUS page and the reassembled JSON is corrupt. Updating
                    # the read buffer in the IRQ makes it fresh before that read.
                    self._serve_contacts(val)
                    self._q.append(("ctloff", b""))
        except Exception:
            pass

    def _seed_events(self, bluetooth):
        g = lambda n, d: getattr(bluetooth, n, d)
        self._E = {
            "central_connect": g("_IRQ_CENTRAL_CONNECT", 1),
            "central_disconnect": g("_IRQ_CENTRAL_DISCONNECT", 2),
            "gatts_write": g("_IRQ_GATTS_WRITE", 3),
        }

    # ---- request processing (on the loop, off the IRQ) ----
    def _process(self):
        q = self._q
        self._q = []
        if q:
            # Any GATT event counts as activity and resets the idle window.
            self._last_activity = time.ticks_ms()
        for kind, val in q:
            try:
                if kind == "connect":
                    self._authed = False
                    self._asm.reset()
                    self._contacts_cache = None
                    self._write_info(False)
                    self._set_status(ST_IDLE)
                elif kind == "disconnect":
                    self._authed = False
                    self._asm.reset()
                    if self._saved:
                        # Config already saved and the phone left — the user is
                        # done. End the session so the radio goes back to the
                        # proximity beacon (or the window's caller).
                        self.dbg.append("disc-after-save")
                        self._running = False
                    else:
                        # Not configured yet; the phone may reconnect. Stay
                        # visible (NimBLE dropped advertising on connect).
                        self._write_info(False)
                        self._advertise()
                elif kind == "auth":
                    self._handle_auth(val)
                elif kind == "cfg":
                    self._handle_cfg(val)
                elif kind == "ctloff":
                    self._handle_ctloff(val)
            except Exception as e:
                self.dbg.append("proc-exc %r" % e)

    def _handle_auth(self, val):
        try:
            attempt = bytes(val).decode("utf-8").strip()
        except Exception:
            attempt = ""
        if self._auth.locked():
            self._notify_status(ST_LOCKED)
            return
        if self._auth.check(attempt):
            self._authed = True
            # Snapshot contacts.json HERE (loop context), not in the IRQ. The blob
            # is static for a whole setup session (a Y-swap can't run while setup
            # owns the radio), so this one read serves every page — and the BLE IRQ
            # (_serve_contacts) never touches flash, which would stall the main task
            # and can starve/drop the GATT link.
            self._contacts_cache = self._load_contacts_bytes()
            self._write_info(True)
            self._notify_status(ST_AUTH_OK)
        else:
            self._authed = False
            self._notify_status(ST_LOCKED if self._auth.locked() else ST_AUTH_FAIL)

    def _handle_cfg(self, val):
        if not self._authed:
            self._notify_status(ST_AUTH_REQUIRED)
            return
        out = self._asm.feed(val)
        if self._asm.overflow:
            self._asm.reset()
            self._notify_status(ST_TOO_LARGE)
            return
        if out is None:
            return                       # need more chunks
        import json
        try:
            obj = json.loads(bytes(out).decode("utf-8"))
        except Exception:
            self._notify_status(ST_INVALID)
            return
        if not isinstance(obj, dict):
            self._notify_status(ST_INVALID)
            return
        base = self._load_config()
        cfg = sanitize_config(obj, base)
        if self._write_config(cfg):
            self._write_info(True)       # refresh INFO with the saved config
            self._saved = True
            self._saved_at = _now()
            self._notify_status(ST_OK)
            if self._on_saved:
                try:
                    self._on_saved()
                except Exception:
                    pass
        else:
            self._notify_status(ST_INVALID)

    def _serve_contacts(self, val):
        """Write the requested contacts page into the read characteristic. Called
        SYNCHRONOUSLY from the IRQ so the buffer is fresh before the phone reads
        (see the ctloff branch in _irq). Slices an IN-MEMORY snapshot only — NO
        flash I/O here. The snapshot is taken once on the loop at auth time
        (_handle_auth); contacts.json can't change during a session, so it stays
        valid for every page. Reading the file in the IRQ instead would stall the
        main task and can starve the GATT link (the drop this method now avoids)."""
        if not self._authed:
            return
        if len(val) >= 2:
            offset = val[0] | (val[1] << 8)
        elif len(val) == 1:
            offset = val[0]
        else:
            offset = 0
        cache = self._contacts_cache
        if cache is None:                # not primed yet (shouldn't happen post-auth)
            cache = b"[]"
        page = contacts_response(cache, offset)
        try:
            self._ble.gatts_write(self._h["contacts"], page)
        except Exception:
            pass

    def _handle_ctloff(self, val):
        # The page was already written in the IRQ (_serve_contacts); on the loop we
        # only settle the status characteristic.
        if not self._authed:
            self._notify_status(ST_AUTH_REQUIRED)
            return
        self._set_status(ST_OK)

    # ---- GATT read buffers / status ----
    def _prime_reads(self):
        self._write_info(False)
        self._set_status(ST_IDLE)

    def _write_info(self, authed):
        cfg = self._load_config() if authed else None
        data = build_info(self._badge_id, authed, cfg)
        try:
            self._ble.gatts_write(self._h["info"], data)
        except Exception:
            pass

    def _set_status(self, code):
        try:
            self._ble.gatts_write(self._h["status"], code.encode("utf-8"))
        except Exception:
            pass

    def _notify_status(self, code):
        self._set_status(code)
        if self._conn is not None:
            try:
                self._ble.gatts_notify(self._conn, self._h["status"], code.encode("utf-8"))
            except Exception:
                pass

    # ---- storage ----
    def _load_config(self):
        try:
            with open(self._app_dir + "/config.json") as f:
                import json
                return json.load(f)
        except Exception:
            return {}

    def _write_config(self, cfg):
        import json
        path = self._app_dir + "/config.json"
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cfg, f)
            os.rename(tmp, path)
        except Exception:
            return False
        return True

    def _load_contacts_bytes(self):
        try:
            with open(self._app_dir + "/contacts.json", "rb") as f:
                return f.read()
        except Exception:
            return b"[]"

    # ---- teardown ----
    def _teardown(self):
        if not self._ble:
            return
        try:
            self._ble.gap_advertise(None)
        except Exception:
            pass
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except Exception:
                pass
        self._conn = None
        self._authed = False
