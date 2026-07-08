# Group Nametag + Proximity Finder — Implementation Plan

**Status:** planning complete, not yet implemented. This document is a
self-contained handoff — everything a developer or LLM needs to build the
app without access to the conversation that produced it.

## 1. What this is

A MicroPython app for the **Fri3d Camp 2024 badge** ("Badge 2024", ESP32-S3)
that works as:

1. An **animated nametag**: shows a group's logo and the wearer's name.
2. A **proximity finder**: uses Bluetooth Low Energy to detect other badges
   running this same app that share at least one *group* with the wearer,
   and alerts when one comes within range — "who from my groups is
   nearby." A member may belong to **multiple groups**; a badge sharing
   *any* of those groups triggers an alert.

It is **generic and redistributable**: any hackerspace/makerspace can flash
it onto their own Fri3d badges, set their own group name(s), member name,
and logo — via plain file edits, no code changes — and it will find *their*
people, filtering out badges from other groups. It was initially scoped
around a specific group ("Makerspace Baasrode") but has been deliberately
generalized so the group name(s), member identity, and logo are all
per-install configuration, not hardcoded.

It must coexist with this project's existing badge apps
(`../fri3dbadge2024/app/name_badge/`, a neon-arcade name badge; `../fri3dbadge2024/app/neon_launcher/`,
the app-picker that replaces the stock launcher) and be selectable from
that same picker — not replace it, not modify it.

## 2. Target hardware & firmware facts (verified, load-bearing)

These are hard constraints this plan was designed around — verify still
true before implementing if this doc is picked up much later.

- **Board**: Fri3d Camp 2024 Badge, ESP32-S3-WROOM-1 N16R8V (16 MB flash,
  8 MB PSRAM), MAC-addressed, USB-Serial/JTAG on `/dev/ttyACM0`.
- **Display**: GC9307 (ST7789-compatible), **296×240**, driven via lvgl
  (built into the firmware).
- **LEDs**: 5× WS2812 on GPIO12, exposed as `fri3d.badge.leds.leds`
  (`leds[i] = (r,g,b)`, `leds.fill((r,g,b))`, `leds.write()`, `leds.n == 5`).
- **Buzzer**: GPIO46, exposed as `fri3d.badge.buzzer.buzzer`
  (`buzzer.freq(hz)`, `buzzer.duty_u16(v)`, `0` = silent).
- **Buttons**: A/B/X/Y/MENU/START, exposed as `fri3d.badge.buttons.buttons`.
  **Read raw pin level for held-state**, not the debounced `.value()` (it
  auto-clears ~200 ms after press — edge-oriented). Pattern used by both
  existing apps:
  ```python
  def _held(name):
      b = getattr(buttons, name, None)
      return b is not None and b._pin.value() == 0   # pull-up: 0 = pressed
  ```
