# !Fri3d Friends — nametag, friend finder & contact swap

A MicroPythonOS app for the **Fri3d Camp 2024 & 2026 badges** that does three
things:

1. **Animated nametag** — shows your name large (it scrolls if too long), with
   your group(s) as coloured pills (the colour is unique to each group), a live
   NTP-synced clock (top-left) and battery % (top-right).
2. **Proximity finder** — over Bluetooth Low Energy, it detects other badges
   running this app that share **at least one group** with you, and alerts you
   when one comes within radio range ("someone from my groups is nearby").
3. **Contact swap (Y button)** — press **Y** near another badge whose owner also
   presses **Y** within ~5 s (they need *not* be a friend or in your group), and
   the two badges exchange contact info over a short Bluetooth connection. You
   send your **name** and **group(s)**, plus a free-form set of fields you choose
   (Email, Phone, Website, Discord, bitcoin wallet…). What you receive is stored
   on the badge with the date/time — **one entry per swap**.

A **phone setup page over Bluetooth** (Web Bluetooth) lets you edit your name /
groups / contact fields and view/export received contacts from a phone or laptop
— the badge keyboard is too cumbersome for lots of text. It needs **no WiFi or
network**: the phone talks Bluetooth straight to the badge. Scan the QR the badge
shows to open the page; a 4-digit code on the badge gates access.

It's **generic and redistributable**: any hackerspace/makerspace can flash it and
set their own group name(s) and member name by editing one file (no code changes)
— and it finds *their* people.

## Install

### From the AppStore (recommended)

On your badge, open the **AppStore** app (BadgeHub backend), find **!Fri3d
Friends**, and install. It shows up at the top of the launcher afterwards (the
`!` sorts it first). Updates are offered automatically when a newer version is
published.

### From source (development)

The app folder is `app/com.fri3dcamp.fri3dfriends/`. Copy the whole folder to
`/apps/` on the badge with [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html),
then reset:

```bash
mpremote connect /dev/ttyACM0 cp -r app/com.fri3dcamp.fri3dfriends :/apps/
mpremote connect /dev/ttyACM0 reset
```

(If you added it without rebooting, run `AppManager.refresh_apps()` in the REPL,
or just reboot.)

## Configure (no code edits)

Edit **`/apps/com.fri3dcamp.fri3dfriends/config.json`**:

```json
{
  "groups": ["Makerspace Baasrode"],
  "name": "Alex",
  "rssi_floor": -120,
  "sound": true,
  "banner_ms": 5000,
  "contact": {
    "Email": "alex@example.org",
    "Phone": "+32 0000 000",
    "Website": "example.org",
    "Discord": "alex#1234"
  }
}
```

- **`groups`** — one or more group names you belong to. Two badges alert on each
  other when their group lists **overlap** (any shared group). Each name is
  hashed before broadcast (never sent as text), so type it the same way on every
  member's badge (case/whitespace are ignored). Up to ~5 groups.
- **`name`** — your display name (shown large across the top of the screen).
- **`rssi_floor`** — optional coarse range gate in dBm. Default **`-120`** =
  detect anything the radio can hear (full range). Raise it to only alert on
  badges that are close, e.g. `-80` (≈ same tent / ~10 m) or `-70` (≈ next to me).
  It's a noise/range filter, *not* fine calibration. See **DESIGN.md §5** for a
  fuller dBm→range guidance table and the open-field link-budget estimate.
- **`sound`** — `true`/`false`, whether the arrival buzzer sting plays. Toggled
  with **B** and **persisted** across reboots. Default `true`.
- **`banner_ms`** — how long the "friend arrived" banner stays on screen, in ms.
  Default `5000` (5 s). Values below `500` are ignored (a `0`/negative would hide
  every banner) and fall back to the default.
- **`board`** — optional, `"2024"` or `"2026"` to force the hardware profile
  (otherwise auto-detected). Only needed if autodetection fails.
- **`contact`** — your "my contact info": a free-form object of `"field":
  "value"` pairs (Discord, website, phone, bitcoin wallet, anything). This is the
  data sent to another badge when you both press **Y**. Easiest to edit from the
  **phone setup page** (below) rather than by hand.

## Setup / contacts from your phone (over Bluetooth, no keyboard, no WiFi)

The badge runs a small **Web-Bluetooth setup service** so you can edit everything
above (name, groups, and the free-form `contact` fields) and view/export the
contacts you've received — from a **phone or laptop browser**, over Bluetooth.
No network is involved: the page talks GATT straight to the badge, which is ideal
at Fri3d Camp where badges and phones sit on different SSIDs/subnets.

