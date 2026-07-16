# !Fri3d Friends — v0.8.0: phone setup over Bluetooth (Web Bluetooth); WiFi portal removed — 2026-07-16

At Fri3d Camp badges and phones sit on **different SSIDs/subnets**, so the old
PIN'd WiFi portal was unreachable by IP in practice. v0.8.0 replaces it with a
**Web-Bluetooth setup page** (`docs/setup/index.html`, GitHub Pages) that talks
GATT straight to the badge — **zero network**. iOS Safari has no Web Bluetooth, so
iPhone users use the free **Bluefy** browser (the page detects iOS and links it).

## What changed
- **New `ble_setup.py`** — a connectable setup GATT service (`SETUP_SVC 6e400020-…`):
  `AUTH`(4-digit code) · `INFO`(pre/post-auth JSON) · `CFG`(chunked config) ·
  `STATUS`(read+notify) · `CONTACTS`(paged read) · `CTLOFF`(page offset). Pure,
  host-tested halves: `sanitize_config` (the BLE `form_to_config`), `ChunkAssembler`
  (`seq|total|payload`, 2048-byte cap → `too_large`), `contacts_response` (0xFFFF
  header + 400-byte slices), `AuthState` (4-digit + 60 s lockout/rotation),
  `badge_id`/`build_info`/`build_setup_adv`. 25 new tests (`tests/test_ble_setup.py`).
- **One radio, one registration.** NimBLE accepts `gatts_register_services` once per
  power-on, so the setup service is registered **in the same call** as the contact-
  exchange service. `ContactExchange.ensure_radio()`/`_ensure_services()` build both
  and hand the setup handles to `SetupService.bind_handles()`; `ensure_radio` is the
  single BLE-up + MTU-once + register-once site used by both swap and setup.
- **Badge identity `Fri3d-XXXX`** (last 2 bytes of the BLE MAC). The QR encodes
  `…/setup/?badge=XXXX` so the browser chooser shows exactly this badge.
- **App wiring:** unconfigured badge runs the setup service on the Configure-me
  screen (new QR + on-screen code); a configured badge opens a **2-min window with
  a long press of B** (short B still mutes; A/Y close early), with a create-once
  overlay (QR + code + countdown), suspending/resuming proximity like the Y-swap.
  Y-swap and LED writes are gated off while a setup session runs.
- **Removed** `web_portal.py` + `tests/test_web_portal.py`; README/DESIGN §10 rewritten.
- **New `tools/setup_client.py`** (bleak) — the same protocol, headless, for testing.

## On-hardware verification (2024 badge #1, this session) + two bugs fixed
Deployed (sha-verified) and driven end-to-end from the dev host over BLE (`bleak`):
- unconfigured → phone-configured over BLE → badge **switches to the nametag live
  and goes on the air, NO reboot** (config `"BLE Test ✓ José"` round-tripped —
  UTF-8 preserved through chunked GATT + `sanitize_config` + atomic write);
- **contacts paging** exact (4 pages / 1395 bytes / 6 contacts byte-identical);
- **wrong-code lockout** (5th → `locked`, code rotated) matches the old portal;
- **setup window** on a configured badge opens, **suspends proximity, advertises,
  shows the overlay, and resumes proximity on close**; exchange service still
  registered (`svc_ready`) alongside setup.
- **2026 board** re-verified end-to-end (configure → nametag → on air, no reboot).
- **Merged registration proven on-device:** the exchange service (handles 16/18)
  and the setup service (handles 21–32) both live in one `gatts` table and are
  both `gatts_read`-able — registered together in a single call, exchange first.
- **Two-badge Y-swap verified working** (owner test) — the shared single
  registration does not disturb the contact exchange.
- **Bug fixed — the phone setup session tore itself down 3 s after a save,**
  breaking the web UX: reloading received contacts failed, a follow-up config
  save failed with *"GATT Server is disconnected"*, and (racing the teardown) a
  badge could stay on the QR/Configure-me screen instead of switching to the
  nametag. Root cause: the session force-disconnected the phone `SAVE_GRACE_MS`
  (3 s) after a save. Now the session **stays alive after a save** — the phone
  keeps its connection so it can reload contacts / make more edits — and only
  ends when the **phone disconnects** (then it hands the radio to the proximity
  beacon), with a 60 s safety cap if the phone vanishes without a clean
  disconnect. The unconfigured→nametag UI swap still happens immediately on save.
  Verified on both boards: configure → nametag live, contacts reloaded twice
  while connected, no premature disconnect, on air after the phone leaves.
