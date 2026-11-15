#!/usr/bin/env python3
"""
Мост Loxone Miniserver (Gen 1/Gen 2) -> Умный дом Яндекса (Алиса).

Запуск:
    python server.py config.toml          # запустить сервер
    python server.py config.toml --list   # показать найденные устройства и их состояние

Яндекс вызывает этот сервер по HTTPS (через туннель), сервер управляет
Miniserver'ом по локальной сети через HTTP API (/jdev/sps/io/...).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import secrets
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

log = logging.getLogger("loxone-alice")

# ---------------------------------------------------------------- Loxone ---

T_LIGHT = "devices.types.light"
T_SWITCH = "devices.types.switch"
T_SOCKET = "devices.types.socket"
T_CURTAIN = "devices.types.openable.curtain"
C_ONOFF = "devices.capabilities.on_off"
C_RANGE = "devices.capabilities.range"


class Loxone:
    """Работа с Miniserver'ом по HTTP (Basic Auth)."""

    def __init__(self, c: dict):
        self.base = f"http://{c['host']}".rstrip("/")
        cred = f"{c['user']}:{c['password']}".encode("utf-8")
        self.auth_header = "Basic " + base64.b64encode(cred).decode()
        self.include = set(c.get("include", []))
        self.exclude = set(c.get("exclude", []))
        self.session: aiohttp.ClientSession | None = None
        self.devices: dict[str, dict] = {}   # id -> описание
        self.cache: dict[str, dict] = {}     # id -> {"on": bool, "level": int}
        self.reachable = False

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": self.auth_header}, timeout=aiohttp.ClientTimeout(total=2.5))
        await self.load_structure()

    async def close(self):
        if self.session:
            await self.session.close()

    async def _get_json(self, path: str):
        assert self.session
        async with self.session.get(self.base + path) as r:
            if r.status == 401:
                raise RuntimeError("Miniserver: неверный логин или пароль (401)")
            r.raise_for_status()
            data = await r.json(content_type=None)
        self.reachable = True
        return data

    async def io(self, uuid: str, command: str | None = None):
        """GET /jdev/sps/io/<uuid>[/<command>] -> value."""
        path = f"/jdev/sps/io/{uuid}" + (f"/{command}" if command else "")
        try:
            data = await self._get_json(path)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self.reachable = False
            raise
        ll = data.get("LL", {})
        code = str(ll.get("Code", ll.get("code", "")))
        if code != "200":
            raise RuntimeError(f"Miniserver ответил кодом {code} на {path}")
        return ll.get("value")

    # ---- структура ----------------------------------------------------

    async def load_structure(self):
        s = await self._get_json("/data/LoxAPP3.json")
        rooms = {k: v.get("name") for k, v in s.get("rooms", {}).items()}
        cats = {k: v.get("type", "") for k, v in s.get("cats", {}).items()}
        devices: dict[str, dict] = {}

        def wanted(uuid, ctl):
            if uuid in self.exclude or ctl.get("name") in self.exclude:
                return False
            if self.include:
                return uuid in self.include or ctl.get("name") in self.include
            return True

        def add(uuid, ctl, parent=None):
            t = ctl.get("type")
            room_id = ctl.get("room") or (parent or {}).get("room")
            cat_id = ctl.get("cat") or (parent or {}).get("cat")
            is_light = cats.get(cat_id) == "lights" or (parent or {}).get(
                "type") == "LightControllerV2"
            name = ctl.get("name") or (parent or {}).get("name") or uuid
            dev = None
            if t == "Switch":
                dev = {"kind": "switch",
                       "type": T_LIGHT if is_light else T_SOCKET}
            elif t in ("Dimmer", "EIBDimmer"):
                dev = {"kind": "dimmer", "type": T_LIGHT}
            elif t == "Jalousie":
                dev = {"kind": "jalousie", "type": T_CURTAIN}
            if dev and wanted(uuid, ctl):
                dev.update(id=uuid, name=name, room=rooms.get(room_id),
                           loxone_type=t)
                devices[uuid] = dev
            for sub_uuid, sub in (ctl.get("subControls") or {}).items():
                add(sub_uuid, sub, ctl)

        for uuid, ctl in s.get("controls", {}).items():
            add(uuid, ctl)

        # одинаковые имена в одной комнате Алиса не различит — пронумеруем
        seen: dict[tuple, int] = {}
        for d in devices.values():
            key = (d["room"], d["name"].lower())
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                d["name"] = f"{d['name']} {seen[key]}"

        self.devices = devices
        log.info("Найдено устройств: %d", len(devices))

    # ---- состояние ----------------------------------------------------

    async def read_state(self, dev_id: str) -> dict:
        """Пытается прочитать состояние; при неудаче отдаёт последнее известное."""
        dev = self.devices[dev_id]
        st = self.cache.setdefault(dev_id, {"on": False, "level": 0})
        try:
            raw = await self.io(dev_id)
            val = float(raw)
            if dev["kind"] == "switch":
                st["on"] = val > 0
            elif dev["kind"] == "dimmer":
                st["level"] = int(round(val))
                st["on"] = val > 0
            elif dev["kind"] == "jalousie":
                # Loxone: 0 = поднято (открыто), 1 = опущено (закрыто)
                pos = val if val <= 1 else val / 100
                st["level"] = int(round((1 - pos) * 100))
                st["on"] = st["level"] > 0
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise
        except Exception as e:  # значение не число / нет такого ответа
            log.debug("Не удалось прочитать %s: %s", dev_id, e)
        return st

    async def act(self, dev_id: str, cap_type: str, instance: str, value,
                  relative: bool = False):
        dev = self.devices[dev_id]
        st = self.cache.setdefault(dev_id, {"on": False, "level": 0})
        kind = dev["kind"]
        if cap_type == C_ONOFF:
            if kind == "jalousie":
                await self.io(dev_id, "FullUp" if value else "FullDown")
                st["level"] = 100 if value else 0
            else:
                await self.io(dev_id, "on" if value else "off")
            st["on"] = bool(value)
        elif cap_type == C_RANGE:
            v = float(value)
            if relative:
                v = st.get("level", 0) + v
            v = int(max(0, min(100, round(v))))
            if kind == "dimmer":
                await self.io(dev_id, str(v))
                st["on"] = v > 0
            elif kind == "jalousie":
                await self.io(dev_id, f"manualPosition/{100 - v}")
                st["on"] = v > 0
            else:
                raise ValueError("range не поддерживается")
            st["level"] = v
        else:
            raise ValueError(f"неизвестная способность {cap_type}")


