"""Off-device unit tests for web_portal.py pure request/config helpers.

Covers the host-testable half (no asyncio server / sockets needed):
  - form-urlencoded parsing (repeated keys, +/%xx decoding)
  - form_to_config merge: groups split, sound checkbox, numeric coercion,
    free-form contact ck[]/cv[] pairing, bad numbers fall back to defaults.
"""
import web_portal as wp
from web_portal import parse_form, form_to_config, _url_unquote, _esc


def test_url_unquote_utf8_multibyte():
    # Browsers percent-encode form text as UTF-8 bytes; multi-byte characters
    # must be reassembled (not turned into Latin-1 mojibake). (F-1)
    assert _url_unquote("Jos%C3%A9") == "José"
    assert _url_unquote("caf%C3%A9+%26+No%C3%ABl") == "café & Noël"
    # A raw (already-decoded) UTF-8 str passes through unchanged.
    assert _url_unquote("Zoë") == "Zoë"


def test_esc_escapes_single_quote():
    # Every form attribute is single-quoted, so `'` must be escaped. (F-2)
    assert _esc("O'Brien") == "O&#39;Brien"
    assert "'" not in _esc("L'Atelier <b>&\"x\"</b>")


def test_form_to_config_clamps_banner_ms():
    # A 0/negative banner_ms would hide every banner -> clamp to the default. (F-12)
    assert form_to_config(parse_form("banner_ms=0"), {})["banner_ms"] == 5000
    assert form_to_config(parse_form("banner_ms=-5000"), {})["banner_ms"] == 5000


def test_parse_form_basic_and_encoding():
    f = parse_form("a=1&b=hello+world&c=%40home")
    assert f["a"] == ["1"]
    assert f["b"] == ["hello world"]
    assert f["c"] == ["@home"]


def test_parse_form_repeated_keys_keep_order():
    f = parse_form("ck=discord&cv=dave&ck=web&cv=site")
    assert f["ck"] == ["discord", "web"]
    assert f["cv"] == ["dave", "site"]


def test_parse_form_empty_and_valueless():
    f = parse_form("")
    assert f == {}
    f = parse_form("flag&x=1")
    assert f["flag"] == [""]
    assert f["x"] == ["1"]


def test_form_to_config_full():
    form = parse_form("name=David&groups=Alpha%2C+Beta+%2C%2C&"
                      "rssi_floor=-80&banner_ms=3000&sound=on&"
                      "ck=discord&cv=dave%231&ck=btc&cv=bc1qxyz")
    cfg = form_to_config(form, {"keep": "me"})
    assert cfg["keep"] == "me"                     # base preserved
    assert cfg["name"] == "David"
    assert "handle" not in cfg                     # handle field removed
    assert cfg["groups"] == ["Alpha", "Beta"]      # trimmed, empties dropped
    assert cfg["rssi_floor"] == -80
    assert cfg["banner_ms"] == 3000
    assert cfg["sound"] is True
    assert cfg["contact"] == {"discord": "dave#1", "btc": "bc1qxyz"}


def test_form_to_config_sound_unchecked_and_bad_numbers():
    cfg = form_to_config(parse_form("name=X&rssi_floor=abc&banner_ms="), {})
    assert cfg["sound"] is False                   # checkbox absent
    assert cfg["rssi_floor"] == -120               # fallback
    assert cfg["banner_ms"] == 5000                # fallback


def test_form_to_config_contact_skips_blank_keys():
    cfg = form_to_config(parse_form("name=X&ck=&cv=orphan&ck=real&cv=v"), {})
    assert cfg["contact"] == {"real": "v"}