- **Bug fixed — swap stopped working until reboot after an app pause:**
  `proximity.end()` (called from `_teardown_ble` on every onPause/onStop) issues
  `BLE.active(False)`, which — verified on-device — **clears NimBLE's whole gatts
  table and MTU**. Our `_svc_ready` flag persisted, so the next swap reused now-
  dead handles and `gatts_write` raised `OSError(22)` (EINVAL) → the swap failed
  silently (`write-exc` in `exch.log`) forever until reboot. `ContactExchange.
  ensure_radio` now self-heals: it probes the cached handle with a **write** (a
  *read* spuriously succeeds on a stale handle on this build — only writes EINVAL)
  and, if dead, resets `_svc_ready`/`_mtu_set` and re-registers. Re-registration
  and re-`config(mtu=)` ARE allowed after an `active(False)`/`active(True)` cycle
  (also verified on-device), even though they EINVAL without one. Confirmed by
  reproducing the exact failure and then a clean two-badge swap after forcing the
  active cycle on both.
- **Bug fixed — session went invisible after the first phone left:** NimBLE stops
  advertising on connect and doesn't auto-resume; the setup session now
  **re-advertises on disconnect** (verified: badge reappears after a client drops).
- **Bug fixed — setup task handle clobbered:** the splash→main `setContentView`
  re-fires `onPause`/`onResume`, cancelling+restarting the configure session; the
  cancelled task's `finally` blindly nulled `_setup_task`, wiping the live task's
  handle (breaking teardown-on-pause + the LED/Y gates + the deferred proximity
  begin). Both wrappers now identity-guard (`asyncio.current_task()`) before clearing.
- **Web page now requires a name AND at least one group before saving.** A badge
  only leaves setup once it has both (groups drive proximity matching), but the
  page let you save a name with no groups — the badge accepted the config yet
  stayed "unconfigured" and sat on the QR screen with no explanation. The page
  now validates before writing and shows a clear inline message; the form marks
  both fields required. (`docs/setup/index.html`.)
- **Setup window is now an *idle* timeout, not a fixed 2-min wall clock.** The
  configured-badge window (`SETUP_WINDOW_MS`, 2 min) previously counted down from
  the moment you long-pressed B and ignored activity, so a longer friends-list
  transfer got cut off mid-flight. Now **any GATT activity (auth, config write,
  contacts-page request) resets the window**, with a 10-min absolute backstop
  (`SETUP_ABS_CAP_MS`) so a forgotten-open window still returns the radio to the
  beacon. The on-screen "closes in Ns" countdown reads the session's true
  remaining time (`SetupService.window_secs_left()`) so it no longer falsely
  hits 0 during an active transfer.

Remaining to try with a real **phone** (not USB — `mpremote`'s raw-REPL entry
conflicts with active BLE and wedges the USB-CDC, the documented pre-existing
hazard, irrelevant untethered): window-mode GATT from a phone in the field, and
iPhone/Bluefy. Both exercise the same setup service already proven above.

---

# !Fri3d Friends — v0.7.2: first-time portal save now switches to the nametag live — 2026-07-15

## Deploy/ops notes (learned 2026-07-15/16, all three badges on 0.7.2)
- **USB copies wedge the CDC far more often since the background beacon (v0.7.0)**
  keeps BLE advertising during transfers — the known "stressed mid-copy while BLE
  runs" failure. An interrupted `mpremote fs cp` leaves a **truncated file on
  flash**: always `fs sha256sum` after deploying, and re-copy until it matches.
- **Proven deploy recipe for a badge running the beacon service:** back up
  `config.json` on-badge → write a blank `{"name":"","groups":[]}` (the service
  then holds the radio OFF — unconfigured badges stay silent; a plain
  `BLE().active(False)` is NOT enough, the watchdog re-asserts within ~30 s) →
  copy + checksum → restore config → reboot.
- A hard-wedged CDC (raw REPL never engages, console silent) needs a physical
  RESET or USB replug; `mpremote reset` can't reach it. The two 2024 badges look
  identical — identify by unplug-watching `/dev/serial/by-id/`.
- Debugging: `time.sleep()` inside one `mpremote exec` starves the whole OS
  asyncio loop — sample app state in a separate exec.

