# Probe v4 — filesystem layout, BLE config params, lvgl display API.
import os, sys
def P(*a):
    print(*a)

P("PV4-START")

def walk(p, depth=0):
    if depth > 2:
        return
    try:
        ents = os.listdir(p)
    except Exception:
        return
    for e in ents:
        full = p + "/" + e if p else e
        P(full)
        if e in ("lib", "__pycache__", "remote", "sdcard", "backups"):
            continue
        try:
            if (os.stat(full)[0] & 0x4000):  # dir
                walk(full, depth + 1)
        except Exception:
            pass

P("SEC-fs-root")
try:
    P("root:", os.listdir("/"))
except Exception as e:
    P("root err:", repr(e))
P("SEC-fs-walk")
walk("/")

# Where is fri3d?
P("SEC-fri3d-loc")
for cand in ["/lib/fri3d", "/fri3d", "/remote/fri3d", "/user", "/fri3d/apps"]:
    try:
        P(cand, ":", os.listdir(cand))
    except Exception as e:
        P(cand, "err:", repr(e))

# BLE config params that ARE supported
P("SEC-ble-config")
try:
    import bluetooth
    ble = bluetooth.BLE()
    ble.active(True)
    for key in ["mac", "mtu", "rxbuf", "txbuf", "addr", "addr_type", "gap_name", "io", "bond", "mitm", "le_secure"]:
        try:
            P(key, "=", repr(ble.config(key)))
        except Exception as e:
            P(key, "err:", repr(e)[:40])
    ble.active(False)
except Exception as e:
    P("ble err:", repr(e))

# lvgl display API surface
P("SEC-lvgl-display")
try:
    import lvgl as lv
    for fn in ["display_get_default", "disp_get_default", "display_create"]:
        P("has", fn, ":", hasattr(lv, fn))
    # enumerate 'disp'/'display' attrs
    P("disp/display attrs:", [a for a in dir(lv) if "disp" in a.lower() or "bright" in a.lower() or "backlight" in a.lower()])
except Exception as e:
    P("lvgl err:", repr(e))

P("PV4-DONE")
