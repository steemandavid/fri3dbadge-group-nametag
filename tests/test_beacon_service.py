"""Off-device unit tests for beacon_service.py pure helpers.

Covers the host-testable half (no BLE / mpos needed):
  - load_beacon_config: unconfigured rules (missing/blank name, no groups,
    bad JSON, missing file) and the (own_ids, name) result matching what the
    app itself would advertise.
  - app_in_stack: screen-stack membership by appFullName.
"""
import json

import beacon_service as bs
from ble_proximity import hash_groups

FULLNAME = "com.fri3dcamp.fri3dfriends"


def _write_cfg(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return str(tmp_path)


def test_load_beacon_config_configured(tmp_path):
    d = _write_cfg(tmp_path, {"name": "Alex", "groups": ["Makerspace Baasrode"]})
    own_ids, name = bs.load_beacon_config(d)
    assert name == "Alex"
    assert own_ids == hash_groups(["Makerspace Baasrode"])[0]


def test_load_beacon_config_unconfigured_variants(tmp_path):
    # Blank name, whitespace name, no groups, wrong groups type -> None (silent).
    for cfg in ({"name": "", "groups": ["G"]},
                {"name": "   ", "groups": ["G"]},
                {"name": "Alex", "groups": []},
                {"name": "Alex", "groups": "not-a-list"},
                {"groups": ["G"]},
                {"name": 42, "groups": ["G"]}):
        assert bs.load_beacon_config(_write_cfg(tmp_path, cfg)) is None


def test_load_beacon_config_missing_or_corrupt_file(tmp_path):
    assert bs.load_beacon_config(str(tmp_path)) is None      # no config.json
    (tmp_path / "config.json").write_text("{not json")
    assert bs.load_beacon_config(str(tmp_path)) is None


class _FakeActivity:
    def __init__(self, fullname):
        self.appFullName = fullname


def test_app_in_stack():
    us = (_FakeActivity(FULLNAME), "scr", None, None)
    launcher = (_FakeActivity("com.micropythonos.launcher"), "scr", None, None)
    assert bs.app_in_stack([launcher, us], FULLNAME)          # top
    assert bs.app_in_stack([us, launcher], FULLNAME)          # buried
    assert not bs.app_in_stack([launcher], FULLNAME)
    assert not bs.app_in_stack([], FULLNAME)
    # Entries with a None/odd activity don't blow up the check.
    assert not bs.app_in_stack([(None, "scr", None, None), ("weird",)], FULLNAME)
