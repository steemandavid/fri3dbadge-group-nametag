# Phase 5 Code Review — Contact Swap, Splash, WiFi Portal & Rebrand (v0.4.0 → v0.6.0)

**Document ID:** FRI3DFRIENDS-REVIEW-P5-01
**Reviewer:** Code Review Agent
**Date:** 2026-07-15
**Scope:** All development since the Phase 0–4 review baseline (`a8a20c9`) through HEAD — commits `b458190..d3f6d90`: UI redesign + Fri3d 2026 support, splash, Y-button contact exchange, WiFi setup portal, NTP clock, friend LEDs, the two field-bug fixes (BLE re-entrancy, LED/GATT starvation), per-swap contacts, portal-save fix, and the !Fri3d Friends rebrand
**FSD Reference:** `PLAN.md` (original spec) + `DESIGN.md` §7–§11 (approved adaptations & new-feature spec) + `changelog.md` entries 2026-07-11 … 2026-07-14 (de-facto phase plan)
**Commit Reviewed:** `d3f6d90` (v0.6.0)

---

## Verdict: PASS WITH NOTES

The phase's core engineering is sound and shows real field discipline: the contact-exchange
re-entrancy fix (`_mtu_set`/`_svc_ready` one-shot flags, every NimBLE setup call individually
guarded) and the LED-starvation fix (main loop quiescent while `_exchanging`) are **correctly
implemented as described**, `run_window` is airtight with `try/finally` around teardown +
`proximity.resume()`, all Phase 0–4 review fixes (D-1…D-12) remain intact with no regressions,
and every new time-gate uses wrap-safe `ticks_diff`/`ticks_add`. No CRITICAL defects found.

Five MAJOR issues hold it back from an unqualified PASS, and three of them cluster in
`web_portal.py`'s input handling — the path the README *recommends* for all text entry:
(1) percent-decoding produces **mojibake for every non-ASCII character** saved via the portal
(a "José" or "café"-type name — common at a Flemish camp — is silently corrupted, and a group
name saved via the portal then hashes differently from the same name typed in `config.json`,
breaking matching); (2) `_esc()` doesn't escape single quotes while every form attribute is
single-quoted, so an apostrophe (`O'Brien`, `L'Atelier`) breaks the form and **silently
truncates the value on the next save**; (3) the POST body is read with a single short-read-prone
`read()`, so a large form can silently lose fields. The other two are lifecycle edges: the
exchange task is never cancelled on exit (it can outlive the Activity by up to 5 s and touch
freed LVGL widgets — the exact hazard class the D-1 `_finishing` fix addressed), and a Y-press
on an *unconfigured* badge leaves the BLE stack active with a stale IRQ handler after app exit.

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
| `app/com.fri3dcamp.fri3dfriends/fri3d_friends.py` (1249 ln) | Main Activity: splash, nametag UI, main loop, buttons (2024/2026), friend LEDs, clock/NTP, exchange + portal wiring, config reload |
| `app/com.fri3dcamp.fri3dfriends/contact_exchange.py` (580 ln) | NEW — Y-button connectable-GATT contact swap (HXCG rendezvous, role tie-break, envelope, storage helpers) |
| `app/com.fri3dcamp.fri3dfriends/web_portal.py` (474 ln) | NEW — PIN-gated HTTP config/contacts portal on `asyncio.start_server` |
| `app/com.fri3dcamp.fri3dfriends/ble_proximity.py` (498 ln) | Delta review: `suspend()`/`resume()`, `handle` removal; regression pass on D-2/D-7/D-8 fixes |
| `app/com.fri3dcamp.fri3dfriends/MANIFEST.JSON`, `config.json` | Manifest/config sanity |
| `tests/test_ble_proximity.py` (30), `tests/test_contact_exchange.py` (21), `tests/test_web_portal.py` (6), `conftest.py` | Off-device test coverage (57 total) |

Context read (not re-reviewed): `PLAN.md`, `DESIGN.md`, `README.md`, `changelog.md`,
`Code_Review_Phase0-4_20260708_0800.md`, `Code_Review_Fixes_20260708.md`.

**Test execution note:** `pytest tests/` could not be run this session (the harness's
command-safety service was unavailable throughout; all review work was done via read-only
file inspection). The changelog records **57/57 green** at v0.5.0, the suite is unchanged
since, and the 57 count was verified by inspection (30 + 21 + 6 test functions). Re-running
`pytest tests/` is listed as a condition below.

