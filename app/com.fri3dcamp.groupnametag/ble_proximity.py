# ble_proximity.py — BLE group-aware proximity finder for the Fri3d Camp 2024 badge
# (running MicroPythonOS).
#
# Two cleanly separated halves:
#
#   1. PURE WIRE-FORMAT FUNCTIONS (fnv1a_16, normalize_group, hash_groups,
#      name_budget, truncate_utf8, build_payload, parse_payload, intersect,
#      shared_name_for). These have NO dependency on `bluetooth` / `mpos` /
#      `lvgl` / `asyncio` and are unit-tested off-device (tests/). Importing
#      this module on a host must work.
#
#   2. The BLEProximity radio wrapper (begin/end/scan/advertise + IRQ-driven
#      state machine). It imports `bluetooth` LAZILY inside its methods so the
#      host can still load this module to test the pure functions.
#
# See DESIGN.md for the full protocol rationale (PLAN.md §6 adapted for the
# connected badge, which uses `ble.config("mac")` — public/static — and has no
# `fri3d.application` framework).

# ---------------------------------------------------------------------------
# Constants — protocol
# ---------------------------------------------------------------------------

MAGIC = b"HSNT"                 # "Hackerspace NameTag" — identifies any badge running this app
VERSION = 1                     # wire-format version byte
COMPANY_ID = b"\xff\xff"        # little-endian placeholder (reserved/testing range)
AD_TYPE_MFG = 0xFF              # Manufacturer Specific Data

MAX_GROUPS = 5                  # cap on advertised group IDs (2 bytes each on the wire)

# 31-byte legacy adv budget, no Flags AD emitted (non-connectable beacon):
#   2 (AD len+type) + 2 (company) + 4 (magic) + 1 (version) + 1 (group count)
#   + 2*G (groups) + 1 (name len)  =  11 + 2*G  of overhead, name gets the rest.
ADV_TOTAL = 31
OVERHEAD = 2 + 2 + 4 + 1 + 1 + 1   # AD-header + company + magic + version + gcount + namelen

# Tuning (radio wrapper)
ADV_MS = 250                    # advertise interval (ms)
EVICT_MS = 30000                # peer gone if not seen for this long (ms)
# Scanning: a CONTINUOUS, dense scan with an explicit interval/window. This is
# critical — MicroPython's gap_scan() with DEFAULT args enables NimBLE's
# duplicate filter, so each peer is reported only ~once and presence flaps as
# peers age out. Passing explicit interval_us/window_us disables that filter,
# so every advertisement is reported and last_seen stays fresh (age <1 s) even
# amid collisions from several co-located badges. 50% duty (window 60 ms,
# interval 120 ms) holds peers rock-solid for a fraction of the RX power of
# 100% duty. gap_scan(0, ...) runs indefinitely; we re-arm every SCAN_REARM_MS
# as insurance in case the stack ever stops it.
SCAN_WINDOW_US = 60000          # 60 ms listen window
SCAN_INTERVAL_US = 120000       # 120 ms scan interval  -> 50% duty, dense
SCAN_REARM_MS = 30000           # restart the continuous scan this often
RSSI_FLOOR_DEFAULT = -120       # disabled (ESP32-S3 sensitivity ~-97 dBm)


# ---------------------------------------------------------------------------
# 1. PURE WIRE-FORMAT FUNCTIONS
# ---------------------------------------------------------------------------

def fnv1a_16(data):
    """FNV-1a 32-bit of `data` (bytes), folded to 16 bits via xor-folding.

    Collision-tolerant group identifier, NOT a security mechanism. Same bytes
    always produce the same 16-bit id; different group names practically differ.
    """
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return (h ^ (h >> 16)) & 0xFFFF


def normalize_group(name):
    """Normalize a group name before hashing: strip + lower.

    Trivial formatting differences between two members typing the same group
    ('Makerspace Baasrode ' vs 'makerspace baasrode') still hash identically.
    Non-string / None -> '' (so they hash to a consistent, ignorable value).
    """
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


def hash_groups(groups, max_groups=MAX_GROUPS):
    """Hash a list of group names into a deduplicated, sorted, capped list of
    16-bit ids.

    Returns (ids, dropped) where `ids` is sorted ascending and `dropped` is the
    number of distinct ids beyond max_groups that were discarded (we keep the
    lowest `max_groups`, deterministically — both ends agree).
    """
    seen = set()
    for g in groups or []:
        n = normalize_group(g)
        if not n:
            continue
        seen.add(fnv1a_16(n.encode("utf-8")))
    ids = sorted(seen)
    if len(ids) > max_groups:
        dropped = len(ids) - max_groups
        ids = ids[:max_groups]
    else:
        dropped = 0
    return ids, dropped


