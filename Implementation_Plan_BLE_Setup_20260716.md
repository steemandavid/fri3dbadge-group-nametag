# Implementation Plan — BLE phone setup (Web Bluetooth) + WiFi portal removal

**Date:** 2026-07-16 · **Target version:** 0.8.0 · **Status:** approved design, not started
**Written as a handover:** the implementing model/developer is assumed to have NO prior
context beyond this repository. Read this file fully before writing any code.

---

## 0. Read these first

| File | Why |
|---|---|
| `README.md` | What the app does; build/publish; controls |
| `DESIGN.md` | Architecture + **verified platform facts**, esp. §1 (crash classes), §9 (contact exchange), §12 (background beacon) |
| `changelog.md` (top 3 entries) | v0.6.2–v0.7.2: the crash classes and deploy lessons were learned the hard way |
| `app/com.fri3dcamp.fri3dfriends/contact_exchange.py` | The proven GATT-server pattern you will extend |
| `app/com.fri3dcamp.fri3dfriends/web_portal.py` | The thing you are REPLACING — port its auth/lockout logic and `form_to_config` validation before deleting |
| `/home/john/claudecode/fri3d-usb-devices.md` | Which USB device is which badge; **deploy recipe**; CDC-wedge recovery |
| `tests/` | Host-test conventions (pure functions, `conftest.py` puts the app dir on `sys.path`) |

## 1. Goal and decisions already made (do not relitigate)

At Fri3d Camp the badges join a **separate SSID/subnet**: phones can never reach the
badge's HTTP portal by IP. Decisions taken with the project owner (David Steeman):

1. **Replace the WiFi portal with a Web Bluetooth setup page** — a static HTML page
   (GitHub Pages) that talks GATT directly to the badge. Works with zero network.
2. **Delete the WiFi portal entirely** (`web_portal.py`, its tests, all UI hooks).
   Received-contacts viewing/export moves to the BLE page.
3. **Personal-hotspot fallback: rejected** (too many user steps). Do not build it.
4. **iOS:** accepted trade-off — Safari has no Web Bluetooth; iPhone users use the
   free **Bluefy** browser app (the same page works in it unchanged). The page must
   detect `!navigator.bluetooth` on iOS and show a Bluefy hint + link.
5. **QR → one-tap connect:** the Web Bluetooth chooser CANNOT be bypassed (browser
   privacy model). Mitigation: badge advertises as `Fri3d-XXXX` (unique per badge),
   the QR encodes `?badge=XXXX`, the page filters `requestDevice` on that exact name
   → the chooser shows exactly ONE entry. `XXXX` is also shown on the badge screen.

## 2. Platform landmines (violating these = badge crash/reboot; all field-verified)

1. **NEVER delete a live LVGL screen or widget, and never call `setContentView`
   twice on the same Activity.** `setContentView` starts a non-blocking 500 ms
   `lv.screen_load_anim` and *pushes* the OS screen stack, re-firing this same
   Activity's `onPause/onResume`. Deleting the outgoing screen = use-after-free =
   hard reboot (v0.6.2 incident). The sanctioned pattern is **hide-never-delete +
   create-in-place** — see `_swap_setup_for_nametag()` in `fri3d_friends.py`.
2. **NimBLE one-time-only calls:** `BLE.config(mtu=…)` and `gatts_register_services`
   may effectively be issued ONCE per power-on (`contact_exchange.py:353`,
   `_mtu_set`/`_svc_ready` flags; re-issuing → `OSError(22)` until reboot).
   ⇒ **The new setup service MUST be registered in the same single
   `gatts_register_services` call as the exchange service.** Extend
   `ContactExchange._ensure_services()` to register both and expose the setup
   handles; do NOT add a second registration site.
