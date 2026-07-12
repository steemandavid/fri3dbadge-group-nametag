# web_portal.py — PIN-gated on-badge config/contacts web portal.
#
# The badge keyboard is too cumbersome for entering lots of text, so this serves
# a tiny web form over the LAN (the badge is assumed already on WiFi via the OS).
# You edit your name / groups / free-form "my contact info" and view/export the
# contacts you've received by swapping with other badges (Y button).
#
# Access model: an always-on HTTP server on a shared camp LAN is reachable by
# everyone on the subnet, so it is gated by a random PIN shown ON THE BADGE
# (prove you can physically see it — the same idea as Bluetooth/smart-TV
# pairing). A correct PIN sets a short-lived session cookie; repeated wrong
# guesses lock out briefly and regenerate the PIN. It's plain HTTP, so the PIN
# gates *access*, not traffic encryption — an acceptable trust model for a badge.
#
# Cooperative: runs on the app's existing asyncio loop via asyncio.start_server;
# no threads. All handlers are defensive and never raise into the loop.

import json

try:
    import uasyncio as asyncio           # MicroPython
except ImportError:                      # host/dev
    import asyncio

try:
    import utime as time
except ImportError:
    import time

import os

PORT = 8080
PIN_DIGITS = 5
SESSION_TTL_MS = 30 * 60 * 1000          # 30 min session
PENDING_MS = 20000                       # how long the badge shows the challenge PIN
MAX_FAILS = 5
LOCKOUT_MS = 30000
MAX_BODY = 8192


def _rand_int(n):
    """Return a random int in [0, n) using os.urandom (falls back to time)."""
    try:
        b = os.urandom(4)
        v = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    except Exception:
        v = int(time.ticks_ms()) if hasattr(time, "ticks_ms") else 0
    return v % n


def _rand_token():
    try:
        return "".join("%02x" % b for b in os.urandom(8))
    except Exception:
        return "%016x" % (int(time.ticks_ms()) if hasattr(time, "ticks_ms") else 0)


def _now():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _elapsed(since):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(_now(), since)
    return _now() - since


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _url_unquote(s):
    s = s.replace("+", " ")
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "%" and i + 2 < len(s):
            try:
                out.append(chr(int(s[i + 1:i + 3], 16)))
                i += 3
                continue
            except Exception:
                pass
        out.append(c)
        i += 1
    return "".join(out)


def parse_form(body):
    """Parse application/x-www-form-urlencoded into a dict of lists (order kept).
    Pure + host-testable."""
    out = {}
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8")
        except Exception:
            body = ""
    for pair in body.split("&"):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
        else:
            k, v = pair, ""
        k = _url_unquote(k)
        v = _url_unquote(v)
        out.setdefault(k, []).append(v)
    return out


def form_to_config(form, base):
    """Merge a parsed config form into a copy of `base` config dict. Pure +
    host-testable: no file/BLE access."""
    cfg = dict(base) if isinstance(base, dict) else {}
    def one(key, default=""):
        v = form.get(key, [default])
        return v[0] if v else default
    cfg["name"] = one("name").strip()
    cfg["handle"] = one("handle").strip()
    groups = one("groups")
    cfg["groups"] = [g.strip() for g in groups.split(",") if g.strip()]
    try:
        cfg["rssi_floor"] = int(one("rssi_floor", "-120"))
    except (TypeError, ValueError):
        cfg["rssi_floor"] = -120
    cfg["sound"] = bool(form.get("sound"))
    try:
        cfg["banner_ms"] = int(one("banner_ms", "5000"))
    except (TypeError, ValueError):
        cfg["banner_ms"] = 5000
    # Free-form contact fields: parallel ck[]/cv[] arrays.
    keys = form.get("ck", [])
    vals = form.get("cv", [])
    contact = {}
    for i, k in enumerate(keys):
        k = k.strip()
        v = vals[i].strip() if i < len(vals) else ""
        if k:
            contact[k] = v
    cfg["contact"] = contact
    return cfg


