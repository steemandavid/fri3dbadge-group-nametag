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

- Animated-GIF logos / scan-response name extension / persisted mute remain out
  of scope.
