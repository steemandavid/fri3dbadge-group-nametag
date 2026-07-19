"""Off-device unit tests for ble_setup.py pure protocol helpers.

Covers the host-testable half (no bluetooth/asyncio radio needed):
  - sanitize_config: the BLE equivalent of the old web_portal.form_to_config
    (groups split, sound, numeric coercion/clamp, contact dict, partial merge).
  - ChunkAssembler: reassembly, out-of-order, re-send, restart, overflow.
  - contacts_response: header + paged slices + end-of-stream.
  - AuthState: correct/wrong code, lockout + rotation after MAX_FAILS.
  - badge_id / setup_name / build_info / build_setup_adv byte layout.
"""
import json

import ble_setup as bs
from ble_setup import (
    sanitize_config, ChunkAssembler, contacts_response, AuthState,
    badge_id, setup_name, build_info, build_setup_adv,
    CONTACTS_HEADER_OFFSET, CONTACTS_PAGE, MAX_CFG_BYTES,
)


# --------------------------------------------------------------- sanitize_config

def test_sanitize_config_full():
    new = {"name": "David", "groups": ["Alpha", " Beta ", "", "  "],
           "rssi_floor": -80, "banner_ms": 3000, "sound": True,
           "contact": {"discord": "dave#1", "btc": " bc1qxyz "}}
    cfg = sanitize_config(new, {"keep": "me"})
    assert cfg["keep"] == "me"                     # base preserved
    assert cfg["name"] == "David"
    assert cfg["groups"] == ["Alpha", "Beta"]      # trimmed, empties dropped
    assert cfg["rssi_floor"] == -80
    assert cfg["banner_ms"] == 3000
    assert cfg["sound"] is True
    assert cfg["contact"] == {"discord": "dave#1", "btc": "bc1qxyz"}


def test_sanitize_config_groups_as_comma_string():
    cfg = sanitize_config({"groups": "Alpha, Beta ,,"}, {})
    assert cfg["groups"] == ["Alpha", "Beta"]


def test_sanitize_config_clamps_banner_ms():
    # A 0/negative banner_ms would hide every banner -> clamp to the default.
    assert sanitize_config({"banner_ms": 0}, {})["banner_ms"] == 5000
    assert sanitize_config({"banner_ms": -5000}, {})["banner_ms"] == 5000


def test_sanitize_config_bad_numbers_fall_back():
    cfg = sanitize_config({"name": "X", "rssi_floor": "abc", "banner_ms": "x"}, {})
    assert cfg["rssi_floor"] == -120
    assert cfg["banner_ms"] == 5000


def test_sanitize_config_sound_defaults_and_explicit_false():
    assert sanitize_config({"name": "X"}, {})["sound"] is True     # default
    assert sanitize_config({"sound": False}, {})["sound"] is False
    assert sanitize_config({"name": "X"}, {"sound": False})["sound"] is False  # keep base


def test_sanitize_config_partial_keeps_base():
    base = {"name": "Old", "groups": ["G"], "contact": {"web": "x"}, "rssi_floor": -70,
            "banner_ms": 2000, "sound": False}
    cfg = sanitize_config({"name": "New"}, base)
    assert cfg["name"] == "New"
    assert cfg["groups"] == ["G"]                  # untouched
    assert cfg["contact"] == {"web": "x"}
    assert cfg["rssi_floor"] == -70
    assert cfg["banner_ms"] == 2000
    assert cfg["sound"] is False


def test_sanitize_config_contact_skips_blank_keys_and_coerces():
    cfg = sanitize_config({"contact": {"": "orphan", "  ": "x", "real": "v", 5: "no"}}, {})
    assert cfg["contact"] == {"real": "v"}


def test_sanitize_config_non_dict_input():
    # A non-dict payload returns the base copy unchanged (never raises).
    assert sanitize_config("not a dict", {"name": "keep"}) == {"name": "keep"}
    assert sanitize_config(None, {"a": 1}) == {"a": 1}


def test_sanitize_config_utf8_names_preserved():
    cfg = sanitize_config({"name": "José", "contact": {"café": "Noël"}}, {})
    assert cfg["name"] == "José"
    assert cfg["contact"] == {"café": "Noël"}


# --------------------------------------------------------------- ChunkAssembler

def _frame(seq, total, payload):
    return bytes([seq, total]) + payload


def test_chunk_assembler_single_chunk():
    a = ChunkAssembler()
    assert a.feed(_frame(0, 1, b'{"name":"x"}')) == b'{"name":"x"}'


def test_chunk_assembler_multi_chunk_in_order():
    a = ChunkAssembler()
    assert a.feed(_frame(0, 3, b"aaa")) is None
    assert a.feed(_frame(1, 3, b"bbb")) is None
    assert a.feed(_frame(2, 3, b"ccc")) == b"aaabbbccc"


