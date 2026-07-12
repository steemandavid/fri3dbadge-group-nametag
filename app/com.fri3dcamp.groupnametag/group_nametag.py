# group_nametag.py — "!friends nearby" (Group Nametag + BLE Proximity Finder).
#
# A MicroPythonOS Activity that runs on BOTH the Fri3d Camp 2024 badge and the
# Fri3d Camp 2026 badge (both ESP32-S3 + MicroPythonOS). Shows your name (big,
# scrolls when long) and your group(s) as full-width coloured pills, and quietly
# alerts you when another badge sharing one of your groups comes within Bluetooth
# range. Press A for a per-friend panel; B mutes; X quits.
#
# Controls:
#   A      = toggle the friends-nearby detail panel
#   B      = mute / unmute the alert buzzer  (persisted; label reflects state)
#   X      = (OS) quit to the launcher
# (START is intentionally unused.)
#
# Board differences are abstracted at runtime (2024: direct-GPIO buttons + GPIO46
# buzzer + 296x240; 2026: CH32X035 I2C-expander buttons + GPIO38 buzzer + 320x240
# + backlight). See DESIGN.md "2024 vs 2026".

import os
import sys
import math
import time
import json
import asyncio

APP_DIR = "/apps/com.fri3dcamp.groupnametag"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import lvgl as lv
from mpos import Activity, TaskManager, BatteryManager, lights
import mpos

from ble_proximity import (
    BLEProximity, build_own_table, hash_groups, fnv1a_16,
    EVICT_MS, RSSI_FLOOR_DEFAULT,
)
from contact_exchange import ContactExchange, merge_received
from web_portal import WebPortal

FULLNAME = "com.fri3dcamp.groupnametag"


def _read_version():
    """Read this app's version from its MANIFEST.JSON ('?' if not found)."""
    for base in ("/apps", "/builtin/apps"):
        try:
            with open(base + "/" + FULLNAME + "/MANIFEST.JSON") as f:
                return json.load(f).get("version", "?")
        except Exception:
            pass
    return "?"


def _asset_bytes(name):
    """Read a binary asset from this app's folder (or builtin), or None."""
    for base in ("/apps", "/builtin/apps"):
        try:
            with open(base + "/" + FULLNAME + "/" + name, "rb") as f:
                return f.read()
        except Exception:
            pass
    return None


def _now_str():
    """Local wall-clock as 'YYYY-MM-DDTHH:MM:SS' (accurate once NTP-synced)."""
    try:
        t = time.localtime()
        return "%04d-%02d-%02dT%02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
    except Exception:
        return ""

try:
    W = mpos.DisplayMetrics.width()
    H = mpos.DisplayMetrics.height()
except Exception:
    W, H = 296, 240

BANNER_MS_DEFAULT = 5000
TICK_MS = 30

# Name: font_montserrat_28 scaled up; long names marquee-scroll on one line.
NAME_SCALE = 512                  # 2.0x (UNUSED now — scaling a scrolling label starves the CPU)
NAME_TOP = 24
NAME_H_SCALED = 33                # font_montserrat_28 line height (no transform scale)
NAME_W = W - 60

BATT_X = W - 72
BATT_Y = 8

# Live clock: top-LEFT, same font/colour as the battery % (top-right).
CLOCK_X = 8
CLOCK_Y = 8

NTP_RESYNC_MS = 10 * 60 * 1000    # keep the RTC NTP-synced ~every 10 min on WiFi

# Group pills: full width, stacked vertically, below the name.
PILL_MARGIN_X = 16
PILL_TOP = NAME_TOP + NAME_H_SCALED + 8     # clear of the scaled name
PILL_H = 22
PILL_GAP = 4
MAX_PILLS = 4

CONTROLS_TOP = H - 16

# Button hardware (see DESIGN.md "2024 vs 2026").
START_PIN = 0
BTN_2024 = {"a": 39, "b": 40, "y": 41}    # direct GPIO (active-low, pull-up)
BTN_2024_DIAG = (0, 38, 39, 40, 41, 45)   # raw-GPIO pins logged for diagnostics
BTN_2026_EXP = {"a": 7, "b": 6, "y": 8}   # mpos.io_expander.digital index (active-high)
BUZZER_PIN_2024 = 46
BUZZER_PIN_2026 = 38