---

## 1. Coverage Analysis

Spec for this phase = DESIGN.md §7–§11 + the changelog feature entries (there is no updated
PLAN.md for these features — see §3).

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Splash: title/version/author/logo/org at explicit y-positions, 3 s, then nametag | DESIGN §8 | **DONE** | `_build_splash`/`_splash_then_enter` (`fri3d_friends.py:558-622`); version read from MANIFEST; in-memory PNG decode with graceful fallback |
| Live clock top-left, `CLOCK_X=24`, ~1 Hz, NTP resync ~10 min, first resync deferred | DESIGN §8 | **DONE** | `_refresh_clock`/`_resync_time` (`:1040-1092`); wrap-safe throttles; stale comment, see F-10 |
| Name at 42 px bundled TTF, fallback montserrat_28, no transform-scale | DESIGN §11a | **DONE** | `_load_name_font` (`:378-389`), SCROLL_CIRCULAR long-mode |
| Friends line inset W−40, WRAP, ~90-char cap | DESIGN §11a | **DONE** | `:665-671`, `:989-995` |
| Group pills, per-group signature colour | changelog 07-11 | **DONE** | `_place_pills` (`:447-481`) |
| Friend LEDs: 1/friend, group colour, breathe 0.015–0.18 / 3800 ms, stagger, board-keyed 4/5, frame-cached, flash override | DESIGN §11 | **DONE** | `_update_leds` (`:776-807`); matches spec constants exactly |
| Contact exchange: 5 s window, HXCG connectable beacon + scan, lower-MAC=server, MYINFO/THEIRS GATT, MTU 515, ≤500 B envelope | DESIGN §9 | **DONE** | `contact_exchange.py`; envelope drops fields last-first, name always kept — tested |
| Re-entrancy fix: `config(mtu=)`/`gatts_register_services` one-time, setup fully guarded | changelog 07-13 | **DONE** | Verified: flags only set **after** the op succeeds (`:363-368`, `:299-320`), so a failed first attempt retries — correct |
| LED-starvation fix: loop quiescent while `_exchanging` | changelog 07-13 | **PARTIAL** | All periodic work skipped (`:911-913`) — but button *actions* still run mid-swap (F-7) |
| Per-swap contact entries, no dedup, cap 200 oldest-first, `received_at`/`received_ticks` | changelog 07-14 | **DONE** | `add_received` (`contact_exchange.py:178-199`), tested incl. cap/evict |
| `merge_received = add_received` alias | changelog 07-14 | **DONE** | `:205` |
| Handle field fully removed | changelog 07-14 | **DONE** | `begin(groups, name, rssi_floor)` matches the caller; no leftovers found |
| Suspend/resume coexistence, no `active(False)` churn | DESIGN §9 | **DONE** | `ble_proximity.py:338-367`; resume reinstalls IRQ + beacon + re-arms scan; no-ops safely after `end()` |
| Portal: routes /, /save, /contacts, /contacts.json; PIN per boot; session cookie; 5-fail lockout + rotation; pending-PIN challenge on badge | DESIGN §10 | **DONE** | Auth enforced on every non-/login route (`web_portal.py:324-341`); `os.urandom` PIN/token |
| Portal save → deferred safe in-place reload, no screen rebuild, no BLE cycling; "Config saved ✓" banner + portal note | changelog 07-14 | **DONE** | `_reload_config`/`_apply_reload` (`fri3d_friends.py:1219-1249`) honor the constraint; exchange-in-progress re-defers |
| Portal is the recommended text-entry path, handles arbitrary values | README | **PARTIAL** | Non-ASCII mojibake (F-1), apostrophe corruption (F-2), short-read loss (F-3) |
| Unconfigured badge: hint, no advertise/scan | README / PLAN §8 | **PARTIAL** | Proximity stays silent ✔; a Y-press still fires the exchange and leaves BLE active after exit (F-5) |
| 2026 board support: detect, expander buttons (active-high), GPIO38 buzzer, 320×240, backlight dim | DESIGN §7 | **DONE** | Polarity handled correctly both boards (`:308-324`); dim only with peers absent + 2026 backlight |
| Clean teardown on exit (radio silent, LEDs off, portal stopped) | PLAN §10 | **PARTIAL** | Normal paths solid (onPause/onStop/onDestroy); holes on exit-during-swap (F-4) and unconfigured-swap (F-5); onDestroy misses `_stop_portal` (F-13) |

