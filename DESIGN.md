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
  reboot). This app: `com.fri3dcamp.fri3dfriends`.
- **Config** is read from `/apps/com.fri3dcamp.fri3dfriends/config.json` (the
  per-install `groups`/`name`/`rssi_floor`). Edit + reset (or refresh)
  — same UX intent as the plan's "edit app.json + reset".

### Verified hardware / API facts (2026-07-07)

| Concern | Finding |
|---|---|
| Display | GC9307 296×240, driven by lvgl v9 (`lv.obj()`/`lv.image()`/...). `lv.display_get_default()` (not `disp_get_default`). Decoders compiled in. |
| Logo decode | **`mpos.fs_driver` registers an LV_FS `"S:"` drive** → `im.set_src("S:/apps/com.fri3dcamp.fri3dfriends/logo.png")` decodes PNG/JPEG by path. No in-memory-buffer gymnastics needed (PLAN §7 primary-path spike is moot). Fallback: a runtime-drawn placeholder if the file is missing/broken. |
| LEDs | 5× WS2812 on GPIO12 via `mpos.lights` (`set_all(r,g,b)`, `set_led(i,r,g,b)`, `clear()`, `write()`, `get_led_count()==5`). `set_all` takes 3 args, **not** a tuple. |
| Buzzer | GPIO46 via `machine.PWM(Pin(46))` (`freq`, `duty_u16`, `0`=silent). |
| Buttons | raw `machine.Pin(gp, IN, PULL_UP)`, `value()==0` = pressed: START=0, X=38, A=39, B=40, Y=41, MENU=45. (`mpos.InputManager` is for lvgl pointer indevs, not these buttons.) |
| Battery | `mpos.BatteryManager.get_battery_percentage()` (returns e.g. `100.0`). Raw `ADC(Pin(13))` also works (~2407). |
| BLE | standard `bluetooth.BLE()` (NimBLE). **Stable public address confirmed** (see §3). |
| **BLE `active(False)` clears the gatts server** | Verified on-device (2026-07-16): `active(False)` **wipes the whole `gatts_register_services` registration and the negotiated MTU**. Cached value handles then survive a `gatts_read` (spuriously) but **`gatts_write` raises `OSError(22)` EINVAL**. Re-registering / re-`config(mtu=)` **IS allowed after an `active(False)`/`active(True)` cycle** (only EINVALs when re-issued *without* a deactivate). ⇒ any component caching gatts handles across a possible `active(False)` (e.g. `proximity.end()` on onPause) must **re-register**, not reuse — `ContactExchange.ensure_radio` self-heals via a write-probe. This was the "swap dies until reboot after an app pause" bug. |
| lvgl flags | `add_flag`/`remove_flag(lv.obj.FLAG.HIDDEN)` — there is **no `clear_flag`**. Fonts available: montserrat 12,14,16,18,20,24,28. New `lv.label()` default text is `"Text"`. |
| MicroPython `print()` | rejects `flush=`. |
| **Backlight/brightness** | **No API exists** (`display_get_default()` has no `set_brightness`/backlight attr). → backlight-dim feature **disabled** (PLAN §8; "drop if absent" rule). |

### Recovery / discipline notes
- A wedged badge (port opens, MCU silent) is recovered with
  `esptool.py --port /dev/ttyACM0 --before usb_reset --after hard_reset run`
  (bound with `timeout -s KILL`). `usbreset` only re-enumerates USB, not the core.
- `mpos.capture_screenshot()` **cannot produce a usable screenshot** on this build.
  From a raw paste probe it deadlocks lvgl; from raw REPL (`mpremote run`/`exec`)
  it instead writes a 153,600-B file (320×240×2 RGB565) that is a **scrambled
  partial draw buffer, not a composited frame** — brute-forcing the row stride
  120–512 yields zero empty columns for every width, so no byte-order/stride/
  dimension reinterpretation gives a readable image. lvgl `snapshot` /
  `snapshot_create` are **not compiled in**. For "what's on screen", read the
  widgets instead: `mpos.get_all_widgets_with_text(lv.screen_active())` →
  `w.get_text()` (both it and `print_screen_labels()` take the screen object as
  one positional arg). See changelog 2026-07-10.
