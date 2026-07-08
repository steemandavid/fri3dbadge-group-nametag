# Minimal, non-mutating hardware probe. Reads only. Prints section markers.
import sys

def P(*a):
    print(*a)

P("PROBE-START")

# 1. fri3d.badge module surface
P("SEC-1-fri3d")
try:
    import fri3d.badge as fb
    P("fb attrs:", [a for a in dir(fb) if not a.startswith("_")])
except Exception as e:
    P("fb err:", repr(e))
for mod in ["capabilities", "communicator", "expansion"]:
    try:
        m = __import__("fri3d.badge." + mod, None, None, [mod])
        P(mod, ":", [a for a in dir(m) if not a.startswith("_")])
    except Exception as e:
        P(mod, "err:", repr(e))

# 2. battery via ADC13
P("SEC-2-battery")
try:
    from machine import ADC, Pin
    a = ADC(Pin(13))
    try: a.atten(ADC.ATTN_11V)
    except: pass
    P("adc13 read:", a.read())
except Exception as e:
    P("battery err:", repr(e))

# 3. brightness/backlight search in loaded modules
P("SEC-3-brightness")
hits = []
for name, mod in list(sys.modules.items()):
    try:
        for at in dir(mod):
            if at and ("bright" in at.lower() or "backlight" in at.lower() or "batt" in at.lower()):
                hits.append(name + "." + at)
    except Exception:
        pass
P("hits:", hits)

# 4. lvgl display object surface
P("SEC-4-lvgl-display")
try:
    import lvgl as lv
    d = lv.disp_get_default()
    P("disp attrs:", [a for a in dir(d) if not a.startswith("_")][:60])
except Exception as e:
    P("lvgl err:", repr(e))

# 5. BLE: read-only — default addr + addr_type only
P("SEC-5-ble")
try:
    import bluetooth
    ble = bluetooth.BLE()
    ble.active(True)
    P("addr:", ble.config("addr"))
    try:
        P("addr_type:", ble.config("addr_type"))
    except Exception as e:
        P("addr_type err:", repr(e))
    ble.active(False)
except Exception as e:
    P("ble err:", repr(e))

P("PROBE-DONE")