- **MicroPython**: v1.23 fork ([`badge_2024_micropython`](https://github.com/Fri3dCamp/badge_2024_micropython)),
  board `FRI3D_BADGE_2024`.
- **Bluetooth is compiled in and central+peripheral capable.** Confirmed by
  reading the actual board build config in the repo:
  - `ports/esp32/boards/FRI3D_BADGE_2024/mpconfigboard.cmake` includes
    `boards/sdkconfig.ble` in `SDKCONFIG_DEFAULTS`.
  - `boards/sdkconfig.ble` sets `CONFIG_BT_ENABLED=y`,
    `CONFIG_BT_NIMBLE_ENABLED=y`, `CONFIG_BT_CONTROLLER_ENABLED=y`.
  - `ports/esp32/mpconfigport.h` sets `MICROPY_PY_BLUETOOTH (1)`,
    `MICROPY_BLUETOOTH_NIMBLE (1)`, and critically
    `MICROPY_PY_BLUETOOTH_ENABLE_CENTRAL_MODE (1)` — i.e. the MicroPython
    `bluetooth` module (standard `bluetooth.BLE()` API, IRQ-based) supports
    scanning as well as advertising.
  - No `fri3d` package wrapper exists for BLE — talk to `bluetooth.BLE()`
    directly.
- **Image decoders are compiled into lvgl.** Confirmed in
  `fri3d/lvgl_esp32_mpy/binding/lv_conf.h`: `LV_USE_TJPGD 1` (JPEG),
  `LV_USE_LODEPNG 1` (PNG), `LV_USE_GIF 1` (GIF decoder present but **not
  used** — logos are static PNG/JPEG this iteration, see §7/§11). **However**
  lvgl's filesystem
  layer (`lv_fs`) is *not* enabled for any real filesystem — only
  `LV_USE_FS_MEMFS 1` — `LV_USE_FS_STDIO/POSIX/FATFS/LITTLEFS` are all `0`.
  This means: decoding must happen from an **in-memory byte buffer**
  handed to lvgl (`lv.image_dsc_t`), not from an `lv_fs`-style path string
  like `"S:logo.png"`. This exact in-memory-decode call pattern is
  **unverified on this MicroPython binding** — see §7 Risks, spike this
  first.
- **A known-working raster image technique already exists** in this repo:
  `../fri3dbadge2024/app/name_badge/art.py`'s `fb_image()` wraps a raw RGB565 byte buffer
  in `lv.image_dsc_t` and it's been verified rendering on real hardware:
  ```python
  lv.image_dsc_t({
      "header": {"cf": lv.COLOR_FORMAT.RGB565, "w": w, "h": h},
      "data_size": len(buf),
      "data": buf,
  })
  im = lv.image(scr)
  im.set_src(dsc)
  im.set_pos(x, y)
  im.set_scale(256)   # 256 == 1.0x
  ```
  This is the **fallback** raster path if raw-PNG/JPEG-buffer decoding
  doesn't work out (§7).

## 3. Framework conventions to reuse (read these files before coding)

This app must follow the existing `fri3d.application` framework exactly
like the two existing apps do — reference implementations:

- `../fri3dbadge2024/app/name_badge/name_badge.py` — full example of: screen setup/wipe,
  label/rect helpers, raw-button polling, LED/buzzer helpers, an
  intro-then-loop `start()`/`stop()` structure, keeping image descriptors
  alive against GC (`self._keep.append(...)`).
- `../fri3dbadge2024/app/name_badge/art.py` — the `fb_image()` raster helper (§2) and a
  палитра of RGB565 colors.
- `../fri3dbadge2024/app/neon_launcher/neon_launcher.py` — the app-picker; **no changes
  needed here** — it lists every non-hidden app `AppManager` discovers, so
  a new app just needs a valid `app.json` in a scanned path.
- `../fri3dbadge2024/app/name_badge/app.json` and `../fri3dbadge2024/app/main.py` — the per-app config
  and boot-entry conventions.

Key framework facts, read directly from the framework source in
`../fri3dbadge2024/repos/badge_2024_micropython/fri3d/fri3d_application/src/payload/fri3d/application/`:

- **`App` base class** (`app.py`): subclass it, implement `async def
  start(self)` (required) and `async def stop(self)` (optional cleanup).
  `self.config` is a read-only property returning exactly the `"config"`
  dict from that app's `app.json` — **this is the per-install
  configuration mechanism to use for groups/name/handle/etc.**, no need to
  invent a separate config-file parser.
- **`AppManager`** (`app_manager.py`): on `.scan()`, walks
  `/remote/fri3d/apps`, `/remote/user`, `/fri3d/apps`, `/user`,
  `/sdcard/user`; any subfolder containing `app.json` becomes an app,
  keyed by its dotted path (e.g. `user.group_nametag`). It reads `app.json`
  once at boot — **editing `app.json` requires a device reset to take
  effect** (no hot-reload).
- **`app.json` schema**: `{"name": <menu label>, "cls": <class name to
  import from the package>, "hidden": <bool>, "config": {...}}`.

## 4. New project layout

Everything for this app lives in this folder
(`fri3dbadge-group-nametag/`, now a sibling of the `fri3dbadge2024/`
project rather than nested inside it), independent of that project's
`app/` and `tools/` trees, which it references for conventions only:

```
fri3dbadge-group-nametag/
  PLAN.md                    (this file)
  README.md                  quick-start for any group adopting the app
  DESIGN.md                  BLE protocol spec + open-field range / link
                             budget + rssi_floor guidance table (§6.3)
  app/
    group_nametag/
      app.json                 per-group/member config (see §5)
      __init__.py               from .group_nametag import GroupNametag
      group_nametag.py          the App: idle screen, alerts, buttons, lifecycle
      ble_proximity.py          BLE advertise/scan + group-aware state machine
      logo.png                  placeholder logo; each group replaces this file
  tools/
    convert_logo.py           fallback host-side (Pillow) image → RGB565 .bin
                               converter, used only if on-device decode (§7
                               risk 1) doesn't pan out
  tests/
    test_ble_proximity.py    off-device pytest: round-trip encode/decode,
                              fnv1a_16 hashing, dedup/sort, set-intersection
                              matching, UTF-8-boundary truncation, version
                              rejection, malformed-packet dropping (every
                              unit-test bullet in §10). Imports the *pure*
                              wire-format functions from ble_proximity.py, so
                              that module must keep `import bluetooth` /
                              `fri3d` / `lvgl` out of the pure functions'
                              import path (import them lazily inside the
                              radio/IRQ wrappers) — else the host can't load it.
    conftest.py              sys.path shim so the host test can
                              `import ble_proximity` straight from
                              app/group_nametag/ without a full device tree.
```

On-device this uploads to `/user/group_nametag/...`. Use this project's
existing `../fri3dbadge2024/tools/badge_run.py upload <local> <remote>` (or
ViperIDE / mpremote) — it takes independent local/remote paths, so this folder's
location doesn't need to mirror the device path.

## 5. Config schema (`app.json`)

```json
{
  "name": "NAMETAG",
  "cls": "GroupNametag",
  "hidden": false,
  "config": {
    "groups": ["My Hackerspace", "Ham Radio ON4"],
    "name": "Alex",
    "handle": "YOURCALL",
    "rssi_floor": -120
  }
}
```

Note the two distinct `name` keys: the **top-level** `"name"` (here
`"NAMETAG"`) is the label shown in the app-picker menu; `config.name` (here
`"Alex"`) is the wearer's display name. They are unrelated.

- `groups` — a list of one or more free-text group names this member
  belongs to. Each is hashed locally into a 2-byte ID (never transmitted
  as text — see §6). Two badges alert on each other when their group sets
  **intersect** — i.e. they share at least one group. A member in a single
  group is just a one-element list (`["My Hackerspace"]`). More groups
  means a shorter name budget on the wire (2 bytes each — see §6.1), so a
  documented `MAX_GROUPS` (≈5) caps the list.
- `name` — display name, shown big on the idle screen and in proximity
  alerts.
- `handle` — optional secondary line (e.g. a callsign/nickname); may be
  empty string.
- `rssi_floor` — optional integer dBm; **default `-120` (disabled)** when
  absent. Discards any advertisement weaker than this before it reaches the
  proximity state machine — a coarse range limiter (see §6.3). Leave it at
  `-120` for full-range detection; raise it (e.g. `-80`) to only alert on
  badges physically close by. Per-install like everything else here: no
  code edit, just a number in `app.json` + a device reset.

**Provisioning a new group or member — no code edits, ever:**
1. Copy the `app/group_nametag/` folder onto the badge — one copy per badge
   is all you need; only rename it if you deliberately want several configs
   side by side.
2. Edit `groups`, `name`, `handle` in `app.json`.
3. Replace `logo.png` with the group's own logo image (see §7 for size
   guidance).
4. Upload the folder to `/user/group_nametag/` on the badge, reset.

## 6. BLE protocol (`ble_proximity.py`)

Isolate all BLE and wire-format logic from UI code so the encode/decode
functions are unit-testable on a host with plain `bluetooth`-module-free
Python (mock the byte-manipulation functions independently of the actual
`bluetooth.BLE()` calls).

### 6.1 Advertising payload

Non-connectable legacy advertising (31-byte budget). One manufacturer-
specific AD structure (AD type `0xFF`):

| Field | Size | Notes |
|---|---:|---|
| AD length | 1 | standard AD structure header |
| AD type | 1 | `0xFF` (Manufacturer Specific Data) |
| Company ID | 2 | placeholder `0xFFFF` (reserved/testing range — this is a hobby beacon, not seeking an assigned company ID); little-endian per BLE spec |
| App magic | 4 | fixed ASCII `b"HSNT"` ("Hackerspace NameTag") — identifies *any* badge running this app, regardless of group |
| Version | 1 | wire-format version, currently `0x01`. Receivers **must drop** adverts whose version they don't understand — lets a future format (e.g. the §11 scan-response extension) coexist with v1 badges |
| Group count | 1 | `G` = number of group IDs that follow (1…`MAX_GROUPS`) |
| Group IDs | 2×G | one `fnv1a_16(...)` per group the wearer belongs to, **little-endian**, **deduplicated and sorted ascending** — see §6.2 |
| Name length | 1 | length in bytes of the following field |
| Name | ≤N | UTF-8 `name` (+ optionally `" " + handle`), **truncated on a UTF-8 character boundary** (never mid-codepoint) to the remaining budget. Primary budget ≈ `20 − 2×G` bytes — we emit **no** Flags AD structure (a non-connectable beacon doesn't need one, and MicroPython's `gap_advertise` sends exactly the bytes given): 31 − 2 mfg-header − 2 company − 4 magic − 1 version − 1 group-count − 2×G groups − 1 namelen. Adding a 3-byte Flags AD later would reduce this to `17 − 2×G`. |

Advertise with `gap_advertise(interval_us, adv_data=<payload>,
connectable=False)`.

- **Advertising interval**: `ADV_MS ≈ 250` (tunable) — fast enough that a
  new arrival is picked up within a scan window or two, slow enough to be
  easy on the battery. The 30 s eviction window (§6.3) is deliberately many
  advert-intervals long, so brief misses don't drop a still-present peer.
- **Stable identity**: set a **public/static** device address
  (`BLE.config(addr_mode=0x00)` — verify the exact enum on this build) so a
  badge keeps *one* address for the whole session. If the address rotated
  (resolvable/non-resolvable private address), the same badge would reappear
  under new keys and re-alert endlessly, and the nearby list would fill with
  ghosts. Receivers key their table on `(addr_type, addr)` accordingly
  (§6.3). See §9 spike 3 for the expected default and the documented
  fallback if a stable address can't be set.

### 6.2 Group hashing

```python
def fnv1a_16(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return (h ^ (h >> 16)) & 0xFFFF
```
Hash **each** group name separately, `.strip().lower()`-normalized first
so trivial formatting differences between two members typing the same group
name still match. Deduplicate the resulting IDs and sort them ascending
before packing them into the advertisement — this keeps the payload
deterministic (important for the round-trip unit tests) and collapses
accidental duplicate group entries. If more than `MAX_GROUPS` distinct IDs
remain after dedup, keep the `MAX_GROUPS` **lowest** IDs (deterministic, so
both ends agree) and log a warning — a member in more groups than fit is an
edge case, and the alternative of silently varying which groups advertise
would be worse.
Document in `DESIGN.md` that this is a **collision-tolerant identifier,
not a security mechanism** — 16 bits is plenty at the scale of "a few dozen
hackerspaces show up to the same camp," not adversarial-safe.

### 6.3 Scanning & state machine

- Foreground-only: `gap_scan()` runs only while this app's `start()` loop
  is active; call `ble.active(False)` in `stop()` to fully release the
  radio.
- Duty-cycled, not continuous: e.g. **passive** scan ~1.5 s every ~4 s
  (tunable constants), leaving headroom for the render loop and simultaneous
  advertising. Passive (not active) scanning is correct here: the beacon is
  non-connectable and sends no scan response, so active scanning would only
  waste airtime/power for nothing. Worst-case detection latency for a new
  arrival is therefore about one duty cycle (~4 s) — comfortably inside the
  30 s presence window.
- `IRQ_SCAN_RESULT` handler: parse the manufacturer AD **defensively** —
  anyone can broadcast 31 arbitrary bytes, so bounds-check every length
  field (`group_count`, `name_length`) against the actual received buffer
  before slicing, and drop anything malformed. Verify the 4-byte magic, then
  the 1-byte version (`0x01`; unknown version → drop). Unpack the advertised
  list of group IDs (little-endian) and intersect it with this badge's own
  group-hash set (`{fnv1a_16(g) for g in config.groups}`). **Empty
  intersection → silently dropped** (this is the "find your own people"
  filter). Key the table on the peer's **stable identity** `(addr_type,
  addr)` (§6.1). On a non-empty intersection, update a table:
  ```python
  seen[addr] = {
      "name": <decoded name>,
      "groups": <shared group names, mapped back from the matching
                 hashes via this badge's own name→hash table>,
      "rssi_ewma": <exponential moving average of RSSI, informational>,
      "last_seen_ms": <time.ticks_ms()>,
      "notified": <bool>,
  }
  ```
- **Presence = in BLE range** (per the wearer's decision): a peer counts as
  *present* the moment its first matching advertisement is received — i.e.
  as soon as it is within radio range — and *absent* once it is evicted
  (see below). There is **no distance calibration and no RSSI enter/exit
  hysteresis**; "nearby" means "my radio can hear them." This deliberately
  trades precise range control for zero on-site setup — at a camp, BLE
  range through tents is roughly "same area," which matches the "who from
  my groups is around?" intent.
- **Optional noise floor**: the `rssi_floor` config value (§5, per-install
  in `app.json`) discards packets weaker than the threshold before they
  reach the state machine. **Defaults to `-120` (disabled)** when the key is
  absent, non-integer, or outside a sane range (clamp/validate on load) —
  the ESP32-S3 radio's own sensitivity limit is ~-97 dBm, so nothing
  decodable is ever below -120; every packet the radio manages to receive
  passes, giving full-range detection (matching the "presence = in BLE
  range" decision). Raising it turns it into a coarse distance gate —
  *noise rejection / range limiting, not fine calibration*. RSSI is still
  smoothed into `rssi_ewma` and shown in the detail view for information.
  **TODO (DESIGN.md):** the open-field link-budget estimate and an
  `rssi_floor` guidance table (≈ `-120` = full range ~50–100 m LoS; `-80` ≈
  same tent / ~10 m; `-70` ≈ right next to me) — see the range discussion
  the plan review produced.
- **Eviction**: entries whose `last_seen_ms` is older than a timeout
  (`EVICT_MS = 30 s` of no adv seen) are dropped from `seen` — always
  compare with `time.ticks_diff()`, never raw subtraction, since
  `time.ticks_ms()` wraps. This is what makes a peer go *absent*; because
  advertisements arrive every few hundred ms, a peer that has genuinely left
  clears within one timeout window. The
  generous 30 s window rides over the momentary drop-outs that are normal
  at the fringe of open-field range (see below), so a peer who is still
  around but briefly unheard is not flapped to *absent* and then re-alerted.
- **Debounced notify-once-per-encounter**: the first `absent → present`
  transition for an address (first matching adv while not already in
  `seen`, or re-appearing after eviction) is a "new arrival" event → UI
  alert, then set `notified = True`. Eviction (`present → absent`) drops the
  entry, so a later return re-triggers exactly once. The eviction timeout
  *is* the debounce: a peer hovering right at the edge of range re-alerts at
  most once per timeout window, never once per dropped packet.

## 7. Logo handling (`group_nametag.py`)

**Primary path — on-device decode (spike this first, §9):**
```python
data = open('logo.png', 'rb').read()   # or .jpg — static PNG/JPEG (GIF deferred, §11)
dsc = lv.image_dsc_t({
    "header": {"cf": lv.COLOR_FORMAT.RAW, "w": 0, "h": 0},   # exact cf/flags TBD by the spike
    "data_size": len(data),
    "data": data,
})
im = lv.image(scr)
im.set_src(dsc)
im.set_scale(<computed to fit a target box, e.g. 120x120>)
```
The exact `cf` enum value / whether width+height must be pre-known for the
registered PNG/JPEG decoders to pick up the buffer is **the thing the spike
must determine** — lvgl v9's decoder `info_cb` is supposed to sniff
dimensions from the encoded header, but the exact MicroPython binding
surface (`lv.COLOR_FORMAT.RAW` availability, whether `image_dsc_t` accepts
0/0 dimensions) is unverified here.

**Fallback path — host-side pre-conversion:** `tools/convert_logo.py`
(Pillow, run on a dev machine, not on the badge) resizes/quantizes any
image to RGB565 and writes `logo.bin`, decoded on-device with the
already-verified `fb_image()`-style raw-buffer technique from
`../fri3dbadge2024/app/name_badge/art.py` (§2). Loader logic should try the raw image file
first, and use `logo.bin` if present/if raw decode raises.

**Robustness**: if decoding fails outright (bad file, unsupported format,
memory pressure), fall back to a small bundled placeholder image and log
the failure — a badge with a broken logo file should still boot as a
usable nametag, not crash.

**Size guidance** (document in `README.md`): keep source logos
≲300×300 px and ≲150 KB — the badge has 8 MB PSRAM so this is comfortable
headroom, not a hard limit, but keeps decode time and RAM snappy on a
microcontroller. `set_scale()` is a **uniform** scale, so to fit an
arbitrary (possibly non-square) logo into a target box compute
`scale = min(box_w / img_w, box_h / img_h) × 256` and centre it — that way
exact pixel dimensions and aspect ratio don't matter, the image just fits.

## 8. UI / animation (`group_nametag.py`)

- **Idle screen**: logo centered upper-half, gentle continuous "breathing"
  scale animation (period ~2–3 s, e.g. `set_scale(240 + 16*sin(t))`) plus a
  fade/scale-in intro on `start()` — reuse the same time-based-loop pattern
  `name_badge.py`'s `_show_hobby()` intro animation already demonstrates,
  no new animation framework needed. `name`/`handle` rendered below as
  `lv.label` (Montserrat, reusing the `_label` helper pattern from
  `neon_launcher.py`/`name_badge.py`). A small muted-color line pinned near
  the bottom (`"nearby: Alice, Bob"`), refreshed every frame from
  `ble_proximity`'s current in-range set (empty → hidden or "nobody
  nearby").
  - **Own group(s) line** (feature): a small muted line (e.g. a top corner)
    showing this badge's *own* configured group name(s), so the wearer can
    confirm at a glance who will be able to find them before relying on it.
  - **Battery indicator** (feature): if the fri3d framework exposes battery
    state (verify the API; defer if it doesn't), a small icon/percentage in
    a corner — this is an all-day wearable, so trustworthy battery info
    matters.
  - **First-run / unconfigured hint** (feature): if `config.name` is empty
    or `config.groups` is missing/empty, replace the idle content with a
    clear "Configure me — edit app.json (name, groups)" message and **do not
    start BLE** (advertising nothing is better than advertising an
    un-provisioned badge). The badge still renders as a static placeholder
    nametag — this is the graceful path for a freshly-flashed, not-yet-edited
    copy.
- **Alert overlay**: on a new-arrival event, show a banner (`lv.label` +
  background rect, `name_badge`'s `_rect()` pattern) for ~2.5 s, flash the
  5 NeoPixels (reuse `leds`/`leds.fill`/`leds.write()` as `name_badge.py`
  does), and play a short buzzer sting (reuse `buzzer.freq()`/`duty_u16()` +
  the `_sting()` pattern), then fade back to idle.
  - **Per-group signature — colour + tone** (feature): derive both the LED
    colour and the sting's pitch **deterministically from the group ID**
    (e.g. map the 16-bit group hash to an LED hue and to a note), so each
    group has a recognisable signature and the wearer can tell *which* group
    just arrived without reading the screen. No extra config — it falls out
    of the hash. If the peer shares several of your groups, use the
    lowest-sorted shared group ID so both badges agree on the signature.
  - **Coalescing** (feature): if several arrivals land within one banner
    window, coalesce rather than stacking banners — e.g. "Alice + 2 more
    nearby" (grouped by shared group where practical) with a single
    LED/sting cue.
  The persistent nearby-list line is independent of this transient banner
  and keeps showing everyone currently in range.
- **Buttons** (raw-pin held-state polling, per §2):
  - `X` — toggle sound (mute/unmute alert buzzer, mirrors `name_badge`).
  - `A` — toggle a detail view of the nearby list (name + shared group +
    smoothed RSSI + seconds-since-last-seen per entry). RSSI is shown for
    information / optional `rssi_floor` tuning, not required calibration
    (§6.3).
  - `B` / `START` — return to the app-picker (mirrors both existing apps).
- **Lifecycle**:
  - `start()`: wipe/setup the shared screen (reuse the `_wipe()` pattern —
    `scr.clean()` with a manual child-delete fallback), load the logo
    (§7), read `self.config` for `groups`/`name`/`handle`/`rssi_floor`.
    **If `name` is empty or `groups` is missing/empty**, show the
    unconfigured hint and skip BLE entirely (see idle screen, above).
    Otherwise call `ble.begin(groups, name, handle, rssi_floor)` (sets the
    stable public address, activates BLE, starts advertising at `ADV_MS`,
    launches the periodic passive-scan `asyncio.Task`), then run the main
    poll/render `while True` loop (buttons → alerts → idle animation →
    backlight dim check → `await asyncio.sleep_ms(30)`, matching the ~30 ms
    tick both existing apps use).
  - **Backlight dim-out** (feature): track the last input/alert time; after
    `DIM_MS` (~30 s) of no button press and no new-arrival alert, dim (or
    blank) the backlight to save battery on this all-day wearable; restore
    full brightness on any button press or new-arrival event. Use the fri3d
    display/backlight brightness API (verify the exact call during
    implementation). BLE advertising/scanning continues while dimmed — only
    the screen sleeps, so you're still discoverable and still detecting.
  - `stop()`: cancel the scan task, `ble.end()` (`BLE.active(False)`) so
    the radio is fully released before another app runs, restore the
    backlight to full, clear LEDs/buzzer (mirrors `name_badge.stop()`), drop
    image/descriptor references so GC can reclaim them.

## 9. Build order / spikes

Do these two **before** building the full UI, since both are unverified
assumptions the rest of the design leans on:

1. **Logo decode spike**: paste a standalone script on-device that reads a
   real PNG (and separately a JPEG) file's raw bytes and attempts the
   §7 primary-path decode. Confirm it renders correctly. If it fails after
   reasonable troubleshooting, commit to the `convert_logo.py` fallback
   path instead and adjust `group_nametag.py`'s loader accordingly.
2. **Concurrent advertise+scan spike**: paste a standalone script that
   calls `gap_advertise()` and `gap_scan()` at the same time and confirms
   both work without crashing/hanging the NimBLE stack, over at least a
   few duty cycles. If unstable, fall back to alternating: advertise for a
   window, stop, scan for a window, repeat — instead of running both
   continuously.
3. **Hardware-API probe** (quick, not a full spike): confirm three things —
   the display/backlight **brightness** call used by the dim-out feature
   (§8), whether the framework exposes **battery** state for the idle
   indicator (§8), and the exact `bluetooth.BLE().config()` `addr_mode` enum
   for a **stable public address** (§6.1). Of these, **backlight and battery
   are optional features** — if either API doesn't exist, drop that feature
   and note it in `DESIGN.md`; don't block. The **stable address is not
   optional** — it underpins the no-re-alert-ghosts guarantee (§6.3/§10):
   - **Expected:** ESP32/NimBLE defaults to a **public** address derived
     from the factory MAC, stable for the whole session (and usually across
     reboots). If so, confirm the default and key `seen` on
     `(addr_type, addr)` — done, no explicit config call strictly needed.
   - **Fallback if a public/static address genuinely can't be obtained:**
     this is a real degradation, not a droppable feature. Mitigations, in
     order: (a) the 30 s eviction window (§6.3) already prevents per-packet
     flapping; (b) accept one re-alert per address rotation and document it
     as a **known limitation** in `DESIGN.md`; (c) **flag to the maintainer before
     shipping** — mid-session address rotation fundamentally breaks the
     "one nearby-list entry per peer" UX, so don't pretend it's solved.

Then build `ble_proximity.py` (with its own off-device unit tests for the
pure encode/decode/hash functions), then `group_nametag.py`'s UI on top,
then wire up `app.json` + `__init__.py`, then do the full on-device
integration test.

### 9.1 Phased deliverables

The build order above is the dependency chain; this table packages it into
**gated phases** — each has a concrete deliverable, an exit criterion you
can fail, and the §10 checklist items it closes. Don't start a phase until
the prior phase's gate is green. The three Phase-0 spikes may run in
parallel; phases 1–5 are strictly sequential (each builds on the prior
deliverable).

| Phase | Deliverable | Exit gate (fail it = don't proceed) | Closes (§10) |
|---|---|---|---|
| **0. De-risk** | The three spike scripts + hardware probe; findings written into `DESIGN.md` | Each spike passes **or** its fallback is committed: logo→`convert_logo.py`; concurrent adv+scan→alternating windows; `addr_mode` confirmed-or-degradation-chosen (spike 3, addr note) | logo-decode; adv+scan; hardware-API-probe |
| **1. BLE core + host tests** | `app/group_nametag/ble_proximity.py` (pure encode/decode/hash/intersection + thin radio wrapper) and `tests/test_ble_proximity.py` | `pytest tests/` green on the host (no `bluetooth`/`fri3d`/`lvgl`), covering every unit-test bullet in §10 | Off-device unit tests |
| **2. Logo loader** | Logo load path in `group_nametag.py` (primary or fallback per Phase 0), bundled-placeholder on failure | Renders correctly on device; a deliberately broken logo file boots as a usable nametag, not a crash | (logo integration — completes logo-decode) |
| **3. Idle UI shell (no BLE yet)** | Idle screen: logo + breathing anim, name/handle, own-groups line, first-run/unconfigured hint, button wiring (X / A / B-START), `start()`/`stop()` lifecycle (wipe + GC) | Single-badge smoke renders and animates; `B`/`START` returns to picker; unconfigured badge shows hint and stays silent | single-badge smoke; app-picker (partial); own-groups / unconfigured-hint |
| **4. Alerts + signature** | Alert overlay (banner + LED + buzzer), per-group colour/tone signature, coalescing, A-detail view, X-mute wired to buzzer | An overlapping peer alerts **exactly once** on enter and re-triggers exactly once after eviction+return; signature differs by group; disjoint set ignored; near-simultaneous arrivals coalesce | proximity round-trip; disjoint-ignored; per-group signature; coalescing |
| **5. Integration + polish** | On-device full integration; backlight dim-out + battery indicator (only if APIs exist, per Phase 0); multi-badge field test; write `README.md` + `DESIGN.md` (incl. its queued `rssi_floor` table + link-budget TODOs from §6.3) | Full §10 checklist green; phone scanner confirms advertising **stops** after exit; long-session stable address shows no ghosts; animation/input not stuttered by duty cycle | responsiveness; stable-address; backlight-dim — plus the `README.md` / `DESIGN.md` doc deliverables (not in §10) |

Notes:
- `README.md` and `DESIGN.md` are deliverables landed in **Phase 5**, not
  orphans — they collect everything learned in Phases 0–4 plus the queued
  `DESIGN.md` TODOs (§6.3 `rssi_floor` guidance table + open-field link
  budget).
- "Exit gate" means a concrete, falsifiable check, not "feels done." If a
  gate can't pass, that's a blocker for the phase, not a skip.

## 10. Verification checklist

- [ ] Logo decode spike passes (or fallback path is implemented and used)
- [ ] Concurrent adv+scan spike passes (or sequential fallback implemented)
- [ ] Hardware-API probe: `addr_mode` (stable address), backlight-brightness
      and battery-state calls confirmed — or the dependent feature dropped
      and noted in `DESIGN.md`
- [ ] Off-device unit tests for `ble_proximity.py`: AD payload round-trip
      encode/decode **with 1 and with several group IDs**, `fnv1a_16` group
      hashing (same name → same hash after normalization; different names →
      different hash in practice), per-group hashing + **dedup/sort**
      determinism, group-set **intersection** matching (overlap → match,
      disjoint → no match), name truncation at the (group-count-dependent)
      byte-budget boundary **on a UTF-8 character boundary** (never
      mid-codepoint), version byte round-trips and an **unknown version is
      rejected**, `MAX_GROUPS` overflow keeps the lowest IDs, and a
      **malformed/hostile advert** (bad magic, truncated buffer, length
      fields exceeding the buffer) is dropped without raising
- [ ] Single-badge smoke test: idle screen renders (logo animates, name/
      handle shown); a phone BLE scanner (e.g. nRF Connect) sees the
      advertisement with correct magic/group-hash/name in the manufacturer
      data
- [ ] Proximity round-trip: a phone BLE advertiser (or second badge)
      broadcasting a group set that **overlaps** the badge's own triggers
      the alert exactly once as it comes into range, shows up in the nearby
      list, and leaving (eviction) + returning re-triggers exactly once
- [ ] A **disjoint** group set (no shared group) is correctly ignored (no
      alert, not listed); and a member in several groups is alerted on by
      peers sharing *any* one of them
- [ ] Responsiveness: scan/advertise duty cycle doesn't visibly stutter
      the idle animation or delay button input
- [ ] App appears correctly in `neon_launcher`'s picker and returns
      cleanly to it on `B`/`START` (no leaked BLE activity — verify with a
      phone scanner that advertising stops after exiting)
- [ ] Stable address: over a long session a matching peer stays a **single**
      nearby-list entry and does not re-alert (no address-rotation ghosts)
- [ ] Per-group signature: two different groups produce a visibly different
      LED colour + sting; a peer sharing several groups uses the
      lowest-sorted shared group's signature (both badges agree)
- [ ] Coalesced alert when several peers arrive within one banner window
      (no banner stacking, single LED/sting cue)
- [ ] Own-group(s) line shows this badge's configured groups; an
      **unconfigured** badge (empty `name`/`groups`) shows the config hint
      and does **not** advertise or scan (confirm silent on a phone scanner)
- [ ] Backlight dims after `DIM_MS` idle and restores on button press or a
      new-arrival alert; BLE advertising/scanning keeps running while dimmed

## 11. Explicitly out of scope (for this iteration)

- Any fleet-provisioning tooling beyond copy-folder-and-edit-three-fields
  (fine at hobbyist/single-hackerspace scale; revisit if this sees wider
  adoption).
- Background/always-on detection while another app is in the foreground —
  by design (the wearer's explicit choice): this app's BLE only runs while it's
  the active screen.
- Any authentication/security around group membership — the group hash is
  a convenience filter, not a trust mechanism; anyone can advertise a
  colliding/spoofed payload.
- Scan-response payload extension for longer names / more groups — not
  needed at the current `20 − 2×G`-byte name budget for now; noted as a
  future option in `DESIGN.md` if a member in many groups (or with a long
  name) ever makes the single-adv budget too tight. The 1-byte wire-format
  version (§6.1) is what lets such an extension roll out without breaking
  v1 badges.
- Animated GIF logos — the lvgl GIF decoder is compiled in, but logos are
  static PNG/JPEG this iteration (a GIF needs the `lv.gif` widget and would
  conflict with the breathing animation). Revisit if a group really wants an
  animated logo.
- Persisting the `X`-mute (and backlight brightness) across resets — mute is
  per-session only for now; the first-run hint is the only new persistent-
  config surface added. Revisit if users find re-muting after every reset
  annoying.
