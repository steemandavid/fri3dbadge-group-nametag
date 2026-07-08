#!/usr/bin/env python3
"""Host-side BlueZ LE advertiser: broadcasts an HSNT manufacturer-data beacon
so the badge's real scan can detect+alert on a matching group.

Usage: host_advertise.py <mdata_hex> <seconds>
  mdata_hex = manufacturer data AFTER the 2-byte company id (i.e. MAGIC..payload)
"""
import sys
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

BLUEZ_NAME = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
AD_PATH = "/org/fri3d/host_adv"
LE_ADV = "org.bluez.LEAdvertisement1"
PROPS = "org.freedesktop.DBus.Properties"


class HostAdvertisement(dbus.service.Object):
    def __init__(self, bus, mdata):
        self._mdata = mdata
        super().__init__(bus, AD_PATH)

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != LE_ADV:
            return {}
        mdata = dbus.Array([dbus.Byte(b) for b in self._mdata], signature="y")
        return {
            "Type": dbus.String("peripheral"),
            "ServiceUUIDs": dbus.Array([], signature="s"),
            "ManufacturerData": dbus.Dictionary(
                {dbus.UInt16(0xFFFF): mdata}, signature="qv"
            ),
            "IncludeTxPower": dbus.Boolean(False),
        }

    @dbus.service.method(PROPS, in_signature="ssv", out_signature="")
    def Set(self, interface, prop, value):
        pass

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self.GetAll(interface).get(prop)

    @dbus.service.method(LE_ADV, in_signature="")
    def Release(self):
        pass


def main():
    mdata_hex = sys.argv[1] if len(sys.argv) > 1 else ""
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    mdata = bytes.fromhex(mdata_hex)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    adv = HostAdvertisement(bus, mdata)
    adapter = bus.get_object(BLUEZ_NAME, ADAPTER_PATH)
    mgr = dbus.Interface(adapter, "org.bluez.LEAdvertisingManager1")

    registered = {"ok": False}

    def ok():
        registered["ok"] = True
        print("ADV_REGISTERED")

    def err(e):
        print("ADV_REGISTER_ERR", repr(e))
        loop.quit()

    mgr.RegisterAdvertisement(AD_PATH, {}, reply_handler=ok, error_handler=err)

    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, lambda: (mgr.UnregisterAdvertisement(AD_PATH), loop.quit()))
    loop.run()
    print("ADV_FINISHED")


if __name__ == "__main__":
    main()