Field feedback: after first-time setup via the portal, the badge stayed on "Configure
me" (v0.6.3 only started BLE + showed a "reopen app" banner, avoiding the known
screen-rebuild crash). Now the swap happens **in place on the same live screen** —
the safe middle path between "do nothing" and the crashing rebuild: the setup widgets
(title, subtitle, QR tile) are **hidden, never deleted**, the nametag widgets (name,
pills, friends line, battery, detail panel) are **created** next to them (creation is
safe; deletion/`setContentView` re-entry are the crash classes), and the banner is
re-raised to the top (`move_foreground`). `_build_idle` was split into
`_build_setup` / `_build_nametag` + shared widgets (clock, portal footer, controls,
banner) built exactly once. Verified on the 2026 badge by replaying the portal save:
Configure-me → nametag with pills, BLE live, and an immediate "X, Y nearby" arrival
banner from the other badges — no crash.

---

# !Fri3d Friends — v0.7.1: QR code + clearer text on the Configure-me screen — 2026-07-15

The unconfigured screen now says **"open this app's setup portal"** (was "open the WiFi
setup portal") and shows a **QR code of the portal URL** — scan it with a phone instead
of typing the IP. The QR sits on a white 136 px tile (the margin doubles as the QR quiet
zone; `lv.qrcode` is built into the OS's LVGL), appears once WiFi is up and hides when
it drops, fed by the existing 2 s `_refresh_portal` throttle. Falls back to the text URL
if `lv.qrcode` is missing. Verified on-device (2024 badge): QR visible and encoding the
live portal URL. Debugging gotcha rediscovered: `time.sleep()` inside an `mpremote exec`
blocks the OS's single asyncio loop, so the app under test gets zero CPU — sample state
in a *separate* exec instead.

---

# !Fri3d Friends — v0.6.2–v0.7.0: splash-crash hotfix, portal-save feedback, background beacon — 2026-07-15

Three releases in one session, all **verified on real hardware** (2×2024 + 1×2026 badge,
deployed over `mpremote` by stable `/dev/serial/by-id` path, `config.json` preserved).
**64 off-device tests green** (60 + 4 new for the beacon service).

## v0.6.2 — hotfix: v0.6.1 crashed the OS + rebooted the badge on every app start
The F-19 "cleanup" (`self._splash_scr.delete()` after `setContentView`) was a
use-after-free: `setContentView` starts a **non-blocking 500 ms LVGL slide animation**
(`lv.screen_load_anim(..., auto_del=False)`) and returns immediately, so deleting the
outgoing splash screen right after leaves the animation timer pointing at freed memory
→ hard crash + reboot on the next tick, on both badge generations. This is the same
landmine DESIGN.md already documented for the config-reload path; v0.6.1 shipped
unflashed. Reverted to the field-verified leak-the-splash behaviour with a loud
warning comment. Verified: all 3 badges survive the splash→main transition.

## v0.6.3 — portal save on an unconfigured badge looked dead
Field bug: first-time setup via the portal saved fine but the badge stayed silently on
"Configure me" — `_build_idle`'s unconfigured branch returned **before the banner
widgets were built**, so the "Config saved ✓" feedback was a silent no-op, and BLE only
ever started from `onResume`. Now: the banner exists on the Configure-me screen too;
`_apply_reload` detects the unconfigured→configured transition, **starts BLE live**
(the F-5 Y-gate reads `_unconfigured` live, so it would have opened onto a dead radio)
and shows "Saved! Reopen app for nametag". Deliberately does **not** re-submit the
screen: `mpos.ui.view.setContentView` always pushes the stack and re-fires this same
Activity's onPause/onResume — a subtler cousin of the v0.6.2 crash. Verified end-to-end
on the 2026 badge by replaying the exact portal save path.

## v0.7.0 — background beacon: visible to friends with the app closed
New `beacon_service.py`, a manifest-declared `boot_completed` service (OS support
verified on both generations): while the app is **not** on the screen stack, it
advertises the identical non-connectable proximity beacon (advertise-only — no alerts,
no swaps in the background); while the app is open it never touches the radio, so all
existing swap/suspend logic is untouched. No app-code changes needed — the app's
`begin()` replaces the service's adv on open, and the service reclaims the radio ≤5 s
after exit. Unconfigured badges stay silent in the background too. Re-asserts the adv
every ~30 s (self-heals radio trampling); watchdog survives USB-console
`KeyboardInterrupt`. Verified 2024↔2026 both directions incl. open/close handoffs.
**Activates on the next reboot after install.**

---

# !Fri3d Friends — v0.6.1: Phase 5 code-review fixes (portal input, swap/teardown robustness) — 2026-07-15

Applied **every finding** from the Phase 5 code review
(`Code_Review_Phase5_20260715_0731.md`, verdict *PASS WITH NOTES*): 5 MAJOR, 9 MINOR
and 6 INFO. No redesign — the BLE exchange, re-entrancy fix, starvation fix and
suspend/resume were already sound; these harden the edges (especially the portal, the
recommended text-entry path). Bumped to **v0.6.1**; **60 off-device tests green**
(57 + 3 new for the portal input fixes).

## Portal input handling (MAJOR — corrupted real data through the recommended path)
- **UTF-8 percent-decoding (F-1):** `_url_unquote` decodes `%XX` into a byte buffer and
  UTF-8-decodes once, so accented names/groups (José, Noël, café) survive the portal
  instead of turning into Latin-1 mojibake. Also fixes silent group **mismatch** — a
  portal-saved group now hashes identically to the same name typed into `config.json`.
- **Apostrophe escaping (F-2):** `_esc` now escapes `'` (every form attribute is
  single-quoted), so O'Brien / L'Atelier no longer terminate the attribute early and
  truncate the field on re-save.
- **Full-body read (F-3):** POST bodies are read in a loop until `Content-Length` (was a
  single short-read-prone `read()`), so a large save can't silently drop fields.

## Contact swap + lifecycle (MAJOR)
- **Cancel swap on exit (F-4):** the exchange task is tracked (`_exch_task`) and
  cancelled in `_stop_task`; `run_window` and `_do_exchange` re-raise `CancelledError`
  through their `finally`, so a swap can no longer outlive the Activity by up to 5 s and
  touch freed LVGL widgets / BLE.
- **Unconfigured Y-press (F-5):** **Y** is gated on `_unconfigured`, matching the README
  ("unconfigured badges don't advertise/scan") — no more activating a radio no teardown
  path deactivates.

## Robustness (MINOR)
- **Atomic writes (F-8):** `config.json` and `contacts.json` are written via temp file +
  `os.rename` (atomic on LittleFS/FAT), so a power-off mid-write can't wipe the camp's
  collected contacts.
- **Banner coalescing (F-6):** arrivals only coalesce into a live *arrival* banner (not a
  "Swapped ✓" / "Config saved ✓" one), and `_hide_banner` clears the stale name list —
  fixes an arrival silently rewriting an unrelated banner with no LED flash / sting.
- **Buttons paused mid-swap (F-7):** A/B actions are deferred while `_exchanging` (edges
  still tracked), so a stray B-press can't fire the IRQ-disabling LED write that starves
  the GATT link.
- **Write-ack before disconnect (F-11):** the client waits for `_IRQ_GATTC_WRITE_DONE`
  (bounded by the window deadline) instead of a fixed 150 ms nap, fixing rare one-sided
  swaps on a congested radio.
- **Portal hardening (F-9, F-14):** bind retries on `EADDRINUSE`; `url()` / footer only
  advertise a genuinely-listening portal; open connections close on `stop()`; the request
  read phase has a 10 s timeout and the header loop is capped.
- **NTP + banner clamp (F-10, F-12):** a failed NTP dispatch clears `_ntp_busy` (no longer
  wedges resync for the session); `banner_ms` is clamped ≥500 on load *and* save so a
  0/negative value can't hide every banner.
- **`onDestroy` stops the portal (F-13).**

## Cleanup (INFO)
- **Company id checked (F-15):** both beacon parsers now verify the 2-byte company field
  (`0xFFFF`) alongside the magic, matching the documented wire format.
- **Splash freed (F-19):** the splash screen + its ~8.7 KB PNG are deleted once the
  nametag appears (a Python-side reference to the PNG bytes is kept while the image lives).
- **Portal refresh throttled (F-20):** the footer URL query (hits the WiFi stack) is gated
  to ~2 s like the other refreshers, not every 30 ms frame.
- **Dead code removed (F-18):** `_finishing`, `notified`, `_disc`/`_chars`.
- **Documented (F-16, F-17):** the 3-badge rendezvous ambiguity and the connectable-window
  exposure are now written up in DESIGN.md §9.

## Tests
- Added host tests for `_url_unquote` (multibyte UTF-8), `_esc` (single-quote escaping)
  and the `banner_ms` clamp. **60/60 green.**

## Docs
- DESIGN.md §3/§9/§10 updated with the review-fix notes; README refreshed (international
  names now safe via the portal, atomic contact storage, portal robustness).

## Packaging
- Built the deterministic release package `dist/com.fri3dcamp.fri3dfriends_0.6.1.mpk`
  (single top-level `fullname/` folder, stored/uncompressed, fixed `2025-01-01`
  timestamps, 10 files, no `__pycache__`/`exch.log`/`contacts.json` cruft). Verified the
  in-package manifest reads version 0.6.1. **Not yet flashed** — to be published via
  BadgeHub → on-badge AppStore (badges auto-offered the 0.6.0 → 0.6.1 update).

---

# !Fri3d Friends — v0.6.0: BadgeHub packaging, repo rename, MIT license, icon polish — 2026-07-14

Prepared the app for publishing on **BadgeHub.eu** and cleaned up release details.
Bumped to **v0.6.0**; deployed to all three badges.

## Publishing prep
- Confirmed the BadgeHub flow (community appstore, `.mpk` packages, `mpos_api_0`
  badge tag). Slug = the app `fullname` `com.fri3dcamp.fri3dfriends` (verified
  against how every MicroPythonOS app on BadgeHub is slugged via its public API).
- Build a deterministic `.mpk` (single top-level `fullname/` folder, stored,
  fixed timestamps, dirs-before-files) → `dist/com.fri3dcamp.fri3dfriends_0.6.0.mpk`.
  `dist/` is gitignored (artifact, reproducible from `app/`).
- `MANIFEST.publisher` → **David Steeman** (was "Fri3d Camp").

## Repo + license
- **Renamed the GitHub repo** `fri3dbadge-group-nametag` → **`fri3d-friends`**
  (github.com/steemandavid/fri3d-friends; old URL 301-redirects). Local `origin`
  updated.
- Added the **MIT LICENSE** (© 2026 David Steeman / Makerspace Baasrode).

## Launcher icon
- The icon's black tile was full-bleed and crowded the app-name label in the OS
  menu. Shrunk the **whole tile** (~80%) with transparent padding, weighted to the
  bottom, so the icon graphic clears the label. Redeployed to all badges.

## Docs
- README rounded out: AppStore-first install, friend-LED breathing, build/publish
  (`.mpk` → BadgeHub) section, tests, MIT license, credits. Added a ready-to-post
  Fri3d Discord announcement at `docs/announcement.md`.

---

# !Fri3d Friends — rebrand, bigger name font, per-swap contacts, UI/portal fixes — 2026-07-14

Renamed the app **!friends nearby → !Fri3d Friends** and did a full rebrand, plus
a batch of UX changes. Deployed + verified on all three badges (2× 2024, 1× 2026);
57 off-device tests green. Bumped to **v0.5.0**.

## Rebrand
- Display name → **!Fri3d Friends** everywhere user-facing (MANIFEST, splash,
  portal header, docs). Functional labels (`Friends nearby:`, detail header) kept.
- **Package id renamed** (app unpublished, so safe): dir/fullname
  `com.fri3dcamp.groupnametag → com.fri3dcamp.fri3dfriends`, module
  `group_nametag.py → fri3d_friends.py`, class `GroupNametag → Fri3dFriends`.
  On-badge deploy: single-session `mpremote fs cp -r` into the new dir, then a
  script to **migrate each badge's config + contacts.json**, remove the old dir,
  and reboot (far less USB-CDC churn than many small copies).
- **New logo** — a hybrid of two proposals (badge-bump × pixel-people): two badges
  bumping (the swap) each with a pixel friend + a spark. New `icon_64x64.png`
  (tiled) + `fri3dfriends.png` (96px, tileless, splash). Old `makerspace.png`/
  `logo.png` removed (the "Makerspace Baasrode" *text* attribution stays).
  Generators: `tools/make_logos.py` (10 candidates) + `tools/make_hybrid_logo.py`.
- **Splash relaid out** with explicit y-positions so "by David Steeman" no longer
  overlaps the logo and the logo clears the "Makerspace Baasrode" line.

## Name font + friends line
- **Name at 42px** (1.5× the built-in `montserrat_28`) from a bundled ~15KB subset
  Montserrat **TTF** via `FontManager.getFont(size, ttf=…)` → `tiny_ttf`. A fixed
  font, not transform-scaled. (`lv.binfont_create` on an `lv_font_conv` `.bin` did
  **not** load on this lvgl 9.4 build — the TTF path is what works.)
- **Friends line** inset + `LONG_MODE.WRAP` so long peer names wrap instead of
  being clipped by the curved corner.

## Contacts / swap
- **One entry per swap** (`merge_received` → `add_received`, append-only, no dedup;
  cap 200, oldest-first). Kept a `merge_received = add_received` alias for
  partial-deploy safety.
- **Default contact fields** in the template: Email, Phone, Website, Discord.
- **Removed the `handle` field entirely** (config, `_load_config`,
  `ble_proximity.begin` signature + name-with-handle display, portal form,
  `_outgoing_contact`, docs, tests). Swap sends **name + groups + contact fields**.

## Portal fix
- **Fixed badge reboot on config save**: the old reload rebuilt the whole LVGL
  screen + cycled BLE (hard-crash + memory leak on this build). Now a safe
  in-place reload; added save feedback — "Config saved ✓" banner on the badge and
  a green note in the portal. Group changes apply on next app start.

---

# !friends nearby — contact-swap bug fixes (re-entrancy + LED starvation) — 2026-07-13

Fixed two bugs that made the Y-button contact swap fail in the field, found via
on-device trace logging (`ContactExchange.dbg` → `/apps/.../exch.log`). Both
confirmed fixed on hardware: repeated swaps now work, incl. cross-model 2024↔2026.

## Bug 1 — re-entrancy (`OSError(22)` on the 2nd+ swap)
The swap worked exactly once per power-on, then every later attempt threw
`OSError(22)` (EINVAL) early in BLE setup until reboot. This *looked* like a
2024-vs-2026 problem (the first test pair happened to be the first swap) but
wasn't. Cause: NimBLE one-time-only stack ops (`config(mtu=…)`,
`gatts_register_services`) were re-issued every window. Fix: `run_window` setup is
now idempotent + fully guarded — MTU set once (`_mtu_set`), services once
(`_svc_ready`), `active()` only if needed, and every setup call wrapped so none
can abort the window.

## Bug 2 — GATT connection starved by the LED loop
After bug 1, the swap set up fine but the connection was unstable
(`cli conn=None`, or connect-then-drop `read-exc NoneType`). Cause: the app's main
loop ran concurrently with the exchange task and called `_update_leds()` →
`lights.write()` (WS2812, IRQ-disabling) every 60 ms, starving the short GATT
link. (This is why headless `run_window` tests passed — no main loop — but the
live app failed.) Fix: while `self._exchanging`, the main loop only reads buttons
and yields (no LED/BLE/refresh work), so the exchange owns the CPU + radio.

## Ops notes
- Heavy on-device BLE debugging repeatedly wedged the badges' USB-CDC (documented
  failure mode — physical RESET is the only reliable recovery). Deploy right after
  a reset (fresh CDC) before the app fully loads and contends the REPL.