def name_budget(num_groups, total=ADV_TOTAL, overhead=OVERHEAD):
    """Bytes available for the name field given `num_groups` advertised ids."""
    nb = total - overhead - 2 * num_groups
    return nb if nb > 0 else 0


def truncate_utf8(s, max_bytes):
    """Truncate string `s` so its UTF-8 encoding fits in `max_bytes`, cutting
    only on a character (codepoint) boundary — never mid-codepoint."""
    if max_bytes <= 0:
        return ""
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    # Walk back to a UTF-8 boundary. A continuation byte has top bits 10xxxxxx.
    cut = max_bytes
    while cut > 0 and (enc[cut] & 0xC0) == 0x80:
        cut -= 1
    return enc[:cut].decode("utf-8", "ignore")


def build_payload(group_ids, name):
    """Build the full legacy advertising payload (one manufacturer AD structure).

    `group_ids` must already be deduped/sorted/capped (use hash_groups()).
    `name` is truncated to the available budget on a UTF-8 boundary.
    Returns bytes of length <= ADV_TOTAL.
    """
    gids = sorted(set(int(g) & 0xFFFF for g in group_ids))[:MAX_GROUPS]
    nb = name_budget(len(gids))
    disp = truncate_utf8(name or "", nb)
    name_b = disp.encode("utf-8")

    body = (
        COMPANY_ID +
        MAGIC +
        bytes([VERSION]) +
        bytes([len(gids)]) +
        b"".join(_u16_le(g) for g in gids) +
        bytes([len(name_b)]) +
        name_b
    )
    # AD structure: [length-of-following, type, body...]
    return bytes([len(body) + 1, AD_TYPE_MFG]) + body


def parse_payload(adv):
    """Defensively parse an advertising payload and extract our beacon if present.

    `adv` is the raw advertisement bytes (one or more AD structures). Returns a
    dict {version, group_ids, name} on a valid v1 HSNT beacon, or None for:
    anything malformed, wrong magic, unknown version, or length fields that
    overrun the buffer. Never raises.
    """
    if not isinstance(adv, (bytes, bytearray)):
        return None
    i = 0
    n = len(adv)
    while i + 1 < n:
        slen = adv[i]
        if slen == 0:
            i += 1
            continue
        ad_type = adv[i + 1]
        field = adv[i + 2:i + 2 + slen - 1]
        i += 1 + slen
        if ad_type != AD_TYPE_MFG:
            continue
        # Need: company(2) + magic(4) = 6 bytes minimum to even check.
        if len(field) < 2 + len(MAGIC):
            continue
        if field[2:2 + len(MAGIC)] != MAGIC:
            continue
        rest = field[2 + len(MAGIC):]
        # rest = version(1) gcount(1) gids(2*gcount) namelen(1) name(namelen)
        if len(rest) < 2:
            continue
        version = rest[0]
        if version != VERSION:
            continue            # unknown version -> drop (forward-compat)
        gcount = rest[1]
        gid_end = 2 + 2 * gcount
        if len(rest) < gid_end + 1:
            continue            # truncated
        gids = []
        for k in range(gcount):
            lo = rest[2 + 2 * k]
            hi = rest[3 + 2 * k]
            gids.append(lo | (hi << 8))
        namelen = rest[gid_end]
        name_start = gid_end + 1
        if len(rest) < name_start + namelen:
            continue            # truncated
        name_b = rest[name_start:name_start + namelen]
        try:
            name = name_b.decode("utf-8", "replace")
        except Exception:
            name = ""
        return {"version": version, "group_ids": gids, "name": name}
    return None


def intersect(own_ids, peer_ids):
    """Return the set of group ids present in BOTH lists (the match test)."""
    a = set(own_ids)
    return a.intersection(peer_ids)


def shared_name_for(own_groups, peer_ids):
    """Map the (sorted ascending) shared group ids back to the lowest-sorted
    shared group's display name, using this badge's own name->hash table.

    own_groups: list of (name, id) pairs (display name + its hash).
    peer_ids: the peer's advertised group ids.
    Returns (shared_id_lowest, shared_name) or (None, None) if no overlap.
    The lowest-sorted shared id is the deterministic 'signature' both badges
    agree on for colour/tone.
    """
    pid = set(peer_ids)
    id_to_name = {}
    for name, gid in own_groups:
        if gid in pid:
            id_to_name.setdefault(gid, name)   # first (original order) name wins per id
    if not id_to_name:
        return None, None
    low = min(id_to_name)
    return low, id_to_name[low]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _u16_le(v):
    return bytes([v & 0xFF, (v >> 8) & 0xFF])


