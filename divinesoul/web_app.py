"""aiohttp web server: report API, dashboard, theme, icons."""
import base64
import hmac
import re
import time

from aiohttp import web

from .config import (
    REPORT_SECRET,
    DASHBOARD_KEY,
    WEB_SERVER_PORT,
    DEFAULT_THEME,
    PALETTES,
)
from . import state
from .persistence import save_accounts, save_theme
from .theme import (
    HAS_PIL,
    resolve_theme,
    generate_default_icon,
    get_icon_version,
    build_manifest_json,
)
from .pwa_assets import ICON_192_B64, ICON_512_B64, SW_JS, PWA_HTML
from .discord_bot import sorted_accounts, display_name, is_account_online

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


async def handle_status(request):
    if not hmac.compare_digest(request.query.get("key", ""), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    now = time.time()
    out = []
    for key, data in sorted_accounts():
        online = is_account_online(data, now)
        place_id, job_id = data.get("placeId"), data.get("jobId")
        out.append({
            "key": key,
            "name": display_name(key, data),
            "online": online,
            "game": data.get("gameName") or "Unknown",
            "lastSeenSecondsAgo": int(now - data.get("lastSeen", now)),
            "joinUrl": (
                f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
                if online and place_id and job_id else None
            ),
        })

    total = len(out)
    online_count = sum(1 for a in out if a["online"])
    return web.json_response({
        "total": total,
        "online": online_count,
        "offline": total - online_count,
        "accounts": out,
    })


async def handle_dashboard(request):
    return web.Response(text=PWA_HTML, content_type="text/html")


async def handle_manifest(request):
    return web.Response(text=build_manifest_json(), content_type="application/manifest+json")


async def handle_sw(request):
    return web.Response(text=SW_JS, content_type="application/javascript")


def _icon_response(size: int) -> web.Response:
    if HAS_PIL:
        accent = state.theme.get("accent", DEFAULT_THEME["accent"])
        body = generate_default_icon(size, accent)
    else:
        body = base64.b64decode(ICON_192_B64 if size <= 192 else ICON_512_B64)
    return web.Response(
        body=body,
        content_type="image/png",
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "ETag": f'"{get_icon_version()}"',
        },
    )


async def handle_icon_192(request):
    return _icon_response(192)


async def handle_icon_512(request):
    return _icon_response(512)


async def handle_report(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not hmac.compare_digest(str(data.get("secret", "")), REPORT_SECRET):
        return web.json_response({"error": "unauthorized"}, status=401)

    user_id = data.get("userId")
    player_name = data.get("playerName")
    legacy_label = data.get("label")

    key = str(user_id) if user_id else (player_name or legacy_label)
    if not key:
        return web.json_response(
            {"error": "missing userId/playerName/label - nothing to identify this account by"},
            status=400,
        )

    state.accounts[key] = {
        "placeId": data.get("placeId"),
        "jobId": data.get("jobId"),
        "gameName": data.get("gameName", "Unknown"),
        "playerName": player_name or legacy_label,
        "userId": user_id,
        "lastSeen": time.time(),
        "intervalSeconds": data.get("intervalSeconds", 300),
    }
    save_accounts()
    return web.json_response({"ok": True})


async def handle_get_theme(request):
    return web.json_response(resolve_theme())


async def handle_post_theme(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not hmac.compare_digest(str(data.get("key", "")), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    if "accent" in data:
        accent = data["accent"]
        if not isinstance(accent, str) or not HEX_COLOR_RE.match(accent):
            return web.json_response({"error": "accent must be a #rrggbb hex color"}, status=400)
        state.theme["accent"] = accent

    if "mode" in data:
        mode = data["mode"]
        if mode not in PALETTES:
            return web.json_response({"error": "mode must be 'light' or 'dark'"}, status=400)
        state.theme["mode"] = mode

    save_theme()
    return web.json_response(resolve_theme())


async def handle_post_theme_reset(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not hmac.compare_digest(str(data.get("key", "")), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    state.theme["accent"] = DEFAULT_THEME["accent"]
    save_theme()
    return web.json_response(resolve_theme())


async def handle_health(request):
    return web.json_response({"ok": True, "accounts": len(state.accounts)})


async def start_web_server():
    app = web.Application()
    app.router.add_post("/report", handle_report)
    app.router.add_get("/", handle_health)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/sw.js", handle_sw)
    app.router.add_get("/icon-192.png", handle_icon_192)
    app.router.add_get("/icon-512.png", handle_icon_512)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/theme", handle_get_theme)
    app.router.add_post("/theme", handle_post_theme)
    app.router.add_post("/theme/reset", handle_post_theme_reset)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()
