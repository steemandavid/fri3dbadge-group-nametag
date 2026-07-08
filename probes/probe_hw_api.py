# Hardware API probe for the Group Nametag app.
# Checks the three unverified APIs from PLAN.md §9 spike 3:
#   - BLE addr_mode enum + default (stable public address)
#   - display / backlight brightness
#   - battery state
# Also lists fri3d.badge module surface for anything useful.

import sys

def sec(title):
    print("\n==== " + title + " ====")

def safe(label, fn):
    try:
        v = fn()
        print(label, "->", repr(v))
        return v
    except Exception as e:
        print(label, "-> ERROR:", repr(e))
        return None

# ---- fri3d.badge surface ----
sec("fri3d.badge modules")
try:
    import fri3d.badge as fb
    print("fri3d.badge attrs:", [a for a in dir(fb) if not a.startswith("_")])
except Exception as e:
    print("fri3d.badge import error:", repr(e))

for mod in ["display", "buttons", "leds", "buzzer", "capabilities", "communicator", "expansion", "joystick"]:
    sec("fri3d.badge." + mod)
    try:
        m = __import__("fri3d.badge." + mod, None, None, [mod])
        print("attrs:", [a for a in dir(m) if not a.startswith("_")])
    except Exception as e:
        print("import error:", repr(e))

# ---- battery ----
sec("BATTERY search")
# GPIO13 ADC per BADGE.md. Try machine.ADC directly.
def try_batt_adc():
    from machine import ADC, Pin
    a = ADC(Pin(13))
    a.atten(ADC.ATTN_11V) if hasattr(a, "atten") else None
    a.width(ADC.WIDTH_12BIT) if hasattr(a, "width") else None
    return a.read()
v = safe("ADC(13).read()", try_batt_adc)
# fri3d capabilities might expose it
try:
    from fri3d.badge import capabilities as caps
    print("capabilities attrs:", [a for a in dir(caps) if not a.startswith("_")])
except Exception as e:
    print("caps error:", repr(e))

# ---- backlight / brightness ----
sec("BACKLIGHT / BRIGHTNESS search")
import lvgl as lv
scr = lv.screen_active()
# Try display brightness / backlight via common surfaces
for expr in [
    "lambda: lv.disp_get_default().set_brightness(128)",
    "lambda: lv.disp_get_default().backlight",
    "lambda: __import__('machine').Pin(2, __import__('machine').Pin.OUT).value()",
]:
    safe(expr, eval(expr))
# Search all loaded modules for brightness/backlight
import sys as _sys
hits = []
for name, mod in list(_sys.modules.items()):
    for a in dir(mod) if mod else []:
        if a and ("bright" in a.lower() or "backlight" in a.lower()):
            hits.append(name + "." + a)
print("brightness/backlight hits in loaded modules:", hits[:30])

# ---- BLE: addr_mode, default address ----
sec("BLE addr / addr_mode")
import bluetooth
from bluetooth import BLE
ble = BLE()
def ble_on():
    ble.active(True)
    return "on"
safe("ble.active(True)", ble_on)
# Default address + type
safe("ble.config('addr')", lambda: ble.config('addr'))
safe("ble.config('addr_type')", lambda: ble.config('addr_type'))
# What config keys are accepted?
for key in ["mac", "addr", "addr_type", "mtu", "rxbuf", "gap_name", "mimodem"]:
    safe("config(%r)" % key, (lambda k: lambda: ble.config(k))(key))
# addr_mode enum: MicroPython uses 0x00 public, 0x01 random, 0x02 rpa, 0x03 nrpa
for am in [0x00, 0x01]:
    def set_am(mode=am):
        ble.config(addr_mode=mode)
        return ble.config('addr')
    safe("config(addr_mode=0x%02x)->addr" % am, set_am)
safe("addr after public:", lambda: ble.config('addr'))
ble.active(False)

print("\n==== PROBE DONE ====")