## 2. Deviation Report

Severity: **CRITICAL** (failure/harm) · **MAJOR** (real bug / spec deviation likely to bite) · **MINOR** (improvement) · **INFO** (observation).

### F-1 · MAJOR · Portal percent-decoding mojibakes every non-ASCII character; portal-saved group names then fail to match
`web_portal.py:73-88` (`_url_unquote`), used by `parse_form` (`:91-110`).
```python
out.append(chr(int(s[i + 1:i + 3], 16)))
```
Browsers percent-encode form text as **UTF-8 bytes** (`é` → `%C3%A9`). Decoding each `%XX` to
`chr(byte)` yields Latin-1 characters (`Ã©`), never reassembling the UTF-8 sequence. Every
accented name, group, or contact value saved through the portal — the README's recommended
editing path, at a Belgian camp full of `José`/`Noël`/`café`-class strings — is silently
corrupted in `config.json`, displayed corrupted on the badge, and **broadcast corrupted** in
the beacon. Worse, `groups` are hashed from the string: a group saved via the portal hashes
differently from the identical group typed directly into `config.json` on a friend's badge →
**the two badges silently never match**. Fix: decode `%XX` pairs into a byte buffer and
`.decode("utf-8", "replace")` once at the end. The tests only exercise ASCII (`%40`, `%2C`),
which is why this survived. *(Also true, mildly: `parse_form` on a str already decoded — the
fix belongs in `_url_unquote` operating on bytes.)*

### F-2 · MAJOR · `_esc()` doesn't escape single quotes, but every form attribute is single-quoted → apostrophes break the form and corrupt data on re-save
`web_portal.py:68-70` (`_esc`), `:422-424` and `:436-451` (`_config_page`).
```python
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
...
"<label>Name</label><input name='name' value='%s'>"
```
A legitimate value containing `'` — name `O'Brien`, group `L'Atelier`, any contact value with
an apostrophe — terminates the `value='…'` attribute early. The rendered form shows the
truncated value (`O`), and pressing **Save** then writes the truncated value back to
`config.json`: silent data corruption through normal use. It is also a textbook attribute-
injection vector (`x' autofocus onfocus='…`), though here only owner-supplied values reach
attributes, so the practical impact is the data corruption. Attacker-controlled data (received
contacts) is rendered only in element-content context on `/contacts`, where the current
escaping *is* sufficient — verified. Fix: add `.replace("'", "&#39;")` to `_esc` (or switch the
attributes to double quotes; `"` is already escaped).

### F-3 · MAJOR · POST body read with a single `read()` — short reads silently truncate the form and drop config fields
`web_portal.py:289`:
```python
body = await reader.read(min(clen, MAX_BODY))
```
`StreamReader.read(n)` returns **up to** n bytes — whatever is buffered once at least one byte
arrives. A save whose body spans more than one TCP segment (~1.4 KB; easily reached with a
long contact-field list, `MAX_BODY` allows 8 KB) can return only the first segment. `parse_form`
parses the fragment without error and `form_to_config` fills the missing keys with defaults →
**a save silently wipes fields** (contact entries lost, `sound` reset, etc.). Fix: loop until
`clen` bytes (or use `readexactly`), with a modest timeout.

### F-4 · MAJOR · The exchange task is never cancelled on exit; it outlives the Activity by up to 5 s, then touches LVGL widgets and resumes BLE state on a torn-down app
`fri3d_friends.py:944-946` (spawned untracked), `:875-887` (`_stop_task` cancels only
`self._task`/`self._splash_task`), `:1112-1142` (`_do_exchange`).
```python
if ev == "y":
    if not self._exchanging:
        TaskManager.create_task(self._do_exchange())
```
Quit the app (OS **X**) during a swap window: `onPause`/`onStop` cancel the main loop, stop the
portal, and call `_teardown_ble()` → `BLEProximity.end()` → `active(False)` — while
`_do_exchange`/`run_window` keeps running to its 5 s deadline. Its BLE calls now all raise
(individually guarded, so it limps on), then `_do_exchange` calls `self._show_banner(...)`
(`:719-724` — `set_text` on widgets the OS may have deleted with the screen) and appends to
`exch.log`. Use-after-free on lvgl objects is precisely the hard-crash class the D-1
`_finishing` fix existed for; a Python-level guard does not necessarily contain it on this
build. `proximity.resume()` is safe in this path only by luck of the `self._ble is None` check.
Fix: keep the task handle (`self._exch_task`) and cancel it in `_stop_task`; have
`run_window` re-raise `CancelledError` through its `finally`.

