# contact_exchange.py — Y-button "swap contacts" for the Fri3d badge
# (MicroPythonOS). Two badges in BLE range — NOT necessarily friends / same
# group — that each press Y within the same ~5 s window find each other and
# exchange their "my contact info" over a short, connectable GATT session.
#
# Same two-part split as ble_proximity.py:
#
#   1. PURE FUNCTIONS (build/parse the exchange beacon, decide_role, the contact
#      JSON envelope, add_received). NO dependency on bluetooth/mpos/asyncio —
#      unit-tested off-device (tests/test_contact_exchange.py). Importing this
#      module on a host must work.
#
#   2. The ContactExchange radio wrapper (a connectable-advertising + scanning
#      rendezvous, then a GATT server/client data swap). It imports `bluetooth`
#      LAZILY inside its methods, and cooperates with the proximity radio by
#      asking it to suspend()/resume() for the duration of the window (NimBLE has
#      a single legacy advertising set + one IRQ handler, so the two features
#      take turns rather than run at once).
#
# See DESIGN.md "Contact exchange" for the protocol rationale. The radio half
# needs a two-badge on-device round-trip to fully verify (like the proximity
# round-trip); the pure half is fully covered off-device.

# ---------------------------------------------------------------------------
# Constants — protocol
# ---------------------------------------------------------------------------

X_MAGIC = b"HXCG"               # "Hackerspace eXchange" — identifies an exchange beacon
X_VERSION = 1                   # exchange wire-format version
COMPANY_ID = b"\xff\xff"        # little-endian placeholder (reserved/testing range)
AD_TYPE_MFG = 0xFF              # Manufacturer Specific Data

X_FLAG_WANT = 0x01              # bit0: this badge is currently in an exchange window

WINDOW_MS = 5000                # how long a Y-press keeps the badge "looking to swap"
MAX_CONTACT_BYTES = 500         # cap on the contact envelope (fits one raised-MTU read)
GATT_MTU = 515                  # request a large ATT MTU so the envelope fits one op
MAX_CONTACTS = 200              # cap on the stored received-contact list

# Custom 128-bit UUIDs for the exchange GATT service (Nordic-UART-derived base).
SVC_UUID   = "6e400010-b5a3-f393-e0a9-e50e24dcca9e"
MYINFO_CHR = "6e400011-b5a3-f393-e0a9-e50e24dcca9e"   # READ: this badge's contact envelope
THEIRS_CHR = "6e400012-b5a3-f393-e0a9-e50e24dcca9e"   # WRITE: the peer pushes its envelope here


# ---------------------------------------------------------------------------
# 1. PURE FUNCTIONS
# ---------------------------------------------------------------------------

def _u16_le(v):
    return bytes([v & 0xFF, (v >> 8) & 0xFF])


def build_exchange_adv(nonce=0, want=True):
    """Build the connectable exchange beacon (one manufacturer AD structure).

    The peer's address (used both to connect and to decide roles) comes from the
    scan result itself, so it is NOT embedded here. `nonce` (0..65535) lets a
    receiver tell two separate windows of the same badge apart. Returns bytes.
    """
    flags = X_FLAG_WANT if want else 0
    body = (
        COMPANY_ID +
        X_MAGIC +
        bytes([X_VERSION]) +
        bytes([flags & 0xFF]) +
        _u16_le(int(nonce) & 0xFFFF)
    )
    return bytes([len(body) + 1, AD_TYPE_MFG]) + body


def parse_exchange_adv(adv):
    """Parse an advertisement; return {version, flags, nonce, want} for a valid
    exchange beacon, else None. Defensive — never raises."""
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
        if len(field) < 2 + len(X_MAGIC):
            continue
        if field[2:2 + len(X_MAGIC)] != X_MAGIC:
            continue
        rest = field[2 + len(X_MAGIC):]
        # rest = version(1) flags(1) nonce(2)
        if len(rest) < 4:
            continue
        version = rest[0]
        if version != X_VERSION:
            continue            # unknown version -> drop (forward-compat)
        flags = rest[1]
        nonce = rest[2] | (rest[3] << 8)
        return {"version": version, "flags": flags, "nonce": nonce,
                "want": bool(flags & X_FLAG_WANT)}
    return None


