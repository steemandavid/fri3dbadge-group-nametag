# beacon_service.py — background BLE beacon for "!Fri3d Friends".
#
# Keeps this badge visible to friends' badges while the app itself is closed:
# a boot service (MANIFEST "services" -> boot_completed) that advertises the
# same non-connectable proximity beacon the app sends, whenever the app is NOT
# on the screen stack. Advertise-only — no scanning, no alerts, no contact
# swap in the background; open the app for those. Advertising is fire-and-
# forget on NimBLE, so the service needs no IRQ handler and only a slow
# watchdog loop (poll the app state, keep the beacon asserted).
#
# Radio ownership rule (single BLE() stack, single adv set):
#   - App anywhere on the screen stack  -> hands OFF the radio entirely; the
#     Activity begins/suspends/tears down BLE through its own lifecycle.
#   - App not on the stack              -> the service owns the radio:
#     active(True) + non-connectable advertise, re-asserted every ~60 s in
#     case another app touched it.
# The handoff needs no coordination calls: the app's begin() simply replaces
# our (identical) adv when it opens, and its teardown active(False) is undone
# by the next watchdog poll after it exits.

import sys
import json

APP_DIR = "/apps/com.fri3dcamp.fri3dfriends"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from ble_proximity import build_payload, build_own_table, hash_groups, ADV_MS

try:
    from mpos import Service, TaskManager
except ImportError:              # host-side unit tests: no mpos on CPython
    TaskManager = None

    class Service:
        def __init__(self):
            pass


POLL_MS = 5000                   # app-state / beacon-health check cadence
REFRESH_POLLS = 6                # re-assert the adv every 6 polls (~30 s)


def load_beacon_config(app_dir=APP_DIR):
    """Read config.json -> (own_ids, name), or None if unconfigured/unreadable.

    Mirrors Fri3dFriends._load_config's rule: a badge with no name or no
    valid group is "unconfigured" and must stay off the air (README promise).
    """
    try:
        with open(app_dir + "/config.json") as f:
            cfg = json.load(f)
    except Exception:
        return None
    groups = cfg.get("groups")
    if not isinstance(groups, list):
        return None
    name = cfg.get("name")
    name = name.strip() if isinstance(name, str) else ""
    ids = [gid for _, gid in build_own_table(groups)]
    if not name or not ids:
        return None
    own_ids, _ = hash_groups(groups)
    return own_ids, name


def app_in_stack(stack, fullname):
    """True if any activity on the screen stack belongs to `fullname`.

    Checks the whole stack, not just the top: if our Activity is buried under
    another one its BLE is already torn down (onPause), but its lifecycle will
    take the radio straight back on resume — the service must not fight it.
    Pure (operates on a list of (activity, ...) tuples) for host testing.
    """
    for entry in stack:
        act = entry[0] if entry else None
        if act is not None and getattr(act, "appFullName", None) == fullname:
            return True
    return False


class Fri3dBeaconService(Service):
    def __init__(self):
        super().__init__()
        self._running = False
        self._task = None
        self._ble = None
        self._advertising = False
        self._polls = 0

    # ---- Service lifecycle ----
    def onStart(self, intent=None):
        self._running = True
        try:
            self._task = TaskManager.create_task(self._watchdog())
        except Exception:
            self._running = False

    def onDestroy(self):
        self._running = False
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self._stop_beacon()

    # ---- radio ----
    def _app_is_open(self):
        try:
            from mpos.ui import view
            fullname = getattr(self, "appFullName", None) or "com.fri3dcamp.fri3dfriends"
            return app_in_stack(view.screen_stack, fullname)
        except Exception:
            return True          # can't tell -> assume open, keep hands off

    def _start_beacon(self):
        cfg = load_beacon_config()
        if cfg is None:          # unconfigured badge: stay silent
            self._stop_beacon()
            return
        own_ids, name = cfg
        try:
            from bluetooth import BLE
            adv = build_payload(own_ids, name)
            self._ble = BLE()
            self._ble.active(True)
            self._ble.irq(None)  # drop a stale IRQ left by the closed app
            self._ble.gap_advertise(ADV_MS * 1000, adv_data=adv, connectable=False)
            self._advertising = True
        except Exception:
            self._advertising = False   # retry on a later poll

    def _stop_beacon(self):
        if self._advertising:
            try:
                if self._ble:
                    self._ble.gap_advertise(None)
            except Exception:
                pass
        self._advertising = False
        self._ble = None

    # ---- watchdog ----
    async def _watchdog(self):
        import asyncio
        while self._running:
            # One guard around body AND sleep, and it must catch BaseException,
            # not just Exception: a Ctrl-C on the USB console is delivered as
            # KeyboardInterrupt to whatever coroutine is running, and if it
            # lands in our await it would otherwise kill the beacon for good.
            # Only cancellation (onDestroy) may end this loop.
            try:
                if self._app_is_open():
                    # The Activity owns the radio (its begin() replaced our adv
                    # with an identical one). Just forget our state — never call
                    # into BLE from here while the app could be mid-lifecycle.
                    self._advertising = False
                    self._ble = None
                    self._polls = 0
                else:
                    self._polls += 1
                    if not self._advertising or self._polls >= REFRESH_POLLS:
                        self._polls = 0
                        self._start_beacon()   # rereads config; no-op if unconfigured
                await asyncio.sleep_ms(POLL_MS)
            except asyncio.CancelledError:
                raise
            except BaseException:
                try:
                    await asyncio.sleep_ms(POLL_MS)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    pass