3. **One radio, strict ownership.** Owners in priority order: contact-exchange
   window (Y-swap) > proximity beacon/scan (app foreground) > background beacon
   service (app closed; `beacon_service.py` — it checks the screen stack and never
   touches BLE while the app is anywhere on it). The WS2812 `lights.write()`
   disables IRQs and starves GATT — the main loop already skips LED work while
   `_exchanging`; treat an active setup session the same way.
4. **`except Exception` does not catch `KeyboardInterrupt`** — a USB-console Ctrl-C
   lands in whatever coroutine is running. Long-lived loops must follow the
   `beacon_service._watchdog` guard pattern (catch `BaseException`, re-raise
   `CancelledError` only).
5. **Async/UI:** single-threaded asyncio; `TICK_MS=30` main loop; never block.
   `time.sleep()` inside an `mpremote exec` starves the entire OS (sample state in
   a separate exec when hardware-testing).

## 3. Architecture

```
┌────────────── phone / laptop browser ──────────────┐
│ docs/setup/index.html  (static, GitHub Pages, HTTPS)│
│  Web Bluetooth: requestDevice({name:"Fri3d-XXXX"}) │
└────────────┬───────────────────────────────────────┘
             │ BLE GATT (MTU 512, negotiated once)
┌────────────▼───────────────────────────────────────┐
│ badge: setup GATT service (new ble_setup.py)        │
│  registered together with the exchange service      │
│  active ONLY: (a) Configure-me screen showing, or   │
│  (b) configured badge, user opens a 2-min window    │
│  auth: 4-digit code shown on badge screen           │
│  writes config atomically → existing _reload_config │
│  (incl. the live setup→nametag swap of v0.7.2)      │
└─────────────────────────────────────────────────────┘
```

### 3.1 Badge identity
`XXXX` = last 2 bytes of `bluetooth.BLE().config('mac')[1]`, uppercase hex
(stable per board — the fused MAC). Full BLE name: `Fri3d-XXXX` (10 chars).
Shown on the Configure-me screen and in the setup-window overlay.

### 3.2 GATT service (UUIDs — same Nordic-UART-derived base as the exchange)
```
SETUP_SVC    6e400020-b5a3-f393-e0a9-e50e24dcca9e
AUTH_CHR     6e400021-…   WRITE      ascii 4-digit code; per-connection auth
INFO_CHR     6e400022-…   READ       pre-auth: {"v":1,"badge":"XXXX","authed":false}
                                     post-auth: full config JSON (name/groups/contact/…)
CFG_CHR      6e400023-…   WRITE      chunked new-config JSON (see framing)
STATUS_CHR   6e400024-…   READ+NOTIFY last-op result (see codes)
CONTACTS_CHR 6e400025-…   READ       one page of contacts.json at the set offset
CTLOFF_CHR   6e400026-…   WRITE      u16-LE offset for the next CONTACTS read
```
- **Auth:** code is random 4-digit, generated when the setup session opens, shown on
  screen (port `web_portal.py`'s `_new_pin`/lockout: ≥5 wrong → 60 s lockout + rotate).
  Auth state is per-connection; reset on disconnect. All chars except AUTH/INFO(pre)
  reject unauthed access via STATUS `auth_required`.
- **CFG framing:** each write = `seq:u8 | total:u8 | payload≤(MTU-3-2)`. Reassemble,
  cap 2048 bytes total; on last chunk: `json.loads` → validate with a ported
  `form_to_config`-equivalent (`sanitize_config(dict, base) -> dict`) → atomic write
  (`_atomic_write_json`) → fire the app's `_reload_config()` → STATUS `ok` + notify.
  Any failure: STATUS `invalid` / `too_large` / `auth_required`; badge state unchanged.
- **Contacts read:** page 0 convention — client writes offset 0xFFFF to CTLOFF →
  CONTACTS read returns header `{"len":<total>,"page":400}`; then offset N → 400-byte
  slice of `contacts.json`. Client assembles + `JSON.parse` + client-side download.
- **MTU:** reuse the exchange's one-time `config(mtu=515)` (`_mtu_set`).
- **Advertising (setup mode):** connectable; `adv_data` = flags + complete local name
  `Fri3d-XXXX`; the 128-bit service UUID goes in `resp_data` (31-byte budget). The
  page filters by NAME and lists the service under `optionalServices` (a services
  filter would require the UUID in adv_data — not needed).

### 3.3 When setup mode runs (security = radio discipline)
- **Unconfigured badge, app open (Configure-me showing):** setup advertising ON,
  auth code + `Fri3d-XXXX` + QR on screen. This replaces "unconfigured badges are
  silent" with "unconfigured badges never run the *proximity* beacon" (update README
  wording). App closed → `beacon_service` keeps the radio fully OFF (unchanged).