- **Open the page.** Scan the **QR code** the badge shows (on the first-run
  "Configure me" screen, or in the setup window on a configured badge). It points
  at a static page on GitHub Pages:
  `https://steemandavid.github.io/fri3d-friends/setup/?badge=XXXX`. `XXXX` is the
  badge's unique id, also printed on screen (`Fri3d-XXXX`).
- **Connect.** Tap **Connect** and pick the badge from the Bluetooth chooser. The
  `?badge=XXXX` filter means the chooser shows exactly **your** badge.
- **Unlock.** The badge shows a **4-digit code**. Type it in (prove you can see
  the screen — the Bluetooth-pairing trust model). Five wrong tries lock the badge
  out briefly and rotate the code.
- **Edit & save.** Change your name / groups / runtime settings and add/remove
  free-form contact fields, then **Save** — the badge applies name + contact +
  runtime settings live (group changes need an app restart, as before). Switch to
  the **Contacts** tab to view received contacts and **download** them as JSON.
- **Opening setup on a configured badge:** **hold B for ~1.5 s** (a short B press
  still toggles mute). This opens a **2-minute** setup window (QR + code + a
  countdown); press **A/Y** to close it early. Proximity pauses during the window
  and resumes when it closes.
- **Browsers:** works in **Chrome/Edge** (Android + desktop). **iPhone/iPad**:
  Safari has no Web Bluetooth — the page detects this and points you at the free
  **Bluefy** browser (the same page works there unchanged).
- **International names work:** accented and non-ASCII names/groups (José, Noël,
  L'Atelier, …) are handled correctly. Edits are written **atomically**, so a
  battery dying mid-save won't corrupt your config or wipe your received contacts.
- There is also a headless CLI client, `tools/setup_client.py` (needs
  `pip install bleak`), that speaks the same protocol for testing/scripting.

## Swapping contacts (Y button)

Press **Y** and, within ~5 s, have a nearby badge's owner press **Y** too. The
two badges find each other over Bluetooth and swap their `contact` info in both
directions — **no shared group or friendship required**, just radio range. The
banner confirms `Swapped with <name> ✓`; the received fields are stored on your
badge with the date & time and are visible in the phone setup page's **Contacts**
tab. (If nobody else is swapping in the window you get `No one swapping nearby`.)

> Note: the screen shows your group(s) as **coloured pills** (colour derived from
> the group name). The **!Fri3d Friends logo** (`fri3dfriends.png`) appears on the
> startup splash. The app runs on **both the Fri3d 2024 and 2026 badges**
> (auto-detected; force with the `board` key above).

> **First-run / unconfigured:** if `name` is empty or `groups` is empty, the
> badge shows a **"Configure me" screen with a QR code** of its Bluetooth setup
> page and **does not run the proximity beacon/scan** — safer than beeping as a
> blank badge. It *does* advertise as `Fri3d-XXXX` so a phone can connect and
> configure it. Scan the QR, connect, enter the on-screen code, fill in your name
> and group(s), and save: the badge **switches to the nametag and goes on the air
> immediately** — no reboot needed.

## Controls

| Button | Action |
|---|---|
| **A** | Open the friends-nearby panel (cards: name · shared group · signal bars · dBm · age) |
| **B** | Short press: mute / unmute the alert buzzer (saved). **Hold ~1.5 s:** open the phone-setup window (Bluetooth) |
| **Y** | Swap contacts with another badge nearby (they press **Y** too, within ~5 s) |
| **X** | *(handled by the OS)* quit to the launcher / OS menu |
| **START** | *(unused)* |

At launch a **3-second splash** shows the app name, version, "by David Steeman"
and the Makerspace Baasrode logo, then the nametag appears.

The idle screen shows: your **name** large across the top (it scrolls if too
long), your **group(s)** as coloured pills directly under it, battery %
(top-right, inset from the rounded corner), and a **friends line** —
`Friends nearby: Alice, Bob` when peers are in range, or `looking for friends…`
when none. On a new arrival, a banner appears for ~5 s (`banner_ms`), the LEDs
flash, and a short buzzer sting plays — with a **colour + tone unique to the
shared group** (derived from the group hash, so you can recognise *which* group
just arrived without reading the screen). Several arrivals in one window coalesce
into one banner ("Alice + 2 more nearby"). Press **A** for a per-friend panel
(name, shared group, signal bars, dBm, seconds since last seen); press **B** to
mute.

**Friend LEDs:** each nearby friend also gets their own badge LED, slowly and
dimly **breathing that friend's group colour** (friend 1 → LED 1, friend 2 →
LED 2, …) — a quiet, glanceable "who's around" without looking at the screen.
The name is rendered in a bundled 42px font; long friend names wrap.