COL_BG = 0x0B0E14
COL_NAME = 0xFFFFFF
COL_NEAR = 0xFFE066
COL_NONE = 0x6A7280
COL_HINT = 0xFF8C42
COL_BATT = 0x8FA8B8
COL_BANNER = 0x143A2A
COL_MUTED = 0x7B8AA0
COL_PANEL = 0x121826
COL_CARD = 0x162033
COL_CARD_LINE = 0x28324A
COL_BAR_ON = 0x9FE0A0
COL_BAR_OFF = 0x2A3346


def _detect_2026():
    try:
        if str(mpos.DeviceInfo.get_hardware_id()).startswith("fri3d_2026"):
            return True
    except Exception:
        pass
    try:
        _ = mpos.io_expander.version
        return True
    except Exception:
        return False


def _hsv(h, s=0.85, v=0.6):
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
    return (gid * 137.508) % 360, 440 + (gid % 12) * 55


def _col(hexv):
    return lv.color_hex(hexv)


def _rssi_bars(rssi):
    try:
        lvl = (int(rssi) + 100) // 15
    except Exception:
        lvl = 0
    return 0 if lvl < 0 else (4 if lvl > 4 else lvl)


class GroupNametag(Activity):
    def __init__(self):
        super().__init__()
        self._ble = BLEProximity()
        self._config = {}
        self._own_table = []
        self._unconfigured = False
        self._is_2026 = False
        self._has_backlight = False
        self._sound = True
        self._banner_ms = BANNER_MS_DEFAULT
        self._detail = False
        self._btn_pins = {}
        self._pin_prev = {}
        self._prev = {}
        self._task = None
        self._t0 = 0
        self._last_input_ms = 0
        self._banner = None
        self._banner_bg = None
        self._banner_until = 0
        self._alert_names = []
        self._finishing = False
        self._buzzer = None
        self._dimmed = False
        self._batt_next_ms = 0
        self._name_lbl = None
        self._batt_lbl = None
        self._clock_lbl = None
        self._clock_last = None
        self._clock_next_ms = 0
        self._next_ntp_ms = 0
        self._ntp_busy = False
        self._contact = {}
        self._exch = ContactExchange()
        self._exchanging = False
        self._portal = None
        self._portal_lbl = None
        self._portal_last = None
        self._reload_pending = False
        self._splash_scr = None
        self._splash_task = None
        self._entered = False
        self._pills = []
        self._friends_lbl = None
        self._friends_top = PILL_TOP
        self._detail_panel = None
        self._detail_header = None
        self._detail_rows = []
        self._controls_lbl = None
        self._friends_last = None
        self._detail_header_last = None
        self._batt_last = None

    # ------------------------------------------------------------------ config
    def _load_config(self):
        cfg = {"groups": [], "name": "", "handle": "", "rssi_floor": RSSI_FLOOR_DEFAULT,
               "sound": True, "banner_ms": BANNER_MS_DEFAULT}
        try:
            with open(APP_DIR + "/config.json", "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
        if not isinstance(cfg.get("groups"), list):
            cfg["groups"] = []
        cfg["name"] = (cfg.get("name") or "").strip()
        cfg["handle"] = (cfg.get("handle") or "").strip()
        try:
            rf = int(cfg.get("rssi_floor", RSSI_FLOOR_DEFAULT))
        except (TypeError, ValueError):
            rf = RSSI_FLOOR_DEFAULT
        cfg["rssi_floor"] = rf
        board = cfg.get("board")
        if board == "2026":
            self._is_2026 = True
        elif board == "2024":
            self._is_2026 = False
        else:
            self._is_2026 = _detect_2026()
        self._sound = bool(cfg.get("sound", True))
        try:
            self._banner_ms = int(cfg.get("banner_ms", BANNER_MS_DEFAULT))
        except (TypeError, ValueError):
            self._banner_ms = BANNER_MS_DEFAULT
        contact = cfg.get("contact")
        if not isinstance(contact, dict):
            contact = {}
        cfg["contact"] = contact
        self._contact = contact
        self._config = cfg
        self._own_table = build_own_table(cfg["groups"])
        ids = [gid for _, gid in self._own_table]
        self._unconfigured = (not cfg["name"]) or (not ids)

    def _save_config(self, key, value):
        try:
            with open(APP_DIR + "/config.json", "r") as f:
                cfg = json.load(f)
            cfg[key] = value
            with open(APP_DIR + "/config.json", "w") as f:
                json.dump(cfg, f)
        except Exception:
            pass

    # ------------------------------------------------------------------ buttons
    def _setup_buttons(self):
        from machine import Pin
        self._btn_pins = {}
        self._pin_prev = {}
        if self._is_2026:
            return  # 2026 reads via mpos.io_expander; no raw pins
        for gp in BTN_2024_DIAG:
            try:
                self._btn_pins["p%d" % gp] = Pin(gp, Pin.IN, Pin.PULL_UP)
                self._pin_prev[gp] = 1
            except Exception:
                pass

    def _held(self, name):
        if self._is_2026:
            idx = BTN_2026_EXP.get(name)
            if idx is None:
                return False
            try:
                return bool(mpos.io_expander.digital[idx])
            except Exception:
                return False
        gp = BTN_2024.get(name)
        if gp is None:
            return False
        p = self._btn_pins.get("p%d" % gp)
        try:
            return p is not None and p.value() == 0
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
            pin = BUZZER_PIN_2026 if self._is_2026 else BUZZER_PIN_2024
            self._buzzer = PWM(Pin(pin), freq=2000, duty_u16=0)
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
        self._has_backlight = False
        if self._is_2026:
            try:
                mpos.io_expander.lcd_brightness = 100
                self._has_backlight = True
            except Exception:
                self._has_backlight = False

    def _set_brightness(self, v):
        if not self._has_backlight:
            return
        try:
            mpos.io_expander.lcd_brightness = max(0, min(100, int(v * 100 // 255)))
        except Exception:
            self._has_backlight = False

    # ------------------------------------------------------------------ widgets
    def _label(self, scr, x, y, text, color, font=None, center=False, w=W):
        lbl = lv.label(scr)
        lbl.set_text(text)
        lbl.set_style_text_color(_col(color), 0)
        if font:
            lbl.set_style_text_font(font, 0)
        if center:
            lbl.set_width(w)
            lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
            lbl.set_pos(x, y)
        else:
            lbl.set_pos(x, y)
        return lbl

    def _rbox(self, parent, x, y, w, h, color, opa=lv.OPA.COVER, radius=0):
        o = lv.obj(parent)
        o.remove_style_all()
        o.set_size(w, h)
        o.set_pos(x, y)
        o.set_style_bg_color(_col(color), 0)
        o.set_style_bg_opa(opa, 0)
        if radius:
            o.set_style_radius(radius, 0)
        return o

    def _make_bars(self, parent, x, y):
        bars = []
        bx = x
        for i in range(4):
            bh = 3 + i * 2
            bars.append(self._rbox(parent, bx, y + (9 - bh), 3, bh, COL_BAR_OFF, radius=1))
            bx += 4
        return bars

    def _set_bars(self, bars, level):
        for i, b in enumerate(bars):
            try:
                b.set_style_bg_color(_col(COL_BAR_ON if i < level else COL_BAR_OFF), 0)
            except Exception:
                pass

    def _color_for_gid(self, gid):
        hue, _ = _sig_from_id(gid)
        r, g, b = _hsv(hue)
        return (r << 16) | (g << 8) | b

    def _short(self, s, n):
        s = (s or "").strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    def _controls_text(self):
        # "B:mute" while unmuted, "B:unmute" while muted.
        return "A:list  B:%s  Y:swap" % ("mute" if self._sound else "unmute")

    # ------------------------------------------------------------------ pills (full width, stacked)
    def _place_pills(self, scr):
        self._pills = []
        groups = self._own_table[:MAX_PILLS]
        y = PILL_TOP
        pw = W - 2 * PILL_MARGIN_X
        if not groups:
            self._friends_top = PILL_TOP
            return
        for gname, gid in groups:
            hue, _ = _sig_from_id(gid)
            r, g, b = _hsv(hue, s=0.6, v=0.5)
            col = (r << 16) | (g << 8) | b
            pill = self._rbox(scr, PILL_MARGIN_X, y, pw, PILL_H, col, radius=PILL_H // 2)
            try:
                pill.set_style_border_width(1, 0)
                pill.set_style_border_color(_col(0xFFFFFF), 0)
                pill.set_style_border_opa(50, 0)
            except Exception:
                pass
            lbl = lv.label(pill)
            lbl.set_text(gname)
            lbl.set_style_text_color(_col(0xFFFFFF), 0)
            lbl.set_style_text_font(lv.font_montserrat_16, 0)
            try:
                lbl.set_long_mode(lv.label.LONG_MODE.SCROLL_CIRCULAR)
                lbl.set_width(pw - 16)
            except Exception:
                pass
            try:
                lbl.align(lv.ALIGN.LEFT_MID, 8, 0)
            except Exception:
                lbl.set_pos(8, 4)
            self._pills.append((pill, lbl))
            y += PILL_H + PILL_GAP
        self._friends_top = y + 2

    # ------------------------------------------------------------------ detail panel
    def _make_detail_row(self, panel, index, card_w):
        y = 30 + index * (30 + 4)
        row = self._rbox(panel, 4, y, card_w - 8, 30, COL_CARD, radius=8)
        try:
            row.set_style_border_width(1, 0)
            row.set_style_border_color(_col(COL_CARD_LINE), 0)
        except Exception:
            pass
        dot = self._rbox(row, 8, 9, 11, 11, COL_NONE, radius=5)
        name = lv.label(row)
        name.set_pos(26, 2)
        name.set_style_text_color(_col(COL_NAME), 0)
        name.set_style_text_font(lv.font_montserrat_16, 0)
        grp = lv.label(row)
        grp.set_pos(26, 16)
        grp.set_style_text_color(_col(COL_MUTED), 0)
        grp.set_style_text_font(lv.font_montserrat_12, 0)
        dbm = lv.label(row)
        dbm.set_pos(card_w - 16 - 36, 4)
        dbm.set_style_text_color(_col(COL_MUTED), 0)
        dbm.set_style_text_font(lv.font_montserrat_12, 0)
        age = lv.label(row)
        age.set_pos(card_w - 16 - 36, 17)
        age.set_style_text_color(_col(COL_MUTED), 0)
        age.set_style_text_font(lv.font_montserrat_12, 0)
        bars = self._make_bars(row, card_w - 16 - 52, 8)
        self._detail_rows.append({"row": row, "dot": dot, "name": name, "grp": grp,
                                  "bars": bars, "dbm": dbm, "age": age})

    def _fill_row(self, slot, peer):
        name, gname, gid, rssi, age = peer
        lvl = _rssi_bars(rssi)
        dbm = "%ddB" % int(rssi)
        ag = "%ds" % (int(age) // 1000)
        col = self._color_for_gid(gid)
        key = (name, gname, dbm, ag, lvl, col)
        if slot.get("last") == key:        # skip lvgl re-render when unchanged
            return
        slot["last"] = key
        try:
            slot["dot"].set_style_bg_color(_col(col), 0)
        except Exception:
            pass
        try:
            slot["name"].set_text(self._short(name, 16))
        except Exception:
            pass
        self._set_bars(slot["bars"], lvl)
        try:
            slot["dbm"].set_text(dbm)
        except Exception:
            pass
        try:
            slot["grp"].set_text(self._short(gname or "?", 18))
        except Exception:
            pass
        try:
            slot["age"].set_text(ag)
        except Exception:
            pass

    def _show_row(self, slot, show):
        if slot.get("vis") == show:        # don't re-set the flag every tick
            return
        slot["vis"] = show
        try:
            if show:
                slot["row"].remove_flag(lv.obj.FLAG.HIDDEN)
            else:
                slot["row"].add_flag(lv.obj.FLAG.HIDDEN)
        except Exception:
            pass

    # ------------------------------------------------------------------ splash
    def _build_splash(self):
        # Mirrors the proven pattern in org.fri3d.hwtest: in-memory PNG decode
        # (reliable, unlike set_src("S:/...")) with a text fallback.
        sp = lv.obj()
        sp.set_style_pad_all(0, 0)
        sp.set_style_bg_color(_col(COL_BG), 0)
        try:
            sp.remove_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass

        title = lv.label(sp)
        title.set_text("!friends nearby")
        title.set_style_text_color(_col(COL_NEAR), 0)
        title.set_style_text_font(lv.font_montserrat_24, 0)
        title.align(lv.ALIGN.TOP_MID, 0, 24)

        ver = lv.label(sp)
        ver.set_text("v" + _read_version())
        ver.set_style_text_color(_col(COL_NONE), 0)
        ver.set_style_text_font(lv.font_montserrat_14, 0)
        ver.align(lv.ALIGN.TOP_MID, 0, 56)

        who = lv.label(sp)
        who.set_text("by David Steeman")
        who.set_style_text_color(_col(COL_NAME), 0)
        who.set_style_text_font(lv.font_montserrat_16, 0)
        who.align(lv.ALIGN.TOP_MID, 0, 80)

        logo = _asset_bytes("makerspace.png")
        placed = False
        if logo:
            try:
                li = lv.image(sp)
                li.set_src(lv.image_dsc_t({"data_size": len(logo), "data": logo}))
                li.align(lv.ALIGN.CENTER, 0, 24)
                placed = True
            except Exception:
                placed = False

        org = lv.label(sp)
        org.set_text("Makerspace Baasrode")
        org.set_style_text_color(_col(COL_BAR_ON), 0)
        org.set_style_text_font(lv.font_montserrat_16, 0)
        org.align(lv.ALIGN.BOTTOM_MID, 0, -20 if placed else -60)
        return sp

    async def _splash_then_enter(self):
        try:
            await asyncio.sleep_ms(3000)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        self._enter_main()

    def _enter_main(self):
        if self._entered:
            return
        self._entered = True
        try:
            self.setContentView(self._scr)
        except Exception:
            pass

    # ------------------------------------------------------------------ UI build
    def _build_idle(self, scr):
        scr.set_style_bg_color(_col(COL_BG), 0)
        scr.set_style_bg_opa(lv.OPA.COVER, 0)
        cfg = self._config

        if self._unconfigured:
            self._label(scr, 0, 60, "Configure me", COL_HINT, font=lv.font_montserrat_24, center=True)
            self._label(scr, 0, 100, "open the WiFi setup", COL_NONE, font=lv.font_montserrat_16, center=True)
            self._label(scr, 0, 124, "portal (URL below)", COL_NONE, font=lv.font_montserrat_16, center=True)
            self._label(scr, 0, 150, "set: name, groups", COL_NONE, font=lv.font_montserrat_14, center=True)
            self._clock_lbl = self._label(scr, CLOCK_X, CLOCK_Y, "--:--", COL_BATT,
                                          font=lv.font_montserrat_14)
            self._portal_lbl = self._label(scr, 0, CONTROLS_TOP - 14, "", COL_BATT,
                                           font=lv.font_montserrat_12, center=True)
            self._controls_lbl = self._label(scr, 0, CONTROLS_TOP, self._controls_text(),
                                             COL_NONE, font=lv.font_montserrat_12, center=True)
            return

        # Name: largest built-in font, single line, scrolls when too long.
        # (No transform scale: scaling a scrolling label re-renders every frame
        #  and starves the CPU -> missed buttons + stretched chime.)
        self._name_lbl = self._label(scr, (W - NAME_W) // 2, NAME_TOP, cfg["name"],
                                     COL_NAME, font=lv.font_montserrat_28, center=True, w=NAME_W)
        try:
            self._name_lbl.set_long_mode(lv.label.LONG_MODE.SCROLL_CIRCULAR)
        except Exception:
            pass

        self._batt_lbl = self._label(scr, BATT_X, BATT_Y, "--%", COL_BATT, font=lv.font_montserrat_14)

        # Live clock: top-left, same font/colour as the battery %.
        self._clock_lbl = self._label(scr, CLOCK_X, CLOCK_Y, "--:--", COL_BATT,
                                      font=lv.font_montserrat_14)

        # Group pills (full width, stacked) -> sets self._friends_top.
        self._place_pills(scr)

        # Friends line directly under the pills.
        self._friends_lbl = self._label(scr, 0, self._friends_top, "looking for friends…",
                                        COL_NONE, font=lv.font_montserrat_14, center=True)

        # Portal footer (URL, or a login-challenge PIN) directly above controls.
        self._portal_lbl = self._label(scr, 0, CONTROLS_TOP - 14, "", COL_BATT,
                                       font=lv.font_montserrat_12, center=True)

        # Controls (dynamic B label).
        self._controls_lbl = self._label(scr, 0, CONTROLS_TOP, self._controls_text(),
                                         COL_NONE, font=lv.font_montserrat_12, center=True)

        # A-button detail panel (hidden by default).
        dpw = W - 48
        self._detail_panel = self._rbox(scr, (W - dpw) // 2, 60, dpw, H - 64, COL_PANEL, radius=10)
        try:
            self._detail_panel.set_style_border_width(2, 0)
            self._detail_panel.set_style_border_color(_col(COL_NEAR), 0)
            self._detail_panel.set_style_pad_all(4, 0)
        except Exception:
            pass
        self._detail_header = lv.label(self._detail_panel)
        self._detail_header.set_text("FRIENDS NEARBY")
        self._detail_header.set_style_text_color(_col(COL_NEAR), 0)
        self._detail_header.set_style_text_font(lv.font_montserrat_16, 0)
        self._detail_header.set_pos(8, 6)
        for i in range(6):
            self._make_detail_row(self._detail_panel, i, dpw)
        self._detail_panel.add_flag(lv.obj.FLAG.HIDDEN)

        # Alert banner (hidden), on top.
        self._banner_bg = lv.obj(scr)
        self._banner_bg.remove_style_all()
        self._banner_bg.set_size(W - 24, 46)
        self._banner_bg.set_style_bg_color(_col(COL_BANNER), 0)
        self._banner_bg.set_style_bg_opa(lv.OPA.COVER, 0)
        self._banner_bg.set_style_radius(10, 0)
        self._banner_bg.set_style_border_width(2, 0)
        self._banner_bg.set_style_border_color(_col(COL_NEAR), 0)
        self._banner_bg.align(lv.ALIGN.CENTER, 0, 0)
        self._banner = lv.label(self._banner_bg)
        self._banner.set_style_text_color(_col(0xFFFFFF), 0)
        self._banner.set_style_text_font(lv.font_montserrat_18, 0)
        self._banner.set_width(W - 40)
        self._banner.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        self._banner.center()
        self._hide_banner()

    # ------------------------------------------------------------------ banner
    def _show_banner(self, text):
        if self._banner is None or self._banner_bg is None:
            return
        self._banner.set_text(text)
        self._banner_bg.remove_flag(lv.obj.FLAG.HIDDEN)
        self._banner_until = time.ticks_add(time.ticks_ms(), self._banner_ms)

    def _hide_banner(self):
        if self._banner_bg is None:
            return
        self._banner_bg.add_flag(lv.obj.FLAG.HIDDEN)
        self._banner_until = 0

    def _coalesced_text(self, arrivals):
        by_group = {}
        for a in arrivals:
            by_group.setdefault(a["shared_name"] or "?", []).append(a["name"] or "?")
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
        ids = [a["shared_id"] for a in arrivals if a["shared_id"] is not None]
        lowest = min(ids) if ids else 0
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
        # Build the nametag now but show the splash first; swap after 3 s.
        self._scr = lv.obj()
        self._build_idle(self._scr)
        self._splash_scr = self._build_splash()
        self.setContentView(self._splash_scr)

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
        self._start_portal()
        if not self._entered and self._splash_task is None:
            self._splash_task = TaskManager.create_task(self._splash_then_enter())
        self._task = TaskManager.create_task(self._loop())

    def onPause(self, screen):
        super().onPause(screen)
        self._stop_task()
        self._stop_portal()
        self._teardown_ble()
        self._set_brightness(255)

    def onStop(self, screen):
        self._stop_task()
        self._stop_portal()
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
        if self._splash_task is not None:
            try:
                self._splash_task.cancel()
            except Exception:
                pass
            self._splash_task = None

    def _teardown_ble(self):
        try:
            self._ble.end()
        except Exception:
            pass

    # ------------------------------------------------------------------ main loop
    async def _loop(self):
        last = time.ticks_ms()
        while True:
            try:
                now = time.ticks_ms()
                dt = time.ticks_diff(now, last)
                last = now
                self._handle_buttons()
                if self._finishing:
                    break
                if self._reload_pending:
                    self._reload_pending = False
                    self._apply_reload()
                self._ble.tick(now, dt)
                self._drain_arrivals()
                self._refresh_nearby()
                self._refresh_battery(now)
                self._refresh_clock(now)
                self._refresh_portal(now)
                self._resync_time(now)
                if self._banner_until and time.ticks_diff(now, self._banner_until) >= 0:
                    self._hide_banner()
                if (self._has_backlight and not self._dimmed and
                        time.ticks_diff(now, self._last_input_ms) > 30000 and
                        not self._ble.has_peers()):
                    self._set_brightness(60)
                    self._dimmed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep_ms(TICK_MS)

    def _handle_buttons(self):
        for name in ("a", "b", "y"):
            ev = self._edge(name)
            if not ev:
                continue
            self._wake()
            if ev == "y":
                if not self._exchanging:
                    TaskManager.create_task(self._do_exchange())
            elif ev == "b":
                self._sound = not self._sound
                self._save_config("sound", self._sound)
                self._flash_leds(*_hsv(0 if not self._sound else 120))
                if self._controls_lbl is not None:
                    try:
                        self._controls_lbl.set_text(self._controls_text())
                    except Exception:
                        pass
            elif ev == "a":
                self._detail = not self._detail
                if self._detail_panel is not None:
                    try:
                        if self._detail:
                            self._detail_panel.remove_flag(lv.obj.FLAG.HIDDEN)
                        else:
                            self._detail_panel.add_flag(lv.obj.FLAG.HIDDEN)
                    except Exception:
                        pass

    def _drain_arrivals(self):
        if self._unconfigured:
            return
        arrivals = self._ble.take_arrivals()
        if not arrivals:
            return
        now = time.ticks_ms()
        if self._banner_until and time.ticks_diff(now, self._banner_until) < 0:
            self._alert_names.extend(arrivals)
            try:
                self._banner.set_text(self._coalesced_text(self._alert_names))
            except Exception:
                pass
        else:
            self._alert_names = list(arrivals)
            self._fire_alert(self._alert_names)

    def _refresh_nearby(self):
        if self._unconfigured or self._friends_lbl is None:
            return
        peers = self._ble.current_peers()
        n = len(peers)
        new_txt = (("Friends nearby: " + ", ".join(p[0] for p in peers)[:48])
                   if n else "looking for friends…")
        if new_txt != self._friends_last:
            try:
                self._friends_lbl.set_text(new_txt)
                self._friends_lbl.set_style_text_color(_col(COL_NEAR if n else COL_NONE), 0)
            except Exception:
                pass
            self._friends_last = new_txt
        for i, slot in enumerate(self._detail_rows):
            if i < n:
                self._fill_row(slot, peers[i])
                self._show_row(slot, True)
            else:
                self._show_row(slot, False)
        new_hdr = (("FRIENDS NEARBY · %d" % n) if n else "no friends nearby yet")
        if new_hdr != self._detail_header_last and self._detail_header is not None:
            try:
                self._detail_header.set_text(new_hdr)
            except Exception:
                pass
            self._detail_header_last = new_hdr

    # ------------------------------------------------------------------ battery
    def _refresh_battery(self, now):
        if self._batt_lbl is None or time.ticks_diff(now, self._batt_next_ms) < 0:
            return
        self._batt_next_ms = time.ticks_add(now, 5000)
        txt = self._battery_text()
        if txt != self._batt_last:
            try:
                self._batt_lbl.set_text(txt)
            except Exception:
                pass
            self._batt_last = txt

    def _battery_text(self):
        try:
            pct = BatteryManager.get_battery_percentage()
            if pct is None:
                return "--%"
            return "%d%%" % pct
        except Exception:
            return ""

    # ------------------------------------------------------------------ clock + NTP
    def _refresh_clock(self, now):
        if self._clock_lbl is None or time.ticks_diff(now, self._clock_next_ms) < 0:
            return
        self._clock_next_ms = time.ticks_add(now, 1000)
        try:
            t = time.localtime()
            txt = "%02d:%02d" % (t[3], t[4])
        except Exception:
            txt = "--:--"
        if txt != self._clock_last:
            try:
                self._clock_lbl.set_text(txt)
            except Exception:
                pass
            self._clock_last = txt

    def _resync_time(self, now):
        # Keep the RTC NTP-synced ~every 10 min while on WiFi (first tick tries
        # immediately). ntptime.settime() briefly blocks, so run it in a task.
        if self._ntp_busy or time.ticks_diff(now, self._next_ntp_ms) < 0:
            return
        self._next_ntp_ms = time.ticks_add(now, NTP_RESYNC_MS)
        if not self._wifi_connected():
            return
        self._ntp_busy = True
        TaskManager.create_task(self._ntp_sync())

    async def _ntp_sync(self):
        try:
            import ntptime
            ntptime.settime()
        except Exception:
            pass
        finally:
            self._ntp_busy = False

    @staticmethod
    def _wifi_connected():
        try:
            from mpos import WifiService
            return bool(WifiService.is_connected())
        except Exception:
            return False

    # ------------------------------------------------------------------ contact exchange
    async def _do_exchange(self):
        self._exchanging = True
        try:
            self._show_banner("Swapping contacts…")
            self._wake()
            name = self._config.get("name", "") or "Anonymous"
            rec = await self._exch.run_window(self._ble, name, self._contact)
            if rec:
                rec["received_at"] = _now_str()
                try:
                    rec["received_ticks"] = time.ticks_ms()
                except Exception:
                    rec["received_ticks"] = 0
                self._store_contact(rec)
                self._flash_leds(*_hsv(180))
                TaskManager.create_task(self._sting(660))
                self._show_banner("Swapped with %s ✓" % (rec.get("name") or "?"))
            else:
                self._show_banner("No one swapping nearby")
        except Exception:
            try:
                self._show_banner("Swap failed")
            except Exception:
                pass
        finally:
            self._exchanging = False

    def _contacts_path(self):
        return APP_DIR + "/contacts.json"

    def _load_contacts(self):
        try:
            with open(self._contacts_path()) as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _store_contact(self, rec):
        store = self._load_contacts()
        merge_received(store, rec)
        try:
            with open(self._contacts_path(), "w") as f:
                json.dump(store, f)
        except Exception:
            pass

    # ------------------------------------------------------------------ web portal
    def _portal_ip(self):
        try:
            from mpos import WifiService
            if not WifiService.is_connected():
                return None
            return WifiService.get_ipv4_address()
        except Exception:
            return None

    def _start_portal(self):
        if self._portal is not None:
            return
        try:
            self._portal = WebPortal(APP_DIR, on_change=self._reload_config,
                                     ip_getter=self._portal_ip)
            self._portal.start()
        except Exception:
            self._portal = None

    def _stop_portal(self):
        if self._portal is not None:
            try:
                self._portal.stop()
            except Exception:
                pass
            self._portal = None

    def _refresh_portal(self, now):
        if self._portal_lbl is None:
            return
        pin = None
        try:
            pin = self._portal.pending_pin() if self._portal else None
        except Exception:
            pin = None
        if pin:
            txt = "portal PIN: %s" % pin
            col = COL_NEAR
        else:
            url = None
            try:
                url = self._portal.url() if self._portal else None
            except Exception:
                url = None
            txt = ("⚙ " + url) if url else "⚙ WiFi not connected"
            col = COL_BATT
        if txt != self._portal_last:
            try:
                self._portal_lbl.set_text(txt)
                self._portal_lbl.set_style_text_color(_col(col), 0)
            except Exception:
                pass
            self._portal_last = txt

    def _reload_config(self):
        # Called from the portal (same asyncio loop). Defer the actual apply to
        # the main loop so it never races the BLE tick / exchange window.
        self._reload_pending = True

    def _apply_reload(self):
        self._load_config()
        if self._name_lbl is not None:
            try:
                self._name_lbl.set_text(self._config.get("name", ""))
            except Exception:
                pass
        if self._controls_lbl is not None:
            try:
                self._controls_lbl.set_text(self._controls_text())
            except Exception:
                pass
        if self._exchanging:
            return
        # Re-apply the on-air beacon (name/groups) after an edit.
        try:
            self._ble.end()
        except Exception:
            pass
        if not self._unconfigured:
            try:
                self._ble.begin(self._config["groups"], self._config["name"],
                                self._config["handle"], self._config["rssi_floor"])
            except Exception:
                pass