- **Multiple Espressif boards on USB:** the Fri3d 2024 and 2026 badges enumerate
  identically (`303a:4001`, same CDC descriptors) — distinguish them by the USB
  **`iSerial`** (`ID_SERIAL_SHORT`), never by `/dev/ttyACMx` (unstable) or VID:PID
  (ambiguous). Lilygo TTGOs are trivially separate (`1a86:55d4`). Always address the
  target via `/dev/serial/by-id/…<serial>…`.
- **lvgl label long-mode** enum is `lv.label.LONG_MODE.SCROLL_CIRCULAR` (etc.) —
  there is **no** `lv.label.LONG.*` on this build; the wrong name silently no-ops
  (label falls back to WRAP).
- **Don't `transform_scale` a scrolling label:** a scaled scrolling label forces
  lvgl to re-render + re-scale every animation frame, which starves the CPU (missed
  button polls, a stretched asyncio buzzer chime) and can lock up the USB-REPL. Use
  the largest built-in font directly (`font_montserrat_28`).
- **2024 buttons (verified 2026-07-11):** A=GPIO39, B=GPIO40 (raw, active-low,
  pull-up — **no** LVGL key events on the 2024 badge); X = the OS "back"/quit. Read
  A/B via raw `Pin` polling; let X fall through to the OS. (`clear_flag` does not
  exist — use `remove_flag` to un-hide.)
- mpremote `run`/`exec` does **not** soft-reset; `sys.modules` is cached between
  runs — bust the cache (`del sys.modules[m]`) when iterating on uploaded code.

## 2. Project layout