def decide_role(my_mac, peer_mac):
    """Deterministic tie-break so exactly ONE side initiates the connection.

    Both badges independently compare the two 6-byte addresses: the LOWER one is
    the GATT `server` (keeps advertising connectable and waits), the HIGHER one is
    the `client` (stops advertising and connects). Byte comparison is
    lexicographic and both ends see the same pair, so they always agree. Equal
    addresses can't happen between two badges; treated as `server`.
    """
    a = bytes(my_mac)
    b = bytes(peer_mac)
    if a == b:
        return "server"
    return "server" if a < b else "client"


def _coerce_fields(contact):
    """Return a clean {str: str} copy of a contact dict (drop bad keys/values)."""
    out = {}
    if isinstance(contact, dict):
        for k, v in contact.items():
            if not isinstance(k, str):
                continue
            if v is None:
                continue
            if not isinstance(v, str):
                v = str(v)
            k = k.strip()
            if k:
                out[k] = v
    return out


def build_contact_envelope(name, contact, max_bytes=MAX_CONTACT_BYTES):
    """Serialize this badge's identity + contact fields to compact JSON bytes.

    Shape: {"n": <name>, "c": {field: value, ...}}. If the encoding exceeds
    `max_bytes`, contact fields are dropped (last-added first) until it fits; the
    name is always kept. Returns bytes (valid JSON, <= max_bytes when possible).
    """
    import json
    name = name if isinstance(name, str) else ""
    fields = _coerce_fields(contact)
    keys = list(fields.keys())
    while True:
        env = {"n": name, "c": {k: fields[k] for k in keys}}
        data = json.dumps(env).encode("utf-8")
        if len(data) <= max_bytes or not keys:
            return data
        keys.pop()          # drop the last field and retry


def parse_contact_envelope(data):
    """Parse a received envelope -> {"name": str, "fields": {str:str}} or None.
    Defensive — never raises, tolerates bytes or str input."""
    import json
    try:
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        obj = json.loads(data)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("n", "")
    if not isinstance(name, str):
        name = ""
    fields = _coerce_fields(obj.get("c", {}))
    return {"name": name, "fields": fields}


def add_received(store, entry, max_contacts=MAX_CONTACTS):
    """Append a received contact to `store` (a list) — ONE entry per swap.

    Every swap is recorded as its own separate entry, with its own name, fields,
    timestamp and rssi. There is NO deduplication: swapping again with the same
    badge creates another entry (a fresh snapshot of who/what/when). `entry` must
    carry the already-computed fields: mac, name, fields, received_at (formatted
    str), received_ticks (int), rssi. The list is capped to `max_contacts`,
    dropping the oldest (front of the list) when exceeded. Pure (no clock/BLE) so
    it is unit-tested off-device. Returns the same list.
    """
    store.append({
        "mac": entry.get("mac", ""),
        "name": entry.get("name", ""),
        "fields": dict(entry.get("fields", {})),
        "received_at": entry.get("received_at", ""),
        "received_ticks": entry.get("received_ticks", 0),
        "rssi": entry.get("rssi", 0),
    })
    if len(store) > max_contacts:
        del store[:len(store) - max_contacts]      # drop oldest-appended entries
    return store


# Backward-compat alias: an older group_nametag.py imports `merge_received`.
# Keeping this makes contact_exchange.py self-consistent under a partial deploy
# (device flash-copies are flaky here, so the two files can lag each other).
merge_received = add_received


# ---------------------------------------------------------------------------
# 2. RADIO WRAPPER (lazy `import bluetooth`)
# ---------------------------------------------------------------------------