### F-5 · MAJOR · Y-press on an unconfigured badge activates BLE that no teardown path ever deactivates
`fri3d_friends.py:831-836` (BLE `begin()` skipped when unconfigured), `:944-946` (Y not gated on
`_unconfigured`), `contact_exchange.py:358-362` (`active(True)`), `ble_proximity.py:316-318`:
```python
def end(self):
    if not self._ble:
        return
```
On an unconfigured badge `BLEProximity.begin()` never runs, so `proximity._ble is None`. A
Y-press still runs the full exchange window (advertising a connectable HXCG beacon as
"Anonymous" — already at odds with README's "unconfigured … does not advertise or scan").
Afterwards `resume()` correctly no-ops, but on app exit `_teardown_ble()` → `end()` returns
immediately on the `None` check — **`active(False)` is never called**. The radio stays powered
with the dead Activity's exchange IRQ handler installed after the app exits (stale-handler
misrouting for the next BLE user, plus battery draw). Fix: gate Y on `self._unconfigured`
(cheapest, matches the README), and/or make the exchange deactivate BLE itself when it was the
one to activate it.

### F-6 · MINOR · Stale `_alert_names` coalesce into non-arrival banners, and arrivals during those banners get no cue
`fri3d_friends.py:967-982` (`_drain_arrivals`). `_banner_until` is set by *any* banner —
"Swapped with X ✓", "Config saved ✓", "Swapping contacts…" — and `_alert_names` is never
cleared when a banner hides. An arrival landing while a non-arrival banner is up takes the
coalescing branch: it extends `_alert_names` (which still holds names from a *previous* alert
window, minutes old) and rewrites the banner to "OldName, NewName nearby (…)" with **no
LED flash and no sting**. Failure scenario: swap completes → "Swapped with Bob ✓" showing →
Alice walks up → banner silently becomes "Carol, Alice nearby" (Carol arrived an hour ago).
Fix: clear `_alert_names` in `_hide_banner()` and track whether the active banner is an
arrival banner before coalescing into it.

### F-7 · MINOR · Button *actions* still run during a swap — B triggers the exact LED-write/file-write starvation the `_exchanging` pause exists to prevent
`fri3d_friends.py:903-913` + `:947-955`. The loop correctly skips all periodic work while
`_exchanging`, but `_handle_buttons()` runs first and its handlers are not gated: pressing **B**
mid-window does `_save_config()` (flash write) + `_flash_leds()` → `lights.write()` — the
IRQ-disabling WS2812 write documented as the cause of field bug 2 — and **A** toggles lvgl
flags. Failure: user fidgets B during their own 5 s swap → GATT link starved → swap fails
sporadically. Fix: while `_exchanging`, record edges but defer (or ignore) A/B actions.

### F-8 · MINOR · All persistent writes are truncate-then-write; a power-off mid-write loses the whole file
`fri3d_friends.py:284-292` (`_save_config`), `:1155-1162` (`_store_contact`),
`web_portal.py:365-378` (`_save_config`). `open(path, "w")` truncates in place; battery dying
mid-write leaves invalid JSON, and every loader silently returns `{}`/`[]` — i.e. **all
collected contacts, or the whole config, vanish without a message**. The contacts file is the
product of the entire camp. Fix: write to `path + ".tmp"` then `os.rename()` (atomic on
LittleFS/FAT in MicroPython).

### F-9 · MINOR · Portal restart can silently die (no rebind retry), and open connections survive `stop()`
`web_portal.py:169-189`. `stop()` calls `server.close()` but cannot await `wait_closed()`, and
per-connection handler tasks are not cancelled. On a quick pause→resume the new
`start_server` can hit `EADDRINUSE`; `_serve` swallows it (`self._server = None`) and the task
ends — the portal is dead while the badge footer keeps showing a live-looking URL
(`url()` works regardless). No retry exists. Also, handlers blocked in `readline()` (browser
speculative connections send nothing) are held forever — see F-14. Fix: retry the bind a few
times in `_serve`; surface failure via `url()` returning None or an error state.

