"""Pytest conftest: make the on-device app module importable on the host.

`ble_proximity.py` lives in the app folder; here we put that folder first on
sys.path so the host tests can `import ble_proximity` straight away, without a
full device tree. The module keeps `import bluetooth`/`mpos`/`lvgl` out of the
pure wire-format functions' import path (they are imported lazily inside the
radio wrapper), so this import succeeds on CPython.
"""
import os
import sys

_APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "com.fri3dcamp.groupnametag")
)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
