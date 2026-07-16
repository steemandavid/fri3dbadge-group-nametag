#!/usr/bin/env python3
"""setup_client.py — drive the badge's BLE setup GATT service from this machine.

A `bleak` (pip) client that speaks the exact same protocol as the Web-Bluetooth
page (docs/setup/index.html) — so the whole setup flow is testable without a
browser or phone. Doubles as the automated hardware test for ble_setup.py.

    pip install bleak

    # scan for badges in setup mode
    python3 tools/setup_client.py --scan

    # read the (pre-auth) info of a specific badge
    python3 tools/setup_client.py --badge A1B2 --info

    # unlock + read the full config
    python3 tools/setup_client.py --badge A1B2 --code 1234 --info

    # unlock + write config, then verify the badge echoes it back
    python3 tools/setup_client.py --badge A1B2 --code 1234 \
        --name "David Steeman" --groups "ON4BDS,Makerspace" \
        --contact "Discord=dave#1" --contact "web=steeman.be"

    # unlock + download received contacts
    python3 tools/setup_client.py --badge A1B2 --code 1234 --contacts out.json

The badge advertises as `Fri3d-XXXX` in setup mode (unconfigured Configure-me
screen, or a configured badge with the 2-min window open via a long B press).
The 4-digit code is shown on the badge screen.
"""
import argparse
import asyncio
import json
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("This tool needs bleak:  pip install bleak")

SETUP_SVC    = "6e400020-b5a3-f393-e0a9-e50e24dcca9e"
AUTH_CHR     = "6e400021-b5a3-f393-e0a9-e50e24dcca9e"
INFO_CHR     = "6e400022-b5a3-f393-e0a9-e50e24dcca9e"
CFG_CHR      = "6e400023-b5a3-f393-e0a9-e50e24dcca9e"
STATUS_CHR   = "6e400024-b5a3-f393-e0a9-e50e24dcca9e"
CONTACTS_CHR = "6e400025-b5a3-f393-e0a9-e50e24dcca9e"
CTLOFF_CHR   = "6e400026-b5a3-f393-e0a9-e50e24dcca9e"

CONTACTS_HEADER_OFFSET = 0xFFFF
CFG_CHUNK = 180


async def find_device(badge, timeout=8.0):
    want = ("Fri3d-" + badge.upper()) if badge else None
    print("Scanning for %s…" % (want or "Fri3d-* badges"))
    devices = await BleakScanner.discover(timeout=timeout)
    matches = []
    for d in devices:
        name = d.name or ""
        if name.startswith("Fri3d-"):
            matches.append(d)
            if want and name == want:
                return d
    if want:
        print("Did not find %s. Saw: %s" % (want, [d.name for d in matches]))
        return None
    return matches


async def cmd_scan():
    matches = await find_device(None)
    if not matches:
        print("No badges in setup mode found.")
        return
    for d in matches:
        print("  %s  [%s]  rssi=%s" % (d.name, d.address, getattr(d, "rssi", "?")))


def u16le(n):
    return bytes([n & 0xFF, (n >> 8) & 0xFF])


async def read_info(client):
    return json.loads(bytes(await client.read_gatt_char(INFO_CHR)).decode("utf-8"))


async def authenticate(client, code):
    await client.write_gatt_char(AUTH_CHR, code.encode("utf-8"), response=True)
    await asyncio.sleep(0.2)
    info = await read_info(client)
    return bool(info.get("authed")), info


