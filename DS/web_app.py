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
from .persistence import save_accounts, save_theme, load_ui_settings, save_ui_settings
from .theme import (
    HAS_PIL,
    resolve_theme,
    generate_default_icon,
    get_icon_version,
    build_manifest_json,
)
from .pwa_assets import ICON_192_B64, ICON_512_B64, SW_JS, PWA_HTML
from .discord_bot import sorted_accounts, display_name, is_account_online

HISTORY_MAX = 24

async def handle_status(request):
    if not hmac.compare_digest(request.query.get("key", ""), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    now = time.time()
    ui = load_ui_settings()
    out = []
    for key, data in sorted_accounts():
        online = is_account_online(data, now)
        place_id, job_id = data.get("placeId"), data.get("jobId")
        history = data.get("history") or []
        out.append({
            "key": key,
            "name": display_name(key, data),
            "online": online,
            "game": data.get("gameName") or "Unknown",
            "lastSeen": data.get("lastSeen"),
            "lastSeenSecondsAgo": int(now - data.get("lastSeen", now)),
            "intervalSeconds": data.get("intervalSeconds", 300),
            "userId": data.get("userId"),
            "pinned": key in (ui.get("pinned") or []),
            "history": history[-HISTORY_MAX:],
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
        "settings": ui,
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

    now = time.time()
    prev = state.accounts.get(key) or {}
    history = list(prev.get("history") or [])
    history.append(now)
    history = history[-HISTORY_MAX:]
    state.accounts[key] = {
        "placeId": data.get("placeId"),
        "jobId": data.get("jobId"),
        "gameName": data.get("gameName", "Unknown"),
        "playerName": player_name or legacy_label,
        "userId": user_id,
        "lastSeen": now,
        "intervalSeconds": data.get("intervalSeconds", 300),
        "history": history,
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



async def handle_get_settings(request):
    if not hmac.compare_digest(request.query.get("key", ""), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(load_ui_settings())


async def handle_post_settings(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not hmac.compare_digest(str(data.get("key", "")), DASHBOARD_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    ui = load_ui_settings()
    if "pinned" in data and isinstance(data["pinned"], list):
        ui["pinned"] = [str(x) for x in data["pinned"]][:100]
    if "sort" in data and data["sort"] in ("online_first", "name", "last_seen"):
        ui["sort"] = data["sort"]
    if "density" in data and data["density"] in ("comfortable", "compact"):
        ui["density"] = data["density"]
    if "refreshSeconds" in data:
        try:
            rs = int(data["refreshSeconds"])
            if rs in (5, 10, 15, 30, 60):
                ui["refreshSeconds"] = rs
        except (TypeError, ValueError):
            pass
    if "quietHours" in data and isinstance(data["quietHours"], dict):
        qh = data["quietHours"]
        ui["quietHours"] = {
            "enabled": bool(qh.get("enabled")),
            "start": int(qh.get("start", 23)) % 24,
            "end": int(qh.get("end", 7)) % 24,
        }
    # toggle pin helper
    if "togglePin" in data:
        k = str(data["togglePin"])
        pinned = list(ui.get("pinned") or [])
        if k in pinned:
            pinned = [p for p in pinned if p != k]
        else:
            pinned.append(k)
        ui["pinned"] = pinned
    save_ui_settings(ui)
    return web.json_response(ui)


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
    app.router.add_get("/settings", handle_get_settings)
    app.router.add_post("/settings", handle_post_settings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()
