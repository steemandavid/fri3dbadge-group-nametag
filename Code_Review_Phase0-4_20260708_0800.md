# Phase 0–4 Code Review — Group Nametag + BLE Proximity Finder

**Document ID:** GNT-REVIEW-P0-4-001
**Reviewer:** Code Review Agent
**Date:** 2026-07-08
**Scope:** Phases 0–4 (full implementation) + the post-implementation scan-stability fix
**FSD Reference:** `PLAN.md` (spec) + `DESIGN.md` (approved platform adaptation)
**Commit Reviewed:** `dc50c10`

---

## Verdict: PASS WITH NOTES

The implementation is a faithful, well-structured realization of `PLAN.md`, correctly ported to the **MicroPythonOS 0.11.1** platform actually on the bench (an approved deviation documented in `DESIGN.md §1`). The BLE protocol core — wire-format encode/decode, `fnv1a_16` hashing, dedup/sort/cap, defensive parsing, set-intersection matching, UTF-8-boundary truncation — is **correct and thoroughly unit-tested** (30/30 host tests pass, re-run during this review). Platform-API usage in the UI is clean, and BLE teardown on exit is airtight (verified redundantly across `onPause`/`onStop`/`onDestroy`). No CRITICAL defects were found.

Four MAJOR issues hold it back from an unqualified PASS: (1) the render/input loop's `try/except` wraps the *entire* `while`, so the first unhandled per-frame exception **permanently freezes the UI and all buttons**; (2) a real **IRQ-vs-loop race** on the shared `_seen` dict is the most likely trigger for (1); (3) the logo is **never scaled-to-fit** (PLAN §7), and the shipped 160×160 logo already overlaps the name; (4) alert **coalescing only merges arrivals within a single 30 ms frame**, not across the banner window (PLAN §8), so trickle arrivals fire repeated cues. Issues (1) and (2) are cheap to fix and are the only ones I'd treat as pre-field-use conditions.

---

## Table of Contents

