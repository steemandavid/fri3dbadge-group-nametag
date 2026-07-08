# DESIGN — Group Nametag + BLE Proximity Finder

Companion to `PLAN.md`. `PLAN.md` is the original spec (written against the
Fri3d Camp 2024 badge's `fri3d.application` firmware). **This file records how
the implementation was adapted to the badge actually on the bench**, the
verified hardware/firmware facts, the BLE protocol, and the open items.

## 1. Platform reality (deviates from PLAN.md §2/§3 — read this first)

The connected badge is **2024 badge hardware** (ESP32-S3-WROOM-1 N16R8V, MAC
`34:85:18:ab:df:0c`, confirmed via `esptool`) **running MicroPythonOS 0.11.1**
(MicroPython v1.27, `platform == esp32`), **not** the `badge_2024_micropython` +
`fri3d.application` firmware `PLAN.md` was written against.

Consequences (all verified by probe — see `probes/`):

- **No `fri3d` package exists** on the device (`import fri3d` → ImportError; not
  frozen, not on `/lib`). The plan's `App` base class, `app.json`, `AppManager`
  scanning `/remote/fri3d/apps` + `/user`, `self.config`, and the `neon_launcher`
  picker **do not apply**.
- **App model** is MicroPythonOS: an app is a folder `/apps/<fullname>/` with
  `MANIFEST.JSON` (`activities[].entrypoint` + `classname` + `intent_filters`
  `[{action:main, category:launcher}]`), imported as
  `from mpos import Activity`; lifecycle is `onCreate` / `onStart(screen)` /
  `onResume(screen)` / `onPause` / `onStop` / `onDestroy` / `onBackPressed`;
  return to launcher via `self.finish()`. The OS launcher discovers the app
  automatically (AppManager; call `AppManager.refresh_apps()` if added without a
  reboot). This app: `com.fri3dcamp.groupnametag`.
- **Config** is read from `/apps/com.fri3dcamp.groupnametag/config.json` (the
  per-install `groups`/`name`/`handle`/`rssi_floor`). Edit + reset (or refresh)
  — same UX intent as the plan's "edit app.json + reset".

### Verified hardware / API facts (2026-07-07)

| Concern | Finding |
|---|---|
| Display | GC9307 296×240, driven by lvgl v9 (`lv.obj()`/`lv.image()`/...). `lv.display_get_default()` (not `disp_get_default`). Decoders compiled in. |
| Logo decode | **`mpos.fs_driver` registers an LV_FS `"S:"` drive** → `im.set_src("S:/apps/com.fri3dcamp.groupnametag/logo.png")` decodes PNG/JPEG by path. No in-memory-buffer gymnastics needed (PLAN §7 primary-path spike is moot). Fallback: a runtime-drawn placeholder if the file is missing/broken. |
| LEDs | 5× WS2812 on GPIO12 via `mpos.lights` (`set_all(r,g,b)`, `set_led(i,r,g,b)`, `clear()`, `write()`, `get_led_count()==5`). `set_all` takes 3 args, **not** a tuple. |
| Buzzer | GPIO46 via `machine.PWM(Pin(46))` (`freq`, `duty_u16`, `0`=silent). |
| Buttons | raw `machine.Pin(gp, IN, PULL_UP)`, `value()==0` = pressed: START=0, X=38, A=39, B=40, Y=41, MENU=45. (`mpos.InputManager` is for lvgl pointer indevs, not these buttons.) |
| Battery | `mpos.BatteryManager.get_battery_percentage()` (returns e.g. `100.0`). Raw `ADC(Pin(13))` also works (~2407). |
| BLE | standard `bluetooth.BLE()` (NimBLE). **Stable public address confirmed** (see §3). |
| lvgl flags | `add_flag`/`remove_flag(lv.obj.FLAG.HIDDEN)` — there is **no `clear_flag`**. Fonts available: montserrat 12,14,16,18,20,24,28. New `lv.label()` default text is `"Text"`. |
| MicroPython `print()` | rejects `flush=`. |
| **Backlight/brightness** | **No API exists** (`display_get_default()` has no `set_brightness`/backlight attr). → backlight-dim feature **disabled** (PLAN §8; "drop if absent" rule). |

### Recovery / discipline notes
- A wedged badge (port opens, MCU silent) is recovered with
  `esptool.py --port /dev/ttyACM0 --before usb_reset --after hard_reset run`
  (bound with `timeout -s KILL`). `usbreset` only re-enumerates USB, not the core.
- `mpos.capture_screenshot()` from a raw paste probe deadlocks lvgl → **avoid**.
- mpremote `run`/`exec` does **not** soft-reset; `sys.modules` is cached between
  runs — bust the cache (`del sys.modules[m]`) when iterating on uploaded code.

## 2. Project layout

```
app/com.fri3dcamp.groupnametag/   → deployed to /apps/com.fri3dcamp.groupnametag/
  MANIFEST.JSON        MicroPythonOS app manifest (launcher intent)
  group_nametag.py     the Activity (UI, alerts, buttons, lifecycle)
  ble_proximity.py     BLE advertise/scan + group-aware state machine
  config.json          per-group/member config (edit this)
  logo.png             group logo (replace this); placeholder bundled as code fallback
  icon_64x64.png       launcher icon
tests/
  test_ble_proximity.py   off-device pytest (pure wire-format) — 30 tests, green
  conftest.py             sys.path shim (host imports ble_proximity straight from the app dir)
tools/
  host_advertise.py    BlueZ D-Bus LE advertiser (test harness: acts as a 2nd badge)
  pull_file.py         pull a binary file off the badge via chunked base64
```

## 3. BLE protocol (PLAN §6, unchanged in design)

Non-connectable legacy advertising, one Manufacturer-Specific AD structure
(type `0xFF`, company `0xFFFF` placeholder, little-endian):

```
[AD len][0xFF][0xFFFF][ "HSNT" ][ver][gcount][gid_le × gcount][namelen][name_utf8]
                     4 magic    1     1        2×G              1        ≤(20−2G)
```

- `fnv1a_16(name.strip().lower())` per group; **dedup + sort ascending**; cap at
  `MAX_GROUPS=5`, keeping the lowest ids (deterministic). Collision-tolerant, not
  security. Version byte `0x01`; receivers **drop** unknown versions.
- Name UTF-8, truncated on a codepoint boundary to `20 − 2×G` bytes. No Flags AD.
- **Stable address:** `ble.config("mac") == (0, <6 bytes>)` → addr_type **0 =
  public**, derived from the factory MAC, stable for the session (and across
  reboots). The `seen` table is keyed on `(addr_type, addr)`; no address-rotation
  ghosts. (`ble.config("addr")`/`"addr_type"` raise "unknown config param" on this
  build — use `"mac"`.)
- **Scan:** a **continuous, dense scan with an explicit interval/window** —
  `gap_scan(0, SCAN_INTERVAL_US=120000, SCAN_WINDOW_US=60000)` (50% duty), re-armed
  every 30 s as insurance. **This is load-bearing:** MicroPython's `gap_scan()` with
  *default* args turns on NimBLE's duplicate filter, so each peer is reported only
  ~once and presence flaps as peers age out and get evicted/re-detected. Passing
  explicit `interval_us`/`window_us` disables that filter, so every advertisement is
  reported and `last_seen` stays <1 s old even with several co-located badges
  colliding. (Measured: default scan = 2 hits/12 s; explicit 50% duty = 44 hits/12 s,
  peer age 0–1 s over 60 s with 3 badges — rock-solid, no flapping.) This superseded
  the PLAN's duty-cycled 1.5 s/4 s scan, which was too sparse under real collisions.
  Eviction: peers unheard for `EVICT_MS=30 s` are dropped (`time.ticks_diff`).
  Notify-once-per-encounter: first match → arrival event; eviction + return → one re-alert.
- `rssi_floor` config (default `-120` = disabled; radio sensitivity ≈ −97 dBm so
  −120 passes everything) is a coarse pre-filter, not calibration.

## 4. Verification status (PLAN §10 checklist)

| Item | Status | How |
|---|---|---|
| Logo decode | ✅ | `set_src("S:…/logo.png")` succeeds with registered fs_driver; placeholder fallback path coded. |
| Concurrent adv+scan | ✅ | 7 s concurrent run, no NimBLE crash. |
| Hardware-API probe | ✅ | addr (public, stable), lights/buzzer/buttons/battery all confirmed; **backlight absent → dim feature dropped+noted**. |
| Off-device unit tests | ✅ | `pytest tests/` → **30 passed**. |
| Single-badge smoke (advertise) | ✅ | Host bleak scanner received `34:85:18:AB:DF:0E rssi −75 ver 1 gids [0xa07b] name "Alex YOURCALL"` — full HSNT payload correct on-air. |
| Proximity logic (round-trip, disjoint, multi, signature, eviction, re-alert) | ✅ | 17 on-device checks through the **real** `parse_payload → intersect → seen → arrivals → eviction` path (synthetic-but-correct HSNT packets fed to the real IRQ handler). |
| Real badge **scan RX** | ✅ | `gap_scan` IRQ fires on real advertisements (raw counts); non-HSNT adverts correctly ignored. |
| Real **physical** round-trip (badge RX of a real HSNT advertiser) | ✅ | **Closed with a 2nd badge.** Badge #2's real `gap_scan` detected badge #1 over the air: `ARR name="Alex YOURCALL" shared="Makerspace Baasrode" id=0xa07b rssi=−40`. Both apps run + advertise + scan and detect each other (host scan sees both beacons: `Alex YOURCALL` + `Alice`). |
| Disjoint group ignored (real radio) | ✅ | Badge #2 scanning with a non-overlapping group → **0 peers** (Alex ignored). |
| App in launcher + clean exit | ✅ | discovered (`AppManager.refresh_apps()`); **launches in the real OS lifecycle** (`AppManager.start_app` → host scan sees the live beacon); **runs stably 20 s+**; **advertising stops after exit** (`restart_launcher` → 0 hits). |
| Real-app stability in OS | ✅ | advertised continuously 20 s+ after `start_app`; beacon payload stable. |
| Stable address / no ghosts | ✅ addr / ⚠️ long session | public MAC confirmed stable; long-session ghost check pending a live multi-minute run. |
| Per-group colour + tone | ✅ logic | signature `hue/freq` derived from group id (verified differs by group); lowest-shared-id rule coded. Live LED/buzzer flash confirmed individually (LEDs + PWM buzzer work). |
| Coalescing | ✅ | 2-arrival coalesced banner `"Alice, Bob nearby (Makerspace Baasrode)"` verified on-device; real-radio single-peer path confirmed (Alex → `"Alice nearby (Makerspace Baasrode)"`, rssi −50) with a 3-badge setup. |
| Own-group line / unconfigured hint | ✅ | both verified on-device (labels present; hint shows + BLE skipped when `name`/`groups` empty). |
| Backlight dim | ❌ dropped | no API; documented here. |

The host-as-2nd-badge path uses `tools/host_advertise.py` (BlueZ D-Bus
`LEAdvertisement1`, broadcast type, company `0xFFFF`). It registers cleanly; if
your BlueZ/`btmon` setup confirms TX, it completes the physical round-trip.

## 5. Open items / TODO

- **rssi_floor guidance table** (PLAN §6.3) — coarse dBm→range mapping for the
  README (−120 ≈ full range; −80 ≈ same tent/~10 m; −70 ≈ next to me). Treat as a
  *range gate*, not calibration.
- **Open-field link budget** write-up (≈ 50–100 m LoS badge-to-badge, less
  through tents/bodies) — defer to a field measurement.
- Confirm live launcher navigation + that advertising **stops** on exit (phone
  scanner) in a real session.
- Animated-GIF logos / scan-response name extension / persisted mute remain out
  of scope (PLAN §11).