async def write_config(client, cfg):
    payload = json.dumps(cfg).encode("utf-8")
    total = max(1, (len(payload) + CFG_CHUNK - 1) // CFG_CHUNK)
    if total > 255:
        raise SystemExit("config too large (%d bytes)" % len(payload))
    for seq in range(total):
        part = payload[seq * CFG_CHUNK:(seq + 1) * CFG_CHUNK]
        await client.write_gatt_char(CFG_CHR, bytes([seq, total]) + part, response=True)
    await asyncio.sleep(0.3)
    status = bytes(await client.read_gatt_char(STATUS_CHR)).decode("utf-8").rstrip("\x00")
    return status


async def read_contacts(client):
    await client.write_gatt_char(CTLOFF_CHR, u16le(CONTACTS_HEADER_OFFSET), response=True)
    hdr = json.loads(bytes(await client.read_gatt_char(CONTACTS_CHR)).decode("utf-8"))
    total, page = int(hdr.get("len", 0)), int(hdr.get("page", 400))
    buf = bytearray()
    off = 0
    while off < total:
        await client.write_gatt_char(CTLOFF_CHR, u16le(off), response=True)
        chunk = bytes(await client.read_gatt_char(CONTACTS_CHR))
        if not chunk:
            break
        buf.extend(chunk)
        off += len(chunk)
    return bytes(buf[:total])


def build_cfg(args):
    cfg = {}
    if args.name is not None:
        cfg["name"] = args.name
    if args.groups is not None:
        cfg["groups"] = [g.strip() for g in args.groups.split(",") if g.strip()]
    if args.sound is not None:
        cfg["sound"] = args.sound
    if args.banner_ms is not None:
        cfg["banner_ms"] = args.banner_ms
    if args.rssi_floor is not None:
        cfg["rssi_floor"] = args.rssi_floor
    if args.contact:
        contact = {}
        for pair in args.contact:
            k, _, v = pair.partition("=")
            if k.strip():
                contact[k.strip()] = v.strip()
        cfg["contact"] = contact
    return cfg


async def run(args):
    if args.scan:
        await cmd_scan()
        return 0

    dev = await find_device(args.badge)
    if not dev:
        return 2
    print("Connecting to %s [%s]…" % (dev.name, dev.address))
    async with BleakClient(dev) as client:
        info = await read_info(client)
        print("pre-auth INFO:", info)

        if args.code:
            ok, info = await authenticate(client, args.code)
            print("auth:", "OK" if ok else "FAILED")
            if not ok:
                return 3
            if args.info:
                print("config:", json.dumps(info, indent=2, ensure_ascii=False))

            cfg = build_cfg(args)
            if cfg:
                status = await write_config(client, cfg)
                print("save status:", status)
                await asyncio.sleep(0.3)
                back = await read_info(client)
                print("read-back:", json.dumps(back, indent=2, ensure_ascii=False))
                for k, v in cfg.items():
                    if back.get(k) != v:
                        print("  ! mismatch on %r: sent %r got %r" % (k, v, back.get(k)))

            if args.contacts:
                data = await read_contacts(client)
                with open(args.contacts, "wb") as f:
                    f.write(data)
                try:
                    n = len(json.loads(data.decode("utf-8") or "[]"))
                except Exception:
                    n = "?"
                print("wrote %s (%d bytes, %s contacts)" % (args.contacts, len(data), n))
        elif args.info:
            print("(no --code given; only pre-auth info is available)")
    return 0


def main():
    p = argparse.ArgumentParser(description="Drive the Fri3d badge BLE setup service.")
    p.add_argument("--scan", action="store_true", help="list badges in setup mode and exit")
    p.add_argument("--badge", help="badge id XXXX (advertises as Fri3d-XXXX)")
    p.add_argument("--code", help="4-digit code shown on the badge screen")
    p.add_argument("--info", action="store_true", help="print the badge info/config")
    p.add_argument("--name", help="set name")
    p.add_argument("--groups", help="set groups (comma-separated)")
    p.add_argument("--contact", action="append", metavar="KEY=VALUE",
                   help="add a contact field (repeatable)")
    p.add_argument("--sound", type=lambda s: s.lower() in ("1", "true", "yes", "on"),
                   help="alert sound on/off")
    p.add_argument("--banner-ms", type=int, dest="banner_ms")
    p.add_argument("--rssi-floor", type=int, dest="rssi_floor")
    p.add_argument("--contacts", metavar="OUT.json", help="download received contacts to a file")
    args = p.parse_args()
    if not args.scan and not args.badge:
        p.error("give --scan or --badge XXXX")
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
