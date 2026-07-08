#!/usr/bin/env python3
"""Pull a binary file from the badge over USB-Serial, via chunked base64.

Usage: pull_file.py <remote_path> <local_path>
Reads <remote_path> on the badge, emits base64 in guarded chunks, decodes on host.
"""
import sys, time, binascii, base64, serial, re

PORT, BAUD = "/dev/ttyACM0", 115200

def to_repl(ser):
    ser.write(b"\r\x03\x03"); time.sleep(0.25)
    ser.write(b"\r\x02"); time.sleep(0.2); ser.write(b"\r"); time.sleep(0.15)
    ser.reset_input_buffer()

def paste(ser, code, settle=6.0):
    to_repl(ser)
    ser.write(b"\x05"); time.sleep(0.15)
    for i in range(0, len(code), 64):
        ser.write(code[i:i+64]); time.sleep(0.004)
    time.sleep(0.1); ser.write(b"\x04")
    buf = bytearray(); last = time.time(); start = time.time()
    while True:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n)); last = time.time()
        elif time.time() - last > settle:
            break
        if time.time() - start > 60:
            break
        time.sleep(0.02)
    return bytes(buf)

def main():
    remote, local = sys.argv[1], sys.argv[2]
    ser = serial.Serial(PORT, BAUD, timeout=0.1); time.sleep(0.05); ser.reset_input_buffer()
    code = (
        "import binascii\n"
        "try:\n"
        "    _d=open(%r,'rb').read()\n"
        "    _b=binascii.b2a_base64(_d).decode().replace('\\n','')\n"
        "    for i in range(0,len(_b),76):\n"
        "        print('C:'+_b[i:i+76])\n"
        "    print('CLEN',len(_d))\n"
        "except Exception as e:\n"
        "    print('CERR',repr(e))\n" % remote
    ).encode()
    out = paste(ser, code)
    ser.close()
    chunks = re.findall(rb"^C:(.{1,76})$", out, re.MULTILINE)
    data = base64.b64decode(b"".join(chunks)) if chunks else b""
    with open(local, "wb") as f:
        f.write(data)
    m = re.search(rb"CLEN (\d+)", out)
    print("pulled %d bytes (remote reported %s) to %s" % (len(data), m.group(1).decode() if m else "?", local))

if __name__ == "__main__":
    main()