### F-10 · MINOR · `_ntp_busy` can stick True forever; stale "first tick tries immediately" comment
`fri3d_friends.py:1056-1072`. `_ntp_busy = True` is set, then `_thread.start_new_thread` is
tried with `TaskManager.create_task(self._ntp_sync())` as the except-fallback — if *that* call
also raises (TaskManager quirk, allocation failure), the exception is swallowed by the loop's
per-frame guard with `_ntp_busy` still True → **NTP resync permanently disabled** for the
session (clock drifts, `received_at` timestamps drift). Cheap fix: wrap the dispatch in
`try/except` that clears `_ntp_busy` on failure. Also `:1057` still says "first tick tries
immediately" while `onResume` (`:829`) deliberately defers the first sync — stale comment.

### F-11 · MINOR · Client disconnects without confirming its envelope write reached the server
`contact_exchange.py:497-502` (`_run_client`):
```python
self._ble.gattc_write(self._conn, self._h_write_remote, envelope, 1)
...
await asyncio.sleep_ms(150)
self._safe_disconnect()
```
The write is issued with response mode (1) but `_IRQ_GATTC_WRITE_DONE` (already seeded in
`_E`) is never awaited — a fixed 150 ms nap races the ATT round-trip. On a congested radio the
client disconnects before the write completes: the client stores the server's contact, the
server gets nothing ("No one swapping nearby") — a confusing asymmetric swap. Fix: wait for
write-done (bounded by `deadline`) before disconnecting.

### F-12 · MINOR · Portal-saved numbers are unvalidated: `banner_ms=0`/negative makes every banner (including "Config saved ✓" and swap results) invisible
`web_portal.py:128-131` coerces `banner_ms` to any int; `fri3d_friends.py:270-273` accepts it;
`_show_banner` (`:719-724`) then sets `_banner_until = now + banner_ms` which the loop hides on
the next tick. A typo (`-5000`, `0`) disables all user feedback with no hint why. Same for
out-of-range `rssi_floor` — though that one is re-validated app-side (`_validate_floor`).
Fix: clamp `banner_ms` to a sane floor (e.g. ≥500) on load or save.

### F-13 · MINOR · `onDestroy` doesn't stop the portal
`fri3d_friends.py:865-873`. `onPause`/`onStop` call `_stop_portal()`; `onDestroy` calls
`_stop_task`/`_teardown_ble`/buzzer-deinit but not `_stop_portal`. If the OS ever destroys
without a prior stop (or a future refactor reorders), the server task leaks holding port 8080.
Cheap symmetry fix.

### F-14 · MINOR · No request timeouts and unbounded header loop — any LAN client can pin memory or crash the badge
`web_portal.py:264-289`. `readline()`s have no timeout (idle sockets hold a handler task +
buffers forever — browsers open speculative connections that send nothing), and the header
loop accepts an unlimited number of header lines into a dict (a hostile LAN client can stream
headers until the badge OOMs). The stated trust model covers *access*, not availability, but an
OOM hard-crash is cheap to prevent: cap header count (~32) and wrap the read phase in
`asyncio.wait_for(…, 10 s)`.

### F-15 · INFO · Company ID never checked in either beacon parser
`ble_proximity.py:175-179`, `contact_exchange.py:89-93`. Both parsers skip the 2-byte company
field and gate on the 4-byte magic alone. Harmless in practice (magic + version is 5 bytes of
gate), but the wire spec in DESIGN §3 documents company `0xFFFF` as part of the format — either
check it or note it as ignored-by-design.

### F-16 · INFO · Exchange rendezvous edge cases: window-edge overlap and 3-badge ambiguity
`contact_exchange.py:399-435`. Everything is bounded by one shared `deadline` — a rendezvous at
4.9 s leaves ~100 ms for connect+discover+read+write and fails cleanly (acceptable, by design).
With **three** badges swapping simultaneously the pairing is undefined: a third badge's
`central_connect` can land after a role decision, `_conn` is a single slot, and whichever link
event fires last wins; the losers time out gracefully. Non-crashing (verified by code paths),
but worth a DESIGN.md note since camp groups will try it.