class ContactExchange:
    """One-shot contact swap over a connectable GATT session.

    Usage (from the app's asyncio loop):

        rec = await exch.run_window(proximity, my_name, my_contact_dict)

    `proximity` is the live BLEProximity instance; run_window() asks it to
    suspend() for the ~5 s window (so the two BLE features don't fight over the
    single advertising set / IRQ) and resume() afterwards. Returns a received
    dict {mac, name, fields, rssi} on success, or None on timeout/failure. Never
    raises. The caller adds the timestamp and persists it.
    """

    def __init__(self):
        self.dbg = []            # trace of the last run_window (for diagnostics)
        self._ble = None
        self._svc_ready = False
        self._mtu_set = False    # config(mtu=) is one-time-only; re-setting EINVALs
        self._h_myinfo = None
        self._h_theirs = None
        # event constants seeded in run_window()
        self._E = {}
        # per-window state
        self._peer = None          # (addr_type, addr_bytes, rssi) of a chosen peer
        self._role = None
        self._conn = None
        self._received = None      # parsed envelope from the peer
        self._got_write = False
        self._done = False

    # ---- IRQ: capture events, set flags; keep it short and non-raising ----
    def _irq(self, event, data):
        try:
            E = self._E
            if event == E["scan_result"]:
                addr_type, addr, adv_type, rssi, adv_data = data
                if self._peer is not None or self._role is not None:
                    return
                info = parse_exchange_adv(bytes(adv_data))
                if info and info.get("want"):
                    self._peer = (addr_type, bytes(addr), rssi)
            elif event == E["peripheral_connect"]:
                # We (client) connected to the server.
                conn_handle, addr_type, addr = data
                self._conn = conn_handle
            elif event == E["central_connect"]:
                # A client connected to us (server).
                conn_handle, addr_type, addr = data
                self._conn = conn_handle
                if self._peer is None:
                    self._peer = (addr_type, bytes(addr), 0)
            elif event == E["gattc_read_result"]:
                conn_handle, value_handle, char_data = data
                self._received = parse_contact_envelope(bytes(char_data))
            elif event == E["gatts_write"]:
                conn_handle, value_handle = data
                if value_handle == self._h_theirs and self._ble is not None:
                    try:
                        val = self._ble.gatts_read(self._h_theirs)
                        self._received = parse_contact_envelope(bytes(val))
                        self._got_write = True
                    except Exception:
                        pass
            elif event in (E["peripheral_disconnect"], E["central_disconnect"]):
                self._conn = None
                self._done = True
        except Exception:
            pass  # never let an IRQ raise

    def _seed_events(self, bluetooth):
        g = lambda n, d: getattr(bluetooth, n, d)
        self._E = {
            "scan_result": g("_IRQ_SCAN_RESULT", 5),
            "scan_done": g("_IRQ_SCAN_DONE", 6),
            "peripheral_connect": g("_IRQ_PERIPHERAL_CONNECT", 7),
            "peripheral_disconnect": g("_IRQ_PERIPHERAL_DISCONNECT", 8),
            "gattc_service_result": g("_IRQ_GATTC_SERVICE_RESULT", 9),
            "gattc_characteristic_result": g("_IRQ_GATTC_CHARACTERISTIC_RESULT", 11),
            "gattc_read_result": g("_IRQ_GATTC_READ_RESULT", 15),
            "gattc_write_done": g("_IRQ_GATTC_WRITE_DONE", 17),
            "central_connect": g("_IRQ_CENTRAL_CONNECT", 1),
            "central_disconnect": g("_IRQ_CENTRAL_DISCONNECT", 2),
            "gatts_write": g("_IRQ_GATTS_WRITE", 3),
            "mtu_exchanged": g("_IRQ_MTU_EXCHANGED", 21),
        }

    def _ensure_service(self, bluetooth):
        """Register the exchange GATT service once and cache value handles."""
        if self._svc_ready:
            return
        F_READ = getattr(bluetooth, "FLAG_READ", 0x02)
        F_WRITE = getattr(bluetooth, "FLAG_WRITE", 0x08)
        F_WRITE_NR = getattr(bluetooth, "FLAG_WRITE_NO_RESPONSE", 0x04)
        UUID = bluetooth.UUID
        svc = (
            UUID(SVC_UUID),
            (
                (UUID(MYINFO_CHR), F_READ),
                (UUID(THEIRS_CHR), F_WRITE | F_WRITE_NR),
            ),
        )
        ((self._h_myinfo, self._h_theirs),) = self._ble.gatts_register_services((svc,))
        try:
            self._ble.gatts_set_buffer(self._h_myinfo, MAX_CONTACT_BYTES + 100, True)
            self._ble.gatts_set_buffer(self._h_theirs, MAX_CONTACT_BYTES + 100, True)
        except Exception:
            pass
        self._svc_ready = True

    async def run_window(self, proximity, my_name, my_contact):
        import bluetooth
        import time
        import asyncio

        self._peer = None
        self._role = None
        self._conn = None
        self._received = None
        self._got_write = False
        self._done = False
        self.dbg = ["start"]

        envelope = build_contact_envelope(my_name, my_contact)

        # Take the radio over from the proximity feature for this window.
        try:
            proximity.suspend()
        except Exception:
            pass

        result = None
        try:
            # Every setup call is individually guarded: several of these are
            # one-time-only on NimBLE (notably config(mtu=) and gatts_register_
            # services), and re-issuing them on a 2nd exchange raises OSError(22)
            # EINVAL. Guarding + one-shot flags makes run_window fully re-entrant
            # (previously the exchange worked once per launch, then EINVAL'd).
            try:
                self._ble = bluetooth.BLE()
            except Exception as e:
                self.dbg.append("BLE-exc %r" % e)
                self._ble = None
            if self._ble is None:
                return None
            self.dbg.append("BLE()")
            try:
                if not self._ble.active():
                    self._ble.active(True)
            except Exception as e:
                self.dbg.append("active-exc %r" % e)
            if not self._mtu_set:
                try:
                    self._ble.config(mtu=GATT_MTU)
                    self._mtu_set = True
                except Exception as e:
                    self.dbg.append("mtu-exc %r" % e)
            self._seed_events(bluetooth)
            try:
                self._ble.irq(self._irq)
            except Exception as e:
                self.dbg.append("irq-exc %r" % e)
            try:
                self._ensure_service(bluetooth)
            except Exception as e:
                self.dbg.append("svc-exc %r" % e)
            try:
                self._ble.gatts_write(self._h_myinfo, envelope)
            except Exception as e:
                self.dbg.append("write-exc %r" % e)
            self.dbg.append("setup h=%s/%s" % (self._h_myinfo, self._h_theirs))

            my_mac = self._my_mac()
            nonce = (time.ticks_ms() & 0xFFFF)
            adv = build_exchange_adv(nonce=nonce, want=True)

            # Advertise connectable + scan for a peer, until one is found.
            try:
                self._ble.gap_advertise(100000, adv_data=adv, connectable=True)
            except Exception:
                pass
            try:
                self._ble.gap_scan(0, 120000, 60000)
            except Exception:
                pass

            self.dbg.append("setup+adv+scan")
            deadline = time.ticks_add(time.ticks_ms(), WINDOW_MS)
            # Phase 1: rendezvous — wait for a peer beacon (or an inbound connect).
            while (time.ticks_diff(deadline, time.ticks_ms()) > 0 and
                   self._peer is None and self._conn is None):
                await asyncio.sleep_ms(30)

            self.dbg.append("rv peer=%s conn=%s" % (self._peer is not None, self._conn))
            if self._peer is None and self._conn is None:
                self.dbg.append("no-peer")
                return None     # nobody else was swapping

            # Decide our role from the two addresses (unless already connected to).
            if self._conn is not None:
                self._role = "server"
            else:
                peer_addr = self._peer[1]
                self._role = decide_role(my_mac, peer_addr)
            self.dbg.append("role=%s" % self._role)

            if self._role == "client":
                try:
                    self._ble.gap_scan(None)
                except Exception:
                    pass
                try:
                    self._ble.gap_advertise(None)
                except Exception:
                    pass
                result = await self._run_client(bluetooth, time, asyncio, envelope, deadline)
            else:
                # server: keep advertising connectable; the client reads our
                # MYINFO char and writes its envelope to THEIRS.
                try:
                    self._ble.gap_scan(None)
                except Exception:
                    pass
                result = await self._run_server(bluetooth, time, asyncio, deadline)
        except Exception as e:
            self.dbg.append("EXC %r" % e)
            result = None
        finally:
            self.dbg.append("ret=%s" % (result is not None))
            self._teardown_window()
            try:
                proximity.resume()
            except Exception:
                pass
        return result

    async def _run_server(self, bluetooth, time, asyncio, deadline):
        # Wait for the peer to write its envelope (or the connection to end).
        while time.ticks_diff(deadline, time.ticks_ms()) > 0 and not self._got_write:
            await asyncio.sleep_ms(30)
        self.dbg.append("srv got_write=%s conn=%s" % (self._got_write, self._conn))
        # Give the client a moment to finish its read, then drop the link.
        await asyncio.sleep_ms(150)
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except Exception:
                pass
        return self._finalize()

    async def _run_client(self, bluetooth, time, asyncio, envelope, deadline):
        peer_addr_type, peer_addr = self._peer[0], self._peer[1]
        try:
            self._ble.gap_connect(peer_addr_type, peer_addr)
        except Exception as e:
            self.dbg.append("connect-exc %r" % e)
            return None
        # Wait for connection.
        while time.ticks_diff(deadline, time.ticks_ms()) > 0 and self._conn is None:
            await asyncio.sleep_ms(20)
        self.dbg.append("cli conn=%s" % self._conn)
        if self._conn is None:
            return None
        try:
            self._ble.gattc_exchange_mtu(self._conn)
        except Exception:
            pass
        await asyncio.sleep_ms(60)
        # Discover the exchange service's characteristics.
        self._h_read_remote = None
        self._h_write_remote = None
        self._disc = {"chars": []}
        found = await self._discover_client(bluetooth, time, asyncio, deadline)
        self.dbg.append("cli disc=%s r=%s w=%s" % (found, self._h_read_remote, self._h_write_remote))
        if not found:
            self._safe_disconnect()
            return None
        # Read the server's MYINFO, then write our envelope to THEIRS.
        try:
            self._ble.gattc_read(self._conn, self._h_read_remote)
        except Exception as e:
            self.dbg.append("read-exc %r" % e)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0 and self._received is None:
            await asyncio.sleep_ms(20)
        self.dbg.append("cli recv=%s" % (self._received is not None))
        try:
            self._ble.gattc_write(self._conn, self._h_write_remote, envelope, 1)
        except Exception:
            pass
        await asyncio.sleep_ms(150)
        self._safe_disconnect()
        return self._finalize()

    async def _discover_client(self, bluetooth, time, asyncio, deadline):
        # Minimal characteristic discovery: collect (uuid, def_handle, value_handle)
        # via the IRQ, then map our two known UUIDs to their value handles.
        self._chars = []
        Echar = self._E["gattc_characteristic_result"]
        # Temporarily extend the irq to record characteristics.
        want_read = bluetooth.UUID(MYINFO_CHR)
        want_write = bluetooth.UUID(THEIRS_CHR)

        def _char_irq(event, data):
            try:
                if event == Echar:
                    conn, def_handle, value_handle, properties, uuid = data
                    if uuid == want_read:
                        self._h_read_remote = value_handle
                    elif uuid == want_write:
                        self._h_write_remote = value_handle
                self._irq(event, data)
            except Exception:
                pass

        self._ble.irq(_char_irq)
        try:
            self._ble.gattc_discover_characteristics(self._conn, 1, 0xFFFF)
        except Exception:
            pass
        while (time.ticks_diff(deadline, time.ticks_ms()) > 0 and
               (self._h_read_remote is None or self._h_write_remote is None)):
            await asyncio.sleep_ms(20)
        self._ble.irq(self._irq)   # restore
        return self._h_read_remote is not None and self._h_write_remote is not None

    def _finalize(self):
        if not self._received:
            return None
        rssi = self._peer[2] if self._peer else 0
        mac = self._mac_str(self._peer[1]) if self._peer else ""
        return {
            "mac": mac,
            "name": self._received.get("name", ""),
            "fields": self._received.get("fields", {}),
            "rssi": rssi,
        }

    def _safe_disconnect(self):
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except Exception:
                pass

    def _teardown_window(self):
        if not self._ble:
            return
        for fn in (lambda: self._ble.gap_scan(None),
                   lambda: self._ble.gap_advertise(None)):
            try:
                fn()
            except Exception:
                pass
        self._safe_disconnect()
        self._conn = None

    def _my_mac(self):
        try:
            _at, mac = self._ble.config("mac")
            return bytes(mac)
        except Exception:
            return b"\x00\x00\x00\x00\x00\x00"

    @staticmethod
    def _mac_str(addr):
        try:
            return ":".join("%02x" % b for b in addr)
        except Exception:
            return ""
