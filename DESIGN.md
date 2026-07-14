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
  from `MANIFEST.JSON`), "by David Steeman", the Makerspace logo and name. The
  logo uses the **in-memory decode path** `lv.image().set_src(lv.image_dsc_t({...
  bytes ...}))` (asset `makerspace.png`, copied from `org.fri3d.hwtest`) — this is
  reliable, unlike the `set_src("S:/…")` path (§1). Text fallback if the asset is
  missing. **Verified on the 2024 badge** (splash then nametag, no wedge).
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
  (`YYYY-MM-DDTHH:MM:SS` from the NTP-synced RTC) + `received_ticks`.
- **Verification status:** pure functions ✅ (off-device pytest). The radio
  round-trip needs **two badges** pressing Y together (like the proximity round-
  trip) — **not yet run** (single target badge on the bench). Risk noted: some
  NimBLE builds require `gatts_register_services` before the *first* advertise; we
  register lazily after `suspend()` and wrap everything in try/except so a failure
  degrades to `No one swapping nearby` rather than crashing.

## 10. WiFi setup portal (`web_portal.py`)

Assumes the badge is **already on WiFi** (OS auto-connect) — the app does not
manage STA/hotspot, it just reads `WifiService.get_ipv4_address()` and serves.
Always-on while the app is foreground (`_start_portal` in `onResume`, `_stop_
portal` in `onPause`/`onStop`). The built-in `WebServer` is only a WebREPL bridge,
so this is a small custom HTTP server on `asyncio.start_server` (cooperative with
the app's loop; no threads).

- **Routes:** `GET /` (config form + dynamic `contact` key/value editor),
  `POST /save` (writes `config.json`, fires an app reload), `GET /contacts`
  (table), `GET /contacts.json` (export). Form helpers `parse_form` /
  `form_to_config` are pure + unit-tested.
- **Auth = badge-displayed PIN** (pairing model for an always-on server on a
  shared camp LAN): a random 5-digit PIN per boot, entered once → signed session
  cookie (`SESSION_TTL_MS`); ≥5 wrong tries → brief lockout + PIN rotation. The
  nametag footer shows `⚙ http://<ip>:8080`, and surfaces the PIN as a challenge
  (`portal PIN: …`) when an unauthenticated request arrives (`pending_pin()`).
  Plain HTTP → the PIN gates *access*, not traffic; acceptable badge trust model.
- **Config reload:** `on_change` sets a flag consumed on the main loop
  (`_apply_reload`), which does a **safe in-place reload only** — reload config +
  `set_text` the name/controls + show a "Config saved ✓" banner. It deliberately
  does **not** rebuild the screen or restart BLE: deleting the active screen (with
  its scrolling labels) and cycling NimBLE hard-crash + reboot the badge on this
  build (and leak memory). So name/contact/runtime settings apply live; group
  pills and the on-air beacon update on the next app start.
- **Verification status:** the portal **starts and the footer renders correctly**
  on-device (showed `⚙ WiFi not connected` with no network present, and the
  server binds regardless). Browser round-trip (login/save/export) needs the
  badge on a real network — **not yet run** on the bench.

Notifications-on-badge and a BLE phone-companion were considered and **dropped**
(see the plan history): Android notification mirroring would force a native
companion app, and BLE Web-Bluetooth config excludes iOS Safari — the always-on
WiFi portal reaches every phone.