def test_chunk_assembler_out_of_order_and_resend():
    a = ChunkAssembler()
    assert a.feed(_frame(2, 3, b"ccc")) is None
    assert a.feed(_frame(0, 3, b"aaa")) is None
    assert a.feed(_frame(0, 3, b"aaa")) is None    # duplicate, still incomplete
    assert a.feed(_frame(1, 3, b"bbb")) == b"aaabbbccc"


def test_chunk_assembler_restart_on_new_total():
    a = ChunkAssembler()
    a.feed(_frame(0, 3, b"aaa"))
    # A fresh stream with a different total abandons the old partial.
    assert a.feed(_frame(0, 1, b"z")) == b"z"


def test_chunk_assembler_overflow():
    a = ChunkAssembler(max_bytes=10)
    assert a.feed(_frame(0, 2, b"12345")) is None
    a.feed(_frame(1, 2, b"1234567890"))            # now over 10 bytes total
    assert a.overflow is True


def test_chunk_assembler_reset_clears_overflow():
    a = ChunkAssembler(max_bytes=4)
    a.feed(_frame(0, 1, b"12345"))
    assert a.overflow is True
    a.reset()
    assert a.overflow is False
    assert a.feed(_frame(0, 1, b"ok")) == b"ok"


# --------------------------------------------------------------- contacts_response

def test_contacts_response_header():
    data = b'[{"name":"a"}]'
    hdr = json.loads(contacts_response(data, CONTACTS_HEADER_OFFSET).decode())
    assert hdr == {"len": len(data), "page": CONTACTS_PAGE}


def test_contacts_response_paging_reassembles():
    data = bytes((i % 256 for i in range(1000)))   # arbitrary 1000 bytes
    out = bytearray()
    off = 0
    while True:
        slice_ = contacts_response(data, off, page=400)
        if not slice_:
            break
        out.extend(slice_)
        off += 400
    assert bytes(out) == data


def test_contacts_response_out_of_range_empty():
    assert contacts_response(b"abc", 100) == b""
    assert contacts_response(b"abc", 3) == b""     # exactly at end


def test_contacts_response_bad_input():
    assert contacts_response(None, 0) == b"[]"


# --------------------------------------------------------------- AuthState

def test_auth_state_correct_code():
    a = AuthState(rand=lambda n: 234, clock=lambda: 0)  # code = 1000 + 234 = 1234
    a.new_code()
    assert a.code == "1234"
    assert a.check("1234") is True
    assert a.check("0000") is False


def test_auth_state_lockout_and_rotation():
    t = [0]
    codes = iter([1111 - 1000, 2222 - 1000])   # first code 1111, rotated code 2222
    a = AuthState(max_fails=3, lockout_ms=60000,
                  rand=lambda n: next(codes), clock=lambda: t[0])
    a.new_code()
    assert a.code == "1111"
    assert a.check("9999") is False            # fail 1
    assert a.check("9999") is False            # fail 2
    assert a.locked() is False
    assert a.check("9999") is False            # fail 3 -> lockout + rotate
    assert a.locked() is True
    assert a.code == "2222"                    # rotated
    # Even the (new) correct code is refused while locked.
    assert a.check("2222") is False
    # After the lockout elapses, the rotated code works.
    t[0] = 60001
    assert a.locked() is False
    assert a.check("2222") is True


# --------------------------------------------------------------- misc builders

def test_badge_id_and_name():
    assert badge_id(b"\x00\x11\x22\x33\xab\xcd") == "ABCD"
    assert badge_id(bytes([1, 2, 3, 4, 0x0f, 0x0a])) == "0F0A"
    assert setup_name("ABCD") == "Fri3d-ABCD"
    assert badge_id("bad") == "0000"           # defensive


def test_build_info_pre_and_post_auth():
    pre = json.loads(build_info("ABCD", False).decode())
    assert pre == {"v": 1, "badge": "ABCD", "authed": False}
    cfg = {"name": "Dave", "groups": ["G"], "rssi_floor": -80,
           "sound": False, "banner_ms": 1000, "contact": {"web": "x"}}
    post = json.loads(build_info("ABCD", True, cfg).decode())
    assert post["authed"] is True
    assert post["name"] == "Dave"
    assert post["groups"] == ["G"]
    assert post["contact"] == {"web": "x"}
    assert post["sound"] is False


class _FakeClock:
    """Minimal MicroPython-style ticks_* shim so window_secs_left is testable
    on the host (CPython's time module lacks ticks_ms/ticks_diff/ticks_add)."""
    def __init__(self):
        self.now = 0
    def ticks_ms(self):
        return self.now
    def ticks_diff(self, a, b):
        return a - b
    def ticks_add(self, a, d):
        return a + d


