# Group Nametag + BLE Proximity Finder

A MicroPythonOS app for the **Fri3d Camp 2024 badge** that does two things:

1. **Animated nametag** — shows your group's logo and your name, with a gentle
   breathing animation.
2. **Proximity finder** — over Bluetooth Low Energy, it detects other badges
   running this app that share **at least one group** with you, and alerts you
   when one comes within radio range ("someone from my groups is nearby").

It's **generic and redistributable**: any hackerspace/makerspace can flash it,
set their own group name(s), member name, and logo — by editing one file and
swapping one image, no code changes — and it finds *their* people.

## Install (on the badge)

The app folder is `app/com.fri3dcamp.groupnametag/`. Copy it to `/apps/` on the
badge, then refresh the launcher (or reboot):

```bash
# with mpremote (https://docs.micropython.org/en/latest/reference/mpremote.html)
mpremote connect /dev/ttyACM0 cp -r app/com.fri3dcamp.groupnametag :/apps/
mpremote connect /dev/ttyACM0 reset
```

After reboot, **Group Nametag** appears in the launcher. (If you added it without
rebooting, run `AppManager.refresh_apps()` in the REPL or just reboot.)

## Configure (no code edits)

Edit **`/apps/com.fri3dcamp.groupnametag/config.json`**:

```json
{
  "groups": ["Makerspace Baasrode"],
  "name": "Alex",
  "handle": "YOURCALL",
  "rssi_floor": -120
}
```

- **`groups`** — one or more group names you belong to. Two badges alert on each
  other when their group lists **overlap** (any shared group). Each name is
  hashed before broadcast (never sent as text), so type it the same way on every
  member's badge (case/whitespace are ignored). Up to ~5 groups.
- **`name`** — your display name (big, centre).
- **`handle`** — optional second line (callsign / nickname); may be `""`.
- **`rssi_floor`** — optional coarse range gate in dBm. Default **`-120`** =
  detect anything the radio can hear (full range). Raise it to only alert on
  badges that are close, e.g. `-80` (≈ same tent / ~10 m) or `-70` (≈ next to me).
  It's a noise/range filter, *not* fine calibration.

Then **replace `logo.png`** with your group's logo (PNG or JPEG; keep it ≲300×300
and ≲150 KB; non-square is fine — it's auto-fit and centred). Reboot.

> **First-run / unconfigured:** if `name` is empty or `groups` is empty, the
> badge shows a "Configure me" hint and **does not advertise or scan** — safer
> than beeping as a blank badge. Fill in `config.json` and reboot.

## Controls

| Button | Action |
|---|---|
| **A** | Toggle the nearby-list detail view (name · shared group · smoothed RSSI · age) |
| **X** | Mute / unmute the alert buzzer |
| **START** | Exit back to the launcher (the OS back gesture also works) |

The idle screen shows: your logo (breathing), name + handle, your own group(s)
(top-left) so you can confirm who can find you, battery % (top-right), and the
current "nearby: …" line. On a new arrival, a banner appears for ~2.5 s, the LEDs
flash, and a short buzzer sting plays — with a **colour + tone unique to the
shared group** (derived from the group hash, so you can recognise *which* group
just arrived without reading the screen). Several arrivals in one window
coalesce into one banner ("Alice + 2 more nearby").

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