### F-17 · INFO · During the window, MYINFO is readable by any connecting central — not only a Y-pressing peer
`contact_exchange.py:299-320`, `:254-263`. The beacon is connectable and the server accepts any
central; a nearby scanner (e.g. nRF Connect) can connect during your 5 s window and read your
contact envelope without ever advertising HXCG, while you see "No one swapping nearby". Within
the deliberate-share trust model (you pressed Y intending to hand this data to whoever is
nearby), but worth documenting.

### F-18 · INFO · Dead code accumulating
`fri3d_friends.py:209/904` — `_finishing` is initialised and checked but **never set**; the
loop-break is unreachable (B no longer exits; X is OS-handled). `ble_proximity.py:436` —
`notified` written, never read (flagged in the P0-4 review, still present).
`contact_exchange.py:481-483` — `_disc`/`_chars` assigned, never used. All inert; remove for
clarity.

### F-19 · INFO · Splash screen (and its PNG buffer) retained for the app's lifetime; image data GC-safety rests on the binding
`fri3d_friends.py:590-597`, `:615-622`. After `_enter_main` swaps screens, `_splash_scr` is
never deleted — widgets + the ~8.7 KB PNG stay allocated. Additionally the `lv.image_dsc_t`
data buffer (`logo` bytes) has no Python-side reference kept after `_build_splash` returns; the
pattern is copied from `org.fri3d.hwtest` and is field-verified on all three badges, so the
binding evidently holds a reference — but `self._splash_logo = logo` would be free insurance.

### F-20 · INFO · `_refresh_portal` calls `url()` → `WifiService.get_ipv4_address()` every 30 ms tick
`fri3d_friends.py:1192-1217`, `web_portal.py:204-213`. Unthrottled OS/network query per frame;
all other refreshers are throttled. Add a ~2 s gate for symmetry (perf hygiene, no observed
harm).

## 3. Plan vs. Implementation

No `Implementation_Plan_*.md` exists for this phase. `PLAN.md` §9.1's gated phases ended at
Phase 5 (integration/polish) and do not cover the contact exchange, portal, splash, clock, or
LEDs — those were specified incrementally in `DESIGN.md` §7–§11 and the changelog, which this
review treats as the de-facto plan. That documentation trail is unusually good: every feature
listed in the changelog exists in code, and every deliberate deviation is written down.

| Plan item | Planned (changelog/DESIGN) | Actual | Status |
|-----------|---------------------------|--------|--------|
| Files | `contact_exchange.py`, `web_portal.py` new; rename to `fri3d_friends.py`; TTF + 2 PNGs; 2 new test files | Exactly as listed | **Matches** |
| Contact swap protocol | HXCG window/roles/GATT/MTU/cap per DESIGN §9 | Implemented as specified | **Matches** |
| Re-entrancy + starvation fixes | one-shot MTU/service flags; loop quiescent during swap | Implemented; one gap (F-7: button actions not paused) | **Matches (1 gap)** |
| Portal | routes/PIN/lockout/session per DESIGN §10; safe in-place reload | Implemented; input-handling defects F-1/F-2/F-3 | **Matches (defects)** |
| Per-swap contacts | append-only, cap 200, alias kept | `add_received` exact | **Matches** |
| Rebrand | package id, class, module rename; handle removed everywhere | Complete; no `handle` leftovers found | **Matches** |
| Testing strategy | host pytest for pure halves; on-device round-trips for radio/UI | 57 host tests present; changelog documents extensive on-device verification (3 badges, cross-model swap) | **Matches** |
| PLAN §10 checklist invariants | teardown airtight, unconfigured silent, wrap-safe ticks | Mostly held; F-4/F-5 open teardown holes | **Partial** |

**Undocumented deviations:** none material. (The unconfigured-badge Y-press behaviour, F-5, is
the one place code contradicts the written docs rather than extending them.)

## 4. Edge Cases & Safety

- **Hostile HXCG adverts / GATT writes:** `parse_exchange_adv` bounds-checks every field and
  never raises (fuzz-tested by truncation in tests); THEIRS writes are capped by
  `gatts_set_buffer(…, 600)`; `parse_contact_envelope` rejects non-dict/garbage and
  `_coerce_fields` sanitises keys/values to strings. A malicious peer's payload reaches disk
  only as clean `{str: str}`. ✔