def build_own_table(groups):
    """Return [(display_name, id), ...] for the configured groups (dedup by id,
    preserving first-seen display name). Used by shared_name_for()."""
    out = []
    seen = set()
    for g in groups or []:
        n = normalize_group(g)
        if not n:
            continue
        gid = fnv1a_16(n.encode("utf-8"))
        if gid in seen:
            continue
        seen.add(gid)
        out.append((g, gid))
    return out


# ---------------------------------------------------------------------------
# 2. RADIO WRAPPER  (lazy `import bluetooth`)
# ---------------------------------------------------------------------------

class BLEProximity:
    """Advertise this badge's groups/name and scan for matching peers.

    Cooperative: the UI loop calls tick() every frame to run eviction and
    duty-cycled scanning. New arrivals are queued in _arrivals; drain with
    take_arrivals(). current_peers() returns the in-range set for display.
    """

    def __init__(self):
        self._ble = None
        self._active = False
        self._own_ids = []
        self._own_table = []          # [(name, id), ...]
        self._rssi_floor = RSSI_FLOOR_DEFAULT
        self._seen = {}               # key (addr_type, addr_bytes) -> dict
        self._arrivals = []           # queued new-arrival events for UI
        self._pending = []            # raw scan results captured in the IRQ, drained in tick()
        self._adv = None              # current adv payload
        self._next_rearm_ms = 0       # ticks_ms deadline to restart the continuous scan
        self._irq_scan_result = 5     # bluetooth._IRQ_SCAN_RESULT (seeded in begin)
        self._name = ""
        self._suspended = False       # True while the contact-exchange window owns the radio

    # ---- lifecycle ----
    def begin(self, groups, name, handle="", rssi_floor=RSSI_FLOOR_DEFAULT):
        import bluetooth
        from bluetooth import BLE
        self._own_ids, _ = hash_groups(groups)
        self._own_table = build_own_table(groups)
        # Coerce identity fields to str so a non-string config value degrades
        # instead of crashing begin() in build_payload/truncate_utf8.
        self._name = name if isinstance(name, str) else ""
        handle = handle if isinstance(handle, str) else ""
        self._rssi_floor = self._validate_floor(rssi_floor)
        self._irq_scan_result = getattr(bluetooth, "_IRQ_SCAN_RESULT", 5)

        disp = self._name
        if handle:
            disp = self._name + " " + handle
        self._adv = build_payload(self._own_ids, disp)

        self._ble = BLE()
        self._ble.active(True)
        # Stable public address is the default on this build (verified
        # ble.config("mac") -> (0, ...)); no addr_mode change needed.
        self._ble.irq(self._irq)
        adv_ok = True
        try:
            self._ble.gap_advertise(ADV_MS * 1000, adv_data=self._adv, connectable=False)
        except Exception:
            adv_ok = False        # scanning-but-invisible; report it to the caller
        self._next_rearm_ms = 0       # start the continuous scan on the first tick()
        self._active = True
        return adv_ok

    def end(self):
        if not self._ble:
            return
        self._active = False
        try:
            self._ble.gap_scan(None)
        except Exception:
            pass
        try:
            self._ble.gap_advertise(None)
        except Exception:
            pass
        try:
            self._ble.active(False)
        except Exception:
            pass
        self._ble = None
        self._seen = {}
        self._arrivals = []
        self._pending = []

    # ---- hand the radio to / take it back from the contact-exchange window ----
    def suspend(self):
        """Stop advertising + scanning (but keep BLE active) so the contact
        exchange can take over the single adv set / IRQ handler. Idempotent."""
        if not self._ble:
            self._suspended = True
            return
        self._suspended = True
        for fn in (lambda: self._ble.gap_scan(None),
                   lambda: self._ble.gap_advertise(None)):
            try:
                fn()
            except Exception:
                pass

    def resume(self):
        """Reinstall the proximity IRQ + non-connectable beacon and re-arm the
        dense scan after a contact-exchange window returns the radio."""
        self._suspended = False
        if not self._ble:
            return
        try:
            self._ble.irq(self._irq)
        except Exception:
            pass
        if self._adv is not None:
            try:
                self._ble.gap_advertise(ADV_MS * 1000, adv_data=self._adv, connectable=False)
            except Exception:
                pass
        self._next_rearm_ms = 0        # re-arm the continuous scan on the next tick()

    # ---- continuous dense scan (called from UI loop each frame) ----
    def tick(self, now_ms, dt_ms):
        if not self._active or not self._ble or self._suspended:
            return
        from time import ticks_diff, ticks_add
        # Drain scan results captured by the IRQ and update the peer table on
        # THIS (loop) thread, so _seen is never mutated mid-iteration by the IRQ.
        self._process_pending(now_ms)
        # Evict stale peers every frame (cheap).
        self._evict(now_ms)
        # Re-arm the continuous dense scan periodically (starts it on the first
        # tick, and restarts it every SCAN_REARM_MS as insurance). ticks_diff is
        # wrap-safe (PLAN §6.3) — never compare raw ticks_ms values.
        if ticks_diff(now_ms, self._next_rearm_ms) >= 0:
            try:
                self._ble.gap_scan(0, SCAN_INTERVAL_US, SCAN_WINDOW_US)
            except Exception:
                pass
            self._next_rearm_ms = ticks_add(now_ms, SCAN_REARM_MS)

    # ---- IRQ: capture only (no parsing / no _seen mutation here) ----
    def _irq(self, event, data):
        if event != self._irq_scan_result:
            return
        try:
            # On MicroPython NimBLE, _IRQ_SCAN_RESULT data layout:
            #   (addr_type, addr, adv_type, rssi, adv_data)
            addr_type, addr, adv_type, rssi, adv_data = data
            # Copy the transient buffers (only valid during this callback) and
            # queue for processing in tick(). Bound the queue so a stalled loop
            # can't grow it without limit.
            if len(self._pending) < 256:
                self._pending.append((addr_type, bytes(addr), bytes(adv_data), rssi))
        except Exception:
            pass              # never let an IRQ raise

    # ---- deferred processing (runs in tick(), on the loop thread) ----
    def _process_pending(self, now):
        if not self._pending:
            return
        pending = self._pending
        self._pending = []            # atomic rebind; a concurrent IRQ append is safe
        for addr_type, addr, adv_data, rssi in pending:
            self._process_result(addr_type, addr, adv_data, rssi, now)

    def _process_result(self, addr_type, addr, adv_data, rssi, now):
        info = parse_payload(adv_data)
        if info is None:
            return
        shared = intersect(self._own_ids, info["group_ids"])
        if not shared:
            return                  # disjoint -> ignore
        if rssi < self._rssi_floor:
            return                  # below noise floor
        key = (addr_type, addr)
        entry = self._seen.get(key)
        shared_id, shared_name = shared_name_for(self._own_table, info["group_ids"])
        is_new = entry is None
        if is_new:
            entry = {
                "name": info["name"],
                "shared_id": shared_id,
                "shared_name": shared_name,
                "addr_type": addr_type,
                "rssi": rssi,
                "rssi_ewma": float(rssi),
                "last_seen_ms": now,
                "notified": False,
            }
            self._seen[key] = entry
            self._arrivals.append({
                "name": info["name"],
                "shared_id": shared_id,
                "shared_name": shared_name,
                "rssi": rssi,
            })
        else:
            entry["last_seen_ms"] = now
            a = 0.3
            entry["rssi_ewma"] = (1 - a) * entry["rssi_ewma"] + a * rssi
            entry["rssi"] = rssi
            entry["name"] = info["name"]     # refresh (peer may have been renamed)
            entry["shared_name"] = shared_name
            entry["shared_id"] = shared_id

    # ---- eviction ----
    def _evict(self, now_ms):
        from time import ticks_diff
        stale = []
        for key, e in self._seen.items():
            if ticks_diff(now_ms, e["last_seen_ms"]) > EVICT_MS:
                stale.append(key)
        for k in stale:
            del self._seen[k]

    # ---- UI accessors ----
    def take_arrivals(self):
        a = self._arrivals
        self._arrivals = []
        return a

    def current_peers(self):
        # list of (name, shared_name, shared_id, rssi_ewma, age_ms) sorted by name
        from time import ticks_diff, ticks_ms
        now = ticks_ms()
        out = []
        for e in self._seen.values():
            out.append((
                e["name"],
                e["shared_name"],
                e["shared_id"],
                int(e["rssi_ewma"]),
                ticks_diff(now, e["last_seen_ms"]),
            ))
        out.sort(key=lambda x: x[0].lower())
        return out

    def has_peers(self):
        return len(self._seen) > 0

    # ---- validation ----
    @staticmethod
    def _validate_floor(v):
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return RSSI_FLOOR_DEFAULT
        if iv < -120 or iv > 0:
            return RSSI_FLOOR_DEFAULT
        return iv
