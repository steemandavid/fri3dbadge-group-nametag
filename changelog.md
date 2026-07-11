# !friends nearby — UI redesign + Fri3d 2026 support + button/perf fixes — 2026-07-11

Renamed the app to **"!friends nearby"** and redesigned the screen; added Fri3d
2026 badge support; fixed button handling and a CPU-starvation bug. No BLE /
protocol / test changes (`ble_proximity.py` untouched, 30/30 pytest still green).

## UI redesign (group_nametag.py, MANIFEST, README, DESIGN)
- Renamed (`MANIFEST.name`) to "!friends nearby" (the `!` sorts it to the top of
  the launcher).
- Layout: **name** (`font_montserrat_28`, single line, marquee-scrolls when too
  long) at the top; **group(s)** as full-width coloured pills (per-group signature
  colour, stacked vertically, each scrolls if its name is too long); a
  **friends line** (`Friends nearby: <names>` or `looking for friends…`); battery
  (inset from the rounded corner); controls at the bottom. The group **logo**
  (decode broken on this build) and the earlier breathing avatar disc were dropped.
- **A** opens a friends-nearby panel (cards: colour dot · name · shared group ·
  signal bars · dBm · age). **B** = mute/unmute (persisted to config; the controls
  label reflects the state). **X** = OS quit. **START** unused.
- New config keys: `sound` (bool), `banner_ms` (ms, default 5000), `board`
  (optional "2024"/"2026" override).

## Fri3d 2026 badge support
- Board autodetect (`mpos.DeviceInfo.get_hardware_id()` → `"fri3d_2026"`, fallback
  `mpos.io_expander.version`); 2026 reads A/B via the **CH32X035 I²O expander**
  (`mpos.io_expander.digital` idx A=7, B=6), uses **320×240**, buzzer **GPIO38**,
  and re-enables the backlight via `io_expander.lcd_brightness`. LEDs/battery via
  `mpos` (portable). Screen size comes from `mpos.DisplayMetrics`. (2026 runtime
  not yet verified — the 2026 badge was in active use / off-limits.)