- **Hostile data → portal HTML:** received contacts are rendered exclusively in element-content
  context with `&<>"` escaped — no stored-XSS path found from a BLE peer to the operator's
  browser. ✔ (The attribute-context gap F-2 is reachable only by owner-entered values.)
- **Power loss during writes:** F-8 — whole-file loss of contacts/config possible, silently.
- **Corrupted `contacts.json`/`config.json` on load:** both loaders default cleanly (empty
  list/config + hint). ✔ — but that same silence is what makes F-8 a total, unnoticed loss.
- **Unconfigured badge:** proximity stays fully silent (config template ships empty — D-6 fix
  intact) ✔; the Y-press hole is F-5.
- **Envelope size:** name always survives; cap loop terminates (pops until `keys` empty);
  oversized *name* alone can exceed 500 B only if the owner sets a >480-char name — the server
  buffer (+100) still fits typical cases; portal imposes no length caps (F-12 family).
- **Swap during low battery / mid-window exit:** F-4.
- **`decide_role`:** deterministic, symmetric, equal-MAC degenerate case handled; both sides
  derive addresses from the same NimBLE representation (field-verified cross-model). ✔

## 5. Concurrency & Platform Issues

- **Single asyncio loop discipline is well kept:** portal handlers, exchange task, main loop
  and NTP dispatch all cooperate; the portal's `on_change` correctly defers config application
  to the main loop (`_reload_pending`), and `_apply_reload` re-defers when a swap is active. ✔
- **IRQ hygiene:** proximity IRQ still enqueue-only into a bounded (256) list — the D-2 fix is
  intact. The exchange IRQ does more (JSON parse on read-result/write, `gatts_read` in
  handler) but in NimBLE's scheduled (soft) context with tiny payloads — acceptable, same
  judgement as the old D-9. `_discover_client`'s temporary IRQ wrapper is restored on the same
  task; the window owns the radio, so handler swapping cannot race the proximity module
  (suspended, and `resume()` reinstalls unconditionally). ✔
- **Suspend/resume:** correct without `active(False)`; `resume()` restores IRQ + beacon +
  re-arms the scan, and safely no-ops after `end()` (the `self._ble is None` check is what
  keeps F-4 from also re-enabling advertising after exit). `_pending` isn't cleared on suspend,
  so ≤5 s-stale scan results get stamped with a fresh `last_seen_ms` on resume — negligible.
- **`_exchanging` flag:** set/cleared in `try/finally` (`_do_exchange`) — cannot stick. ✔
  The pause skips *all* periodic work including `lights.write()` — F-7 is the one leak.
