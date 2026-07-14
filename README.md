# !Fri3d Friends — Group Nametag + BLE Proximity Finder

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

A **PIN-protected WiFi web portal** (served by the badge while it's on WiFi) lets
you edit your name / groups / contact fields and view/export received contacts
from a phone or laptop — the badge keyboard is too cumbersome for lots of text.

It's **generic and redistributable**: any hackerspace/makerspace can flash it and
set their own group name(s) and member name by editing one file (no code changes)
— and it finds *their* people.

## Install (on the badge)

The app folder is `app/com.fri3dcamp.fri3dfriends/`. Copy it to `/apps/` on the
badge, then refresh the launcher (or reboot):

```bash
# with mpremote (https://docs.micropython.org/en/latest/reference/mpremote.html)
mpremote connect /dev/ttyACM0 cp -r app/com.fri3dcamp.fri3dfriends :/apps/
mpremote connect /dev/ttyACM0 reset
```

After reboot, **!Fri3d Friends** appears at the top of the launcher (the `!`
sorts it first). (If you added it without
rebooting, run `AppManager.refresh_apps()` in the REPL or just reboot.)

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
  Default `5000` (5 s).
- **`board`** — optional, `"2024"` or `"2026"` to force the hardware profile
  (otherwise auto-detected). Only needed if autodetection fails.
- **`contact`** — your "my contact info": a free-form object of `"field":
  "value"` pairs (Discord, website, phone, bitcoin wallet, anything). This is the
  data sent to another badge when you both press **Y**. Easiest to edit in the
  **WiFi portal** (below) rather than by hand.

## Setup / contacts over WiFi (no keyboard needed)

While the badge is connected to WiFi, it serves a small **web portal** so you can
edit everything above (name, groups, and the free-form `contact` fields) and
view/export the contacts you've received — from a phone or laptop browser.

- The badge shows its address at the bottom of the nametag: `⚙ http://<ip>:8080`
  (or `⚙ WiFi not connected`).
- Browsing there asks for a **PIN**. The badge shows the PIN on its screen as a
  login challenge (`portal PIN: 12345`). Type it in once; a session cookie keeps
  you in. Wrong guesses lock out briefly and rotate the PIN. The PIN gates
  *access* only (it's plain HTTP on the local network) — enough for a badge.
- Pages: **/** (edit config + add/remove contact fields), **/contacts** (received
  contacts with date/time), **/contacts.json** (download/export).

## Swapping contacts (Y button)

Press **Y** and, within ~5 s, have a nearby badge's owner press **Y** too. The
two badges find each other over Bluetooth and swap their `contact` info in both
directions — **no shared group or friendship required**, just radio range. The
banner confirms `Swapped with <name> ✓`; the received fields are stored on your
badge with the date & time and are visible in the WiFi portal. (If nobody else is
swapping in the window you get `No one swapping nearby`.)

> Note: the screen shows your group(s) as **coloured pills** (colour derived from
> the group name). The **!Fri3d Friends logo** (`fri3dfriends.png`) appears on the
> startup splash. The app runs on **both the Fri3d 2024 and 2026 badges**
> (auto-detected; force with the `board` key above).

> **First-run / unconfigured:** if `name` is empty or `groups` is empty, the
> badge shows a "Configure me" hint and **does not advertise or scan** — safer
> than beeping as a blank badge. Fill in `config.json` and reboot.

## Controls

| Button | Action |
|---|---|
| **A** | Open the friends-nearby panel (cards: name · shared group · signal bars · dBm · age) |
| **B** | Mute / unmute the alert buzzer (saved to config, survives reboot) |
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

## Project layout

```
app/com.fri3dcamp.fri3dfriends/   → the app (deploy to /apps/…)
  MANIFEST.JSON, fri3d_friends.py, ble_proximity.py (proximity beacon),
  contact_exchange.py (Y-button GATT swap), web_portal.py (PIN-gated setup portal),
  config.json, fri3dfriends.png (splash logo), icon_64x64.png (launcher icon),
  montserrat_name.ttf (42px name font)
tests/        off-device pytest: BLE wire format + contact exchange + portal forms
tools/        host_advertise.py, pull_file.py, make_logos.py + make_hybrid_logo.py (logo gen)
DESIGN.md     protocol spec, verified hardware facts, verification status, open items
PLAN.md       the original full design document
```

See **DESIGN.md** for the full BLE protocol, the platform adaptation notes
(this badge runs MicroPythonOS, not the `fri3d.application` firmware), and the
detailed verification status.