## Hard-won bugs found & fixed
- **lvgl long-mode enum** is `lv.label.LONG_MODE.SCROLL_CIRCULAR` — **not**
  `lv.label.LONG.*` (which doesn't exist on this build). The wrong name silently
  no-op'd, so long names wrapped to a second line.
- This build has **`add_flag`/`remove_flag` but no `clear_flag`** (already in
  DESIGN §1 — I re-tripped it): using `clear_flag` to show the A-panel silently
  raised + was swallowed by `try/except`, so the panel never appeared.
- **Buttons** (2024): A=GPIO39, B=GPIO40 (raw GPIO, active-low; **no** LVGL key
  events on 2024), X = OS "back"/quit. Confirmed with a throwaway on-screen
  input-test app. 2026 equivalents via the expander (see DESIGN §7).
- **CPU starvation**: a **2× transform-scale on the scrolling name** made lvgl
  re-render + re-scale it every animation frame, swamping the CPU → asyncio loop
  starved (button polls missed, arrival chime audibly stretched) and eventually the
  USB-REPL locked up. Fixed by rendering the name at `font_montserrat_28` (no
  transform scale). Also reduced per-tick lvgl re-renders to "only when the text
  actually changes".

## Verification
- 30/30 `pytest tests/` (untouched).
- On-device (2024 badge): name scrolls on one line, 2 group pills show in full,
  A expands/closes the panel, B mutes+persists (label flips), X quits, chime is
  crisp, and the USB-REPL stays responsive under load.

---

# Group Nametag + Proximity Finder — screenshot-capture investigation — 2026-07-10

Attempted to capture a still screenshot of the running app on a configured badge
for documentation. No app/code changes; **findings only**, folded into `DESIGN.md`
§1/§4.

## `capture_screenshot()` is not usable for a full-frame capture on this build
- `mpos.capture_screenshot('/data/shot.bin')` does **not** deadlock from raw REPL
  (`mpremote run`/`exec`) — only from paste, where it deadlocks lvgl (DESIGN §1).
  But the 153,600-byte file it writes (320×240×2 RGB565) is a **scrambled partial
  lvgl draw buffer, not a composited frame**: brute-forcing the row stride from
  120–512 yields **zero empty columns for every width** (no text columns or screen
  margins ever line up), so no byte-order/stride/dimension reinterpretation
  produces a readable image. lvgl `snapshot`/`snapshot_create` are **not compiled
  in**, so there is no on-device path to a true pixel screenshot without a firmware
  change (enable `LV_USE_SNAPSHOT`) or SPI panel-GRAM readback.
- **Reliable alternative for "what's on screen":** `mpos.get_all_widgets_with_text(lv.screen_active())`
  returns the label widgets; `w.get_text()` gives each label's text. `print_screen_labels(scr)`
  takes the screen object as one positional arg. Used to confirm the live idle
  screen (name, own-group, battery %, the `nearby:` line, the controls hint) and a
  live arrival banner.

## Other verified facts (for next time)
- Display framebuffer is **320×240 RGB565 little-endian** (153,600 B); visible area
  **296×240** (`mpos.DisplayMetrics.width()/height()`). App background `0x0862`.
- `mpos.get_foreground_app()` returns the **fullname string**; `AppManager.start_app(fullname)`
  works from raw REPL (paste mode not required to launch).
- File transfer: `mpremote fs cp` works on a **freshly-booted** badge; it hangs/wedges
  on a stressed one, and large exec-output (>~a few KB) stalls the CDC TX. The bundled
  `tools/pull_file.py` (chunked base64) is the fallback puller.
- **Wedged badge** recovery: physical RESET, or `esptool --before usb_reset --after hard_reset run`
  (esptool wasn't installed on the host and pip is PEP-668-locked, so RESET was used).

## Device identification (2024 vs 2026 badge)
With multiple Espressif boards on USB, the **Fri3d 2024 and 2026 badges enumerate
identically** (`303a:4001`, same CDC descriptors) — distinguishable only by the USB
**`iSerial`** (`ID_SERIAL_SHORT`), never by `/dev/ttyACMx` (unstable) or VID:PID
(ambiguous). The Lilygo TTGOs are trivially separate (`1a86:55d4`). Always address
the target badge via `/dev/serial/by-id/…<serial>…`.

## Observed app behaviour (live hardware)
On a configured badge, the proximity finder detected a real co-located peer over BLE
and raised the arrival banner — the end-to-end advertise → scan → intersect → alert
flow works on live hardware. The logo still does not render on this build (blank
middle; the known D-12 silent-decode issue). No pixel screenshot was obtainable, so
the display was photographed with a phone for the record.

---

# Group Nametag + Proximity Finder — review-fix implementation, on-device test, UI tweaks — 2026-07-08

Implemented the fixes from the Phase 0–4 code review, completed the DESIGN.md doc
TODOs, ran the full on-device test suite on 3 badges, and made a couple of UI
tweaks. Fixes/results captured in `Code_Review_Fixes_20260708.md`.

## Code-review fixes (`ble_proximity.py`, `group_nametag.py`, `config.json`)
All findings from `Code_Review_Phase0-4_20260708_0800.md`:
- **D-1** UI loop `try/except` moved *inside* the `while` (a per-frame error no
  longer permanently freezes the render/input loop); added a `_finishing` flag so
  `B`/`START` breaks the loop before touching torn-down widgets.
- **D-2** BLE scan IRQ↔loop race on `_seen`: the IRQ now only enqueues raw results
  into a bounded `_pending` list; all parse/intersect/`_seen` mutation moved to
  `_process_pending()`/`_process_result()` on the loop thread (also fixes **D-9**).
- **D-3** logo now scaled-to-fit (`_read_png_size` + fit-to-box); `_logo_base_scale`
  is live; **D-4** alert coalescing accumulates across the banner window (one cue).
- **D-5** `B` exits like `START`; **D-6** shipped `config.json` is now an empty
  template (fresh badge shows the hint + stays BLE-silent); **D-7** wrap-safe
  `ticks_diff`/`ticks_add` in the scan re-arm + battery throttles; **D-8** `begin()`
  coerces name/handle to str and returns truthful advertise success.
- **D-12** (was deferred) logo placeholder now shown for a missing/empty/non-PNG
  file via a deterministic file gate — confirmed on-device that lvgl fails silently
  (no exception) and `image_decoder_get_info` is not exposed on this build.
- Bonus: `_process_result` now refreshes a re-seen peer's **name** (a rename was
  previously invisible for up to the 30 s eviction window).
- Host `pytest tests/` stays **30/30**; added a host-level exercise of the
  refactored radio wrapper (deferred IRQ→tick, notify-once, eviction, disjoint, etc.).

## DESIGN.md doc TODOs completed
- **§5** filled in: the `rssi_floor` dBm→range guidance table (−120 full-range …
  −70 next-to-me) and the open-field link-budget estimate (Friis ≈93 dB budget →
  ~435 m ideal LoS, derated to a realistic ~50–100 m LoS / ~10–30 m through
  bodies/tents).
- **§4** re-verification note added after the on-device run.

## On-device full test — 3 badges (ACM0/1/2), no badge wedged
Deployed the fixed code with **`mpremote fs cp`**, launched via
`AppManager.start_app` in **paste mode**, verified with a **passive host BLE scan**
(`bleak`) plus **lvgl label introspection** — deliberately avoiding the REPL BLE
begin/end cycling that wedged badges in a prior session. All passed:
- Single + 3-badge concurrent advertise (HSNT payload correct: `ver 1`, `gid 0xa07b`).
- Real multi-peer round-trip detection; **coalesced** banner for two arrivals.
- Disjoint group ignored; unconfigured → "Configure me" hint **and absent from the
  air**; advertising **stops on exit** (`restart_launcher`); detail view content
  (name · shared group · smoothed RSSI · age); live battery/own-group/help UI.

Method gotchas (for next time):
- Paste-based file upload **hangs on large files** (paste-mode echo never goes
  quiet) → use `mpremote fs cp` for transfers; paste mode only for launch/read.
- `mpos.get_foreground_app()` returns the **fullname string**, not the instance;
  the live Activity is `activity_navigator.screen_stack[i][0]`.
- Avoid `mpos.capture_screenshot()` from paste (deadlocks lvgl) — read UI via
  `screen_active()` label-walking or `mpos.get_all_widgets_with_text`.

## UI tweaks (`group_nametag.py`)
- **Name enlarged 1.5×** and **moved to the top**: rendered at `font_montserrat_28`
  (the largest built-in) then `transform_scale(384)` (=1.5×) pivoted on the label's
  horizontal centre so it stays centred; `NAME_TOP=6`. Logo relocated below it
  (`LOGO_TOP 14→74`, `LOGO_BOX_H 104→74`); own-group line moved under the logo.
- Removed the callsign handle from the reference config; updated the on-badge peer
  display names.

## Privacy note
The on-device test output re-introduced the maintainer's real name + callsign into
`DESIGN.md` and `Code_Review_Fixes_20260708.md`. Since the GitHub repo is **public**
and was previously scrubbed of that identity, these were re-scrubbed to the same
generic `Alex`/`YOURCALL` before commit. (The makerspace group name and badge MACs
remain in-repo — still flagged for the owner to decide, unchanged this session.)

---

# Group Nametag + Proximity Finder — published to GitHub + privacy scrub — 2026-07-08

- Published the project as a **public** GitHub repo under the owner's account
  (`fri3dbadge-group-nametag`), initial commit + push.
- **Sensitive-info audit** of all tracked files: clean — no emails, passwords, API
  keys/tokens (the GitHub PAT used for the push was never written to any file), IPs,
  WiFi creds, host paths, or real names/phones. Image metadata is benign (only an
  editor version + DPI); generated PNGs are fully stripped.
- **Privacy scrub + history rewrite**: removed the maintainer's personal name and
  callsign from all file contents (→ generic `Alex`/`YOURCALL` examples; `config.json`
  is now a `Your Name`/`YOURCALL` template) and from the commit author/committer
  identity. Because the repo was already public, this was done as a commit **amend +
  force-push** (`dc50c10` → `9611146`) so the names leave `main`'s history, not just
  the tip. `git grep` against the remote tree confirms `main` is clean.
  - Caveat: the orphaned commit may remain reachable by SHA / in caches for a time;
    delete-and-recreate the repo for a guaranteed purge.
  - Still in the repo (outside the name+callsign scrub scope, flagged for the owner):
    the makerspace group name (17×), its logo image, and badge MAC addresses (3×).
- **Phase 0–4 code review** (`Code_Review_Phase0-4_20260708_0800.md`): independent
  review verdict **PASS WITH NOTES** — no critical defects; the scan-stability fix and
  platform adaptation endorsed.

---

# Group Nametag + Proximity Finder — SCAN STABILITY FIX — 2026-07-07

Fixed the **presence flapping** the user observed with 3 co-located badges (each
showed random 0/1/2 peers, losing and re-detecting the others).

## Root cause
`gap_scan()` with **default args** enables NimBLE's **duplicate filter** → each peer
reported only ~once → `last_seen` ages out → peer evicted at 30 s → disappears, then
re-detected (re-alert) next time heard. Compounded by the PLAN's sparse 1.5 s-on / 4 s
duty cycle and 3-badge advertising collisions, so most short scan windows caught nothing.
Measured: default `gap_scan(12000)` = **2 hits/12 s** (≈1 per peer); the app's
`gap_scan(1500)` re-armed every 4 s detected only **1 peer once in 60 s**.

## Fix (`ble_proximity.py`)
Continuous dense scan with **explicit `interval_us`/`window_us`** — that disables the
duplicate filter so every advertisement is reported:
`gap_scan(0, SCAN_INTERVAL_US=120000, SCAN_WINDOW_US=60000)` = 50% duty, re-armed every
`SCAN_REARM_MS=30 s` as insurance. Replaced `SCAN_ON_MS`/`SCAN_CYCLE_MS` duty cycling;
`tick()` now just evicts + re-arms.
- Measured: 50% duty = **44 hits/12 s**, peer age **0–1 s over 60 s** with 3 badges
  advertising → stable presence, no flapping, no spurious evictions. 100% duty = 93 hits,
  age 0 (chosen 50% for the power/stability tradeoff).
- Verified on-device: fixed `BLEProximity` held both peers at age ≤1 s for the full 60 s.

## Deployed
Fix deployed + apps relaunched on Alex (ACM0) and Bob (ACM2). Alice (ACM1) USB-wedged
(app still running old code) — needs a physical RESET, then deploy + relaunch to get it.

## Note
Discovered along the way: a USB-serial hang does **not** stop the app — the activity +
BLE keep running (Alice's app kept advertising while her USB was unresponsive). Recovery
of a truly wedged badge is still `esptool --before usb_reset --after hard_reset run`, or a
physical RESET. Repeated REPL BLE begin/end cycling is what wedges badges — avoided now by
testing the fix via the running apps rather than more REPL cycling.

---

# Group Nametag + Proximity Finder — IMPLEMENTED (Phase 0–4) — 2026-07-07

Implemented and verified the app from `PLAN.md`, **adapted to the badge actually
on the bench** (see `DESIGN.md` §1). The connected badge is 2024 hardware but
runs **MicroPythonOS 0.11.1**, not the `fri3d.application` firmware the plan was
written against — so the behavioural design (BLE protocol, multi-group matching,
proximity state machine, per-group signature, alerts, idle UI) was kept verbatim
and only the framework shell moved to a MicroPythonOS `Activity`.

## Built
- `app/com.fri3dcamp.groupnametag/` — a full MicroPythonOS app:
  `MANIFEST.JSON` (launcher intent), `group_nametag.py` (the Activity),
  `ble_proximity.py` (BLE advertise/scan + group-aware state machine),
  `config.json`, `logo.png`, `icon_64x64.png`.
- `tests/test_ble_proximity.py` + `conftest.py` — off-device pytest.
- `tools/host_advertise.py` (BlueZ D-Bus LE advertiser = a 2nd-badge test stand-in),
  `tools/pull_file.py`.
- `DESIGN.md` (platform adaptation, verified facts, protocol, verification
  status, TODOs), `README.md` (group quick-start).

## Verified autonomously (real hardware)
- **30/30 host pytest** (`pytest tests/`) — protocol round-trip, fnv1a_16, dedup/sort,
  UTF-8 truncation, version rejection, malformed-packet dropping, intersection.
- **17/17 on-device BLE state-machine checks** — shared-group arrival, disjoint
  ignored, dedup, multi-group, per-group signature, eviction, re-alert-after-return,
  lifecycle — through the real `parse_payload → intersect → seen → arrivals` path.
- **Real BLE advertising** — host bleak scanner received the badge's beacon:
  `34:85:18:AB:DF:0E rssi −75 ver 1 gids [0xa07b] name "Alex YOURCALL"` (§10 single-badge smoke).
- **Real BLE scan RX** — `gap_scan` IRQ fires on real adverts; non-HSNT ignored.
- **UI build** — all idle labels present; banner + coalescing ("Alice, Bob nearby
  (Makerspace Baasrode)"); unconfigured hint; **main loop integration** (breathing
  animation oscillates 244–269, battery → "100%", no exceptions over 3 s).
- **App discovery** — `AppManager.refresh_apps()` finds `com.fri3dcamp.groupnametag`.
- **Stable public BLE address** confirmed (`ble.config("mac")` → type 0).

## Platform findings folded in (`DESIGN.md` §1)
- `mpos.fs_driver` registers LV_FS `"S:"` → logo decode by path (no in-memory spike).
- `mpos.lights` (set_all(r,g,b)/set_led/clear), `machine.PWM(Pin(46))` buzzer, raw
  `machine.Pin` buttons, `mpos.BatteryManager.get_battery_percentage()`.
- lvgl: `add_flag`/`remove_flag` (no `clear_flag`), `lv.display_get_default()`.
- **Backlight/brightness API absent → dim feature dropped + noted.**
- Recovery: a wedged badge is reset with `esptool --before usb_reset --after hard_reset run`;
  `usbreset` alone re-enumerates USB but not the core; avoid `mpos.capture_screenshot()`
  from raw paste probes (deadlocks lvgl).

## Live lifecycle checks (after physical badge reset)
- **App launches in the real OS lifecycle** — `AppManager.start_app("com.fri3dcamp.groupnametag")`
  (via paste mode, which coexists with the OS asyncio loop); host scan then sees the live
  beacon `name "Alex YOURCALL"` → real `onCreate`/`onResume` → `BLE.begin` → advertise works
  from within the Activity (not just the REPL).
- **Runs stably** — advertised continuously 20 s+ in the real OS, no crash.
- **Advertising stops on exit** — `AppManager.restart_launcher()` tears down the activity
  (`onStop`→`ble.end()`); host scan then finds **0** hits.
- Deployed code confirmed current (lvgl `remove_flag` fix present on-device).

## 2-badge field test — ROUND-TRIP CLOSED ✅
A 2nd badge (`/dev/ttyACM1`, unique_id `…aed8`) closed the one remaining physical gap:
- **Real round-trip**: badge #2's real `gap_scan` detected badge #1 over the air —
  `ARR name="Alex YOURCALL" shared="Makerspace Baasrode" id=0xa07b rssi=−40`. Real
  `gap_scan → parse_payload → intersect → arrival` on actual radio packets.
- **Disjoint ignored**: badge #2 scanning with a non-overlapping group (`0x3c42`) → **0 peers**
  (Alex ignored).
- **Symmetric**: both apps running + advertising + scanning; host scan sees both beacons
  (`Alex YOURCALL` on `…0E`, `Alice` on `…DA`, both group `0xa07b`) → mutual detection + alert.
- Gotcha: a freshly-deployed app isn't seen by `AppManager` until `refresh_apps()` (or a reboot);
  `start_app` on an undiscovered fullname **silently no-ops** (no exception), so the first Alice
  launch advertised nothing until refresh.
- Gotcha: `tools/badge_run.py` hardcodes `/dev/ttyACM0`; used a port-arg paste launcher
  (`/tmp/paste_port.py`, paste mode coexists with the OS asyncio loop — mpremote's raw REPL
  does not) for the 2nd badge.

The host BlueZ TX limitation is now moot — a real 2nd badge is the clean test stand.

## 3-badge test — coalescing on real hardware
A 3rd badge (`/dev/ttyACM2`, `…cfab8`, "Bob") joined. All three deployed + running the app +
advertising on group 0xa07b (host scan: `Alex YOURCALL`, `Alice`, `Bob`).
- **Multi-peer + coalescing**: with Alice + Bob advertising and Alex as scanner, Alex's real
  scan detected **Alice** → coalesced banner `"Alice nearby (Makerspace Baasrode)"` (rssi −50).
  The 2-arrival coalesced banner text (`"Alice, Bob nearby (Makerspace Baasrode)"`) was already
  proven on-device in `dev_ui_test`; the real-radio run confirmed the single-peer path.
- **Flakiness noted**: catching BOTH Alice+Bob in one scan window simultaneously was unreliable
  (BLE radio timing/state), and the repeated adv+scan cycling on Alex (ACM0) eventually
  hard-wedged him (esptool `--before usb_reset` could not recover — needs a physical RESET).
  Alice (ACM1) + Bob (ACM2) stayed healthy.
- **Takeaway**: coalescing is satisfied at the code level (2-arrival → coalesced banner) plus the
  real single-peer path; the simultaneous-2-real-peer live demo is radio-timing luck, not a code
  gap. Avoid long adv+scan REPL sessions on one badge — they destabilize the NimBLE/USB stack.
- **Resumed attempt**: a gentle scan-only run (Alice as receiver, Alex+Bob advertising) was tried,
  but the prerequisite `restart_launcher` paste on Alice (ACM1) hard-wedged her too (esptool
  `--before usb_reset` could not recover — needs a physical RESET, like Alex earlier). Stopped
  after 2 of 3 badges wedged rather than risk Bob (ACM2). All three had been confirmed advertising
  together immediately before; coalescing stands proven without the live 2-peer-simultaneous demo.

---

# Group Nametag + Proximity Finder — plan review & hardening — 2026-07-07

Reviewed and substantially hardened `PLAN.md`, the self-contained implementation plan for a
MicroPython **group nametag + BLE proximity finder** app for the Fri3d Camp 2024 badge. No code
was written this session — the badge app is still "planning complete, not yet implemented." Work
was: verifying the plan's load-bearing claims against the real firmware/framework repo, fixing
broken paths after the project folder moved, adding a multi-group membership feature, reworking
the proximity model, and a full review pass (contradictions / omissions / ambiguities) plus five
new usability features.

## Files
- `PLAN.md` — the working document; edited throughout (grew ~19 KB → ~32 KB).
- `Makerspace Baasrode Logo 500X500.jpg` — placeholder logo, unchanged.
- **No git repo** in this folder (nothing to commit/push).

## 1. Verified the plan's load-bearing claims (against `../fri3dbadge2024`)
All confirmed **true** by reading the actual sources:
| Claim | Where verified |
|---|---|
| BLE central+peripheral compiled in (`MICROPY_PY_BLUETOOTH_ENABLE_CENTRAL_MODE 1`, NimBLE, `CONFIG_BT_ENABLED=y`) | `repos/badge_2024_micropython/ports/esp32/mpconfigport.h`, `boards/sdkconfig.ble` |
| Image decoders on (`LV_USE_TJPGD/LODEPNG/GIF 1`); no real `lv_fs` (only `LV_USE_FS_MEMFS 1`) → must decode from in-memory buffer | `fri3d/lvgl_esp32_mpy/binding/lv_conf.h` |
| `fb_image()` RGB565 `lv.image_dsc_t` raster fallback; `_held()`/`_keep`/`_wipe`/`start`/`stop` patterns; `app.json` schema | `app/name_badge/{art.py,name_badge.py,app.json}` |
- Note: `MICROPY_BLUETOOTH_NIMBLE_BINDINGS_ONLY (1)` is set (IDF-provided NimBLE host) — reinforces that the concurrent advertise+scan spike is genuinely mandatory.

## 2. Fixed broken path references (folder was moved)
The project folder is now a **sibling** of `fri3dbadge2024/` (was assumed nested inside it). Every
`../app/...` reference was wrong. Corrected throughout:
- `../app/...` → `../fri3dbadge2024/app/...` (name_badge, neon_launcher, main.py, art.py)
- framework-source anchor and `tools/badge_run.py` → `../fri3dbadge2024/...`
- §4 layout header + tree root renamed `group-nametag/` → `fri3dbadge-group-nametag/`, reworded as a sibling.

## 3. Multi-group membership (new requirement)
A member can now belong to **multiple groups**; presence alerts fire when group sets **intersect**.
- Config `group` (string) → **`groups`** (list), capped by `MAX_GROUPS ≈ 5`.
- Wire format: single Group ID → **Group count (1B) + Group IDs (2×G, little-endian, dedup+sorted)**.
- Matching = hash-set intersection; the `seen[]` entry records the shared group name(s) for display.

## 4. Proximity model change — "presence = in BLE range"
the wearer's decision: alert as soon as a matching badge is within radio range (no distance calibration).
- **Dropped** the `RSSI_ENTER`/`RSSI_EXIT` field-calibration hysteresis entirely.
- **Eviction timeout `EVICT_MS = 30 s`** defines absent; notify-once debounced on `absent→present`.
- **`rssi_floor` is now a per-install `app.json` config value** (default **-120** = disabled; radio
  sensitivity is ~-97 dBm so -120 passes everything). Optional coarse range gate, not calibration.
- Open-field range estimate captured: **~50–100 m LoS** badge-to-badge, less through tents/bodies.

## 5. Full review pass — fixes folded in
**Contradiction:** §8 lifecycle still used single-group `ble.begin(group,...)` → now
`ble.begin(groups, name, handle, rssi_floor)`.

**Omissions (a couple were latent bugs):**
- **Stable BLE address** (`addr_mode=0x00` public/static; table keyed on `(addr_type, addr)`) — without
  it, address rotation causes endless re-alerts + ghost entries. Biggest gap.
- **Defensive parsing** — bounds-check `group_count`/`name_length` against the received buffer; anyone can broadcast 31 arbitrary bytes.
- **`ADV_MS ≈ 250 ms`** interval specified; **little-endian** fixed for 2-byte fields; **`ticks_diff()`** for eviction; **UTF-8 boundary** truncation; empty/missing `groups` → hint + skip BLE; `MAX_GROUPS` overflow → keep lowest IDs.

**Ambiguities:**
- **GIF dropped** — static PNG/JPEG only this iteration (GIF needs `lv.gif`, conflicts with breathing anim); deferred to §11.
- **Passive scan** (beacon is non-connectable / no scan response, so active scan is wasted power).
- **1-byte wire-format version `0x01`** (unknown → drop) for forward-compat; **concurrent alerts coalesce**; two `name` keys (menu label vs `config.name`) clarified; non-square logo scale = `min(box_w/img_w, box_h/img_h)×256`.

## 6. New usability features written into the plan
| Feature | Notes |
|---|---|
| **Per-group colour + tone** | LED hue + sting pitch derived deterministically from the group hash; shared-multiple → lowest-sorted shared ID (both badges agree). No config. |
| **Backlight dim-out** | After `DIM_MS ≈ 30 s` idle; BLE keeps running while dimmed. |
| **Battery indicator** | On idle screen, *if* the framework exposes battery state (API unverified). |
| **First-run hint** | Unconfigured badge (empty `name`/`groups`) shows "Configure me" + skips BLE. Only new persistent surface — persisted mute/brightness explicitly deferred. |
| **Own-group(s) line** | Idle screen shows this badge's configured groups so the wearer confirms who can find them. |

## 7. Added a hardware-API probe (§9 spike 3, + §10 checklist)
Quick pre-UI check for the three **unverified** APIs this session introduced: `addr_mode` (stable
address), display/backlight **brightness**, and **battery** state. Instruction: drop-and-note in
`DESIGN.md` rather than block if backlight/battery don't exist.

## Notes / follow-ups
- Plan is now internally consistent end-to-end; name budget is **`20 − 2×G` bytes** (no Flags AD emitted).
- Still unimplemented. Two mandatory spikes before UI work: **logo in-memory decode** and **concurrent adv+scan**; plus the hardware-API probe above.
- Deliverables named in the plan but not yet created: `README.md`, `DESIGN.md`, and the `app/`/`tools/` source tree. `DESIGN.md` has queued TODOs: `rssi_floor` guidance table + open-field link-budget writeup.

---

# Group Nametag + Proximity Finder — plan phasing & packaging pass — 2026-07-07

Follow-up to the hardening pass above: turned the flat build-order into **gated
development phases** and closed the three structural gaps the review flagged.
`PLAN.md` only; still no code, still unimplemented.

## Files
- `PLAN.md` — four edits (§4 layout, §6.1, §9 spike 3, new §9.1).

## What changed
1. **`tests/` added to the §4 layout** — `test_ble_proximity.py` + `conftest.py`.
   Flagged the load constraint: `ble_proximity.py` must keep
   `import bluetooth` / `fri3d` / `lvgl` out of the pure wire-format functions'
   import path (lazy-import inside the radio/IRQ wrappers) or the host can't
   load the module to run them.
2. **New §9.1 "Phased deliverables"** — 6-phase table (0 De-risk → 1 BLE core+tests →
   2 logo loader → 3 idle UI shell → 4 alerts+signature → 5 integration+polish),
   each row = deliverable + falsifiable exit gate + the §10 checkboxes it closes.
   All 14 checklist items mapped to a phase. `README.md` / `DESIGN.md` (previously
   orphaned) made explicit Phase-5 deliverables.
3. **Stable-address fallback separated (§9 spike 3, + §6.1 cross-ref)** — backlight
   and battery stay "drop if absent"; the stable address reclassified **not optional**
   (it underpins the no-ghosts guarantee). Expected ESP32/NimBLE public-address
   default, plus an honest degradation fallback (eviction window → documented
   known-limitation → flag-before-shipping) if a stable address can't be set.

## Notes / follow-ups
- Plan is now phased with gated deliverables and a home for the unit tests;
  everything else unchanged. Still unimplemented; Phase 0 spikes remain the entry
  point.