def test_window_secs_left_resets_on_activity(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(bs, "time", clock)
    svc = bs.SetupService(app_dir="/x", exchange=None)

    # No idle limit configured (configure mode) -> None.
    assert svc.window_secs_left() is None

    svc._idle_ms = 120000
    svc._last_activity = clock.ticks_ms()
    assert svc.window_secs_left() == 120           # full window

    clock.now += 30000                             # 30 s of idle time
    assert svc.window_secs_left() == 90

    svc._last_activity = clock.ticks_ms()          # activity resets the window
    assert svc.window_secs_left() == 120

    clock.now += 500000                            # long past the window
    assert svc.window_secs_left() == 0             # clamps at 0, never negative


class _FakeBLE:
    """Minimal gatts sink so SetupService's write paths run off-device."""
    def __init__(self):
        self.writes = []      # (handle, bytes)
        self.notifies = []
    def gatts_write(self, h, d, *a):
        self.writes.append((h, bytes(d)))
    def gatts_notify(self, c, h, d):
        self.notifies.append((h, bytes(d)))
    def gatts_set_buffer(self, *a):
        pass

    def last_write(self, handle):
        for h, d in reversed(self.writes):
            if h == handle:
                return d
        return None


def _authed_service():
    """A SetupService wired to a fake radio, with a counted contacts loader and a
    deterministic auth code ('1000'). Returns (svc, fake_ble, load_calls)."""
    svc = bs.SetupService(app_dir="/x", exchange=None)
    fb = _FakeBLE()
    svc._ble = fb
    svc._h = {"auth": 1, "info": 2, "cfg": 3, "status": 4, "contacts": 5, "ctloff": 6}
    svc._badge_id = "ABCD"
    load_calls = {"n": 0}
    blob = json.dumps([{"name": "friend %02d" % i, "fields": {"web": "x" * 20}}
                       for i in range(20)]).encode("utf-8")   # multi-page (>490 B)
    svc._contacts_blob = blob

    def fake_load():
        load_calls["n"] += 1
        return blob
    svc._load_contacts_bytes = fake_load
    svc._auth = AuthState(rand=lambda n: 0)
    svc._auth.new_code()                                       # -> "1000"
    return svc, fb, load_calls


def test_auth_snapshots_contacts_on_the_loop():
    # Change B: a successful auth loads contacts.json ONCE, on the loop.
    svc, fb, load_calls = _authed_service()
    assert len(svc._contacts_blob) > CONTACTS_PAGE             # exercises paging
    svc._handle_auth(b"1000")
    assert svc._authed is True
    assert load_calls["n"] == 1
    assert svc._contacts_cache == svc._contacts_blob


def test_serve_contacts_never_touches_flash_and_slices_cache():
    # Change B: every page is sliced from the auth-time snapshot; the IRQ path
    # (_serve_contacts) does NO further file I/O, whatever offset is requested.
    svc, fb, load_calls = _authed_service()
    svc._handle_auth(b"1000")
    assert load_calls["n"] == 1
    blob = svc._contacts_blob

    def off_bytes(o):
        return bytes([o & 0xFF, (o >> 8) & 0xFF])

    # header request
    svc._serve_contacts(off_bytes(CONTACTS_HEADER_OFFSET))
    assert fb.last_write(5) == contacts_response(blob, CONTACTS_HEADER_OFFSET)
    # every page, including one past the first page boundary
    for off in (0, CONTACTS_PAGE, 2 * CONTACTS_PAGE, len(blob)):
        svc._serve_contacts(off_bytes(off))
        assert fb.last_write(5) == contacts_response(blob, off)
    # …and not one extra flash read happened in the IRQ across all of that.
    assert load_calls["n"] == 1

    # Reassembling every page must reproduce the original blob exactly.
    reassembled = b""
    off = 0
    while off < len(blob):
        page = contacts_response(blob, off)
        if not page:
            break
        reassembled += page
        off += len(page)
    assert reassembled == blob


def test_serve_contacts_denied_before_auth():
    # An unauthenticated central gets nothing (no page written, no flash read).
    svc, fb, load_calls = _authed_service()
    svc._authed = False
    svc._serve_contacts(bytes([0, 0]))
    assert fb.last_write(5) is None
    assert load_calls["n"] == 0


def test_build_setup_adv_layout():
    adv = build_setup_adv("ABCD")
    # Flags AD (len 2, type 0x01, 0x06) then name AD (type 0x09 "Fri3d-ABCD").
    assert adv[:3] == bytes([0x02, 0x01, 0x06])
    name = b"Fri3d-ABCD"
    assert adv[3] == len(name) + 1
    assert adv[4] == 0x09
    assert adv[5:] == name
    assert len(adv) <= 31