- **Threading:** `_thread.start_new_thread` for NTP is the only real thread; it touches only
  `ntptime` and a bool flag — safe. Fallback path runs the blocking call on the loop (brief
  UI hitch, bounded by ntptime's ~1 s socket timeout) — acceptable degradation.
- **lvgl usage:** consistent with the platform facts — `remove_flag`/`add_flag` only,
  `LONG_MODE.*` enums, no transform-scaled scrolling labels, per-frame re-renders gated by
  change-caching (`_friends_last`, `_batt_last`, `slot["last"]`, `_led_last`). ✔
- **Wrap-safety:** every new time-gate audited (`_led_next_ms`, `_batt_next_ms`,
  `_clock_next_ms`, `_next_ntp_ms`, `_banner_until`, `_led_override_until`, `_next_rearm_ms`,
  exchange `deadline`, portal `_elapsed`/`time_add`) — all `ticks_diff`/`ticks_add`. D-7 stays
  fixed. ✔ (LED phase uses raw `now % LED_BREATHE_MS` — cosmetic one-frame jump at wrap only.)
- **2024/2026 buttons:** polarity correct per board (active-low GPIO vs active-high expander);
  2024 grabs GPIO 0/38/45 as pull-up inputs for diagnostics — X (38) is also the OS quit pin;
  input-with-pullup is what the OS uses, no conflict observed in the field.

## 6. Error Handling

- `run_window` is exemplary: each setup call individually guarded with a `dbg` trace,
  `finally` guarantees window teardown + `proximity.resume()`, and flags are only set on
  success so a partial first failure self-heals on the next window. ✔
- The main loop keeps the D-1 per-frame guard (exception skips a frame, never kills the loop). ✔
  The flip side stands: `except Exception: pass` everywhere means real breakage (portal dead
  per F-9, NTP stuck per F-10, save failures) is invisible — there is no debug counter or log
  hook outside the exchange's `dbg`. Same accepted trade-off as P0-4, now with more surface.
- Portal handlers: every route ends in `_send`; handler exceptions are contained per-connection
  and the writer is closed in `finally`. ✔ `POST /save` failure redirects `/?saved=0` but the
  page renders no error for it — the user sees nothing (cosmetic gap).
- `_do_exchange` reports "Swap failed" on exceptions and "No one swapping nearby" on timeout —
  good user-visible failure states. ✔

## 7. Code Quality

- **Strong:** the pure/radio split now spans three modules and pays off — 57 host tests with no
  device; `contact_exchange.py`'s protocol comments and `web_portal.py`'s trust-model header are
  the kind of documentation that makes review possible; DESIGN.md was updated in lockstep with
  every feature (rare and valuable).
- **Dead code (F-18):** `_finishing` (now vestigial), `notified`, `_disc`/`_chars`.
- **Test gaps:** no tests for `_url_unquote` with multi-byte UTF-8 (would have caught F-1) or
  `_esc` quote handling (F-2); no fake-BLE state-machine test for `run_window` role/timeout
  paths (the riskiest logic in the phase — testable with a stub BLE object); the radio-wrapper
  host tests mentioned in `Code_Review_Fixes_20260708.md` §Verification are not in `tests/`
  (apparently run ad-hoc, not committed). `fri3d_friends.py` has no tests — riskiest untested
  logic: `_drain_arrivals` banner-window interplay (F-6) and `_apply_reload` transitions (F-6/
  unconfigured↔configured edges).
- `exch.log` diagnostic append is unbounded (`fri3d_friends.py:1095-1100`) — the changelog
  already tracks trimming it; do it before the camp (flash wear + eventual FS-full turns every
  swap into a slow failure path).

## 8. Summary

| Category | Critical | Major | Minor | Info |
|----------|:--------:|:-----:|:-----:|:----:|
| Spec conformance | 0 | 1 (F-5) | 2 (F-6, F-7) | 2 (F-15, F-17) |
| Plan conformance | 0 | 0 | 0 | 0 (deviations documented) |
| Correctness | 0 | 2 (F-1, F-2) | 2 (F-11, F-12) | 1 (F-16) |
| Safety / data integrity | 0 | 1 (F-3) | 1 (F-8) | 1 (F-19) |
| Concurrency | 0 | 1 (F-4) | 1 (F-14) | 1 (F-20) |
| Error handling | 0 | 0 | 3 (F-9, F-10, F-13) | 0 |
| Code quality | 0 | 0 | 0 | 1 (F-18) + test gaps (§7) |

## 9. Recommendation

**GO — the phase stands, with conditions before camp-scale field use.** The BLE work (exchange
protocol, re-entrancy fix, starvation fix, suspend/resume) is solid and matches its
documentation; nothing here requires redesign. The pre-camp conditions are the three portal
input bugs, because they corrupt real user data through the *recommended* workflow:

1. **F-1** — UTF-8-correct percent-decoding in `_url_unquote` (accented names/groups are
   table stakes at a Flemish camp; portal-saved groups currently break matching). *(pre-camp)*
2. **F-2** — escape `'` in `_esc` (apostrophe names corrupt on re-save). *(pre-camp)*
3. **F-3** — read the full `Content-Length` body (large saves silently drop fields). *(pre-camp)*

**Strongly recommended:**
4. **F-4** — track + cancel the exchange task in `_stop_task`.
5. **F-5** — gate Y on `_unconfigured` (one line) and/or fix `end()`'s early return.
6. **F-8** — atomic temp+rename writes for `contacts.json`/`config.json` (the contacts file is
   the camp's takeaway artifact).

**Nice-to-have:** F-6/F-7 (banner/button polish), F-9/F-10/F-12/F-13/F-14 (robustness), F-18
dead-code sweep, `exch.log` trim, and host tests for `_url_unquote`/`_esc`/`run_window` to lock
the fixes. Re-run `pytest tests/` (blocked by tooling this session; expected 57/57) as part of
applying any of the above.

The documentation trail (DESIGN.md §7–§11 + changelog) deserves explicit credit: every
deviation this review checked was already written down, which is why a phase this large
produced zero *undocumented* surprises.
