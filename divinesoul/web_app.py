"""aiohttp web server: report API, dashboard, theme, icons."""
import base64
import hmac
import re
import time
from collections import defaultdict

from aiohttp import web

from .config import (
    REPORT_SECRET,
    DASHBOARD_KEY,
    WEB_SERVER_PORT,
    DEFAULT_THEME,
    PALETTES,
    REPORT_LOADSTRING,
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

HISTORY_MAX = 24  # entries, now one per hour of activity (see handle_report) - so 24 = a day
# 6-digit hex only: theme.py's darken/lighten/blend helpers can't parse #rgb.
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# --- Auth helpers -----------------------------------------------------
# Simple in-memory brute-force guard: a static, non-expiring key (DASHBOARD_KEY
# / REPORT_SECRET) has no rate limit of its own, so without this a bad actor
# could just hammer these endpoints. This resets on every process restart and
# isn't shared across workers, but that's fine for a single-process bot/web
# server like this one - it just needs to make guessing impractical, not be
# a hardened WAF.
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_FAILURES = 10
_failed_attempts = defaultdict(list)


def _client_ip(request):
    # Render (and most PaaS) sit behind a proxy, so the real client address
    # is in this header rather than request.remote.
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "unknown"


def _rate_limited(request):
    ip = _client_ip(request)
    now = time.time()
    attempts = _failed_attempts[ip]
    attempts[:] = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    return len(attempts) >= RATE_LIMIT_MAX_FAILURES


def _record_failure(request):
    _failed_attempts[_client_ip(request)].append(time.time())


def _dashboard_key(request, data=None):
    """Prefer the X-Dashboard-Key header - keeps the key out of URLs, which
    otherwise end up in server access logs and browser history on every
    poll. Query string / JSON body 'key' is still accepted as a fallback for
    any client that hasn't picked up the header-based version yet."""
    header = request.headers.get("X-Dashboard-Key")
    if header:
        return header
    if data is not None:
        return str(data.get("key", ""))
    return request.query.get("key", "")


def _check_dashboard_auth(request, data=None):
    """Returns an error web.Response if the request should be rejected,
    or None if it's authorized to proceed."""
    if _rate_limited(request):
        return web.json_response({"error": "too many attempts - try again in a minute"}, status=429)
    if not hmac.compare_digest(_dashboard_key(request, data), DASHBOARD_KEY):
        _record_failure(request)
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


async def handle_status(request):
    err = _check_dashboard_auth(request)
    if err:
        return err

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

    if _rate_limited(request):
        return web.json_response({"error": "too many attempts - try again in a minute"}, status=429)
    if not hmac.compare_digest(str(data.get("secret", "")), REPORT_SECRET):
        _record_failure(request)
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
    # Collapse to one entry per hour of activity instead of one per report.
    # Reports can arrive every minute or so, so without this, HISTORY_MAX
    # raw pings only covered the last few minutes - not useful as a
    # "recent activity" timeline. If the last recorded entry is still
    # within the current hour, just bump it forward instead of appending;
    # only append when a new hour (or a gap after being offline) starts.
    if history and (now - history[-1]) < 3600:
        history[-1] = now
    else:
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

    err = _check_dashboard_auth(request, data)
    if err:
        return err

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

    err = _check_dashboard_auth(request, data)
    if err:
        return err

    state.theme["accent"] = DEFAULT_THEME["accent"]
    save_theme()
    return web.json_response(resolve_theme())


async def handle_script(request):
    """The reporter loadstring shown on the dashboard's Report Script tab."""
    err = _check_dashboard_auth(request)
    if err:
        return err
    return web.json_response({"script": REPORT_LOADSTRING})


async def handle_health(request):
    return web.json_response({"ok": True, "accounts": len(state.accounts)})



async def handle_get_settings(request):
    err = _check_dashboard_auth(request)
    if err:
        return err
    return web.json_response(load_ui_settings())


async def handle_post_settings(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    err = _check_dashboard_auth(request, data)
    if err:
        return err

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


async def handle_remove_account(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    err = _check_dashboard_auth(request, data)
    if err:
        return err

    key = str(data.get("accountKey", ""))
    if not key or key not in state.accounts:
        return web.json_response({"error": "account not found"}, status=404)

    del state.accounts[key]
    save_accounts()

    # Also drop it from the pinned list, if it was pinned - otherwise it'd
    # come back pinned the moment it reports in again.
    ui = load_ui_settings()
    pinned = list(ui.get("pinned") or [])
    if key in pinned:
        ui["pinned"] = [p for p in pinned if p != key]
        save_ui_settings(ui)

    return web.json_response({"ok": True})


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
    app.router.add_get("/script", handle_script)
    app.router.add_get("/theme", handle_get_theme)
    app.router.add_post("/theme", handle_post_theme)
    app.router.add_post("/theme/reset", handle_post_theme_reset)
    app.router.add_get("/settings", handle_get_settings)
    app.router.add_post("/settings", handle_post_settings)
    app.router.add_post("/account/remove", handle_remove_account)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()