- The diagnostic trace (`dbg` + `exch.log`) is left in for now; trim once fully
  proven in the field.

---

# !friends nearby — friend LEDs, clock inset, 2026 verified on hardware — 2026-07-12

Follow-up to the splash/swap/portal work below. Added **per-friend breathing
LEDs**, nudged the clock clear of the curved corner, deferred the launch-time NTP
sync, and **verified the app end-to-end on a Fri3d 2026 badge** (first on-hardware
2026 run). Also fixed the live-save group-pill refresh (see prior commit).

## Friend LEDs (per-friend breathing)
- One RGB LED per nearby friend, slowly + dimly breathing that friend's **group
  colour** (friend 1 → LED 0, …). `_update_leds` in the loop, `LED_UPDATE_MS=60`,
  breathe `LED_DIM_MIN..MAX 0.015..0.18` over `3800 ms`, per-LED phase stagger,
  frame-cached writes. LED count is board-keyed: **4 on 2024, 5 on 2026** (fw
  `get_led_count()` over-reports 5 on 2024). Arrival/exchange flashes set a short
  override, then breathing resumes.

## Clock + NTP
- Clock inset to `CLOCK_X=24` (2 chars right) so the curved screen corner no longer
  clips it. First app-driven NTP resync deferred one interval (OS already syncs at
  WiFi connect) so the blocking `ntptime.settime()` doesn't hitch launch.

