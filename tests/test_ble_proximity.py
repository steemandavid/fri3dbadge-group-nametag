"""Off-device unit tests for ble_proximity.py pure wire-format functions.

Covers every PLAN.md §10 unit-test bullet:
  - AD payload round-trip encode/decode (1 group, several groups)
  - fnv1a_16 hashing (same name -> same hash after normalization; different
    names -> different hash in practice)
  - per-group hashing + dedup/sort determinism
  - group-set intersection matching (overlap -> match; disjoint -> no match)
  - name truncation at the group-count-dependent budget on a UTF-8 boundary
  - version byte round-trips; unknown version rejected
  - MAX_GROUPS overflow keeps the lowest ids
  - malformed/hostile adverts dropped without raising

These import ONLY the pure functions — no bluetooth/fri3d/lvgl needed.
"""
import ble_proximity as bp
from ble_proximity import (
    fnv1a_16, normalize_group, hash_groups, name_budget, truncate_utf8,
    build_payload, parse_payload, intersect, shared_name_for, build_own_table,
    MAGIC, VERSION, ADV_TOTAL, OVERHEAD, MAX_GROUPS,
)


# ---------------------------------------------------------------------------
# fnv1a_16 hashing
# ---------------------------------------------------------------------------
def test_fnv_is_deterministic():
    assert fnv1a_16(b"Makerspace Baasrode") == fnv1a_16(b"Makerspace Baasrode")


def test_fnv_is_16_bits():
    for s in (b"a", b"hello", b"Makerspace Baasrode", b"x" * 200):
        v = fnv1a_16(s)
        assert 0 <= v <= 0xFFFF


def test_fnv_different_inputs_differ():
    # In practice distinct group names should not collide.
    vals = {fnv1a_16(n) for n in (
        b"alpha", b"beta", b"gamma", b"delta", b"epsilon",
        b"Makerspace Baasrode", b"Hack42", b"RevSpace",
    )}
    assert len(vals) == 8


def test_fnv_known_vector():
    # Known FNV-1a-32 of empty string is 0x811c9dc5 -> xor-fold = 0x811c ^ 0x9dc5
    # = 0x1cd9. Pins the implementation to a reference value.
    assert fnv1a_16(b"") == (0x811C9DC5 ^ (0x811C9DC5 >> 16)) & 0xFFFF


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def test_normalize_collapses_trivial_differences():
    assert normalize_group("Makerspace Baasrode") == normalize_group(" makerspace baasrode ")
    assert normalize_group("RevSpace") == normalize_group("REVSPACE")
    assert normalize_group("  Hack42 ") == "hack42"


def test_normalize_non_string():
    assert normalize_group(None) == ""
    assert normalize_group(123) == ""
    assert normalize_group("") == ""


# ---------------------------------------------------------------------------
# hash_groups: dedup + sort + cap determinism
# ---------------------------------------------------------------------------
def test_hash_groups_dedup_and_sort():
    ids, dropped = hash_groups(["Beta", "Alpha", "beta", " ALPHA ", "Gamma"])
    # 'Beta'/'beta' and 'Alpha'/' ALPHA ' collapse; 3 distinct ids, sorted.
    assert ids == sorted(ids)
    assert len(ids) == 3
    assert dropped == 0


def test_hash_groups_deterministic():
    a, _ = hash_groups(["X", "Y", "Z"])
    b, _ = hash_groups(["Z", "Y", "X"])
    assert a == b  # order-independent


def test_hash_groups_overflow_keeps_lowest():
    # Force a collision-free set larger than MAX_GROUPS.
    found = []
    i = 0
    while len(found) < MAX_GROUPS + 4:
        h = fnv1a_16(("g%d" % i).encode())
        if h not in found:
            found.append(h)
        i += 1
    names = ["g%d" % j for j in range(i) if fnv1a_16(("g%d" % j).encode()) in found]
    ids, dropped = hash_groups(names)
    assert len(ids) == MAX_GROUPS
    assert dropped == 4
    # The kept ids are the MAX_GROUPS lowest of the full set.
    assert ids == sorted(found)[:MAX_GROUPS]


def test_hash_groups_empty_and_garbage():
    ids, dropped = hash_groups([])
    assert ids == [] and dropped == 0
    ids, dropped = hash_groups(["", "   ", None])
    assert ids == [] and dropped == 0


# ---------------------------------------------------------------------------
# build/parse round-trip
# ---------------------------------------------------------------------------
def test_roundtrip_one_group():
    ids, _ = hash_groups(["Makerspace Baasrode"])
    adv = build_payload(ids, "Alex")
    assert len(adv) <= ADV_TOTAL
    info = parse_payload(adv)
    assert info is not None
    assert info["version"] == VERSION
    assert info["group_ids"] == ids
    assert info["name"] == "Alex"


def test_roundtrip_several_groups():
    ids, _ = hash_groups(["Hack42", "RevSpace", "Makerspace Baasrode", "Hacker Hotel"])
    adv = build_payload(ids, "Alice ON4XYZ")
    info = parse_payload(adv)
    assert info is not None
    assert info["group_ids"] == ids
    assert info["name"] == "Alice ON4XYZ"


def test_payload_has_magic_and_version_and_company():
    ids, _ = hash_groups(["X"])
    adv = build_payload(ids, "n")
    # AD header
    assert adv[1] == 0xFF
    body = adv[2:]
    assert body[0:2] == b"\xff\xff"          # company id (LE placeholder)
    assert body[2:2 + len(MAGIC)] == MAGIC   # magic
    assert body[2 + len(MAGIC)] == VERSION   # version byte


