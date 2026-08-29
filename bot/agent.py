import asyncio
import os

from aiohttp import web

from .engine import BurnSession
from .loader import fetch_and_load
from .selfupdate import run_self_update
from .traffic import BIG_FILES


def _auth(request, token: str) -> bool:
    t = request.headers.get("X-Token") or request.query.get("token", "")
    return bool(token) and t == token


async def main() -> None:
    token = os.environ.get("AGENT_TOKEN")
    if not token:
        raise SystemExit("AGENT_TOKEN env var required")
    port = int(os.environ.get("AGENT_PORT", "8787"))
    workers = int(os.environ.get("WORKERS", "64"))
    socks_port = int(os.environ.get("SOCKS_PORT", "10808"))
    singbox = os.environ.get("SINGBOX_BIN", "sing-box")
    ua = os.environ.get("SUB_UA", "v2rayN/6.42")
    hwid_env = os.environ.get("SUB_HWID", "")
    install_dir = os.environ.get("INSTALL_DIR", "/opt/vpn-traffic-bot")
    branch = os.environ.get("REPO_BRANCH", "claude/traffic-consuming-bot-iuxyrf")
    service = os.environ.get("SERVICE_NAME", "vpn-traffic-agent")

    engine = BurnSession(workers, singbox, socks_port)

    async def ping(_req):
        return web.json_response({"ok": True})

    async def stats(req):
        if not _auth(req, token):
            return web.json_response({"ok": False, "error": "auth"}, status=401)
        c = engine.counter
        return web.json_response({
            "ok": True, "running": engine.running(),
            "eaten": (c.bytes if c else 0), "errors": (c.errors if c else 0),
            "nodes": engine.node_count,
        })

    async def do_burn(req):
        if not _auth(req, token):
            return web.json_response({"ok": False, "error": "auth"}, status=401)
        try:
            body = await req.json()
        except Exception:
            body = {}
        url = body.get("url")
        if not url:
            return web.json_response({"ok": False, "error": "no url"})
        hwid = body.get("hwid") or hwid_env
        w = int(body.get("workers") or workers)
        limit = int(body.get("limit_bytes") or 0)
        if engine.running():
            await engine.stop()
        loop = asyncio.get_event_loop()
        try:
            outbounds, ua_used, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(url, ua=ua, hwid=hwid or None)
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        if not outbounds:
            return web.json_response({"ok": False, "error": "no nodes"})
        engine.workers = w
        try:
            n = await engine.start(outbounds, limit, list(BIG_FILES), title="agent")
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "nodes": n})

    async def do_stop(req):
        if not _auth(req, token):
            return web.json_response({"ok": False, "error": "auth"}, status=401)
        await engine.stop()
        return web.json_response({"ok": True})

    async def do_update(req):
        if not _auth(req, token):
            return web.json_response({"ok": False, "error": "auth"}, status=401)
        run_self_update(install_dir, branch, service)
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes([
        web.get("/ping", ping),
        web.get("/stats", stats),
        web.post("/burn", do_burn),
        web.post("/stop", do_stop),
        web.post("/update", do_update),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"agent up on :{port}", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
