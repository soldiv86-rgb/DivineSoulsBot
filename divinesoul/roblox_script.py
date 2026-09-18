"""Roblox reporter script template (shared by the Discord bot and the web dashboard)."""

SECRET_PLACEHOLDER = "PASTE_YOUR_REPORT_SECRET_HERE"


def _lua_string(value: str) -> str:
    """Escape a value so it is safe inside a double-quoted Lua string literal."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def build_roblox_script(base_url: str, secret: str = SECRET_PLACEHOLDER) -> str:
    """Return the Lua reporter script.

    Pass the real REPORT_SECRET only where the viewer is authenticated (the
    dashboard's Script tab). The Discord button uses the placeholder so the
    secret is never posted into a channel.
    """
    base = _lua_string(base_url.rstrip("/"))
    secret = _lua_string(secret)
    return f'''-- DivineSoul reporter (put in ServerScriptService as a Script)
-- Enable HttpService in Game Settings → Security
local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local MarketplaceService = game:GetService("MarketplaceService")

local REPORT_URL = "{base}/report"
local SECRET = "{secret}"
local INTERVAL = 30

local function gameName()
\tlocal ok, info = pcall(function()
\t\treturn MarketplaceService:GetProductInfo(game.PlaceId)
\tend)
\tif ok and info and info.Name then
\t\treturn info.Name
\tend
\treturn "Unknown"
end

local function report(player)
\tlocal ok, err = pcall(function()
\t\tHttpService:RequestAsync({{
\t\t\tUrl = REPORT_URL,
\t\t\tMethod = "POST",
\t\t\tHeaders = {{ ["Content-Type"] = "application/json" }},
\t\t\tBody = HttpService:JSONEncode({{
\t\t\t\tsecret = SECRET,
\t\t\t\tuserId = player.UserId,
\t\t\t\tplayerName = player.Name,
\t\t\t\tplaceId = game.PlaceId,
\t\t\t\tjobId = game.JobId,
\t\t\t\tgameName = gameName(),
\t\t\t\tintervalSeconds = INTERVAL,
\t\t\t}}),
\t\t}})
\tend)
\tif not ok then
\t\twarn("[DivineSoul] report failed:", err)
\tend
end

while true do
\tfor _, player in ipairs(Players:GetPlayers()) do
\t\treport(player)
\tend
\ttask.wait(INTERVAL)
end
'''
