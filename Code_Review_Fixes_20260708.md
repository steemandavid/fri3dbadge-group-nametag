# Code Review Fixes — Group Nametag (Phases 0–4)

**Date:** 2026-07-08
**Source review:** `Code_Review_Phase0-4_20260708_0800.md`
**Files touched:** `app/com.fri3dcamp.groupnametag/ble_proximity.py`, `app/com.fri3dcamp.groupnametag/group_nametag.py`, `app/com.fri3dcamp.groupnametag/config.json`

This document records the review findings that were fixed, the root cause, and the
change applied. Findings are referenced by their review IDs (D-1 … D-12).

---

## MAJOR

### D-1 — Whole-loop `try/except` froze the UI + all buttons on the first per-frame exception
**File:** `group_nametag.py` `_loop()`
**Problem:** the `try:` wrapped the entire `while True`, with `except Exception: pass`
*outside* the loop. Any unhandled per-frame exception terminated the render/input
loop permanently and silently — animation, nearby refresh, arrival draining, and
crucially **button polling** (`X`/`A`/`START`) all stopped; only the OS back gesture
could leave.
**Fix:** moved the `try/except` *inside* the loop body so a transient fault skips a
single frame instead of killing the task. `asyncio.CancelledError` is still
re-raised (cooperative cancel), and `await asyncio.sleep_ms(TICK_MS)` runs outside
the guard so the loop always yields. Added an explicit `self._finishing` flag: when
`START`/`B` calls `finish()`, the loop breaks immediately instead of continuing to
operate widgets that teardown may have freed (addresses the post-`finish()`
use-after-free noted in review §5).

### D-2 — IRQ mutated `_seen` while the async loop iterated it (`dict changed size during iteration`)
**File:** `ble_proximity.py` `_handle_scan_result` / `tick` / `_evict` / `current_peers`
**Problem:** the BLE scan-result IRQ (scheduler context, interleaved with the asyncio
loop at bytecode boundaries) inserted new peers into `self._seen` while `_evict`
(`items()`) and `current_peers` (`values()`) iterated it. A peer arriving mid-iteration
raised `RuntimeError` on the reader side (which is not IRQ-guarded) — the most likely
trigger for D-1.
**Fix:** the IRQ now does **no parsing and no `_seen` mutation** — it only copies the
raw scan result `(addr_type, bytes(addr), bytes(adv_data), rssi)` into a `self._pending`
queue (bounded to 256 entries to cap memory if the loop ever stalls). All
`parse_payload` / intersect / `rssi_floor` / `_seen` / `_arrivals` work moved into
`_process_pending()` → `_process_result()`, called from `tick()` on the loop thread.
The queue hand-off (`pending = self._pending; self._pending = []`) is safe against a
concurrent IRQ `append` (rebinding an attribute is atomic; no lost items). With all
`_seen` mutation now on the loop thread, the eviction/read iterations can no longer
race. This also resolves **D-9** (heavyweight allocation inside the IRQ callback).

### D-3 — Logo was never scaled-to-fit; `_logo_base_scale` was dead; the 160×160 logo overlapped the name
**File:** `group_nametag.py` `_place_logo` / `_animate`
**Problem:** PLAN §7 requires `scale = min(box_w/img_w, box_h/img_h) × 256`. The code
never queried the image size and never set a base scale (`_logo_base_scale = 256` was
assigned once and never read); `_animate` breathed around a literal `256` (native
pixels). The bundled 160×160 logo at `TOP_MID, y=18` spanned y≈18–178 and overlapped
the name at y=130; a larger group logo would overflow the 296×240 screen.
**Fix:** `_place_logo` now reads the PNG dimensions from the file header
(`_read_png_size`, deterministic — no dependence on lvgl decode timing), computes a
base scale that fits the source into a `LOGO_BOX_W×LOGO_BOX_H` (180×104) target box,
stores it in `self._logo_base_scale`, sets the image pivot to the source centre, and
aligns it so the *scaled* image sits in the upper area clear of the name. `_animate`
now breathes around `self._logo_base_scale` instead of a literal. Falls back to the
old behaviour (base 256, `TOP_MID,18`) if the dimensions can't be read, and to the
runtime placeholder disc if `set_src` raises.
**Note:** exact on-device pixel centring should be sanity-checked on hardware; the
substantive defect (no fit computation → oversized/overlapping logo) is resolved and
`_logo_base_scale` is now live.

### D-4 — Alert coalescing only merged arrivals within one 30 ms frame, not across the banner window
**File:** `group_nametag.py` `_drain_arrivals` / `_fire_alert`
**Problem:** `take_arrivals()` was drained every `TICK_MS=30 ms` and any non-empty
batch immediately fired a full cue (banner + LED flash + buzzer sting). Arrivals a few
hundred ms apart each produced their own sting/flash — the banner stacking PLAN §8
says to avoid.
**Fix:** arrivals now accumulate across the active banner window. On the first arrival
(no banner showing) a single cue fires (`_fire_alert`) and the window opens. Additional
arrivals **while the banner is still up** are appended to `self._alert_names` and the
banner text is recomputed to include them (`_coalesced_text` over the full set) — but
**no** additional sting/LED fires. Once the banner auto-hides, the next arrival starts
a fresh window (one cue), preserving the notify-once-per-encounter feel.

---

## MINOR

### D-5 — `B` button did not return to the launcher (PLAN §8: "B / START")
**File:** `group_nametag.py` `_handle_buttons`
**Fix:** `B` now calls `self.finish()` (via the shared `_finishing` path), matching
`START` and the documented control. Help line updated to `A:detail  X:mute  B/START:exit`.