def yandex_device(d: dict) -> dict:
    caps = [{"type": C_ONOFF, "retrievable": True}]
    if d["kind"] == "dimmer":
        caps.append({"type": C_RANGE, "retrievable": True, "parameters": {
            "instance": "brightness", "unit": "unit.percent",
            "range": {"min": 0, "max": 100, "precision": 1}}})
    elif d["kind"] == "jalousie":
        caps.append({"type": C_RANGE, "retrievable": True, "parameters": {
            "instance": "open", "unit": "unit.percent",
            "range": {"min": 0, "max": 100, "precision": 1}}})
    out = {"id": d["id"], "name": d["name"], "type": d["type"],
           "capabilities": caps,
           "device_info": {"manufacturer": "Loxone", "model": d["loxone_type"]}}
    if d.get("room"):
        out["room"] = d["room"]
    return out


def yandex_state(d: dict, st: dict) -> list:
    caps = [{"type": C_ONOFF, "state": {"instance": "on", "value": st["on"]}}]
    if d["kind"] == "dimmer":
        caps.append({"type": C_RANGE, "state": {
            "instance": "brightness", "value": st["level"] or 0}})
    elif d["kind"] == "jalousie":
        caps.append({"type": C_RANGE, "state": {
            "instance": "open", "value": st["level"]}})
    return caps

# ----------------------------------------------------------------- OAuth ---