```
app/com.fri3dcamp.fri3dfriends/   → deployed to /apps/com.fri3dcamp.fri3dfriends/
  MANIFEST.JSON        MicroPythonOS app manifest (launcher intent)
  fri3d_friends.py     the Activity (UI, alerts, buttons, lifecycle)
  ble_proximity.py     BLE advertise/scan + group-aware state machine
  contact_exchange.py  Y-button connectable-GATT contact swap (+ shared service registration)
  ble_setup.py         Web-Bluetooth phone-setup GATT service (config/contacts)
  config.json          per-group/member config (edit from the phone setup page)
  fri3dfriends.png     !Fri3d Friends splash logo   |  icon_64x64.png  launcher icon
  montserrat_name.ttf  42px name font (subset TTF)
tests/
  test_ble_proximity.py   off-device pytest (pure wire-format) — 30 tests, green
  conftest.py             sys.path shim (host imports ble_proximity straight from the app dir)
tools/
  host_advertise.py    BlueZ D-Bus LE advertiser (test harness: acts as a 2nd badge)
  pull_file.py         pull a binary file off the badge via chunked base64
  setup_client.py      bleak GATT client for the setup service (headless test/scripting)
docs/setup/index.html  the Web-Bluetooth setup page (GitHub Pages)
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
- Both parsers (`parse_payload` here, `parse_exchange_adv` in `contact_exchange.py`)
  gate on the 2-byte **company id** (`0xFFFF`) *and* the 4-byte magic before
  accepting a beacon (was magic-only; tightened in the 2026-07-15 review — F-15).
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
| Still screenshot of running app | ❌ not achievable | `capture_screenshot()` writes a scrambled partial buffer (zero empty columns for every stride 120–512); `lv.snapshot` not compiled in. Read screen content via `get_all_widgets_with_text(screen_active())` instead. See changelog 2026-07-10. |

The host-as-2nd-badge path uses `tools/host_advertise.py` (BlueZ D-Bus
`LEAdvertisement1`, broadcast type, company `0xFFFF`). It registers cleanly; if
your BlueZ/`btmon` setup confirms TX, it completes the physical round-trip.

### Re-verification after the code-review fixes (2026-07-08)

The post-review fixes (see `Code_Review_Fixes_20260708.md`) were re-verified on the
same 3 badges (Alex/ACM0, Alice/ACM1, Bob/ACM2). Method: deploy with `mpremote fs
cp`, launch via `AppManager.start_app` in paste mode, and observe with a **passive**
host BLE scan plus lvgl label introspection (`mpos.get_all_widgets_with_text` /
walking `screen_active()`), instead of the REPL BLE begin/end cycling that wedged
badges previously — **no badge wedged this round.** Confirmed: 3-badge concurrent
advertise+scan, real multi-peer round-trip (`nearby: Alice, Bob`), coalesced banner,
disjoint-ignored, unconfigured→hint+BLE-silent, advertising stops on exit, detail
view content, and battery/own-group/help-line UI. The **long-session ghost check**
(the ⚠️ above) remains the one open item — addresses are still stable public MACs,
but a multi-minute soak wasn't run to keep the session short and avoid stressing the
NimBLE/USB stack.

## 5. `rssi_floor` guidance & open-field link budget

`rssi_floor` (per-install `config.json`, PLAN §5/§6.3) is a **coarse range gate,
not calibration** — it drops adverts whose received RSSI is below the threshold
*before* they reach the proximity state machine. It trades precise distance
control for zero on-site setup; RSSI varies with orientation, bodies, tents and
multipath, so treat these as order-of-magnitude, not metered, distances.

### 5.1 Guidance table

| `rssi_floor` | Effect | Rough open-field range that passes | Use when |
|---|---|---|---|
| `-120` (default) | **Disabled** — every packet the radio can decode passes | Full range: ~50–100 m LoS, less through crowds/tents | "Who from my groups is anywhere around?" (camp-wide) — the default. |
| `-90` | Light gate — rejects only the weakest fringe packets | ~30–50 m | Trim the far fringe while staying area-wide. |
| `-80` | Moderate gate | ~same tent / ~10 m | "Who's in my immediate area / same tent?" |
| `-70` | Tight gate | ~a few metres | "Who's right next to me?" (badge-tap-range demos). |
| `-60` and up | Very tight | ~touching / <1 m | Deliberate close-proximity only; will miss most real encounters. |

Notes:
- The ESP32-S3 receiver sensitivity floor is **≈ −97 dBm**, so anything below
  about −97 is never decodable anyway — `-120` is simply "no gate."
- RSSI is still smoothed into `rssi_ewma` and shown in the `A` detail view for
  information/tuning regardless of the floor.
- Raise the floor gradually: −120 → −90 → −80 and watch the detail view's dBm
  column for your actual peers before committing to a tighter value.

### 5.2 Open-field link budget (estimate)

Back-of-envelope for two badges, BLE legacy advertising on 2.4 GHz:

- **TX power:** ESP32-S3 default ≈ **0 dBm** (NimBLE default; not raised by this app).
- **Antenna gain:** PCB chip antenna ≈ **−2 dBi** effective each end (conservative,
  body-detuned on a worn badge).
- **RX sensitivity:** ≈ **−97 dBm** (coded-PHY not used; legacy 1M PHY).
- **Free-space path loss** (Friis) at 2.44 GHz: `FSPL(dB) ≈ 40.2 + 20·log10(d_m)`.

Allowable path loss = `TX − RX_sens + gains` ≈ `0 − (−97) + (−2 −2)` ≈ **93 dB**.
Solving `93 = 40.2 + 20·log10(d)` → `log10(d) ≈ 2.64` → **d ≈ 435 m** ideal LoS.
Real-world derating for a worn badge (body shadowing −10…−20 dB, fade/multipath
margin −10 dB, 2.4 GHz crowd/Wi-Fi congestion at a camp) pulls this down by
20–40 dB, i.e. to a **realistic ~50–100 m line-of-sight, dropping to ~10–30 m
through tents/bodies/crowds** — matching the "presence = in BLE range ≈ same
area" design intent (§3, PLAN §6.3). Bodies between two badges (the wearer's own
torso included) are the dominant real loss; expect worse when the peer is behind
you. **These are estimates — a field measurement would refine them; the design
does not depend on the exact number, only on "roughly same area."**

## 6. Remaining out-of-scope items (PLAN §11)

- Animated-GIF logos / scan-response name extension remain out of scope.
  (Persisted mute is now implemented — `sound` config key, toggled with B.)

## 7. 2024 vs 2026 badge support

The app runs on both the Fri3d Camp 2024 and 2026 badges (both ESP32-S3 +
MicroPythonOS). Hardware differences are abstracted in `fri3d_friends.py` and
selected at runtime. **Board detect:** `mpos.DeviceInfo.get_hardware_id()` →
`"fri3d_2026"` (fallback: `mpos.io_expander.version` exists only on 2026); a
config `"board"` key overrides autodetect. Screen size comes from
`mpos.DisplayMetrics` (2024: 296×240, 2026: 320×240).

| | 2024 | 2026 |
|---|---|---|
| Buttons A/B/X/Y/MENU | direct GPIO 39/40/38/41/45 (active-low, pull-up) | **CH32X035 I²C expander** `mpos.io_expander.digital` idx A=7, B=6, X=9, Y=8, MENU=5 (active-high) |
| START | GPIO 0 (active-low) | GPIO 0 (same) |
| Buzzer | GPIO 46 (PWM) | **GPIO 38** (PWM) — note GPIO38 is button-X on 2024 |
| LEDs / battery | `mpos.lights` / `mpos.BatteryManager` | same (portable) |
| Backlight (dim) | none (no API → dim is a no-op) | **yes** via `mpos.io_expander.lcd_brightness` (0-100; dim re-enabled) |
| X button | app reads GPIO38 (but OS also uses X = back) | OS = ESC/quit; **app does not bind X** (B is mute, START exits) |

The app binds **A** (panel), **B** (mute), **Y** (contact swap) — read via the
single `_held(name)` chokepoint which branches on `self._is_2026` (raw `Pin` poll
on 2024, `io_expander.digital` on 2026; Y = GPIO41 / expander idx 8). **START** is
unused; **X is left to the OS** (quit) on both badges. Source for the 2026 pinout:
the local `fri3d-badge-2026-developer-guide.md` + `fri3d-badge-hwtest/.../hwtest.py`
(authoritative `fri3d_2026.py` board module + live USB reads); the public
badge_2026 docs describe the CH32X035 conceptually but give no pin table. The 2026
path is **implemented but not yet runtime-verified** by the author (the 2026 badge
was in active use / off-limits during development).

## 8. Splash + live clock (v0.4.0)

- **Splash** (`_build_splash` / `_splash_then_enter` in `fri3d_friends.py`):
  built in `onCreate` and shown as the content view; a `TaskManager` task sleeps
  3 s then swaps to the nametag (`_enter_main`). Shows app name, version (read
  from `MANIFEST.JSON`), "by David Steeman", the **!Fri3d Friends logo**
  (`fri3dfriends.png`, the badge-bump × pixel-people hybrid) and "Makerspace
  Baasrode". The logo uses the **in-memory decode path**
  `lv.image().set_src(lv.image_dsc_t({… bytes …}))` — reliable, unlike the
  `set_src("S:/…")` path (§1). Explicit vertical positions (title y16 / ver y50 /
  author y72 / logo y100–196 / org y206) keep clear gaps so nothing overlaps.
  **Verified on all three badges** (splash then nametag, no wedge).
- **Clock**: top-left label in the same font/colour as the battery %
  (`_refresh_clock`, `HH:MM`, updated ~1/s), inset to `CLOCK_X=24` so the curved
  screen corner doesn't clip it. Time comes from the RTC, kept accurate by NTP:
  MicroPythonOS syncs on WiFi connect, and `_resync_time` re-syncs ~every 10 min
  (`ntptime.settime()`, only when `WifiService.is_connected()`). The first
  app-driven resync is deferred (the OS already synced at connect) so the
  **blocking** `settime()` never hitches launch. **Verified**: running badges
  showed correct wall-clock time.

## 11a. Name font & friends-line wrapping

- **Name** is rendered at **42 px** (1.5× the largest built-in `montserrat_28`)
  from a bundled subset TrueType (`montserrat_name.ttf`, ~15 KB, Latin+Latin-1)
  via `FontManager.getFont(size=42, ttf="M:apps/…/montserrat_name.ttf")` →
  `lv.tiny_ttf_create_file`. **Fixed font, not transform-scaled** (scaling a
  scrolling label starves the CPU — §1). Falls back to `font_montserrat_28` if the
  TTF can't be loaded. Note: `lv.binfont_create` on a `lv_font_conv` `.bin` did
  **not** load on this lvgl 9.4 build (format mismatch) — the TTF path is the one
  that works. `_load_name_font()` in `onCreate`.
- **Friends line** is inset (`W-40`) and `LONG_MODE.WRAP` so long peer names
  (e.g. `David Steeman ON4BDS`) wrap to the next line instead of being clipped by
  the rounded screen corner; the name list is capped at ~90 chars.

## 11. Friend LEDs — per-friend breathing (v0.4.0)

Each nearby friend gets one RGB LED, slowly + dimly **breathing that friend's
group colour** (friend 1 → LED 0, friend 2 → LED 1, …; the rest off). Driven by
`_update_leds(now)` in the main loop (every `LED_UPDATE_MS=60`):
- LED count is **board-keyed**: **4 on 2024, 5 on 2026** (`_led_count()`) — the
  firmware `get_led_count()` over-reports 5 on the 2024, which physically has 4.
- Colour = the peer's shared-group signature (`_sig_from_id`→`_hsv`, same hue as
  the on-screen pill), scaled by a slow cosine breathe between `LED_DIM_MIN=0.015`
  and `LED_DIM_MAX=0.18` over `LED_BREATHE_MS=3800`, with a per-LED phase stagger
  so they don't pulse in lockstep. Frame-cached to skip redundant `lights.write()`.
- Arrival/exchange flashes (`_flash_leds`) set a short `LED_FLASH_MS` override;
  breathing resumes automatically afterward.
- **Verified on the 2026 badge**: with 2 co-located friends, LEDs 0+1 breathed the
  dim green Makerspace-Baasrode colour, animating (green channel ~3↔44/255) and
  phase-staggered; LEDs 2–4 off. The 2024 path (4 LEDs) uses the identical logic.

## 9. Contact exchange — Y button (`contact_exchange.py`)

A short **connectable GATT session** (advertising's 31 bytes can't carry
arbitrary contact JSON). Same pure/radio split as `ble_proximity.py`; the pure
half is unit-tested off-device.

- **Rendezvous = overlapping press-triggered windows** (not wall-clock slots, so
  no synced clocks needed). Pressing **Y** opens a `WINDOW_MS = 5000` window in
  which the badge advertises a **connectable** beacon (magic `HXCG`, a `want`
  flag + nonce) *and* scans for peers' `HXCG` beacons. Two overlapping windows
  find each other.
- **Role tie-break** (`decide_role`): the two 6-byte addresses are compared;
  **lower = GATT server** (keeps advertising, waits), **higher = client** (stops
  advertising, `gap_connect`s). Both ends compute the same result, so exactly one
  connection is made (no double-connect).
- **The swap** is bidirectional in one connection: the server exposes a readable
  `MYINFO` characteristic (its envelope) and a writable `THEIRS` characteristic;
  the client reads `MYINFO` (gets the server's info) and writes its own envelope
  to `THEIRS` (`_IRQ_GATTS_WRITE` on the server). MTU is raised
  (`config(mtu=515)` + `gattc_exchange_mtu`) and the envelope capped to
  `MAX_CONTACT_BYTES=500` (fields dropped last-first to fit) so it rides one
  ATT op. Envelope = `{"n": name, "c": {field: value…}}` (`build/parse_contact_
  envelope`, defensive). `_outgoing_contact()` builds `c` from the user's
  free-form `contact` fields plus the badge's own **`Groups`** (auto-injected from
  config; a user field of the same name wins). The `groups` config key is
  otherwise local (nametag/proximity only).
- **Coexistence:** NimBLE has a single legacy adv set + one IRQ handler, so the
  exchange takes the radio over for the window: `BLEProximity.suspend()` stops the
  proximity scan/adv (without `active(False)` — avoids the begin/end churn that
  wedges the CDC, §1), and `resume()` reinstalls the proximity IRQ + non-
  connectable beacon and re-arms the scan afterward.
- **Storage:** received contacts are appended to
  `/apps/…/contacts.json` by `add_received` — **one entry per swap, no dedup**
  (swapping again with the same badge creates another entry, a fresh snapshot of
  who/what/when), capped at `MAX_CONTACTS=200` (oldest-appended dropped first).
  Each record carries mac, name, fields, rssi, `received_at`
  (`YYYY-MM-DDTHH:MM:SS` from the NTP-synced RTC) + `received_ticks`. The write is
  atomic (temp file + `os.rename`) so a power-off can't corrupt the file — the
  contacts list is the camp's takeaway artifact.
- **Rendezvous edge cases** (documented by the 2026-07-15 review, all non-crashing):
  - *Window-edge overlap (F-16):* everything is bounded by one shared `deadline`, so
    a rendezvous at ~4.9 s leaves too little time to connect+read+write and fails
    cleanly (`No one swapping nearby`) — acceptable by design.
  - *Three badges at once (F-16):* pairing is undefined — `_conn` is a single slot
    and whichever link event fires last wins; the losers time out gracefully. Camp
    groups *will* try three-way swaps; do them pairwise for a reliable result.
  - *Connectable exposure (F-17):* during your 5 s window the beacon is connectable
    and the server accepts any central, so a nearby scanner (e.g. nRF Connect) could
    read your `MYINFO` envelope without pressing Y. This is within the deliberate-
    share trust model (you pressed Y intending to hand this data to whoever's near),
    but note that "who can read it" is "anyone connecting during the window", not
    "only a Y-pressing peer".
- **Verification status:** ✅ pure functions (off-device pytest) **and the real
  two-badge round-trip** — swaps work repeatedly, incl. cross-model 2024↔2026.
  Two field bugs were found + fixed (see changelog 2026-07-13): (1) re-entrancy —
  `config(mtu=)`/`gatts_register_services` are one-time-only on NimBLE, so the
  2nd+ swap threw `OSError(22)` until reboot (fixed by making setup idempotent +
  guarded, `_mtu_set`/`_svc_ready`); (2) the main loop's `_update_leds()`
  `lights.write()` (IRQ-disabling) starved the short GATT connection — the loop
  now pauses all periodic work while `self._exchanging`.
- **2026-07-15 review hardening:** the swap task is cancelled on app exit
  (`run_window` re-raises `CancelledError` through its `finally`, so it can't touch
  a torn-down Activity — F-4); **Y** is gated on `_unconfigured` so a blank badge
  never activates an un-torn-down radio (F-5); button A/B actions are deferred while
  `_exchanging` so a stray press can't fire the starving LED write (F-7); and the
  client waits for `_IRQ_GATTC_WRITE_DONE` (bounded by the deadline) before
  disconnecting, instead of a fixed 150 ms nap that raced the ATT round-trip and
  caused rare one-sided swaps (F-11).

## 10. BLE phone setup (`ble_setup.py` + `docs/setup/index.html`, v0.8.0)

Replaces the old WiFi web portal (`web_portal.py`, removed in v0.8.0). At Fri3d
Camp the badges join a **separate SSID/subnet** from phones, so an HTTP portal by
IP is unreachable in practice. Instead a **static Web-Bluetooth page** (served
from GitHub Pages, `docs/setup/index.html`) talks GATT straight to the badge —
**zero network**. iOS Safari lacks Web Bluetooth; iPhone users use the free
**Bluefy** browser (the page detects `!navigator.bluetooth` on iOS and links it).

- **One radio, one registration.** NimBLE accepts `gatts_register_services` only
  **once per power-on**, so the setup service is registered **in the same call**
  as the contact-exchange service: `ContactExchange.ensure_radio()` /
  `_ensure_services()` build both service tuples, register them together, and
  hand the setup value handles to `SetupService.bind_handles()`. `ensure_radio()`
  is the single site that brings BLE up + sets the MTU once (515) + registers —
  used by both `run_window()` (swap) and `SetupService.run()` (setup).
- **GATT service** (`SETUP_SVC 6e400020-…`, Nordic-UART-derived base):
  `AUTH`(w) 4-digit code · `INFO`(r) pre-auth `{"v":1,"badge":"XXXX","authed":false}`
  / post-auth full config JSON · `CFG`(w) chunked config · `STATUS`(r+notify)
  last-op code · `CONTACTS`(r) one page · `CTLOFF`(w) u16-LE page offset.
- **Auth = badge-displayed code** (pairing model): a random 4-digit code per
  session shown on screen; per-connection auth; ≥5 wrong → 60 s lockout + code
  rotation (`AuthState`, ported from the portal's `_new_pin`/lockout). All chars
  except `AUTH`/pre-auth `INFO` answer `auth_required` until authed.
- **CFG framing:** each write is `seq:u8 | total:u8 | payload`. `ChunkAssembler`
  reassembles (cap 2048 → `too_large`); on the last chunk → `json.loads` →
  `sanitize_config(dict, base)` (the pure BLE equivalent of `form_to_config`) →
  atomic `config.json` write → fire the app's `_reload_config()` → `STATUS ok`
  + notify. Any failure → `invalid`/`too_large`/`auth_required`; state unchanged.
- **Contacts read (paging):** client writes offset `0xFFFF` → `CONTACTS` returns
  `{"len":N,"page":400}`; then offset 0,400,… → 400-byte slices of
  `contacts.json`. Client reassembles + `JSON.parse` + downloads. `contacts_response`
  is pure + unit-tested.
- **Advertising (setup mode):** connectable, `adv_data` = flags + complete local
  name `Fri3d-XXXX` (`XXXX` = last 2 bytes of the BLE MAC, uppercase — stable per
  board); the 128-bit service UUID goes in `resp_data`. The page filters
  `requestDevice` by that exact name so the chooser shows exactly one badge. The
  QR the badge shows encodes `…/setup/?badge=XXXX`.
- **When setup runs (security = radio discipline):**
  - *Unconfigured, app open (Configure-me screen):* `SetupService.run("configure")`
    advertises + serves GATT for the whole screen (no proximity beacon runs on an
    unconfigured badge — the README "silent" promise becomes "never runs the
    *proximity* beacon"; it does advertise `Fri3d-XXXX` so a phone can configure
    it). App closed → `beacon_service` keeps the radio off (unchanged).
  - *Configured badge:* **hold B ≥1.5 s** opens a window
    (`SetupService.run("window", proximity=self._ble, timeout_ms=SETUP_WINDOW_MS)`):
    it `suspend()`s proximity, advertises, shows a create-once overlay (QR + code
    + countdown), and `resume()`s on close/timeout (A/Y close early). START is
    intentionally unused; long-press B is board-agnostic (short B still mutes).
    `timeout_ms` is an **idle** window (2 min): any GATT event resets it (see
    `_process` bumping `_last_activity`), so a long friends-list transfer never
    times out mid-flight; `SETUP_ABS_CAP_MS` (10 min) is an absolute backstop. The
    overlay countdown reads `SetupService.window_secs_left()` for the true remaining
    idle time rather than a fixed deadline.
  - The main loop skips LED writes / scan while a setup session runs (a WS2812
    `lights.write()` disables IRQs and would starve the GATT link — same rule as
    the swap window). Y-swap is gated off while a session/window is active.
- **First-time configure (v0.6.3 → v0.8.0):** when a save flips the badge
  unconfigured → configured, `_apply_reload` calls `_swap_setup_for_nametag()`
  (setup widgets **hidden, never deleted**; nametag widgets **created in place**
  on the same live screen; banner re-raised via `move_foreground()`) and sets
  `_pending_begin`. Proximity `begin()` is deferred to the main loop until the
  setup session has **fully torn down** (`_setup_task is None`), so the setup
  advertising and the proximity beacon never run at once. Widget *creation* on a
  live screen is safe; *deletion* and second `setContentView` are the crash
  classes (see `_enter_main`).
- **Pure/host-tested:** `sanitize_config`, `ChunkAssembler`, `contacts_response`,
  `AuthState`, `badge_id`/`build_info`/`build_setup_adv` — `tests/test_ble_setup.py`.
  The radio half is exercised end-to-end by `docs/setup/index.html` (Chrome /
  Bluefy) and headlessly by `tools/setup_client.py` (`bleak`).

Notifications-on-badge and a personal-hotspot fallback were considered and
**dropped** (see the plan history and `Implementation_Plan_BLE_Setup_20260716.md`):
the iOS trade-off (Bluefy) is accepted because a WiFi portal is simply unreachable
across the camp's split SSIDs.

## 12. Background beacon service (`beacon_service.py`, v0.7.0)

Keeps the badge visible to friends' badges while the app is closed. A
manifest-declared boot service (`"services"` → `boot_completed`; supported by
the installed OS on both badge generations — verified via
`AppManager.get_services_for_action`). **Advertise-only**: non-connectable adv
of the exact same `build_payload` beacon; no scanning, alerts or swaps in the
background.

- **Radio ownership rule** (single `BLE()` stack / adv set): a slow watchdog
  (5 s poll) checks whether the app's Activity is anywhere on
  `mpos.ui.view.screen_stack` (membership, not top-only — the app pushes *two*
  entries: splash + main). App on the stack → service never touches BLE (the
  Activity's lifecycle owns begin/suspend/teardown, incl. the swap window).
  App absent → service `active(True)` + advertises, re-asserting every ~30 s.
- **Handoff needs no coordination calls**: the app's `begin()` replaces the
  service's identical adv on open; the app's teardown `active(False)` is undone
  by the next watchdog poll (≤5 s beacon gap) after exit.
- **Unconfigured badge stays silent** in the background too
  (`load_beacon_config` mirrors `_load_config`'s unconfigured rule).
- **Watchdog survivability**: the loop catches `BaseException` (only
  `CancelledError` passes) around body *and* sleep — a USB-console Ctrl-C is
  delivered as `KeyboardInterrupt` to whatever coroutine is running and must
  not kill the beacon permanently.
- **Verified on-device (2026-07-15)**, 2024 ↔ 2026 both directions: beacon on
  the air with the app never opened; app-open → app-owned beacon continues;
  app-exit → service reclaims ≤ poll interval; adv killed externally →
  self-heals within a refresh cycle. Known cosmetic quirk: dev-time REPL scans
  on a badge can knock its adv off the air for ≤30 s (self-heals).
- **Boot-only start**: the service activates on the next reboot after
  install/update; an AppStore update does not hot-swap a running service.
