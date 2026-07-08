# Targeted probe v3 — no full-module scan. Get BLE addr + display surface + sys.path.
def P(*a):
    print(*a)

P("PROBE3-START")

P("sys.path:", __import__("sys").path)
P("sys.modules count:", len(list(__import__("sys").modules)))

# fri3d importable under framework? check path
try:
    import fri3d
    P("fri3d OK:", [a for a in dir(fri3d) if not a.startswith("_")])
except Exception as e:
    P("fri3d top err:", repr(e))

# lvgl display object methods (brightness/backlight?)
P("SEC-display")
try:
    import lvgl as lv
    d = lv.disp_get_default()
    P("disp methods:", [a for a in dir(d) if not a.startswith("_")])
    # try common brightness calls (read-only / safe)
    for m in ["set_brightness", "get_brightness", "backlight", "set_backlight"]:
        P("has", m, ":", hasattr(d, m))
except Exception as e:
    P("display err:", repr(e))

# fri3d.badge.display / leds surface via the hardware path
P("SEC-fri3dbadge")
for imp in ["from fri3d.badge.leds import leds",
            "from fri3d.badge import display",
            "from fri3d.badge import buzzer"]:
    try:
        exec(imp)
        P(imp, "OK")
    except Exception as e:
        P(imp, "err:", repr(e))

# BLE addr + addr_type (read only)
P("SEC-ble")
try:
    import bluetooth
    ble = bluetooth.BLE()
    ble.active(True)
    P("addr:", ble.config("addr"))
    try:
        P("addr_type:", ble.config("addr_type"))
    except Exception as e:
        P("addr_type err:", repr(e))
    # try setting public addr_mode (0x00) — read back addr
    try:
        ble.config(addr_mode=0)
        P("after addr_mode=0, addr:", ble.config("addr"))
    except Exception as e:
        P("addr_mode=0 err:", repr(e))
    ble.active(False)
except Exception as e:
    P("ble err:", repr(e))

P("PROBE3-DONE")