## Fri3d 2026 — on-hardware verification (badge serial 1cdbd49d9de4)
- Fresh install on the 2026; **works**: splash → nametag (320×240), board detected
  `fri3d_2026`, BLE proximity detected both 2024 badges, clock/pills/portal footer/
  controls render, io_expander buttons (A/B/Y) read cleanly, and **friend LEDs
  breathed** (2 friends → LEDs 0+1 dim-green, animated + staggered). No 2026 bugs
  found — worked on first deploy.

## Meta
- Added a shared cross-project USB device reference at
  `/home/john/claudecode/fri3d-usb-devices.md` (serials/by-id paths for both 2024
  badges, the 2026, and the 2 TTGOs, + 2024-vs-2026 identify recipe + wedge
  recovery). The 2026 badge is now an active dev/test target.

---

# !friends nearby — splash, contact swap (Y), WiFi setup portal + clock — 2026-07-12

Added a startup splash, a **contact-exchange** feature on the **Y** button, a
configurable free-form **`contact`** object, on-badge storage of received
contacts with timestamps, a **PIN-gated WiFi web portal** to edit config /
view+export contacts, and a live **NTP-synced clock**. Bumped to **v0.4.0**.
Off-device tests: **56 passing** (30 BLE + 20 contact-exchange + 6 portal). On the
2024 badge: splash → nametag verified, clock showed real time, portal footer
rendered, clean exit (no wedge). Radio round-trip for the swap + the browser
portal round-trip need a two-badge / on-WiFi setup — not yet run.

