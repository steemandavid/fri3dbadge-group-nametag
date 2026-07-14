"""Off-device unit tests for contact_exchange.py pure functions.

Covers the wire-format + storage half (no bluetooth/mpos/asyncio needed):
  - exchange beacon build/parse round-trip; malformed/foreign adverts dropped
  - unknown version rejected; want-flag round-trips
  - decide_role tie-break: deterministic, symmetric, exactly one initiator
  - contact envelope build/parse round-trip (unicode, size cap, dropped fields)
  - parse_contact_envelope defends against garbage
  - add_received appends one entry per swap (no dedup), cap/evict oldest
"""
import contact_exchange as cx
from contact_exchange import (
    build_exchange_adv, parse_exchange_adv, decide_role,
    build_contact_envelope, parse_contact_envelope, add_received,
    X_MAGIC, X_VERSION, MAX_CONTACT_BYTES,
)


# ---------------------------------------------------------------------------
# exchange beacon
# ---------------------------------------------------------------------------
def test_exchange_adv_round_trip():
    adv = build_exchange_adv(nonce=0x1234, want=True)
    info = parse_exchange_adv(adv)
    assert info is not None
    assert info["version"] == X_VERSION
    assert info["nonce"] == 0x1234
    assert info["want"] is True


def test_exchange_adv_want_false():
    info = parse_exchange_adv(build_exchange_adv(nonce=1, want=False))
    assert info is not None and info["want"] is False


def test_exchange_adv_has_magic_and_is_short():
    adv = build_exchange_adv(nonce=0xFFFF)
    assert X_MAGIC in adv
    assert len(adv) <= 31          # fits a legacy advertisement


def test_exchange_adv_nonce_wraps():
    info = parse_exchange_adv(build_exchange_adv(nonce=0x1FFFF))
    assert info["nonce"] == 0xFFFF   # masked to 16 bits


def test_parse_rejects_non_bytes():
    assert parse_exchange_adv(None) is None
    assert parse_exchange_adv("hello") is None


def test_parse_rejects_foreign_advert():
    # A well-formed manufacturer AD but not ours (wrong magic).
    foreign = bytes([7, 0xFF, 0xFF, 0xFF]) + b"NOPE"
    assert parse_exchange_adv(foreign) is None


def test_parse_rejects_unknown_version():
    adv = bytearray(build_exchange_adv(nonce=5))
    # version byte sits right after company(2)+magic(4) inside the AD body:
    #   [len][type][company:2][magic:4][ver] -> index 2+2+4 = 8
    adv[8] = 0x7F
    assert parse_exchange_adv(bytes(adv)) is None


def test_parse_survives_truncation():
    adv = build_exchange_adv(nonce=5)
    for i in range(len(adv)):
        # must never raise, whatever the prefix length
        parse_exchange_adv(adv[:i])


# ---------------------------------------------------------------------------
# decide_role
# ---------------------------------------------------------------------------
def test_decide_role_lower_is_server():
    lo = b"\x00\x11\x22\x33\x44\x55"
    hi = b"\xaa\xbb\xcc\xdd\xee\xff"
    assert decide_role(lo, hi) == "server"
    assert decide_role(hi, lo) == "client"


def test_decide_role_is_symmetric_single_initiator():
    a = b"\x12\x34\x56\x78\x9a\xbc"
    b = b"\x12\x34\x56\x78\x9a\xbd"   # differs only in last byte
    # Exactly one of the two sides is the client (the initiator).
    roles = {decide_role(a, b), decide_role(b, a)}
    assert roles == {"server", "client"}


def test_decide_role_equal_is_server():
    m = b"\x01\x02\x03\x04\x05\x06"
    assert decide_role(m, m) == "server"


# ---------------------------------------------------------------------------
# contact envelope
# ---------------------------------------------------------------------------
def test_envelope_round_trip():
    fields = {"discord": "dave#1234", "website": "steeman.be", "btc": "bc1qxyz"}
    env = build_contact_envelope("David Steeman", fields)
    out = parse_contact_envelope(env)
    assert out["name"] == "David Steeman"
    assert out["fields"] == fields


def test_envelope_unicode():
    env = build_contact_envelope("Séb ⚡", {"note": "café ☕"})
    out = parse_contact_envelope(env)
    assert out["name"] == "Séb ⚡"
    assert out["fields"]["note"] == "café ☕"


def test_envelope_coerces_non_string_values():
    env = build_contact_envelope("N", {"phone": 12345, "bad": None})
    out = parse_contact_envelope(env)
    assert out["fields"]["phone"] == "12345"
    assert "bad" not in out["fields"]     # None dropped


def test_envelope_respects_size_cap():
    big = {("k%02d" % i): ("v" * 40) for i in range(50)}
    env = build_contact_envelope("Name", big, max_bytes=200)
    assert len(env) <= 200
    out = parse_contact_envelope(env)
    assert out["name"] == "Name"          # name always survives
    assert len(out["fields"]) < len(big)  # some fields dropped to fit


def test_parse_envelope_rejects_garbage():
    assert parse_contact_envelope(b"\x00\x01\x02not json") is None
    assert parse_contact_envelope("[1,2,3]") is None    # not a dict
    assert parse_contact_envelope("null") is None


# ---------------------------------------------------------------------------
# add_received  (one entry per swap, no dedup)
# ---------------------------------------------------------------------------
def _entry(mac, name, fields, at, ticks=0, rssi=-50):
    return {"mac": mac, "name": name, "fields": fields,
            "received_at": at, "received_ticks": ticks, "rssi": rssi}


def test_add_appends_new():
    store = []
    add_received(store, _entry("aa", "Alice", {"x": "1"}, "2026-07-12T10:00:00"))
    assert len(store) == 1
    r = store[0]
    assert r["name"] == "Alice"
    assert r["fields"] == {"x": "1"}
    assert r["received_at"] == "2026-07-12T10:00:00"
    assert r["mac"] == "aa"


def test_add_same_mac_creates_separate_entries():
    # Swapping again with the SAME badge yields a second, independent entry.
    store = []
    add_received(store, _entry("aa", "Alice", {"x": "1"}, "2026-07-12T10:00:00"))
    add_received(store, _entry("aa", "Alice B", {"x": "2"}, "2026-07-12T11:00:00"))
    assert len(store) == 2
    assert [r["name"] for r in store] == ["Alice", "Alice B"]
    assert [r["received_at"] for r in store] == ["2026-07-12T10:00:00", "2026-07-12T11:00:00"]
    assert [r["fields"] for r in store] == [{"x": "1"}, {"x": "2"}]


def test_add_fields_snapshot_is_independent():
    # Each entry keeps its own copy of the fields dict (no shared reference).
    store = []
    f = {"x": "1"}
    add_received(store, _entry("aa", "A", f, "t1"))
    f["x"] = "mutated"
    assert store[0]["fields"] == {"x": "1"}


def test_add_distinct_macs():
    store = []
    add_received(store, _entry("aa", "A", {}, "t1"))
    add_received(store, _entry("bb", "B", {}, "t2"))
    assert [r["mac"] for r in store] == ["aa", "bb"]


def test_add_caps_and_evicts_oldest():
    store = []
    for i in range(5):
        add_received(store, _entry("m%02d" % i, "n%d" % i, {},
                                   "2026-07-12T10:%02d:00" % i), max_contacts=3)
    assert len(store) == 3
    names = [r["name"] for r in store]
    assert names == ["n2", "n3", "n4"]      # two oldest-appended dropped, order kept