### D-6 — Shipped `config.json` used non-empty placeholders, defeating the first-run hint
**File:** `config.json`
**Problem:** `"name": "Your Name"`, `"handle": "YOURCALL"` made a freshly-flashed badge
count as *configured* (`_unconfigured = (not name) or (not ids)`), so it skipped the
"Configure me" hint and advertised the placeholder identity — the opposite of PLAN §8's
"advertising nothing is better than advertising an un-provisioned badge."
**Fix:** shipped `config.json` now has empty `name`/`groups`/`handle`, so an un-edited
badge shows the hint and stays BLE-silent until provisioned. (README documents the
fields to fill in.)

### D-7 — Wrap-unsafe raw `ticks_ms` arithmetic in two throttles
**Files:** `ble_proximity.py` `tick` (scan re-arm), `group_nametag.py` `_refresh_battery`
**Problem:** both used raw `>=`/`<` compares and `+` on `ticks_ms()` values, which wrap
(~12 days) — violating the PLAN §6.3 "always use `ticks_diff`" rule that every other
time-gate in the code follows.
**Fix:** scan re-arm now uses `ticks_diff(now_ms, self._next_rearm_ms) >= 0` and
`ticks_add(...)`; battery throttle uses `ticks_diff`/`ticks_add`. Both are now
wrap-safe and consistent with the rest of the code.

### D-8 — `begin()` reported success on advertising failure and could crash on a non-string name
**File:** `ble_proximity.py` `begin`
**Fix:** `name`/`handle` are coerced to `str` before `build_payload` (a non-string
`config.name` no longer raises out of `begin`). `gap_advertise` success is tracked and
`begin` now returns `True`/`False` truthfully so a caller can detect an invisible
(scanning-but-not-advertising) radio.

---

### D-12 — Placeholder now shown for a missing/empty/non-PNG logo (deterministic gate)
**File:** `group_nametag.py` `_place_logo` / `_logo_file_ok`
**Problem (was deferred):** lvgl decodes the logo lazily and, on a missing/corrupt
file, **logs internally and renders nothing rather than raising** — so the
`try/except` around `set_src` never fired and the placeholder never showed (blank
space instead of the "HS" disc). A render-based "did it decode?" probe is
unreliable because an off-screen image reports size 0 until laid out, and lvgl
does not expose `image_decoder_get_info` on this build (both **verified on-device**).
**Fix:** `_place_logo` now gates on the file itself — if `_read_png_size()` can't
parse a PNG header **and** `_logo_file_ok()` (exists + non-empty via `os.stat`)
fails, it goes straight to the drawn placeholder instead of attempting `set_src`.
This deterministically covers the realistic breakage (missing / empty / non-PNG /
truncated-header file). The narrow residual — a file with a valid PNG header but a
corrupt body — still decodes to blank without crashing; documented, not fixed
(would need a decode API lvgl doesn't expose here).

## Also addressed (lower priority)

- **`_fire_alert` `min()` over an empty sequence** (review §4, INFO): now guarded with a
  default so it can never raise if every arrival lacked a `shared_id`.
- **D-11 unreachable "+N more"** is now effectively exercised: with D-4 accumulation the
  single-group banner path accumulates multiple names, so the compact form is reachable.

## Not changed (intentionally)

- **D-10** (breathing centred slightly above native) — superseded by D-3, which now
  centres the breath on the computed fit scale.
- **Dead backlight/dim scaffolding** — left inert (correctly guarded); removal is
  cosmetic and deferred to avoid churn.

## Verification

**Host:** `pytest tests/` → **30/30 pass**; plus a host-level exercise of the
refactored radio wrapper (deferred IRQ→tick, arrival, notify-once, eviction +
re-alert-once, disjoint-ignored, rssi_floor gate, explicit-interval scan arming,
queue bound).

**On-device — 3 badges (Alex/ACM0, Alice/ACM1, Bob/ACM2), 2026-07-08, fixed code
deployed via `mpremote fs cp`, verified via passive host BLE scan + lvgl label
introspection (`mpos` UI helpers); no badge wedged:**

| Check | Result |
|---|---|
| Single-badge advertise (D-8 path) | ✅ host scan: `Alex YOURCALL`, `ver 1`, `gids [0xa07b]` |
| 3 badges advertise concurrently (adv+scan) | ✅ all three beacons seen together |
| Idle UI (name/handle/own-group/battery/nearby + **D-5** help text) | ✅ labels read: `Alex / YOURCALL / Makerspace Baasrode / 95% / nearby:… / A:detail  X:mute  B/START:exit` |
| Real round-trip detection (multi-peer) | ✅ Alex: `nearby: Alice, Bob`; symmetric on Alice & Bob |
| **D-4** coalescing across banner window | ✅ single banner `Bob, Alice nearby (Makerspace Baasrode)` for two arrivals |
| Disjoint group ignored | ✅ Bob on `Ham Radio ON4` (gid `0x2373`) → `nobody nearby` despite peers in range |
| **D-6** unconfigured hint + BLE-silent | ✅ empty config → `Configure me …` screen + **absent** from host scan |
| Stop-on-exit | ✅ `restart_launcher` → badge gone from host scan (advertising stopped) |
| Detail view (`A`) content | ✅ `Bob  Makerspace B  -38dBm  0s` (name + shared group + ewma RSSI + age) |
| Config reload on relaunch | ✅ group/name updated after `restart_launcher`+`start_app` |
| No crash on any launch (loop/logo-fit/coalesce paths execute) | ✅ |

**Not exercised (physical only):** `X`-mute audible output and `A`/`B` via real
button presses (GPIO — driven through the Activity instance instead); exact logo
pixel-centring needs a human eye — recommended visual spot-check.
