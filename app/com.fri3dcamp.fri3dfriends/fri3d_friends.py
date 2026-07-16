# fri3d_friends.py — "!Fri3d Friends" (Group Nametag + BLE Proximity Finder).
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

APP_DIR = "/apps/com.fri3dcamp.fri3dfriends"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import lvgl as lv
from mpos import Activity, TaskManager, BatteryManager, lights
import mpos

from ble_proximity import (
    BLEProximity, build_own_table, hash_groups, fnv1a_16,
    EVICT_MS, RSSI_FLOOR_DEFAULT,
)
from contact_exchange import ContactExchange, add_received
from ble_setup import SetupService, setup_name, SETUP_WINDOW_MS

FULLNAME = "com.fri3dcamp.fri3dfriends"

# Static Web-Bluetooth setup page (GitHub Pages). The badge shows a QR of this
# URL + its own id so a phone lands on the right badge in the chooser.
SETUP_URL_BASE = "https://steemandavid.github.io/fri3d-friends/setup/"

# A configured badge opens a setup window with a LONG press of B (short press
# still toggles mute). START is intentionally unused / not present on both boards.
SETUP_HOLD_MS = 1500


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


def _atomic_write_json(path, obj):
    """Write JSON to `path` via a temp file + rename (atomic on LittleFS/FAT).
    A power-off mid-write then can't corrupt the file into an unloadable state
    (which every loader silently turns into {}/[] — total, unnoticed data loss)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.rename(tmp, path)


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

# Name: rendered in a bundled TrueType Montserrat at NAME_FONT_SIZE (1.5x the
# largest built-in font_montserrat_28) via FontManager — a FIXED font, not a
# transform-scaled one (scaling a scrolling label starves the CPU). Falls back to
# font_montserrat_28 if the TTF can't be loaded. Long names marquee-scroll.
NAME_FONT_SIZE = 42               # 1.5 × 28
NAME_TTF = "M:apps/com.fri3dcamp.fri3dfriends/montserrat_name.ttf"
NAME_TOP = 22
NAME_H_SCALED = 52                # ~line height of the 42px name font
NAME_W = W - 40

BATT_X = W - 72
BATT_Y = 8

# Live clock: top-LEFT, same font/colour as the battery % (top-right). Inset ~2
# chars from the edge so the curved screen corner doesn't clip it.
CLOCK_X = 24
CLOCK_Y = 8

NTP_RESYNC_MS = 10 * 60 * 1000    # keep the RTC NTP-synced ~every 10 min on WiFi

# Friend LEDs: one RGB LED per nearby friend, slowly + dimly breathing that
# friend's group colour. 2024 badge has 4 physical LEDs, 2026 has 5 (firmware
# get_led_count() reports 5 on both, so key off the board).
LED_BREATHE_MS = 3800             # one full breathe cycle
LED_DIM_MIN = 0.015               # min brightness fraction (nearly off at trough)
LED_DIM_MAX = 0.18                # max brightness fraction (dim peak)
LED_UPDATE_MS = 60                # LED refresh cadence (smooth enough for a slow breathe)
LED_FLASH_MS = 900                # arrival/exchange flash holds this long before breathing resumes

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


class Fri3dFriends(Activity):
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
        self._banner_is_arrival = False
        self._alert_names = []
        self._buzzer = None
        self._dimmed = False
        self._led_next_ms = 0
        self._led_override_until = 0
        self._led_last = None
        self._batt_next_ms = 0
        self._name_lbl = None
        self._name_font = None
        self._batt_lbl = None
        self._clock_lbl = None
        self._clock_last = None
        self._clock_next_ms = 0
        self._next_ntp_ms = 0
        self._ntp_busy = False
        self._contact = {}
        self._exch = ContactExchange()
        self._exchanging = False
        self._exch_task = None
        # BLE phone-setup (Web Bluetooth). The setup GATT service is registered
        # together with the exchange service (single gatts_register_services call).
        self._setup = SetupService(APP_DIR, self._exch, on_saved=self._reload_config)
        self._exch.attach_setup(self._setup)
        self._setup_task = None
        self._setup_open = False          # True while a configured-badge window runs
        self._b_down_ms = None            # B-button press timestamp (long-press detect)
        self._b_long = False              # this B press already opened a setup window
        self._setup_info_lbl = None       # Configure-me: "Fri3d-XXXX  code NNNN"
        self._setup_hint_lbl = None       # nametag footer: "hold B: phone setup"
        self._setup_last = None
        self._setup_next_ms = 0
        self._setup_win_deadline = 0
        self._pending_begin = False       # begin proximity once setup session ends
        self._overlay = None              # configured-badge window overlay (create-once)
        self._overlay_qr = None
        self._overlay_qr_box = None
        self._overlay_code_lbl = None
        self._overlay_count_lbl = None
        self._overlay_qr_last = None
        self._qr = None
        self._qr_box = None
        self._qr_last = None
        self._setup_widgets = []
        self._reload_pending = False
        self._splash_scr = None
        self._splash_logo = None
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
        cfg = {"groups": [], "name": "", "rssi_floor": RSSI_FLOOR_DEFAULT,
               "sound": True, "banner_ms": BANNER_MS_DEFAULT}
        try:
            with open(APP_DIR + "/config.json", "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
        if not isinstance(cfg.get("groups"), list):
            cfg["groups"] = []
        cfg["name"] = (cfg.get("name") or "").strip()
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
        if self._banner_ms < 500:        # 0/negative would hide every banner
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
            _atomic_write_json(APP_DIR + "/config.json", cfg)
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

    def _load_name_font(self):
        # Load the bundled Montserrat TTF at NAME_FONT_SIZE via the OS FontManager
        # (renders TrueType at any size — a fixed font, not a transform). Fall back
        # to the largest built-in bitmap font if unavailable.
        try:
            from mpos import FontManager
            f = FontManager.getFont(size=NAME_FONT_SIZE, ttf=NAME_TTF)
            if f:
                return f
        except Exception:
            pass
        return lv.font_montserrat_28

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

        # Explicit vertical layout (H=240) so nothing overlaps: title / version /
        # author up top, the 96px logo in the middle with clear gaps above and
        # below, and the Makerspace attribution pinned near the bottom.
        title = lv.label(sp)
        title.set_text("!Fri3d Friends")
        title.set_style_text_color(_col(COL_NEAR), 0)
        title.set_style_text_font(lv.font_montserrat_24, 0)
        title.align(lv.ALIGN.TOP_MID, 0, 16)

        ver = lv.label(sp)
        ver.set_text("v" + _read_version())
        ver.set_style_text_color(_col(COL_NONE), 0)
        ver.set_style_text_font(lv.font_montserrat_14, 0)
        ver.align(lv.ALIGN.TOP_MID, 0, 50)

        who = lv.label(sp)
        who.set_text("by David Steeman")
        who.set_style_text_color(_col(COL_NAME), 0)
        who.set_style_text_font(lv.font_montserrat_16, 0)
        who.align(lv.ALIGN.TOP_MID, 0, 72)

        logo = _asset_bytes("fri3dfriends.png")     # 96x96 app logo
        if logo:
            try:
                li = lv.image(sp)
                li.set_src(lv.image_dsc_t({"data_size": len(logo), "data": logo}))
                li.align(lv.ALIGN.TOP_MID, 0, 100)   # top at y=100 -> bottom ~196
                # Keep a Python-side reference to the PNG bytes for as long as the
                # splash image widget lives, so the buffer can't be GC'd out from
                # under the C binding.
                self._splash_logo = logo
            except Exception:
                pass

        org = lv.label(sp)
        org.set_text("Makerspace Baasrode")
        org.set_style_text_color(_col(COL_BAR_ON), 0)
        org.set_style_text_font(lv.font_montserrat_16, 0)
        org.align(lv.ALIGN.BOTTOM_MID, 0, -14)       # top ~y=210
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
        # NOTE: do NOT call self._splash_scr.delete() here. Deleting a screen
        # object hard-crashes + reboots the badge on this MicroPythonOS build
        # (same landmine as the config-reload screen rebuild — see DESIGN.md
        # "Config reload"); confirmed on-device on all three badges (2026-07-15).
        # So the splash is intentionally leaked for the app's lifetime, same as
        # the field-verified 0.6.0 behaviour — do not "clean this up" again
        # without testing a screen .delete() on real hardware first.

    # ------------------------------------------------------------------ UI build
    def _build_idle(self, scr):
        scr.set_style_bg_color(_col(COL_BG), 0)
        scr.set_style_bg_opa(lv.OPA.COVER, 0)

        # Widgets shared by both layouts, built exactly once. The banner is
        # deliberately built LAST so it z-stacks above everything; if the
        # nametag is built later (first-time configure), it is re-raised.
        if self._unconfigured:
            self._build_setup(scr)
        else:
            self._build_nametag(scr)
        self._clock_lbl = self._label(scr, CLOCK_X, CLOCK_Y, "--:--", COL_BATT,
                                      font=lv.font_montserrat_14)
        # Footer hint (was the WiFi-portal URL): configured badges get a phone
        # setup reachable by holding B. Blank on the Configure-me screen (it
        # already explains itself).
        hint = "" if self._unconfigured else "hold B: phone setup"
        self._setup_hint_lbl = self._label(scr, 0, CONTROLS_TOP - 14, hint, COL_BATT,
                                           font=lv.font_montserrat_12, center=True)
        self._controls_lbl = self._label(scr, 0, CONTROLS_TOP, self._controls_text(),
                                         COL_NONE, font=lv.font_montserrat_12, center=True)
        if not self._unconfigured:
            self._build_setup_overlay(scr)
        self._build_banner(scr)

    def _build_setup(self, scr):
        # First-run "Configure me" layout. Every widget is tracked in
        # _setup_widgets so a save (over BLE) can HIDE (never delete — deleting
        # live widgets/screens crashes this build) the lot and swap to the
        # nametag in place. No WiFi needed: a phone connects over Bluetooth to
        # the static Web-Bluetooth page (SETUP_URL_BASE); the QR carries the URL
        # incl. ?badge=XXXX so the browser chooser shows exactly this badge.
        info = self._label(scr, 0, 210, "starting Bluetooth…", COL_NEAR,
                           font=lv.font_montserrat_16, center=True)
        self._setup_info_lbl = info
        self._setup_widgets = [
            self._label(scr, 0, 6, "Configure me", COL_HINT, font=lv.font_montserrat_24, center=True),
            self._label(scr, 0, 34, "scan with your phone (Bluetooth)", COL_NONE,
                        font=lv.font_montserrat_14, center=True),
            info,
        ]
        # QR of the setup-page URL, on a white tile (the margin doubles as the
        # QR quiet zone). Hidden until the badge id is known (radio up); fed by
        # _refresh_setup. More vertical room now the portal footer line is gone.
        try:
            box = self._rbox(scr, (W - 150) // 2, 54, 150, 150, 0xFFFFFF, radius=6)
            qr = lv.qrcode(box)
            qr.set_size(124)
            qr.set_dark_color(_col(0x000000))
            qr.set_light_color(_col(0xFFFFFF))
            qr.center()
            box.add_flag(lv.obj.FLAG.HIDDEN)
            self._qr = qr
            self._qr_box = box
            self._setup_widgets.append(box)
        except Exception:      # no lv.qrcode in this build: text line still shows
            self._qr = None
            self._qr_box = None

    def _build_setup_overlay(self, scr):
        # Configured-badge setup window overlay: a full-screen panel with the
        # setup QR + on-screen code + countdown. Built ONCE and hidden; shown
        # while a window is open and hidden again on close (never deleted —
        # landmine #1). Sits below the banner (built after this).
        ov = self._rbox(scr, 0, 0, W, H, COL_BG, radius=0)
        try:
            ov.set_style_border_width(2, 0)
            ov.set_style_border_color(_col(COL_HINT), 0)
        except Exception:
            pass
        self._label(ov, 0, 6, "Phone setup", COL_HINT, font=lv.font_montserrat_24, center=True)
        self._label(ov, 0, 34, "scan with your phone (Bluetooth)", COL_NONE,
                    font=lv.font_montserrat_14, center=True)
        try:
            box = self._rbox(ov, (W - 140) // 2, 54, 140, 140, 0xFFFFFF, radius=6)
            qr = lv.qrcode(box)
            qr.set_size(116)
            qr.set_dark_color(_col(0x000000))
            qr.set_light_color(_col(0xFFFFFF))
            qr.center()
            self._overlay_qr = qr
            self._overlay_qr_box = box
        except Exception:
            self._overlay_qr = None
            self._overlay_qr_box = None
        self._overlay_code_lbl = self._label(ov, 0, 200, "", COL_NEAR,
                                             font=lv.font_montserrat_16, center=True)
        self._overlay_count_lbl = self._label(ov, 0, 222, "", COL_NONE,
                                              font=lv.font_montserrat_12, center=True)
        ov.add_flag(lv.obj.FLAG.HIDDEN)
        self._overlay = ov

    def _build_nametag(self, scr):
        cfg = self._config
        # Name: bundled 42px TrueType font (1.5× the built-in max), single line,
        # scrolls when too long. Fixed font, not transform-scaled (scaling a
        # scrolling label re-renders every frame and starves the CPU).
        self._name_lbl = self._label(scr, (W - NAME_W) // 2, NAME_TOP, cfg["name"],
                                     COL_NAME, font=self._name_font, center=True, w=NAME_W)
        try:
            self._name_lbl.set_long_mode(lv.label.LONG_MODE.SCROLL_CIRCULAR)
        except Exception:
            pass

        self._batt_lbl = self._label(scr, BATT_X, BATT_Y, "--%", COL_BATT, font=lv.font_montserrat_14)

        # Group pills (full width, stacked) -> sets self._friends_top.
        self._place_pills(scr)

        # Friends line directly under the pills. Inset from the curved edges and
        # WRAP so long names ("David Steeman ON4BDS") wrap onto the next line
        # instead of being clipped by the rounded screen corner.
        fw = W - 40
        self._friends_lbl = self._label(scr, (W - fw) // 2, self._friends_top,
                                        "looking for friends…", COL_NONE,
                                        font=lv.font_montserrat_14, center=True, w=fw)
        try:
            self._friends_lbl.set_long_mode(lv.label.LONG_MODE.WRAP)
        except Exception:
            pass

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

    def _build_banner(self, scr):
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
    def _show_banner(self, text, is_arrival=False):
        if self._banner is None or self._banner_bg is None:
            return
        self._banner.set_text(text)
        self._banner_bg.remove_flag(lv.obj.FLAG.HIDDEN)
        self._banner_until = time.ticks_add(time.ticks_ms(), self._banner_ms)
        self._banner_is_arrival = is_arrival

    def _hide_banner(self):
        # Clear the arrival state too so a later arrival never coalesces into a
        # stale (minutes-old) name list left over from a previous alert window.
        self._alert_names = []
        self._banner_is_arrival = False
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
        self._show_banner(self._coalesced_text(arrivals), is_arrival=True)
        self._wake()
        TaskManager.create_task(self._sting(freq))

    def _flash_leds(self, r, g, b):
        # A brief bright flash on all LEDs; the per-friend breathing (below)
        # resumes automatically once the override window elapses.
        try:
            n = self._led_count()
            for i in range(n):
                lights.set_led(i, r, g, b)
            lights.write()
            self._led_override_until = time.ticks_add(time.ticks_ms(), LED_FLASH_MS)
            self._led_last = None       # force a breathing redraw after the flash
        except Exception:
            pass

    # ---- per-friend breathing LEDs ----
    def _led_count(self):
        # 4 physical LEDs on 2024, 5 on 2026 (get_led_count() over-reports 5 on 2024).
        return 5 if self._is_2026 else 4

    def _update_leds(self, now):
        # One LED per nearby friend, slowly + dimly breathing that friend's group
        # colour (friend 1 -> LED 0, friend 2 -> LED 1, ...). Others off.
        if time.ticks_diff(now, self._led_next_ms) < 0:
            return
        self._led_next_ms = time.ticks_add(now, LED_UPDATE_MS)
        if self._led_override_until and time.ticks_diff(now, self._led_override_until) < 0:
            return                      # a flash is currently showing
        n = self._led_count()
        peers = [] if self._unconfigured else self._ble.current_peers()
        frame = []
        span = LED_DIM_MAX - LED_DIM_MIN
        for i in range(n):
            if i < len(peers):
                gid = peers[i][2]
                hue, _ = _sig_from_id(gid if gid is not None else 0)
                r, g, b = _hsv(hue, s=0.9, v=1.0)
                # gentle per-LED phase stagger so they don't pulse in lockstep
                phase = (now + i * (LED_BREATHE_MS // max(1, n))) % LED_BREATHE_MS
                s = LED_DIM_MIN + span * (0.5 - 0.5 * math.cos(2 * math.pi * phase / LED_BREATHE_MS))
                frame.append((int(r * s), int(g * s), int(b * s)))
            else:
                frame.append((0, 0, 0))
        if frame == self._led_last:     # skip redundant writes (e.g. all-off)
            return
        self._led_last = frame
        try:
            for i, (r, g, b) in enumerate(frame):
                lights.set_led(i, r, g, b)
            lights.write()
        except Exception:
            pass

    # ------------------------------------------------------------------ lifecycle
    def onCreate(self):
        self._load_config()
        self._setup_buttons()
        self._setup_buzzer()
        self._setup_display()
        self._name_font = self._load_name_font()
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
        # The OS already NTP-syncs on WiFi connect; defer our first resync so the
        # (blocking) ntptime.settime() call never hitches app launch.
        self._next_ntp_ms = time.ticks_add(time.ticks_ms(), NTP_RESYNC_MS)
        self._set_brightness(255)
        if not self._unconfigured:
            try:
                self._ble.begin(self._config["groups"], self._config["name"],
                                self._config["rssi_floor"])
            except Exception:
                pass
        else:
            # Unconfigured badge, app foreground: run the BLE setup service so a
            # phone can configure us over Bluetooth (no proximity radio runs).
            self._start_configure_setup()
        if not self._entered and self._splash_task is None:
            self._splash_task = TaskManager.create_task(self._splash_then_enter())
        self._task = TaskManager.create_task(self._loop())

    def onPause(self, screen):
        super().onPause(screen)
        self._stop_task()
        self._stop_setup()
        self._teardown_ble()
        self._set_brightness(255)
        self._led_last = None
        try:
            lights.clear()
            lights.write()
        except Exception:
            pass

    def onStop(self, screen):
        self._stop_task()
        self._stop_setup()
        self._teardown_ble()
        try:
            lights.clear()
            lights.write()
        except Exception:
            pass

    def onDestroy(self, screen):
        self._stop_task()
        self._stop_setup()
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
        # Cancel an in-flight contact swap too — otherwise it outlives the
        # Activity by up to 5 s and touches LVGL widgets / BLE on a torn-down app
        # (the D-1 use-after-free hazard class). run_window re-raises the cancel.
        if self._exch_task is not None:
            try:
                self._exch_task.cancel()
            except Exception:
                pass
            self._exch_task = None

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
                # During a contact swap, keep the loop out of the radio's way:
                # skip the periodic refreshers — especially _update_leds, whose
                # WS2812 lights.write() disables IRQs and starves the short GATT
                # connection (causing it to fail/drop). The exchange task owns the
                # BLE for its ~5 s window; resume normal work when it's done.
                if self._exchanging:
                    await asyncio.sleep_ms(TICK_MS)
                    continue
                if self._setup_open:
                    # A configured-badge setup window owns the radio for its
                    # ~2 min: keep the loop out of it (no LED writes / scan that
                    # would starve the GATT link) but keep the overlay live.
                    self._refresh_setup(now)
                    self._refresh_clock(now)
                    if self._banner_until and time.ticks_diff(now, self._banner_until) >= 0:
                        self._hide_banner()
                    await asyncio.sleep_ms(TICK_MS)
                    continue
                if self._reload_pending:
                    self._reload_pending = False
                    self._apply_reload()
                # First-run handoff: proximity begins only once the setup session
                # that just saved has fully torn down (so they never advertise at
                # the same time). See _apply_reload's was_unconfigured branch.
                if self._pending_begin and self._setup_task is None:
                    self._pending_begin = False
                    try:
                        self._ble.begin(self._config["groups"], self._config["name"],
                                        self._config["rssi_floor"])
                    except Exception:
                        pass
                self._ble.tick(now, dt)
                self._drain_arrivals()
                self._refresh_nearby()
                # While a setup session runs (Configure-me on an unconfigured
                # badge), skip LED writes — the WS2812 write disables IRQs and
                # would starve a phone's GATT connection (field bug 2 / plan §2.3).
                if self._setup_task is None:
                    self._update_leds(now)
                self._refresh_battery(now)
                self._refresh_clock(now)
                self._refresh_setup(now)
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
        # B is press-and-hold aware: a SHORT press toggles mute (on release), a
        # LONG press (>= SETUP_HOLD_MS) opens the phone-setup window on a
        # configured badge. A and Y stay simple edge triggers. All actions are
        # suppressed during a swap or an open setup window (a WS2812 LED write or
        # an lvgl toggle would starve the short GATT link — field bug 2).
        busy = self._exchanging or self._setup_open or self._setup_task is not None
        self._handle_b_button(busy)
        for name in ("a", "y"):
            ev = self._edge(name)
            if not ev:
                continue
            if self._setup_open:
                # Any A/Y press closes the setup window early.
                self._wake()
                self._stop_setup()
                return
            if busy:
                continue
            self._wake()
            if ev == "y":
                # An unconfigured badge has no BLE running (proximity.begin() was
                # skipped); firing a swap would activate a radio no teardown path
                # deactivates. Match the README: unconfigured badges don't swap.
                if not self._unconfigured:
                    self._exch_task = TaskManager.create_task(self._do_exchange())
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

    def _handle_b_button(self, busy):
        held = self._held("b")
        prev_down = self._b_down_ms is not None
        now = time.ticks_ms()
        if held and not prev_down:
            self._b_down_ms = now                 # press started
            self._b_long = False
        elif held and prev_down:
            # Long-press threshold: open the setup window once, mid-hold.
            if (not self._b_long and not busy and not self._unconfigured and
                    time.ticks_diff(now, self._b_down_ms) >= SETUP_HOLD_MS):
                self._b_long = True
                self._wake()
                self._open_setup_window()
        elif not held and prev_down:
            was_long = self._b_long
            self._b_down_ms = None
            self._b_long = False
            if busy or was_long:
                return                            # long-press already acted
            # Short press -> toggle mute.
            self._wake()
            self._sound = not self._sound
            self._save_config("sound", self._sound)
            self._flash_leds(*_hsv(0 if not self._sound else 120))
            if self._controls_lbl is not None:
                try:
                    self._controls_lbl.set_text(self._controls_text())
                except Exception:
                    pass

    def _drain_arrivals(self):
        if self._unconfigured:
            return
        arrivals = self._ble.take_arrivals()
        if not arrivals:
            return
        now = time.ticks_ms()
        # Only coalesce into a banner that is ITSELF an arrival banner still
        # showing — never into a "Swapped with X ✓" / "Config saved ✓" banner
        # (that would silently rewrite it and skip the LED flash + sting).
        if (self._banner_is_arrival and self._banner_until and
                time.ticks_diff(now, self._banner_until) < 0):
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
        if n:
            names = ", ".join(p[0] for p in peers)
            if len(names) > 90:          # wraps to ~3 lines; cap absurdly long lists
                names = names[:89] + "…"
            new_txt = "Friends nearby: " + names
        else:
            new_txt = "looking for friends…"
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
        # Keep the RTC NTP-synced ~every 10 min while on WiFi (onResume defers the
        # first sync so it never hitches launch). ntptime.settime() briefly
        # blocks, so run it off the loop.
        if self._ntp_busy or time.ticks_diff(now, self._next_ntp_ms) < 0:
            return
        self._next_ntp_ms = time.ticks_add(now, NTP_RESYNC_MS)
        if not self._wifi_connected():
            return
        self._ntp_busy = True
        # ntptime.settime() is a BLOCKING network call — run it in a thread so it
        # never freezes the asyncio loop (which would stall the UI / an in-flight
        # contact exchange). Fall back to a task if _thread is unavailable. If
        # BOTH dispatch paths fail, clear _ntp_busy so resync isn't stuck off
        # for the rest of the session.
        try:
            import _thread
            _thread.start_new_thread(self._ntp_blocking, ())
        except Exception:
            try:
                TaskManager.create_task(self._ntp_sync())
            except Exception:
                self._ntp_busy = False

    def _ntp_blocking(self):
        try:
            import ntptime
            ntptime.settime()
        except Exception:
            pass
        finally:
            self._ntp_busy = False

    async def _ntp_sync(self):
        self._ntp_blocking()

    @staticmethod
    def _wifi_connected():
        try:
            from mpos import WifiService
            return bool(WifiService.is_connected())
        except Exception:
            return False

    # ------------------------------------------------------------------ contact exchange
    def _exch_log(self, msg):
        try:
            with open(APP_DIR + "/exch.log", "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def _outgoing_contact(self):
        # What the swap sends alongside the name. Auto-include the badge's own
        # group(s) as a contact field (a user-defined field of the same name in
        # `contact` wins). Empty values are omitted.
        out = dict(self._contact) if isinstance(self._contact, dict) else {}
        groups = [g for g in (self._config.get("groups") or []) if g]
        if groups:
            out.setdefault("Groups", ", ".join(groups))
        return out

    async def _do_exchange(self):
        self._exchanging = True
        t0 = time.ticks_ms()
        try:
            self._show_banner("Swapping contacts…")
            self._wake()
            name = self._config.get("name", "") or "Anonymous"
            rec = await self._exch.run_window(self._ble, name, self._outgoing_contact())
            self._exch_log("%s board=%s %dms rec=%r trace=%s" % (
                _now_str(), "2026" if self._is_2026 else "2024",
                time.ticks_diff(time.ticks_ms(), t0), rec,
                " | ".join(self._exch.dbg)))
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
        except asyncio.CancelledError:
            raise            # app exiting mid-swap — don't touch widgets, just unwind
        except Exception:
            try:
                self._show_banner("Swap failed")
            except Exception:
                pass
        finally:
            self._exchanging = False
            self._exch_task = None

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
        add_received(store, rec)
        try:
            _atomic_write_json(self._contacts_path(), store)
        except Exception:
            pass

    # ------------------------------------------------------------------ BLE phone setup
    def _setup_url(self, bid):
        return SETUP_URL_BASE + "?badge=" + bid

    def _start_configure_setup(self):
        # Unconfigured badge, app foreground: run the setup GATT service so a
        # phone can configure us over Bluetooth (no proximity radio is running,
        # so the setup service owns the radio for the whole Configure-me screen).
        if self._setup_task is not None:
            return
        try:
            self._setup_task = TaskManager.create_task(self._run_configure_setup())
        except Exception:
            self._setup_task = None

    async def _run_configure_setup(self):
        me = asyncio.current_task()
        try:
            # No proximity to suspend, no timeout: runs until cancelled (screen
            # change / save) — request_stop() ends it after a successful save.
            await self._setup.run("configure", proximity=None, timeout_ms=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            # Only clear the handle if it still points at THIS task: the splash->
            # main setContentView re-fires onPause/onResume, which cancels this
            # session and starts a fresh one — a blind `= None` here would clobber
            # the new session's live handle (breaking teardown-on-pause + gates).
            if self._setup_task is me:
                self._setup_task = None

    def _open_setup_window(self):
        # Configured badge: open a bounded (SETUP_WINDOW_MS) setup window. The
        # session suspends the proximity radio and resumes it on close.
        if (self._unconfigured or self._exchanging or self._setup_open or
                self._setup_task is not None):
            return
        self._setup_open = True
        self._setup_win_deadline = time.ticks_add(time.ticks_ms(), SETUP_WINDOW_MS)
        self._show_setup_overlay()
        try:
            self._setup_task = TaskManager.create_task(self._run_setup_window())
        except Exception:
            self._setup_task = None
            self._setup_open = False
            self._hide_setup_overlay()

    async def _run_setup_window(self):
        me = asyncio.current_task()
        try:
            await self._setup.run("window", proximity=self._ble,
                                  timeout_ms=SETUP_WINDOW_MS)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._setup_open = False
            if self._setup_task is me:   # don't clobber a restarted session (see above)
                self._setup_task = None
            self._hide_setup_overlay()
            self._led_last = None
            try:
                self._show_banner("Setup closed")
            except Exception:
                pass

    def _stop_setup(self):
        # Cancel any running setup session (configure or window) and clear
        # overlay/window state. Mirrors _stop_task's cancel-then-teardown; the
        # session's run() re-raises the cancel and tears the radio down itself.
        if self._setup_task is not None:
            try:
                self._setup.request_stop()
            except Exception:
                pass
            try:
                self._setup_task.cancel()
            except Exception:
                pass
            self._setup_task = None
        self._setup_open = False
        self._hide_setup_overlay()

    def _show_setup_overlay(self):
        if self._overlay is not None:
            try:
                self._overlay.remove_flag(lv.obj.FLAG.HIDDEN)
                self._overlay.move_foreground()
            except Exception:
                pass
        self._setup_last = None
        self._overlay_qr_last = None

    def _hide_setup_overlay(self):
        if self._overlay is not None:
            try:
                self._overlay.add_flag(lv.obj.FLAG.HIDDEN)
            except Exception:
                pass

    def _refresh_setup(self, now):
        # Throttled: read the live badge id / code / (window) countdown from the
        # setup session and update the QR + on-screen text. Runs both on the
        # Configure-me screen (unconfigured) and in the window overlay.
        if time.ticks_diff(now, self._setup_next_ms) < 0:
            return
        self._setup_next_ms = time.ticks_add(now, 1000)
        if self._setup_task is None:
            return
        try:
            bid = self._setup.current_badge_id()
            code = self._setup.current_code()
        except Exception:
            return
        url = self._setup_url(bid) if bid and bid != "0000" else None
        if self._setup_open:
            # Configured-badge window overlay.
            self._update_setup_qr(self._overlay_qr, self._overlay_qr_box, url)
            self._set_lbl(self._overlay_code_lbl,
                          ("%s   code %s" % (setup_name(bid), code)) if code else setup_name(bid))
            # The window is an idle timeout that resets on BLE activity, so ask
            # the session for the true remaining time rather than counting down
            # from a fixed deadline (which would falsely hit 0 mid-transfer).
            try:
                secs = self._setup.window_secs_left()
            except Exception:
                secs = None
            if secs is None:
                secs = time.ticks_diff(self._setup_win_deadline, now) // 1000
            if secs < 0:
                secs = 0
            self._set_lbl(self._overlay_count_lbl, "closes in %ds · Y to close" % secs)
        else:
            # Configure-me screen (unconfigured).
            self._update_setup_qr(self._qr, self._qr_box, url)
            if bid and bid != "0000":
                txt = "%s   code %s" % (setup_name(bid), code) if code else setup_name(bid)
            else:
                txt = "starting Bluetooth…"
            if txt != self._setup_last:
                self._set_lbl(self._setup_info_lbl, txt)
                self._setup_last = txt

    def _update_setup_qr(self, qr, box, url):
        if qr is None or box is None:
            return
        is_configure = qr is self._qr
        last = self._qr_last if is_configure else self._overlay_qr_last
        if url == last:
            return
        try:
            if url:
                qr.update(url, len(url))
                box.remove_flag(lv.obj.FLAG.HIDDEN)
            else:
                box.add_flag(lv.obj.FLAG.HIDDEN)
        except Exception:
            return
        if is_configure:
            self._qr_last = url
        else:
            self._overlay_qr_last = url

    @staticmethod
    def _set_lbl(lbl, text):
        if lbl is None:
            return
        try:
            lbl.set_text(text)
        except Exception:
            pass

    def _reload_config(self):
        # Called from the setup service (same asyncio loop) after a save. Defer
        # the actual apply to the main loop so it never races the BLE tick /
        # exchange window / setup session.
        self._reload_pending = True

    def _apply_reload(self):
        # Runs on the main loop after a portal save. Keep this SAFE: only reload
        # the in-memory config and do in-place label updates. Do NOT rebuild or
        # re-submit the screen here — deleting the active screen (with its
        # scrolling labels) hard-crashes + reboots the badge on this build (see
        # _enter_main), and calling setContentView() a second time re-fires this
        # Activity's own onPause/onStart/onResume (mpos.ui.view.setContentView
        # always pushes onto the global screen stack and cycles the lifecycle of
        # whichever activity owns the new screen — even when that's `self`
        # again), which would tear down and duplicate the very state we're in
        # the middle of updating (portal, main-loop task, BLE). So name +
        # contact + runtime settings apply live; group pills, the friends
        # nametag layout and the on-air beacon (name/groups) update on the next
        # app start.
        if self._exchanging:
            self._reload_pending = True
            return
        was_unconfigured = self._unconfigured
        try:
            self._load_config()
        except Exception:
            pass
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
        if was_unconfigured and not self._unconfigured:
            # First-time setup just completed over BLE. Hand the radio from the
            # setup session to the proximity feature WITHOUT the two advertising
            # at once: the setup session is still advertising Fri3d-XXXX during
            # its save-grace (so the phone can read back the saved config), so we
            # DON'T begin proximity here. Instead flag it — the main loop starts
            # proximity once the setup session has fully ended (_setup_task None).
            # The Y-swap gate stays closed while _setup_task is not None
            # (see _handle_buttons), so Y can't find a half-up radio in between.
            self._pending_begin = True
            self._swap_setup_for_nametag()
        self._show_banner("Config saved ✓")

    def _swap_setup_for_nametag(self):
        # In-place layout swap on the SAME live screen: hide the setup widgets
        # (never delete — deleting live widgets/screens hard-crashes this
        # build) and create the nametag widgets next to them. No
        # setContentView either: re-submitting the screen pushes the OS stack
        # and re-fires this Activity's own onPause/onResume mid-update.
        for wdg in self._setup_widgets:
            try:
                wdg.add_flag(lv.obj.FLAG.HIDDEN)
            except Exception:
                pass
        self._setup_widgets = []
        self._qr = None          # box was hidden above; stop QR refreshes
        self._qr_box = None
        self._qr_last = None
        try:
            self._build_nametag(self._scr)
        except Exception:
            pass
        # This badge started unconfigured, so the nametag footer hint and the
        # setup-window overlay were never built — build them now (create-once).
        if self._setup_hint_lbl is not None:
            try:
                self._setup_hint_lbl.set_text("hold B: phone setup")
            except Exception:
                pass
        if self._overlay is None:
            try:
                self._build_setup_overlay(self._scr)
            except Exception:
                pass
        # New widgets were created after the banner, so re-raise it above them.
        if self._banner_bg is not None:
            try:
                self._banner_bg.move_foreground()
            except Exception:
                try:
                    self._banner_bg.move_to_index(-1)
                except Exception:
                    pass
