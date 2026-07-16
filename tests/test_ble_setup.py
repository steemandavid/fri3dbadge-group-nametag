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


def test_build_setup_adv_layout():
    adv = build_setup_adv("ABCD")
    # Flags AD (len 2, type 0x01, 0x06) then name AD (type 0x09 "Fri3d-ABCD").
    assert adv[:3] == bytes([0x02, 0x01, 0x06])
    name = b"Fri3d-ABCD"
    assert adv[3] == len(name) + 1
    assert adv[4] == 0x09
    assert adv[5:] == name
    assert len(adv) <= 31
