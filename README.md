# !friends nearby — Group Nametag + BLE Proximity Finder

A MicroPythonOS app for the **Fri3d Camp 2024 badge** that does two things:

1. **Animated nametag** — shows your name large (it scrolls if too long), with
   your group(s) as coloured pills (the colour is unique to each group).
2. **Proximity finder** — over Bluetooth Low Energy, it detects other badges
   running this app that share **at least one group** with you, and alerts you
   when one comes within radio range ("someone from my groups is nearby").

It's **generic and redistributable**: any hackerspace/makerspace can flash it and
set their own group name(s) and member name by editing one file (no code changes)
— and it finds *their* people.

## Install (on the badge)

The app folder is `app/com.fri3dcamp.groupnametag/`. Copy it to `/apps/` on the
badge, then refresh the launcher (or reboot):

```bash
# with mpremote (https://docs.micropython.org/en/latest/reference/mpremote.html)
mpremote connect /dev/ttyACM0 cp -r app/com.fri3dcamp.groupnametag :/apps/
mpremote connect /dev/ttyACM0 reset
```

After reboot, **!friends nearby** appears at the top of the launcher (the `!`
sorts it first). (If you added it without
rebooting, run `AppManager.refresh_apps()` in the REPL or just reboot.)

## Configure (no code edits)

Edit **`/apps/com.fri3dcamp.groupnametag/config.json`**:

```json
{
  "groups": ["Makerspace Baasrode"],
  "name": "Alex",
  "handle": "YOURCALL",
  "rssi_floor": -120,
  "sound": true,
  "banner_ms": 5000
}
```

- **`groups`** — one or more group names you belong to. Two badges alert on each
  other when their group lists **overlap** (any shared group). Each name is
  hashed before broadcast (never sent as text), so type it the same way on every
  member's badge (case/whitespace are ignored). Up to ~5 groups.
- **`name`** — your display name (shown large across the top of the screen).
- **`handle`** — optional second line (callsign / nickname); may be `""`.
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

> Note: the screen shows your group(s) as **coloured pills** (colour derived from
> the group name) — there's no logo image to swap. The bundled `logo.png` is not
> displayed. The app runs on **both the Fri3d 2024 and 2026 badges** (auto-detected;
> force with the `board` key above).

> **First-run / unconfigured:** if `name` is empty or `groups` is empty, the
> badge shows a "Configure me" hint and **does not advertise or scan** — safer
> than beeping as a blank badge. Fill in `config.json` and reboot.

## Controls

| Button | Action |
|---|---|
| **A** | Open the friends-nearby panel (cards: name · shared group · signal bars · dBm · age) |
| **B** | Mute / unmute the alert buzzer (saved to config, survives reboot) |
| **X** | *(handled by the OS)* quit to the launcher / OS menu |
| **START** | *(unused)* |

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
app/com.fri3dcamp.groupnametag/   → the app (deploy to /apps/…)
  MANIFEST.JSON, group_nametag.py, ble_proximity.py, config.json, logo.png, icon_64x64.png
tests/        off-device pytest for the BLE wire format (run: pytest tests/)
tools/        host_advertise.py (act as a 2nd badge for testing), pull_file.py
DESIGN.md     protocol spec, verified hardware facts, verification status, open items
PLAN.md       the original full design document
```

See **DESIGN.md** for the full BLE protocol, the platform adaptation notes
(this badge runs MicroPythonOS, not the `fri3d.application` firmware), and the
detailed verification status.