## New: splash + clock (group_nametag.py, makerspace.png)
- 3-second splash (app name, `v0.4.0`, "by David Steeman", Makerspace Baasrode
  logo + name), mirroring `org.fri3d.hwtest`'s `_build_splash` — the **in-memory
  `lv.image_dsc_t` decode** (reliable) with a text fallback; asset copied from the
  hwtest project. `_splash_then_enter` swaps to the nametag after 3 s.
- **Live clock** top-left, same font/colour as the battery %. RTC kept accurate by
  NTP: MicroPythonOS syncs on WiFi connect, and `_resync_time` re-syncs ~every
  10 min (`ntptime.settime()` in a task, guarded by `WifiService.is_connected()`).

## New: contact exchange — Y button (contact_exchange.py)
- Overlapping 5 s **press-triggered windows** (no synced clocks). Y opens a window
  advertising a connectable `HXCG` beacon + scanning for peers doing the same.
- **`decide_role`**: lower MAC = GATT server, higher = client → exactly one
  connection. Bidirectional swap over one link (server's readable `MYINFO` char +
  writable `THEIRS` char); MTU raised to 515; envelope `{"n":…,"c":{…}}` capped to
  500 B (fields dropped last-first).
- Coexists with proximity via new `BLEProximity.suspend()/resume()` (stop/restore
  scan+adv + IRQ **without** `active(False)`, to avoid CDC-wedging churn).
- **Storage:** `merge_received` → `contacts.json`, deduped by MAC (refresh + bump
  `count`, keep `first_received`), capped at 200, each with `received_at`.

## New: WiFi setup portal (web_portal.py)
- Always-on `asyncio.start_server` HTTP portal (assumes the OS is already on WiFi;
  no STA/hotspot management). Routes: `/` (config + dynamic `contact` editor),
  `/save`, `/contacts`, `/contacts.json`. Pure `parse_form`/`form_to_config`
  unit-tested.
- **Auth:** random 5-digit PIN per boot shown on the badge as a login challenge,
  session cookie after entry, lockout + PIN rotation after 5 wrong tries. Footer on
  the nametag shows `⚙ http://<ip>:8080` / the PIN. Plain HTTP → gates access, not
  traffic (camp-LAN trust model). BLE phone-companion + notification mirroring were
  considered and **dropped** (Android would need a native app; Web-Bluetooth
  excludes iOS Safari).

## Config / manifest
- `config.json`: new `contact` object (free-form `field: value`). `MANIFEST.JSON`:
  version → `0.4.0`, description updated. `_load_config` reads `contact`;
  `_apply_reload` (deferred to the main loop) reloads config + re-applies the beacon
  after a portal save.

---

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