class WebPortal:
    def __init__(self, app_dir, on_change=None, ip_getter=None):
        self._app_dir = app_dir
        self._on_change = on_change          # called after a successful save
        self._ip_getter = ip_getter          # callable -> ip string or None
        self._server = None
        self._task = None
        self._pin = None
        self._sessions = {}                  # token -> created ticks
        self._fails = 0
        self._lock_until = None
        self._pending_until = None           # while set+fresh, badge shows the PIN
        self.port = PORT

    # ---- lifecycle ----
    def start(self):
        if self._task is not None:
            return
        self._pin = self._new_pin()
        try:
            self._task = asyncio.create_task(self._serve())
        except Exception:
            self._task = None

    async def _serve(self):
        try:
            self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        except Exception:
            self._server = None

    def stop(self):
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self._sessions = {}
        self._pending_until = None

    # ---- badge-facing helpers ----
    def current_pin(self):
        return self._pin

    def pending_pin(self):
        """The PIN to display as a login challenge, if a login is pending & fresh."""
        if self._pending_until is None:
            return None
        if _elapsed(self._pending_until) < 0:
            return self._pin
        self._pending_until = None
        return None

    def url(self):
        ip = None
        if self._ip_getter:
            try:
                ip = self._ip_getter()
            except Exception:
                ip = None
        if not ip:
            return None
        return "http://%s:%d" % (ip, self.port)

    # ---- auth ----
    def _new_pin(self):
        lo = 10 ** (PIN_DIGITS - 1)
        return str(lo + _rand_int(9 * lo))

    def _locked(self):
        return self._lock_until is not None and _elapsed(self._lock_until) < 0

    def _check_pin(self, pin):
        if self._locked():
            return False
        if pin == self._pin:
            self._fails = 0
            return True
        self._fails += 1
        if self._fails >= MAX_FAILS:
            self._fails = 0
            self._lock_until = time_add(_now(), LOCKOUT_MS)
            self._pin = self._new_pin()          # rotate after a burst of guesses
        return False

    def _new_session(self):
        tok = _rand_token()
        self._sessions[tok] = _now()
        return tok

    def _valid_session(self, cookie):
        tok = self._cookie_val(cookie, "sess")
        if not tok or tok not in self._sessions:
            return False
        if _elapsed(self._sessions[tok]) > SESSION_TTL_MS:
            try:
                del self._sessions[tok]
            except Exception:
                pass
            return False
        return True

    @staticmethod
    def _cookie_val(cookie, name):
        if not cookie:
            return None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    # ---- request handling ----
    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                method, path, _ = line.decode().split(" ", 2)
            except Exception:
                return
            headers = {}
            while True:
                h = await reader.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
                try:
                    k, v = h.decode().split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                except Exception:
                    pass
            body = b""
            try:
                clen = int(headers.get("content-length", "0"))
            except (TypeError, ValueError):
                clen = 0
            if clen > 0:
                body = await reader.read(min(clen, MAX_BODY))

            path = path.split("?", 1)[0]
            cookie = headers.get("cookie", "")
            authed = self._valid_session(cookie)
            await self._route(writer, method, path, body, authed)
        except Exception:
            pass
        finally:
            try:
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route(self, writer, method, path, body, authed):
        if path == "/login":
            if method == "POST":
                form = parse_form(body)
                pin = (form.get("pin", [""])[0] or "").strip()
                if self._check_pin(pin):
                    tok = self._new_session()
                    self._pending_until = None
                    return await self._send(writer, 303, "text/html", "",
                                            extra="Set-Cookie: sess=%s; Path=/\r\nLocation: /\r\n" % tok)
                return await self._send(writer, 200, "text/html", self._login_page(error=True))
            # GET /login — flag a pending login so the badge shows the PIN.
            self._pending_until = time_add(_now(), PENDING_MS)
            return await self._send(writer, 200, "text/html", self._login_page())

        if not authed:
            # Anything else requires a session -> bounce to the login challenge.
            self._pending_until = time_add(_now(), PENDING_MS)
            return await self._send(writer, 303, "text/html", "", extra="Location: /login\r\n")

        if path == "/" and method == "GET":
            return await self._send(writer, 200, "text/html", self._config_page())
        if path == "/save" and method == "POST":
            self._save_config(parse_form(body))
            return await self._send(writer, 303, "text/html", "", extra="Location: /?saved=1\r\n")
        if path == "/contacts" and method == "GET":
            return await self._send(writer, 200, "text/html", self._contacts_page())
        if path == "/contacts.json" and method == "GET":
            return await self._send(writer, 200, "application/json",
                                    json.dumps(self._load_contacts()),
                                    extra='Content-Disposition: attachment; filename="contacts.json"\r\n')
        return await self._send(writer, 404, "text/plain", "not found")

    async def _send(self, writer, code, ctype, body, extra=""):
        if isinstance(body, str):
            body = body.encode("utf-8")
        reason = {200: "OK", 303: "See Other", 404: "Not Found"}.get(code, "OK")
        head = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                "Connection: close\r\n%s\r\n" % (code, reason, ctype, len(body), extra))
        try:
            writer.write(head.encode("utf-8"))
            if body:
                writer.write(body)
            await writer.drain()
        except Exception:
            pass

    # ---- storage ----
    def _load_config(self):
        try:
            with open(self._app_dir + "/config.json") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self, form):
        base = self._load_config()
        cfg = form_to_config(form, base)
        try:
            with open(self._app_dir + "/config.json", "w") as f:
                json.dump(cfg, f)
        except Exception:
            return
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _load_contacts(self):
        try:
            with open(self._app_dir + "/contacts.json") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    # ---- HTML ----
    def _page(self, title, inner):
        return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>%s</title><style>"
                "body{font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;"
                "padding:16px;background:#0b0e14;color:#e6e6e6}"
                "h1{font-size:1.3rem;color:#ffe066}a{color:#7fb2ff}"
                "input{width:100%%;padding:8px;margin:4px 0;box-sizing:border-box;"
                "background:#162033;color:#fff;border:1px solid #28324a;border-radius:6px}"
                "label{font-size:.85rem;color:#8fa8b8}button{padding:10px 16px;margin:8px 4px 0 0;"
                "background:#143a2a;color:#fff;border:1px solid #ffe066;border-radius:8px}"
                ".row{display:flex;gap:6px}.row input{flex:1}.muted{color:#6a7280;font-size:.8rem}"
                "table{width:100%%;border-collapse:collapse}td,th{border-bottom:1px solid #28324a;"
                "padding:6px;text-align:left;font-size:.9rem}</style></head><body>%s</body></html>"
                % (_esc(title), inner))

    def _login_page(self, error=False):
        msg = "<p style='color:#f4534a'>Wrong PIN, try again.</p>" if error else ""
        if self._locked():
            msg = "<p style='color:#f4534a'>Too many attempts — locked briefly.</p>"
        inner = ("<h1>!friends nearby — setup</h1>%s"
                 "<p>Enter the PIN shown on the badge screen.</p>"
                 "<form method='POST' action='/login'>"
                 "<label>PIN</label><input name='pin' inputmode='numeric' autofocus>"
                 "<button type='submit'>Unlock</button></form>"
                 "<p class='muted'>Local PIN-gated portal (plain HTTP on the LAN).</p>" % msg)
        return self._page("Setup — login", inner)

    def _config_page(self):
        cfg = self._load_config()
        contact = cfg.get("contact", {}) if isinstance(cfg.get("contact"), dict) else {}
        rows = ""
        for k, v in contact.items():
            rows += ("<div class='row'><input name='ck' value='%s' placeholder='field'>"
                     "<input name='cv' value='%s' placeholder='value'></div>"
                     % (_esc(k), _esc(v)))
        checked = "checked" if cfg.get("sound", True) else ""
        inner = ("<h1>My nametag &amp; contact info</h1>"
                 "<p><a href='/contacts'>&rarr; received contacts</a></p>"
                 "<form method='POST' action='/save'>"
                 "<label>Name</label><input name='name' value='%s'>"
                 "<label>Handle (optional)</label><input name='handle' value='%s'>"
                 "<label>Groups (comma-separated)</label><input name='groups' value='%s'>"
                 "<label>RSSI floor (dBm, -120 = off)</label><input name='rssi_floor' value='%s'>"
                 "<label>Banner ms</label><input name='banner_ms' value='%s'>"
                 "<label><input type='checkbox' name='sound' %s style='width:auto'> alert sound</label>"
                 "<h3>My contact info</h3><div class='muted'>Any fields you like — Discord, "
                 "website, phone, bitcoin wallet…</div><div id='c'>%s</div>"
                 "<button type='button' onclick='addRow()'>+ field</button>"
                 "<div><button type='submit'>Save</button></div></form>"
                 "<script>function addRow(){var d=document.createElement('div');d.className='row';"
                 "d.innerHTML=\"<input name='ck' placeholder='field'>"
                 "<input name='cv' placeholder='value'>\";"
                 "document.getElementById('c').appendChild(d);}</script>"
                 % (_esc(cfg.get("name", "")), _esc(cfg.get("handle", "")),
                    _esc(", ".join(cfg.get("groups", []) or [])),
                    _esc(cfg.get("rssi_floor", -120)), _esc(cfg.get("banner_ms", 5000)),
                    checked, rows))
        return self._page("My nametag & contact info", inner)

    def _contacts_page(self):
        contacts = self._load_contacts()
        if not contacts:
            body = "<p class='muted'>No contacts received yet. Press Y near another badge that also presses Y.</p>"
        else:
            body = "<table><tr><th>Name</th><th>Received</th><th>Fields</th></tr>"
            for c in contacts:
                fields = c.get("fields", {}) or {}
                fs = "<br>".join("%s: %s" % (_esc(k), _esc(v)) for k, v in fields.items())
                body += ("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (_esc(c.get("name", "?")), _esc(c.get("received_at", "?")), fs))
            body += "</table>"
        inner = ("<h1>Received contacts</h1><p><a href='/'>&larr; setup</a> · "
                 "<a href='/contacts.json'>download JSON</a></p>%s" % body)
        return self._page("Received contacts", inner)


def time_add(t, delta):
    if hasattr(time, "ticks_add"):
        return time.ticks_add(t, delta)
    return t + delta