def test_payload_within_budget():
    ids, _ = hash_groups(["A", "B", "C", "D", "E"])  # max groups
    adv = build_payload(ids, "A" * 200)              # over-long name
    assert len(adv) <= ADV_TOTAL


# ---------------------------------------------------------------------------
# name truncation on a UTF-8 boundary
# ---------------------------------------------------------------------------
def test_truncate_ascii_boundary():
    assert truncate_utf8("Alex", 3) == "Ale"
    assert truncate_utf8("Alex", 100) == "Alex"
    assert truncate_utf8("Alex", 0) == ""


def test_truncate_never_splits_codepoint():
    # 'é' is 2 bytes in UTF-8 (0xC3 0xA9). Cutting at byte 1 must drop it.
    s = "ééé"            # 6 bytes
    t = truncate_utf8(s, 1)
    assert t.encode("utf-8") == b""           # no partial codepoint
    t = truncate_utf8(s, 2)
    assert t == "é"
    t = truncate_utf8(s, 3)
    assert t == "é"                            # the second 'é' would be split
    t = truncate_utf8(s, 4)
    assert t == "éé"


def test_truncate_multibyte_emoji():
    # '🚀' is 4 bytes in UTF-8.
    s = "a🚀b"
    assert truncate_utf8(s, 1) == "a"
    assert truncate_utf8(s, 2) == "a"          # can't include emoji (needs 4)
    assert truncate_utf8(s, 5) == "a🚀"        # 'a'(1) + '🚀'(4) = 5
    assert truncate_utf8(s, 6) == "a🚀b"


def test_name_truncated_in_payload_roundtrips_cleanly():
    ids, _ = hash_groups(["A", "B", "C"])
    nb = name_budget(len(ids))
    name = "Müller Märchen 🚀" * 5             # lots of multibyte
    adv = build_payload(ids, name)
    info = parse_payload(adv)
    assert info is not None
    # Decoded name fits the budget and never has a dangling partial char.
    assert len(info["name"].encode("utf-8")) <= nb
    info["name"].encode("utf-8")               # must be re-encodable (valid UTF-8)


# ---------------------------------------------------------------------------
# version handling
# ---------------------------------------------------------------------------
def test_unknown_version_rejected():
    ids, _ = hash_groups(["X"])
    adv = bytearray(build_payload(ids, "n"))
    # version byte sits at: AD(2) + company(2) + magic(4) = offset 8
    assert adv[8] == VERSION
    adv[8] = 0x02                              # a future version
    assert parse_payload(bytes(adv)) is None


def test_roundtrip_preserves_version():
    ids, _ = hash_groups(["X"])
    info = parse_payload(build_payload(ids, "n"))
    assert info["version"] == VERSION


# ---------------------------------------------------------------------------
# malformed / hostile adverts never raise
# ---------------------------------------------------------------------------
def test_parse_bad_magic_returns_none():
    ids, _ = hash_groups(["X"])
    adv = bytearray(build_payload(ids, "n"))
    adv[4:8] = b"XXXX"                         # corrupt magic
    assert parse_payload(bytes(adv)) is None


def test_parse_truncated_returns_none():
    ids, _ = hash_groups(["X"])
    adv = build_payload(ids, "n")
    for cut in range(0, len(adv)):
        assert parse_payload(adv[:cut]) is None  # never raises, never half-parses


def test_parse_oversized_length_field_returns_none():
    # Hand-craft a structure whose name length overruns the buffer.
    adv = bytes([0x09, 0xFF]) + b"\xff\xff" + MAGIC + bytes([VERSION, 1, 0x01, 0x00, 99]) + b"ab"
    assert parse_payload(adv) is None


def test_parse_empty_and_garbage():
    assert parse_payload(b"") is None
    assert parse_payload(b"\x00\x00\x00") is None
    assert parse_payload(bytes(range(31))) is None


def test_parse_multiple_ad_structures_finds_ours():
    ids, _ = hash_groups(["X"])
    ours = build_payload(ids, "Bob")
    # Prefix with an unrelated AD structure (e.g. a fake TX-power).
    other = bytes([2, 0x0A, 0x00])
    info = parse_payload(other + ours)
    assert info is not None
    assert info["name"] == "Bob"


# ---------------------------------------------------------------------------
# intersection matching
# ---------------------------------------------------------------------------
def test_intersect_overlap_matches():
    own = hash_groups(["Hack42", "RevSpace"])[0]
    peer = hash_groups(["RevSpace", "FooBar"])[0]
    assert intersect(own, peer)                # shared 'RevSpace'


def test_intersect_disjoint_no_match():
    own = hash_groups(["Hack42"])[0]
    peer = hash_groups(["RevSpace"])[0]
    assert not intersect(own, peer)


def test_intersect_multi_any_one_matches():
    own = hash_groups(["A", "B", "C"])[0]
    peer = hash_groups(["C", "Z"])[0]
    assert intersect(own, peer)


def test_shared_name_uses_lowest_id():
    table = build_own_table(["Zeta", "Alpha", "Mu"])    # ids unknown until hashed
    table.sort(key=lambda t: t[1])                       # by id
    lowest_id = table[0][1]
    # Peer shares all three -> signature is the lowest id.
    sid, sname = shared_name_for(table, [t[1] for t in table])
    assert sid == lowest_id
    assert sname == table[0][0]


def test_shared_name_none_when_disjoint():
    table = build_own_table(["Alpha"])
    sid, sname = shared_name_for(table, [0x1234])
    assert sid is None and sname is None
