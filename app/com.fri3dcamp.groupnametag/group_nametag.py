# group_nametag.py — Group Nametag + BLE Proximity Finder.
#
# A MicroPythonOS Activity for the Fri3d Camp 2024 badge (2024 HW, MicroPythonOS
# 0.11.1). Shows a group logo + the wearer's name, and alerts when another badge
# sharing at least one group comes within Bluetooth range.
#
# Adapted from PLAN.md: the behavioural design (BLE protocol, multi-group
# matching, proximity state machine, per-group signature, alerts, idle UI) is
# unchanged; only the framework shell moved from the (absent) fri3d.application
# `App` class to MicroPythonOS's `Activity`. See DESIGN.md.
#
# Controls (raw button pins; see DESIGN.md):
#   X      = mute/unmute the alert buzzer
#   A      = toggle the nearby-list detail view
#   START  = exit back to the launcher   (OS back gesture also works)

import os
import sys
import math
import time
import json
import asyncio

# App dir on the device filesystem (sibling modules + config + logo live here).
APP_DIR = "/apps/com.fri3dcamp.groupnametag"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import lvgl as lv
from mpos import Activity, TaskManager, BatteryManager, lights

from ble_proximity import (
    BLEProximity, build_own_table, hash_groups, fnv1a_16,
    EVICT_MS, RSSI_FLOOR_DEFAULT,
)

W, H = 296, 240

# Tunables
BREATH_PERIOD_MS = 2600
BREATH_AMP = 14          # scale units (256 == 1.0x)
BANNER_MS = 2500
DIM_MS = 30000           # backlight idle dim
TICK_MS = 30

# Button GPIOs (Fri3d 2024 badge; pull-up, value()==0 == pressed)
BTN = {"start": 0, "x": 38, "a": 39, "b": 40, "y": 41, "menu": 45}

# Palette (RGB888)
COL_BG = 0x0B0E14
COL_NAME = 0xFFFFFF
COL_HANDLE = 0x9FB4D0
COL_OWN = 0x6FBF73
COL_NEAR = 0xFFE066
COL_NONE = 0x6A7280
COL_HINT = 0xFF8C42
COL_BATT = 0x8FA8B8
COL_BANNER = 0x143A2A


def _hsv(h, s=0.85, v=0.6):
    """ hsv->rgb tuple, h in degrees. Used for per-group LED colour signature. """
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:    r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:         r, g, b = c, 0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


def _sig_from_id(gid):
    """Deterministic (hue, freq) per-group signature from a 16-bit group id."""
    hue = (gid * 137.508) % 360          # golden-angle hue spread
    freq = 440 + (gid % 12) * 55         # ~one octave+ of buzz tones
    return hue, freq