## How matching works (short version)

Each badge broadcasts a small BLE manufacturer-data beacon containing a fixed
magic (`HSNT`), a version byte, the hashed IDs of its groups, and its (truncated)
name. A receiving badge decodes the beacon, ignores it unless the group sets
**intersect**, and otherwise adds the peer to a "nearby" table. A peer counts as
present the moment its first matching beacon is heard, and absent once it hasn't
been heard for ~30 s — that 30 s window also debounces the edge-of-range
dropouts. You're alerted **once** per encounter, and again once if they leave and
come back.

The group hash is a 16-bit convenience filter, **not** authentication — anyone
can broadcast a matching payload. That's fine for "who from my groups is around?"
at a camp.

## Background beacon (visible even with the app closed)

A small boot service (`beacon_service.py`, declared in the manifest, started by
the OS at boot) keeps **advertising your beacon while the app is closed**, so
friends' badges still spot you when you're in another app or on the launcher.
Background is **advertise-only**: you don't get alerts, LEDs, or contact swaps
until you open the app — but *they* see *you*.

- The service stays completely off the radio while the app is open (the app owns
  BLE exactly as before, including during contact swaps).
- An unconfigured badge stays silent in the background too.
- It activates on the **next reboot** after installing/updating the app.
- If another app uses Bluetooth while Fri3d Friends is closed, the two may fight
  over the radio; the beacon re-asserts itself within ~30 s.

## Project layout

```
app/com.fri3dcamp.fri3dfriends/   → the app (deploy to /apps/…)
  MANIFEST.JSON, fri3d_friends.py, ble_proximity.py (proximity beacon),
  beacon_service.py (background beacon boot service),
  contact_exchange.py (Y-button GATT swap), ble_setup.py (Web-Bluetooth setup GATT service),
  config.json, fri3dfriends.png (splash logo), icon_64x64.png (launcher icon),
  montserrat_name.ttf (42px name font)
docs/setup/index.html   → the Web-Bluetooth setup page (served via GitHub Pages)
tests/        off-device pytest: BLE wire format + contact exchange + setup protocol
tools/        setup_client.py (bleak GATT client), host_advertise.py, pull_file.py, make_logos.py
DESIGN.md     protocol spec, verified hardware facts, verification status, open items
PLAN.md       the original full design document
```

See **DESIGN.md** for the full BLE protocol, the platform adaptation notes
(this badge runs MicroPythonOS, not the `fri3d.application` firmware), and the
detailed verification status.

## Build & publish (`.mpk` → BadgeHub)

Apps are distributed as `.mpk` packages (a ZIP whose single top-level folder is
the app's `fullname`). Build a deterministic one:

```bash
cd app
FN=com.fri3dcamp.fri3dfriends
rm -rf $FN/__pycache__ $FN/.pytest_cache
find $FN -exec touch -t 202501010000.00 {} \;
(find $FN -type d; find $FN -type f) | sort | TZ=CET zip -X -r -0 ../dist/${FN}_$(python3 -c "import json;print(json.load(open('$FN/MANIFEST.JSON'))['version'])").mpk -@
```

To publish, log in at **[badgehub.eu](https://badgehub.eu)** → **Create Project**
(App Identifier / slug = the `fullname` `com.fri3dcamp.fri3dfriends`; under
**Badge** select **`mpos_api_0`**) → upload the `.mpk`. Badges see the new release
on the next AppStore refresh. See the MicroPythonOS
[Bundling Apps](https://docs.micropythonos.com/apps/bundling-apps/) and
[BadgeHub](https://docs.micropythonos.com/apps/badgehub/) docs.

The logo/icon are generated by `tools/make_hybrid_logo.py`; the 42px name font is
a subset Montserrat TTF (`montserrat_name.ttf`).

## Tests

Off-device unit tests (pure wire-format / storage / setup-protocol logic, no
badge needed):

```bash
pytest tests/
```

Against a real badge in setup mode, the full GATT protocol can be exercised
headlessly with `python3 tools/setup_client.py` (needs `pip install bleak`) —
see its `--help`.

The Web-Bluetooth page (`docs/setup/`) is published via **GitHub Pages** (repo
**Settings → Pages → Deploy from branch → `main` / `/docs`**); its URL is what the
badge's QR encodes.

## License

[MIT](LICENSE) © 2026 David Steeman / Makerspace Baasrode.

## Credits

Made by **David Steeman** / **Makerspace Baasrode** for the Fri3d Camp badge.
Feedback and PRs welcome.