- **Configured badge:** user opens a 2-minute setup window from the nametag. Button:
  **START if available** (README says intentionally unused) — verify it exists on
  both boards via `probes/probe_hw_api.py` / the hwtest app (2024: direct GPIO?,
  2026: expander index?). Fallback if START is absent on either board: **long-press
  B ≥1.5 s** (B short-press = mute stays). During the window: `BLEProximity.suspend()`
  → setup advertising → on close/timeout `resume()` (exact pattern of the Y-swap).
  Show a small overlay (create-once, hide/show — landmine #1) with QR + code +
  countdown. Gate Y-swap off while the window is open (reuse `_exchanging`-style
  skip in `_handle_buttons` / the main loop).
- Setup session also ends on: successful config write (+3 s grace), app pause/stop.

### 3.4 The web page (`docs/setup/index.html`)
- ONE self-contained file: inline CSS + JS, no frameworks, no build step. Serve via
  GitHub Pages (repo Settings → Pages → main branch, `/docs` folder). Final URL:
  `https://steemandavid.github.io/fri3d-friends/setup/?badge=XXXX` (~55 chars).
- Flow: parse `?badge=` → Connect button (user gesture required) →
  `requestDevice({filters:[{name:"Fri3d-"+id}], optionalServices:[SETUP_SVC]})`
  (no `?badge=` → fall back to `namePrefix:"Fri3d-"`) → connect → write AUTH (form
  asks for the on-screen code) → read INFO → render form (name, groups
  comma-separated, sound checkbox, banner_ms, rssi_floor, dynamic contact key/value
  rows — mirror the portal's form semantics incl. UTF-8) → Save → chunked CFG write
  → STATUS notify → "Saved ✓, badge shows your nametag".
- Contacts tab: paged read → table (name/groups/fields/timestamp) + "Download JSON".
- iOS: `if (!navigator.bluetooth)` + iOS UA → show "Open this page in Bluefy"
  (App Store link + `bluefy://open?url=…` deep link). Generic no-WebBT desktop hint:
  use Chrome/Edge.
- QR on badge: encodes the full page URL incl. `?badge=XXXX`. It no longer depends
  on WiFi — build it once at screen build (keep `_update_qr` only if you keep any
  dynamic part; otherwise simplify). Consider tile 150 px / QR ~124 px now that the
  portal footer line is gone (more vertical room; H=240, controls line at y=224).

## 4. Deletions (after porting what's noted)

| Delete | Port first |
|---|---|
| `web_portal.py` | `_new_pin`/lockout → auth; `form_to_config` validation → `sanitize_config`; `_esc` NOT needed (no HTML on badge) |
| `tests/test_web_portal.py` (~20 tests) | validation tests → retarget at `sanitize_config` |
| `fri3d_friends.py`: `_start_portal`, `_stop_portal`, `_refresh_portal`, `_portal*` fields, portal footer label, `WebPortal` import, `on_change` wiring (rewire to setup service) | the `_reload_config` → `_apply_reload` path STAYS (the BLE write uses it) |
| README §"Setup / contacts over WiFi", DESIGN §10 | replace with BLE-setup docs |

Nametag footer replacement: show `setup: hold B` (or `START`) instead of the URL.
Clock NTP resync (`_resync_time`) stays — WiFi is now optional but still used for time.

## 5. Implementation order (gated, verify each on hardware before the next)

1. **`ble_setup.py` (new)** — pure protocol first: `sanitize_config`, chunk
   assembler (stateful class, pure), contacts pager, auth state machine. Host tests
   for all of these (target ≥15; repo currently has 64 passing, ~20 leave with the
   portal). Then the GATT/IRQ wrapper following `contact_exchange.py`'s structure
   (irq capture-only, flags, no raising).
2. **Service registration merge** — extend `ContactExchange._ensure_services()` to
   register both services in ONE call; setup handles handed to `ble_setup`. Verify
   on hardware: Y-swap still works after a setup session and vice versa, twice each
   (the re-entrancy bugs of 2026-07-13 are the reference failure).
3. **App wiring** — Configure-me screen (ID + code + new QR), setup window on
   configured badges (button + overlay + suspend/resume), lifecycle teardown
   (onPause/onStop/onDestroy end the session; follow `_stop_task` patterns).
4. **Portal removal** (section 4) + README/DESIGN updates.
5. **Page** — build against a real badge using **desktop Chrome first** (Web
   Bluetooth works on Linux Chrome; fastest iteration). Also write
   `tools/setup_client.py` using `bleak` (pip) so the whole GATT protocol is
   testable from this machine without any browser: auth → read INFO → write config
   → verify badge switched to nametag → paged-read contacts. This doubles as the
   automated hardware test.
6. **End-to-end:** phone (Android Chrome) + the owner's iPhone/Bluefy; unconfigured
   AND reconfigure flows; two badges in setup mode simultaneously (name filter must
   isolate); swap + proximity regression pass.

## 6. Hardware verification — practical rules (save yourself hours)

- Badges by **stable path** `/dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_<serial>-if00`
  (never ttyACMx). Serials + roles: `/home/john/claudecode/fri3d-usb-devices.md`.
- **Deploy recipe** (the background beacon keeps BLE on, which wedges USB-CDC
  mid-copy): back up `config.json` on-badge → write `{"name":"","groups":[]}` (the
  beacon service then holds the radio OFF; `BLE().active(False)` alone is re-asserted
  within ~30 s) → `mpremote fs cp` → **`fs sha256sum` and compare** (interrupted
  copies leave truncated files) → restore config → reboot.
- Wedged CDC (raw REPL never engages, console silent): physical RESET/replug only.
- Boot services start once per boot: after deploying `MANIFEST.JSON`/service changes,
  reboot before judging behaviour.
- Launch the app headlessly: `AppManager.start_app('com.fri3dcamp.fri3dfriends')`;
  read app state via `mpos.ui.view.screen_stack` in a SEPARATE exec (no sleeps).

## 7. Acceptance criteria

1. Fresh badge, no WiFi anywhere: Configure-me shows QR + `Fri3d-XXXX` + code; scan →
   Bluefy/Chrome → one entry in chooser → code → form → Save → badge switches to the
   nametag live and is on the air (other badges alert). **No reboot at any step.**
2. Configured badge: setup window opens/expires/reopens cleanly; config edits apply
   live (name/contact/runtime keys) with the same "group changes need app restart"
   rule as before; Y-swap works immediately after the window closes.
3. Contacts: page shows all received contacts; exported JSON identical to
   `contacts.json` on flash.
4. Wrong auth ×5 → lockout + rotation, matching the old portal behaviour.
5. No portal code remains; `grep -ri "portal" app/` returns nothing functional.
6. All host tests green; no regression in swap/proximity/beacon-service behaviour
   on 2024 *and* 2026 boards.
7. `.mpk` builds via the README recipe; version 0.8.0.

## 8. Out of scope (explicitly)

- Hotspot auto-join (rejected), any cloud relay, mDNS.
- Native mobile apps.
- Changing the proximity/beacon/exchange wire formats (other badges in the field
  run them).
- OS-level fixes for the USB-CDC wedge.