1. [Coverage Analysis](#1-coverage-analysis)
2. [Deviation Report](#2-deviation-report)
3. [Plan vs. Implementation](#3-plan-vs-implementation)
4. [Edge Cases & Safety](#4-edge-cases--safety)
5. [Concurrency & Platform Issues](#5-concurrency--platform-issues)
6. [Error Handling](#6-error-handling)
7. [Code Quality](#7-code-quality)
8. [Summary](#8-summary)
9. [Recommendation](#9-recommendation)

---

## Files Reviewed

| File | Purpose |
|------|---------|
| `app/com.fri3dcamp.groupnametag/ble_proximity.py` | BLE advertise/scan + group-aware proximity state machine (pure wire-format half + radio wrapper) |
| `app/com.fri3dcamp.groupnametag/group_nametag.py` | MicroPythonOS `Activity`: idle UI, logo, alerts, buttons, lifecycle |
| `app/com.fri3dcamp.groupnametag/config.json` | Per-install config (`groups`/`name`/`handle`/`rssi_floor`) |
| `app/com.fri3dcamp.groupnametag/MANIFEST.JSON` | MicroPythonOS app manifest (launcher intent) |
| `tests/test_ble_proximity.py`, `tests/conftest.py` | Off-device pytest for the pure wire-format functions (30 tests) |
| `tools/host_advertise.py`, `tools/pull_file.py` | Host-side test/dev tooling |
| `probes/*.py` (4) | Phase-0 de-risk spike scripts (throwaway) |

---

## 1. Coverage Analysis

Each requirement is mapped to the *MicroPythonOS* implementation (per `DESIGN.md §1`), since the `fri3d.application` framework the plan assumed is absent.

| FSD requirement | § | Status | Notes |
|---|---|---|---|
| BLE advertising payload (HSNT wire format) | §6.1 | **DONE** | `build_payload` — exact overhead `11+2G`, name budget `20−2G`, ≤31 bytes. |
| `fnv1a_16` group hashing + normalize | §6.2 | **DONE** | Matches spec byte-for-byte; pinned by `test_fnv_known_vector`. |
| Dedup + sort + `MAX_GROUPS` keep-lowest | §6.2 | **DONE** | Deterministic; `test_hash_groups_overflow_keeps_lowest`. |
| Defensive parse (drop malformed/hostile/unknown-version) | §6.3 | **DONE** | Every length field bounds-checked; never raises. |
| Group-set intersection matching; disjoint → drop | §6.3 | **DONE** | `intersect`; empty → ignored. |
| Stable-identity keying `(addr_type, addr)` | §6.1 | **DONE** | Public MAC via `ble.config("mac")` (DESIGN §3). |
| Eviction `EVICT_MS=30 s`, `ticks_diff` | §6.3 | **DONE** | Wrap-safe in `_evict`. |
| Notify-once-per-encounter debounce | §6.3 | **DONE** | Enforced by `_seen` membership; re-alert on evict+return. |
| Continuous dense scan (duplicate-filter fix) | changelog | **DONE** | `gap_scan(0, 120000, 60000)`; the load-bearing scan-stability fix. |
| `rssi_floor` validation/default | §5/§6.3 | **DONE** | `_validate_floor`, default −120. |
| Logo decode by LV_FS path + placeholder fallback | §7 | **PARTIAL** | Decodes via `"S:"` path; **fit-to-box scaling missing** (see D-3); fallback only on *raised* exception. |
| Idle screen: logo + breathing anim, name/handle, own-groups, nearby line, battery | §8 | **PARTIAL** | All present; breathing centered on 256 not ~240; own-groups + battery good. |
| First-run/unconfigured hint + BLE-silent | §8 | **PARTIAL** | Logic correct, but **shipped config defeats it** (see D-6). |
| Alert overlay: banner + LED + buzzer + per-group signature | §8 | **DONE** | Non-blocking cues; lowest-shared-id signature. |
| Alert **coalescing** across banner window | §8 | **PARTIAL** | Only within one 30 ms drain (see D-4). |
| Buttons: X mute / A detail / B‑START exit | §8 | **PARTIAL** | X, A, START done; **B not wired** (see D-5). |
| Lifecycle: `begin()` on resume, teardown on exit, advertising stops | §8/§10 | **DONE** | Triple-redundant teardown; verified BLE-silent on exit. |
| Backlight dim-out | §8 | **DROPPED (approved)** | No brightness API; documented DESIGN §1/§4. |
| Off-device unit tests (all §10 pure-function bullets) | §10 | **DONE** | 30/30 pass (re-run this review). |

## 2. Deviation Report

Severity: **CRITICAL** (failure/harm) · **MAJOR** (spec deviation / probable bug) · **MINOR** (improvement) · **INFO** (observation).

### D-1 · MAJOR · Whole-loop `try/except` → first per-frame exception freezes UI + all buttons permanently
`group_nametag.py:432-452`. The `try:` wraps the entire `while True`, with `except Exception: pass` *outside* the loop. Any unhandled exception in `_handle_buttons`, `_ble.tick`, `_drain_arrivals`, `_refresh_nearby`, `_refresh_battery`, or `_animate` terminates the loop **forever, silently** — no animation, no nearby refresh, and crucially **no button polling**, so `X`/`A`/`START` all die (only the OS back gesture can leave). Fix: move the `try/except` *inside* the loop body so a transient fault skips one frame instead of killing the task.

### D-2 · MAJOR · IRQ mutates `_seen` while the async loop iterates it → `RuntimeError: dictionary changed size during iteration`
`ble_proximity.py:387` (`self._seen[key] = entry` in the scan-result IRQ) racing with `:406` (`_evict` iterates `self._seen.items()`) and `:423` (`current_peers` iterates `self._seen.values()`). On ESP32/NimBLE the `BLE.irq` callback is delivered via the scheduler and runs interleaved with the asyncio loop at bytecode boundaries. A **new** peer arriving mid-iteration changes the dict size → `RuntimeError` on the reader side (`tick()`/`current_peers()` are *not* IRQ-guarded). This is the most likely real-world trigger for D-1: a badge walks into range at the wrong instant and the UI freezes. Fix: defer parsing/insertion to `tick()` (IRQ only enqueues `(addr_type, bytes(addr), bytes(adv_data), rssi)`), or snapshot with `list(...)` before iterating. The `stale`-list-then-delete pattern in `_evict` guards only against delete-during-iteration, not the IRQ insert.

### D-3 · MAJOR · Logo never scaled-to-fit; `_logo_base_scale` is dead; shipped 160×160 logo overlaps the name
`group_nametag.py:208-215` (`_place_logo`), `:498-507` (`_animate`), `:98`. PLAN §7 requires `scale = min(box_w/img_w, box_h/img_h) × 256`. The code never queries the decoded size and never sets a base scale (`_logo_base_scale = 256` is assigned once and **never read**); `_animate` uses a literal `int(256 + BREATH_AMP·sin(...))`, i.e. always ~native pixels. The bundled `logo.png` is **160×160** at `TOP_MID, y=18` (spans y≈18–178), while the name label is at y=130 → ~48 px overlap. For the app's redistributable goal (groups drop in their own art — the repo's source logo is 500×500) a larger PNG overflows the 296×240 screen entirely.

### D-4 · MAJOR · Coalescing merges only within one 30 ms drain, not across the banner window
`group_nametag.py:474-479` (`_drain_arrivals`), `:322-335` (`_coalesced_text`). PLAN §8 wants several arrivals *within one ~2.5 s banner window* → a single cue/banner. Here `take_arrivals()` is drained every `TICK_MS=30 ms` and any non-empty batch immediately calls `_fire_alert` (new banner + LED flash + buzzer sting). Only arrivals queued inside the same 30 ms window coalesce; three group-mates arriving ~500 ms apart produce **three stings and three LED flashes** — exactly the stacking the spec forbids. Fix: accumulate arrivals until `_banner_until` before firing one cue.

### D-5 · MINOR · `B` button never returns to the launcher (PLAN §8 says "B / START")
`group_nametag.py:454-472`. `"b"` is edge-tracked but has no handler; only `"start"` calls `self.finish()`. Low impact (START and the OS back gesture work), but a documented control is missing.

### D-6 · MINOR · Shipped `config.json` uses non-empty placeholders → defeats the first-run hint; fresh badge advertises "Your Name YOURCALL"
`config.json` ships `"name": "Your Name"`, `"handle": "YOURCALL"`. Because `_unconfigured = (not name) or (not ids)` (`group_nametag.py:135`), an un-edited badge is treated as **configured**: it skips the "Configure me" hint and **advertises the placeholder identity**. PLAN §8's unconfigured path exists precisely so a freshly-flashed copy stays BLE-silent ("advertising nothing is better than advertising an un-provisioned badge"). Fix: ship `name`/`groups` empty (or a sentinel the loader treats as unconfigured).

### D-7 · MINOR · Wrap-unsafe raw `ticks_ms` arithmetic in two throttles
`ble_proximity.py:342/347` (scan re-arm: `now_ms >= self._next_rearm_ms`, `now_ms + SCAN_REARM_MS`) and `group_nametag.py:522-525` (battery: `now < self._batt_next_ms`, `now + 5000`). Both violate the PLAN §6.3 rule (use `ticks_diff`/`ticks_add`, never raw compare/add). Every *other* time-gate in the code does it correctly. Impact is low (wrap ≈ 12 days, both self-heal) but it's an easily-avoided latent bug and an inconsistency.

### D-8 · MINOR · `begin()` reports success on advertising failure; non-string `name` crashes app start
`ble_proximity.py:301-312`. `gap_advertise` is wrapped in `try/except: pass` and `begin` then `return True` regardless — a real advertising failure leaves the badge scanning but invisible while the caller believes BLE is up. Conversely `build_payload`/`active(True)`/`irq()` are unguarded, so a non-string `config.name` (e.g. an int in `config.json`) reaches `truncate_utf8().encode()` → `AttributeError` out of `begin`, crashing start rather than degrading. `rssi_floor` is validated; `name`/`groups` *types* are not.

### D-9 · INFO · Full parse + allocation inside the BLE IRQ callback
`ble_proximity.py:358-400`. `parse_payload`, `bytes(adv_data)`, `bytes(addr)`, `shared_name_for`, dict construction and `.append` all run in the scheduled scan-result callback. Allocation is *permitted* in this (scheduled, not hard-ISR) context, so this is not a defect — but it is heavyweight work under the deliberate dense 50 %-duty scan, widening the D-2 race window and delaying subsequent BLE events. Deferring to `tick()` fixes D-2 and this together.

### D-10 · INFO · Breathing animation centered on 256 (only ever scales up)
`group_nametag.py:41-42, 503`. `s = int(256 + 14·sin(...))` → 242–270, i.e. 1.0×–1.05×, never below native. PLAN §8 suggests centering near 240 for a symmetric breathe. Purely cosmetic.

### D-11 · INFO · `_coalesced_text` single-group "+N more" branch is unreachable
`group_nametag.py:329-332`. In the single-shared-group path `len(arrivals) - len(names)` is always 0, so the compact "Alice + N more" form never appears there; the banner joins all names and relies on `[:60]`, which can mid-cut a name.

### D-12 · INFO · Placeholder logo fallback only fires on a *raised* exception
`group_nametag.py:208-213`. lvgl image decode often fails silently rather than raising, so a corrupt PNG yields an empty `lv.image` (no crash — good) but not the coloured-disc "HS" placeholder; the "still a usable nametag" intent (PLAN §7) is only partly met. Consider verifying decode (e.g. non-zero width) before accepting.

## 3. Plan vs. Implementation

| Plan item | Planned | Actual | Status |
|-----------|---------|--------|--------|
| Platform / framework | `fri3d.application` `App` + `app.json` + `neon_launcher` (PLAN §2/§3) | MicroPythonOS `Activity` + `MANIFEST.JSON` + AppManager (DESIGN §1) | **Deviation — approved & documented** |
| Files created | `ble_proximity.py`, `group_nametag.py`, `app.json`, `__init__.py`, `logo.png`, `tools/convert_logo.py`, tests | `ble_proximity.py`, `group_nametag.py`, `config.json`, `MANIFEST.JSON`, `icon_64x64.png`, `logo.png`, tests, `tools/host_advertise.py`+`pull_file.py`, `probes/` | **Adapted** — `app.json`→`MANIFEST.JSON`+`config.json`; `__init__.py` unneeded (Activity import); `convert_logo.py` unneeded (LV_FS path decode); host advertiser added as 2nd-badge test stand-in |
| Phase 0 (de-risk spikes) | Logo decode, concurrent adv+scan, HW-API probe | All resolved; findings folded into DESIGN §1/§4; `probes/` retained | **Done** |
| Phase 1 (BLE core + host tests) | Pure functions + 30-ish tests, `pytest` green | 30/30 pass (re-run this review) | **Done** |
| Phase 2 (logo loader) | Path/decode + placeholder | Decodes; **fit-to-box missing** (D-3) | **Partial** |
| Phase 3 (idle UI shell) | Logo/anim, name/handle, own-groups, hint, buttons, lifecycle | Present; B unwired (D-5), hint defeated by shipped config (D-6) | **Partial** |
| Phase 4 (alerts + signature) | Banner+LED+buzzer, signature, coalescing, detail, mute | Present; **coalescing window gap** (D-4) | **Partial** |
| Phase 5 (integration + polish + docs) | On-device integration, dim-out+battery, field test, README/DESIGN, `rssi_floor` table + link budget | Integration + battery done; dim-out dropped (approved); README/DESIGN written; **`rssi_floor` guidance table + open-field link budget still TODO** (DESIGN §5) | **Partial (as documented)** |
| Testing strategy | Host pytest for pure fns + on-device state-machine/radio checks | Matches — 30 host tests + 17 on-device checks + 2/3-badge field tests (changelog/DESIGN §4) | **Matches** |

**Undocumented deviations:** none material. The platform pivot, dropped dim-out, and tooling substitutions are all recorded in `DESIGN.md`. The `rssi_floor` table and link-budget write-up remain open TODOs the team already tracks (DESIGN §5). The gaps above (D-3/D-4/D-5/D-6) are *fidelity* gaps against PLAN §7/§8, not undocumented scope changes.

## 4. Edge Cases & Safety

- **Hostile/ malformed adverts:** robustly handled — `parse_payload` bounds-checks `slen`, `gcount` (via `gid_end`) and `namelen` against already-clamped slices, decodes name with `errors="replace"`, and returns `None` for bad magic / unknown version / truncation at every prefix. Covered by 5 host tests. ✔
- **UTF-8 truncation:** walks back over `10xxxxxx` continuation bytes; `enc[cut]` always in-bounds. Never splits a codepoint. ✔
- **Broken/missing logo:** does not crash (D-12), but placeholder only shows on a raised exception; silent decode failure → blank space (name still renders). Acceptable safety, imperfect fidelity.
- **Empty/malformed `config.json`:** `_load_config` defaults every field, catches file/JSON errors, and derives `_unconfigured` correctly (incl. all-whitespace groups). ✔ — but see D-6 (shipped placeholders aren't treated as unconfigured).
- **`_fire_alert` `min()` over empty sequence** (`group_nametag.py:342`): near-impossible given arrivals only queue on non-empty intersection, but if it ever occurred it would raise into the D-1 outer `except` and kill the loop. A guarded default is cheap insurance.
- **Notify-once / re-alert:** correct — one arrival per `_seen` insert, eviction deletes so a return re-alerts exactly once (PLAN §6.3). ✔

## 5. Concurrency & Platform Issues

- **D-2 IRQ↔loop `_seen` race (MAJOR)** — the one genuine concurrency defect; see Deviation Report.
- **D-9 heavyweight IRQ work (INFO)** — allowed but widens the race window under dense scan.
- **BLE lifecycle / teardown:** `end()` does `gap_scan(None)` + `gap_advertise(None)` + `active(False)`, is idempotent, and is called from `onPause`/`onStop`/`onDestroy`. Advertising provably stops on exit (PLAN §10). ✔
- **Scan-stability fix:** `gap_scan(0, 120000, 60000)` correctly passes explicit interval/window to disable NimBLE's duplicate filter (the root cause of the presence flapping) and re-arms every 30 s. Matches DESIGN §3. ✔
- **Non-blocking cues:** `_sting` and `_leds_off_after` run as separate `TaskManager` tasks, so buzzer/LED timing never blocks the render tick. ✔
- **Platform API usage (lvgl v9 / mpos):** clean — no `clear_flag`, no tuple `set_all` (uses `set_led`/`write`/`clear`/`get_led_count`), `lv.display_get_default()`, `add_flag`/`remove_flag(FLAG.HIDDEN)`, no `print(flush=)`. Matches the DESIGN §1 verified-facts table. ✔
- **Post-`finish()` iteration (D-1-adjacent):** `group_nametag.py:470-472` `return`s from `_handle_buttons` but the same loop iteration continues to `_refresh_nearby` etc.; if `finish()` synchronously deletes widgets, `self._near_lbl.set_text` (unguarded) could touch a freed object → raises into the D-1 outer `except`. Break out of the iteration right after `finish()`.

## 6. Error Handling

- Radio calls in `end()` are individually guarded; `_irq` swallows all exceptions so the IRQ never raises. ✔
- `begin()` over-swallows (D-8): advertising failure is hidden behind `return True`; `name`/`groups` types unvalidated (crash path).
- The UI loop's coarse outer `try/except` (D-1) is the central error-handling flaw — it converts any transient per-frame error into a permanent silent freeze. Per-frame isolation is the correct pattern.
- Config, battery, buzzer, LED, and logo paths are each individually guarded and degrade gracefully. ✔

## 7. Code Quality

- **Strong:** clean separation of pure wire-format functions (host-testable, no `bluetooth`/`mpos`/`lvgl` imports) from the radio wrapper via lazy imports; clear naming; thorough docstrings; the DESIGN.md platform-adaptation record is exemplary.
- **Dead/vestigial code:** `notified` field never read (`ble_proximity.py:385`); `_logo_base_scale` never read (`group_nametag.py:98`); backlight/dim scaffolding retained after the feature was dropped (`DIM_MS`, `_disp`/`_dimmed`, `_setup_display`, `_set_brightness`, `_tick_dim`, `_wake` brightness branch) — inert and correctly guarded, but carrying cost; recommend removal for clarity.
- **Tooling:** `host_advertise.py` emits the correct `company-id 0xFFFF → MAGIC..payload` structure (matches the app's expected AD). `pull_file.py` works but doesn't verify decoded length against the reported `CLEN` (silent partial-pull risk — dev tool, INFO). `probes/*.py` are clean throwaway de-risk spikes, correctly **not** shipped inside the app package.
- **Test gap:** the `BLEProximity` *wrapper* (notify-once/eviction transitions, `_validate_floor`, the intersect/`rssi_floor` gate in `_handle_scan_result`) has no host tests despite being trivially host-testable with injected data; a few such tests would lock the state-machine contract and could have surfaced D-2.

## 8. Summary

| Category | Critical | Major | Minor | Info |
|----------|:--------:|:-----:|:-----:|:----:|
| Spec conformance | 0 | 2 (D-3, D-4) | 2 (D-5, D-6) | 1 (D-10) |
| Plan conformance | 0 | 0 | 0 | 0 (all deviations documented) |
| Correctness | 0 | 0 | 1 (D-7) | 1 (D-11) |
| Safety | 0 | 0 | 0 | 1 (D-12) |
| Concurrency | 0 | 2 (D-1, D-2) | 0 | 1 (D-9) |
| Error handling | 0 | 0 | 1 (D-8) | 0 |
| Code quality | 0 | 0 | 0 | dead code / test gap (§7) |

*D-1 and D-2 are counted under Concurrency (their root domain); D-1 also manifests as the error-handling flaw discussed in §6.*

## 9. Recommendation

**GO to next phase / field use, conditional on fixing D-1 and D-2 first.** Those two are cheap, high-value, and together eliminate a real "UI freezes when a badge walks in at the wrong moment" failure — the worst on-camp outcome. Concretely:

1. **D-1** — move the `try/except` inside the `while` body (isolate per-frame). *(pre-field-use)*
2. **D-2** — have the scan-result IRQ only enqueue raw `(addr_type, bytes(addr), bytes(adv_data), rssi)` and do all `parse`/intersect/`_seen` mutation in `tick()`; or snapshot `list(...)` before iterating. Fixes D-9 too. *(pre-field-use)*

**Strongly recommended before the event (polish/fidelity):**
3. **D-3** — implement PLAN §7 fit-to-box scaling (query decoded size; set base scale; breathe around it), so arbitrary group logos don't overlap the name or overflow.
4. **D-4** — accumulate arrivals across the banner window before firing one coalesced cue.
5. **D-6** — ship `config.json` with empty `name`/`groups` so a fresh badge shows the hint and stays BLE-silent.

**Nice-to-have:** D-5 (wire B to exit), D-7 (wrap-safe ticks), D-8 (validate `name`/surface advertise failure), plus dead-code cleanup and a handful of `BLEProximity`-wrapper host tests. The `rssi_floor` guidance table and open-field link-budget write-up remain open DESIGN §5 TODOs.

The protocol core is solid and well-tested; the outstanding work is concentrated in UI-loop robustness and two feature-fidelity gaps, all of them localized and low-risk to fix.