class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"access": {}, "refresh": {}}
        if path.exists():
            self.data = json.loads(path.read_text("utf-8"))
        self.codes: dict[str, float] = {}

    @staticmethod
    def h(t: str) -> str:
        return hashlib.sha256(t.encode()).hexdigest()

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=1), "utf-8")

    def issue(self) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = 365 * 24 * 3600
        self.data["access"][self.h(access)] = time.time() + ttl
        self.data["refresh"][self.h(refresh)] = time.time() + 2 * ttl
        self.save()
        return {"access_token": access, "token_type": "bearer",
                "expires_in": ttl, "refresh_token": refresh}

    def check(self, kind: str, token: str, consume=False) -> bool:
        k = self.h(token)
        exp = self.data[kind].get(k)
        ok = exp is not None and exp > time.time()
        if consume and exp is not None:
            del self.data[kind][k]
            self.save()
        return ok

    def revoke_all(self):
        self.data = {"access": {}, "refresh": {}}
        self.save()


LOGIN_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loxone → Алиса</title>
<style>body{{font-family:sans-serif;max-width:360px;margin:15vh auto;padding:0 16px}}
input,button{{width:100%;padding:12px;margin:6px 0;font-size:16px;box-sizing:border-box}}</style>
<h2>Loxone → Алиса</h2><p>{msg}</p>
<form method="post"><input type="password" name="password" placeholder="Пароль моста" autofocus>
{hidden}<button>Привязать</button></form>"""

# ------------------------------------------------------------------- App ---


def build_app(cfg: dict, lox: Loxone, tokens: TokenStore) -> web.Application:
    oc = cfg["oauth"]

    def rid(req):
        return req.headers.get("X-Request-Id", "")

    def authorized(req) -> bool:
        a = req.headers.get("Authorization", "")
        return a.startswith("Bearer ") and tokens.check("access", a[7:].strip())

    # --- OAuth ---
    async def authorize(req: web.Request):
        q = dict(req.query)
        if req.method == "POST":
            form = await req.post()
            q.update({k: v for k, v in form.items() if k != "password"})
            pw = form.get("password", "")
        else:
            pw = None
        redirect = q.get("redirect_uri", "")
        if q.get("client_id") != oc["client_id"]:
            return web.Response(text="Неверный client_id", status=400)
        if not redirect.startswith("https://social.yandex."):
            return web.Response(text="Недопустимый redirect_uri", status=400)
        if pw is not None and secrets.compare_digest(pw, oc["password"]):
            code = secrets.token_urlsafe(24)
            tokens.codes[code] = time.time() + 600
            sep = "&" if "?" in redirect else "?"
            params = {"code": code}
            if q.get("state"):
                params["state"] = q["state"]
            raise web.HTTPFound(redirect + sep + urlencode(params))
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in q.items())
        msg = "Неверный пароль" if pw is not None else "Введите пароль из config.toml"
        return web.Response(text=LOGIN_PAGE.format(msg=msg, hidden=hidden),
                            content_type="text/html")

    async def token(req: web.Request):
        f = await req.post()
        cid, csec = f.get("client_id"), f.get("client_secret")
        if not cid and req.headers.get("Authorization", "").startswith("Basic "):
            raw = base64.b64decode(req.headers["Authorization"][6:]).decode()
            cid, _, csec = raw.partition(":")
        if cid != oc["client_id"] or not secrets.compare_digest(
                str(csec or ""), oc["client_secret"]):
            return web.json_response({"error": "invalid_client"}, status=401)
        gt = f.get("grant_type")
        if gt == "authorization_code":
            exp = tokens.codes.pop(f.get("code", ""), 0)
            if exp < time.time():
                return web.json_response({"error": "invalid_grant"}, status=400)
        elif gt == "refresh_token":
            if not tokens.check("refresh", f.get("refresh_token", ""), consume=True):
                return web.json_response({"error": "invalid_grant"}, status=400)
        else:
            return web.json_response({"error": "unsupported_grant_type"}, status=400)
        log.info("Выдан токен (%s)", gt)
        return web.json_response(tokens.issue())

    # --- Smart Home API ---
    async def ping(req):
        return web.Response(text="ok")

    def need_auth(handler):
        async def wrapper(req):
            if not authorized(req):
                return web.Response(status=401)
            return await handler(req)
        return wrapper

    @need_auth
    async def unlink(req):
        tokens.revoke_all()
        log.info("Навык отвязан")
        return web.json_response({"request_id": rid(req)})

    @need_auth
    async def devices(req):
        try:
            await lox.load_structure()
        except Exception as e:
            log.warning("Не удалось обновить структуру: %s", e)
        return web.json_response({"request_id": rid(req), "payload": {
            "user_id": "loxone-owner",
            "devices": [yandex_device(d) for d in lox.devices.values()]}})

    @need_auth
    async def query(req):
        body = await req.json()
        ids = [d["id"] for d in body.get("devices", [])]

        async def one(i):
            d = lox.devices.get(i)
            if not d:
                return {"id": i, "error_code": "DEVICE_NOT_FOUND"}
            try:
                st = await lox.read_state(i)
            except Exception as e:
                log.warning("Нет связи с Miniserver: %s", e)
                return {"id": i, "error_code": "DEVICE_UNREACHABLE"}
            return {"id": i, "capabilities": yandex_state(d, st)}

        res = await asyncio.gather(*(one(i) for i in ids))
        return web.json_response({"request_id": rid(req),
                                  "payload": {"devices": list(res)}})

    @need_auth
    async def action(req):
        body = await req.json()

        async def one(dev):
            i = dev["id"]
            if i not in lox.devices:
                return {"id": i, "action_result": {
                    "status": "ERROR", "error_code": "DEVICE_NOT_FOUND"}}
            caps = []
            for c in dev.get("capabilities", []):
                s = c.get("state", {})
                try:
                    await lox.act(i, c["type"], s.get("instance"), s.get("value"),
                                  bool(s.get("relative")))
                    result = {"status": "DONE"}
                    log.info("%s: %s=%s", lox.devices[i]["name"],
                             s.get("instance"), s.get("value"))
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.warning("Нет связи с Miniserver: %s", e)
                    result = {"status": "ERROR", "error_code": "DEVICE_UNREACHABLE"}
                except Exception as e:
                    log.warning("Ошибка команды %s: %s", i, e)
                    result = {"status": "ERROR", "error_code": "INVALID_ACTION"}
                caps.append({"type": c["type"], "state": {
                    "instance": s.get("instance"), "action_result": result}})
            return {"id": i, "capabilities": caps}

        res = await asyncio.gather(
            *(one(d) for d in body.get("payload", {}).get("devices", [])))
        return web.json_response({"request_id": rid(req),
                                  "payload": {"devices": list(res)}})

    app = web.Application()
    app.router.add_route("*", "/oauth/authorize", authorize)
    app.router.add_post("/oauth/token", token)
    app.router.add_get("/v1.0", ping)
    app.router.add_post("/v1.0/user/unlink", unlink)
    app.router.add_get("/v1.0/user/devices", devices)
    app.router.add_post("/v1.0/user/devices/query", query)
    app.router.add_post("/v1.0/user/devices/action", action)
    return app

# ------------------------------------------------------------------ main ---


async def list_devices(lox: Loxone):
    await lox.start()
    try:
        print(f"\nНайдено устройств: {len(lox.devices)}\n")
        for d in lox.devices.values():
            try:
                raw = await lox.io(d["id"])
            except Exception as e:
                raw = f"ошибка: {e}"
            print(f"  [{d['room'] or '—'}] {d['name']}  "
                  f"({d['loxone_type']} -> {d['type'].split('.')[-1]})  "
                  f"значение={raw!r}\n      id={d['id']}")
    finally:
        await lox.close()


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cfg_path = Path(args[0] if args else "config.toml").resolve()
    cfg = tomllib.loads(cfg_path.read_text("utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    lox = Loxone(cfg["loxone"])
    try:
        await _run(cfg, cfg_path, lox)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        sys.exit(f"Не удаётся связаться с Miniserver {lox.base}: {e!r}")
    except RuntimeError as e:
        sys.exit(str(e))
    finally:
        await lox.close()


async def _run(cfg, cfg_path, lox):

    if "--list" in sys.argv:
        await list_devices(lox)
        return

    await lox.start()
    tokens = TokenStore(cfg_path.parent / "tokens.json")
    app = build_app(cfg, lox, tokens)
    runner = web.AppRunner(app)
    await runner.setup()
    sc = cfg.get("server", {})
    host, port = sc.get("listen_host", "127.0.0.1"), int(sc.get("listen_port", 8090))
    await web.TCPSite(runner, host, port).start()
    log.info("Мост запущен на http://%s:%d", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await lox.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