class GroupNametag(Activity):
    def __init__(self):
        super().__init__()
        self._ble = BLEProximity()
        self._config = {}
        self._own_table = []
        self._unconfigured = False
        self._sound = True
        self._detail = False
        self._btn_pins = {}
        self._prev = {}
        self._task = None
        self._t0 = 0
        self._last_input_ms = 0
        self._logo_im = None
        self._logo_base_scale = 256
        self._banner = None
        self._banner_bg = None
        self._banner_until = 0
        self._buzzer = None
        self._disp = None
        self._dimmed = False
        # widgets (created in _build_idle for configured mode)
        self._name_lbl = None
        self._handle_lbl = None
        self._own_lbl = None
        self._batt_lbl = None
        self._near_lbl = None
        self._detail_lbl = None
        self._batt_next_ms = 0

    # ------------------------------------------------------------------ config
    def _load_config(self):
        cfg = {"groups": [], "name": "", "handle": "", "rssi_floor": RSSI_FLOOR_DEFAULT}
        try:
            with open(APP_DIR + "/config.json", "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
        # validate / normalise
        if not isinstance(cfg.get("groups"), list):
            cfg["groups"] = []
        cfg["name"] = (cfg.get("name") or "").strip()
        cfg["handle"] = (cfg.get("handle") or "").strip()
        try:
            rf = int(cfg.get("rssi_floor", RSSI_FLOOR_DEFAULT))
        except (TypeError, ValueError):
            rf = RSSI_FLOOR_DEFAULT
        cfg["rssi_floor"] = rf
        self._config = cfg
        self._own_table = build_own_table(cfg["groups"])
        ids = [gid for _, gid in self._own_table]
        self._unconfigured = (not cfg["name"]) or (not ids)

    # ------------------------------------------------------------------ buttons
    def _setup_buttons(self):
        from machine import Pin
        self._btn_pins = {}
        for name, gp in BTN.items():
            try:
                self._btn_pins[name] = Pin(gp, Pin.IN, Pin.PULL_UP)
            except Exception:
                pass

    def _held(self, name):
        p = self._btn_pins.get(name)
        if p is None:
            return False
        try:
            return p.value() == 0
        except Exception:
            return False

    def _edge(self, name):
        cur = self._held(name)
        prev = self._prev.get(name, False)
        self._prev[name] = cur
        return name if (cur and not prev) else ""

    def _wake(self):
        self._last_input_ms = time.ticks_ms()
        if self._dimmed:
            self._set_brightness(255)
            self._dimmed = False

    # ------------------------------------------------------------------ buzzer
    def _setup_buzzer(self):
        try:
            from machine import PWM, Pin
            self._buzzer = PWM(Pin(46), freq=2000, duty_u16=0)
        except Exception:
            self._buzzer = None

    async def _sting(self, freq):
        if not self._sound or not self._buzzer:
            return
        try:
            self._buzzer.freq(int(freq))
            self._buzzer.duty_u16(16000)
            await asyncio.sleep_ms(120)
            self._buzzer.freq(int(freq) * 3 // 2)
            await asyncio.sleep_ms(90)
            self._buzzer.duty_u16(0)
        except Exception:
            pass

    # ------------------------------------------------------------------ display
    def _setup_display(self):
        # Backlight/brightness API is absent on this build (verified), so dim is
        # disabled — self._disp stays None and _set_brightness/_tick_dim no-op.
        try:
            d = lv.display_get_default()
            self._disp = d if hasattr(d, "set_brightness") else None
        except Exception:
            self._disp = None

    def _set_brightness(self, v):
        if self._disp is None:
            return
        try:
            self._disp.set_brightness(v)   # API present? defensive (DESIGN.md)
        except Exception:
            self._disp = None              # API absent -> disable dim feature

    # ------------------------------------------------------------------ logo
    def _place_logo(self, scr):
        try:
            self._logo_im = lv.image(scr)
            self._logo_im.set_src("S:" + APP_DIR + "/logo.png")
        except Exception:
            self._logo_im = self._placeholder_logo(scr)
        # fit into a centered upper box; centre so breathing scales in place
        self._logo_im.align(lv.ALIGN.TOP_MID, 0, 18)

    def _placeholder_logo(self, scr):
        # Runtime fallback if logo.png is missing/broken: a coloured disc + tag.
        obj = lv.obj(scr)
        obj.remove_style_all()
        obj.set_size(100, 100)
        obj.set_style_bg_color(lv.color_hex(0x2A6F4F), 0)
        obj.set_style_bg_opa(lv.OPA.COVER, 0)
        obj.set_style_radius(50, 0)
        obj.align(lv.ALIGN.TOP_MID, 0, 30)
        lbl = lv.label(obj)
        lbl.set_text("HS")
        lbl.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
        lbl.set_style_text_font(lv.font_montserrat_28, 0)
        lbl.center()
        return obj

    # ------------------------------------------------------------------ UI build
    def _build_idle(self, scr):
        scr.set_style_bg_color(lv.color_hex(COL_BG), 0)
        scr.set_style_bg_opa(lv.OPA.COVER, 0)
        cfg = self._config

        if self._unconfigured:
            self._label(scr, 0, 70, "Configure me", COL_HINT,
                        font=lv.font_montserrat_24, center=True)
            self._label(scr, 0, 110, "edit  config.json", COL_NONE,
                        font=lv.font_montserrat_16, center=True)
            self._label(scr, 0, 134, "(set: name, groups)", COL_NONE,
                        font=lv.font_montserrat_14, center=True)
            self._label(scr, 0, 168, "then replace logo.png", COL_NONE,
                        font=lv.font_montserrat_14, center=True)
            self._label(scr, 0, 205, "BLE off until configured", COL_BATT,
                        font=lv.font_montserrat_14, center=True)
            return

        self._place_logo(scr)

        self._name_lbl = self._label(scr, 0, 130, cfg["name"], COL_NAME,
                                     font=lv.font_montserrat_28, center=True)
        if cfg["handle"]:
            self._handle_lbl = self._label(scr, 0, 162, cfg["handle"], COL_HANDLE,
                                           font=lv.font_montserrat_16, center=True)
        else:
            self._handle_lbl = None

        # own groups (top-left), battery (top-right)
        own = ", ".join(cfg["groups"])[:34]
        self._own_lbl = self._label(scr, 6, 6, own, COL_OWN,
                                    font=lv.font_montserrat_14)
        self._batt_lbl = self._label(scr, W - 70, 6, "--%", COL_BATT,
                                     font=lv.font_montserrat_14)

        # nearby line (bottom) + detail list
        self._near_lbl = self._label(scr, 0, H - 34, "scanning...", COL_NONE,
                                     font=lv.font_montserrat_14, center=True)
        self._detail_lbl = self._label(scr, 4, 190, "", COL_NEAR,
                                       font=lv.font_montserrat_12)
        self._detail_lbl.add_flag(lv.obj.FLAG.HIDDEN)

        # help line
        self._label(scr, 0, H - 16, "A:detail  X:mute  START:exit", COL_NONE,
                    font=lv.font_montserrat_12, center=True)

        # alert banner (hidden by default)
        self._banner_bg = lv.obj(scr)
        self._banner_bg.remove_style_all()
        self._banner_bg.set_size(W - 24, 46)
        self._banner_bg.set_style_bg_color(lv.color_hex(COL_BANNER), 0)
        self._banner_bg.set_style_bg_opa(lv.OPA.COVER, 0)
        self._banner_bg.set_style_radius(10, 0)
        self._banner_bg.set_style_border_width(2, 0)
        self._banner_bg.set_style_border_color(lv.color_hex(COL_NEAR), 0)
        self._banner_bg.align(lv.ALIGN.CENTER, 0, 0)
        self._banner = lv.label(self._banner_bg)
        self._banner.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
        self._banner.set_style_text_font(lv.font_montserrat_18, 0)
        self._banner.set_width(W - 40)
        self._banner.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        self._banner.center()
        self._hide_banner()

    def _label(self, scr, x, y, text, color, font=None, center=False, w=W):
        lbl = lv.label(scr)
        lbl.set_text(text)
        lbl.set_style_text_color(lv.color_hex(color), 0)
        if font:
            lbl.set_style_text_font(font, 0)
        if center:
            lbl.set_width(w)
            lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
            lbl.set_pos(0, y)
        else:
            lbl.set_pos(x, y)
        return lbl

    # ------------------------------------------------------------------ banner
    def _show_banner(self, text):
        self._banner.set_text(text)
        self._banner_bg.remove_flag(lv.obj.FLAG.HIDDEN)
        self._banner_until = time.ticks_add(time.ticks_ms(), BANNER_MS)

    def _hide_banner(self):
        self._banner_bg.add_flag(lv.obj.FLAG.HIDDEN)
        self._banner_until = 0

    def _coalesced_text(self, arrivals):
        # Group arrivals by shared group signature for the banner.
        by_group = {}
        for a in arrivals:
            key = a["shared_name"] or "?"
            by_group.setdefault(key, []).append(a["name"] or "?")
        if len(by_group) == 1:
            g, names = next(iter(by_group.items()))
            names_s = ", ".join(names)
            extra = "  +%d more" % (len(arrivals) - len(names)) if len(arrivals) > len(names) else ""
            return ("%s nearby (%s)" % (names_s, g))[:60] + extra
        total = sum(len(v) for v in by_group.values())
        first = next(iter(by_group))
        return "%s + %d nearby" % (by_group[first][0], total - 1) if total > 1 else by_group[first][0]

    # ------------------------------------------------------------------ alerts
    def _fire_alert(self, arrivals):
        if not arrivals:
            return
        # signature = lowest shared id among the arrivals (both badges agree)
        lowest = min(a["shared_id"] for a in arrivals if a["shared_id"] is not None)
        hue, freq = _sig_from_id(lowest)
        r, g, b = _hsv(hue)
        self._flash_leds(r, g, b)
        self._show_banner(self._coalesced_text(arrivals))
        self._wake()
        TaskManager.create_task(self._sting(freq))

    def _flash_leds(self, r, g, b):
        try:
            n = lights.get_led_count()
            for i in range(n):
                lights.set_led(i, r, g, b)
            lights.write()
            TaskManager.create_task(self._leds_off_after(900))
        except Exception:
            pass

    async def _leds_off_after(self, ms):
        await asyncio.sleep_ms(ms)
        try:
            lights.clear()
            lights.write()
        except Exception:
            pass

    # ------------------------------------------------------------------ lifecycle
    def onCreate(self):
        self._load_config()
        self._setup_buttons()
        self._setup_buzzer()
        self._setup_display()
        self._scr = lv.obj()
        self._build_idle(self._scr)
        self.setContentView(self._scr)

    def onResume(self, screen):
        super().onResume(screen)
        self._t0 = time.ticks_ms()
        self._last_input_ms = time.ticks_ms()
        self._dimmed = False
        self._set_brightness(255)
        if not self._unconfigured:
            try:
                self._ble.begin(self._config["groups"], self._config["name"],
                                self._config["handle"], self._config["rssi_floor"])
            except Exception:
                pass
        self._task = TaskManager.create_task(self._loop())

    def onPause(self, screen):
        super().onPause(screen)
        self._stop_task()
        self._teardown_ble()
        self._set_brightness(255)

    def onStop(self, screen):
        self._stop_task()
        self._teardown_ble()
        try:
            lights.clear()
            lights.write()
        except Exception:
            pass

    def onDestroy(self, screen):
        self._stop_task()
        self._teardown_ble()
        try:
            if self._buzzer:
                self._buzzer.duty_u16(0)
                self._buzzer.deinit()
        except Exception:
            pass

    def _stop_task(self):
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    def _teardown_ble(self):
        try:
            self._ble.end()
        except Exception:
            pass

    # ------------------------------------------------------------------ main loop
    async def _loop(self):
        last = time.ticks_ms()
        try:
            while True:
                now = time.ticks_ms()
                dt = time.ticks_diff(now, last)
                last = now

                self._handle_buttons()
                self._ble.tick(now, dt)
                self._drain_arrivals()
                self._refresh_nearby()
                self._refresh_battery(now)
                self._animate(now)
                self._tick_dim(now)

                await asyncio.sleep_ms(TICK_MS)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _handle_buttons(self):
        for name in ("a", "x", "start", "b", "y", "menu"):
            ev = self._edge(name)
            if not ev:
                continue
            self._wake()
            if ev == "x":
                self._sound = not self._sound
                self._flash_leds(*_hsv(0 if not self._sound else 120))
            elif ev == "a":
                self._detail = not self._detail
                if self._detail_lbl is not None:
                    if self._detail:
                        self._detail_lbl.remove_flag(lv.obj.FLAG.HIDDEN)
                    else:
                        self._detail_lbl.add_flag(lv.obj.FLAG.HIDDEN)
            elif ev == "start":
                self.finish()
                return

    def _drain_arrivals(self):
        if self._unconfigured:
            return
        arrivals = self._ble.take_arrivals()
        if arrivals:
            self._fire_alert(arrivals)

    def _refresh_nearby(self):
        if self._unconfigured or self._near_lbl is None:
            return
        peers = self._ble.current_peers()
        if not peers:
            self._near_lbl.set_text("nobody nearby")
            self._near_lbl.set_style_text_color(lv.color_hex(COL_NONE), 0)
        else:
            names = ", ".join(p[0] for p in peers)[:40]
            self._near_lbl.set_text("nearby: " + names)
            self._near_lbl.set_style_text_color(lv.color_hex(COL_NEAR), 0)
        if self._detail and self._detail_lbl is not None:
            lines = []
            for name, gname, gid, rssi, age in peers[:6]:
                lines.append("%s  %s  %ddBm  %ds" % (name[:12], (gname or "?")[:12], rssi, age // 1000))
            self._detail_lbl.set_text("\n".join(lines) if lines else "no peers")

    def _animate(self, now):
        # breathing logo scale
        if self._logo_im is not None and not self._unconfigured:
            t = time.ticks_diff(now, self._t0)
            phase = (t % BREATH_PERIOD_MS) / BREATH_PERIOD_MS
            s = int(256 + BREATH_AMP * math.sin(phase * 2 * math.pi))
            try:
                self._logo_im.set_scale(s)
            except Exception:
                pass
        # auto-hide banner
        if self._banner_until and time.ticks_diff(now, self._banner_until) >= 0:
            self._hide_banner()

    def _tick_dim(self, now):
        if self._disp is None:
            return
        if (not self._dimmed and
                time.ticks_diff(now, self._last_input_ms) > DIM_MS and
                not self._ble.has_peers()):
            self._set_brightness(60)
            self._dimmed = True

    # ------------------------------------------------------------------ battery
    def _refresh_battery(self, now):
        if self._batt_lbl is None or now < self._batt_next_ms:
            return
        self._batt_next_ms = now + 5000
        try:
            self._batt_lbl.set_text(self._battery_text())
        except Exception:
            pass

    def _battery_text(self):
        try:
            pct = BatteryManager.get_battery_percentage()
            if pct is None:
                return "--%"
            return "%d%%" % pct
        except Exception:
            return ""
