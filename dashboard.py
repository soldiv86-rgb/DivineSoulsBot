#!/usr/bin/env python3
"""
Patches ONLY the PWA_HTML = \"\"\" ... \"\"\" block in your bot's .py file,
replacing it with the new BigFroot-style dashboard HTML. Everything else
in your file (icons, bot logic, env vars) is left byte-for-byte untouched.

Usage:
    python patch_dashboard.py path/to/your_bot.py

A backup of your original file is written next to it as <name>.bak before
anything is changed.
"""
import re
import sys
from pathlib import Path

NEW_PWA_HTML = r'''PWA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>DivineSouls Dashboard</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="theme-color" content="#0d0d0f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  :root {
    --accent: #FF8C28;
    --bg: #0d0d0f;
    --sidebar: #111113;
    --card: #17171a;
    --border: #26262a;
    --text: #f2f2f2;
    --muted: #8a8a90;
    --online: #57F287;
    --offline: #ED4245;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
  }

  #keyGate { position: fixed; inset: 0; background: var(--bg); display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 14px; padding: 24px; z-index: 20; }
  #keyGate .brand { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  #keyGate .brand .mark { width: 36px; height: 36px; border-radius: 9px; background: linear-gradient(145deg, var(--accent), #c9631a); display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 14px; color: #1a1005; }
  #keyGate h1 { font-size: 18px; margin: 0; }
  #keyGate h1 span { color: var(--accent); }
  #keyGate input { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 12px 14px; border-radius: 10px; font-size: 15px; width: 100%; max-width: 280px; }
  #keyGate button { background: var(--accent); color: #1a1005; font-weight: 700; border: none; padding: 12px 20px; border-radius: 10px; font-size: 15px; cursor: pointer; }
  #keyGate p { color: var(--muted); font-size: 13px; text-align: center; max-width: 260px; }

  #app { display: none; flex: 1; min-height: 100vh; }
  .shell { display: flex; width: 100%; }

  .sidebar { width: 220px; flex-shrink: 0; background: var(--sidebar); border-right: 1px solid var(--border); padding: 18px 12px; display: flex; flex-direction: column; gap: 22px; }
  .sidebar .brand { display: flex; align-items: center; gap: 10px; padding: 0 6px; }
  .sidebar .brand .mark { width: 34px; height: 34px; border-radius: 9px; background: linear-gradient(145deg, var(--accent), #c9631a); display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 13px; color: #1a1005; flex-shrink: 0; }
  .sidebar .brand .txt .name { font-weight: 700; font-size: 14px; letter-spacing: 0.3px; }
  .sidebar .brand .txt .sub { font-size: 11px; color: var(--muted); }
  .navgroup .label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted); padding: 0 10px 8px; }
  .navitem { display: flex; align-items: center; justify-content: space-between; padding: 9px 10px; border-radius: 9px; font-size: 13.5px; color: #cfcfd2; cursor: pointer; border-left: 2px solid transparent; margin-bottom: 2px; }
  .navitem:hover { background: #1b1b1e; }
  .navitem.active { background: #1e1a14; color: var(--accent); border-left: 2px solid var(--accent); }
  .navitem .count { font-size: 11.5px; color: var(--muted); }
  .navitem.active .count { color: var(--accent); }

  .main { flex: 1; min-width: 0; padding: 18px 22px 40px; }
  .topbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 18px; }
  .tabs { display: flex; gap: 6px; flex-wrap: wrap; }
  .tab { background: var(--card); border: 1px solid var(--border); color: var(--muted); font-size: 12.5px; padding: 7px 12px; border-radius: 9px; cursor: pointer; display: flex; align-items: center; gap: 6px; }
  .tab.active { background: #211c15; border-color: #4a3316; color: var(--accent); }
  .tab .badge { background: #2a2a2e; color: #d5d5d8; font-size: 10.5px; padding: 1px 6px; border-radius: 999px; }
  .tab.active .badge { background: var(--accent); color: #1a1005; }
  .spacer { flex: 1; }
  .livechip { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); }
  .livechip .liveDot { width: 7px; height: 7px; border-radius: 50%; background: var(--online); box-shadow: 0 0 5px var(--online); }
  .updatedText { font-size: 12px; color: #5c5c62; }
  #refreshBtn { background: var(--card); border: 1px solid var(--border); color: var(--muted); font-size: 15px; padding: 7px 10px; border-radius: 9px; cursor: pointer; }

  .summary { display: flex; gap: 10px; margin-bottom: 18px; }
  .chip { flex: 1; background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px 10px; text-align: center; }
  .chip .num { font-size: 22px; font-weight: 700; }
  .chip .lbl { font-size: 11px; color: var(--muted); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.5px; }
  .chip.online .num { color: var(--online); }
  .chip.offline .num { color: var(--offline); }
  .chip.total .num { color: var(--accent); }

  #list { display: flex; flex-direction: column; gap: 8px; }
  .row { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; display: flex; align-items: center; gap: 12px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .dot.online { background: var(--online); box-shadow: 0 0 6px var(--online); }
  .dot.offline { background: var(--offline); }
  .info { flex: 1; min-width: 0; }
  .name { font-weight: 600; font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 12px; color: var(--muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .join { background: var(--accent); color: #1a1005; font-weight: 700; font-size: 12px; padding: 8px 12px; border-radius: 9px; text-decoration: none; flex-shrink: 0; }
  .empty { text-align: center; color: var(--muted); padding: 60px 20px; font-size: 14px; }

  footer { text-align: center; color: #4a4a4f; font-size: 11px; padding: 20px 0 4px; }
  footer button { background: none; border: none; color: #4a4a4f; text-decoration: underline; font-size: 11px; cursor: pointer; }

  @media (max-width: 640px) {
    .sidebar { display: none; }
    .main { padding: 14px 14px 32px; }
  }
</style>
</head>
<body>

<div id="keyGate">
  <div class="brand"><div class="mark">DS</div></div>
  <h1>DivineSouls <span>Dashboard</span></h1>
  <p>Enter your dashboard key (set as DASHBOARD_KEY on the bot) to view account status.</p>
  <input id="keyInput" type="password" placeholder="Dashboard key" autocomplete="off">
  <button id="keySubmit">Unlock</button>
</div>

<div id="app">
  <div class="shell">
    <div class="sidebar">
      <div class="brand">
        <div class="mark">DS</div>
        <div class="txt">
          <div class="name">DIVINESOULS</div>
          <div class="sub">Account Dashboard</div>
        </div>
      </div>

      <div class="navgroup">
        <div class="label">Monitor</div>
        <div class="navitem active" data-filter="all">
          <span>Fleet</span><span class="count" id="navAll">0</span>
        </div>
        <div class="navitem" data-filter="online">
          <span>Online</span><span class="count" id="navOnline">0</span>
        </div>
        <div class="navitem" data-filter="offline">
          <span>Offline</span><span class="count" id="navOffline">0</span>
        </div>
      </div>

      <div class="navgroup">
        <div class="label">Account</div>
        <div class="navitem" id="resetKeyNav">
          <span>Dashboard key</span>
        </div>
      </div>
    </div>

    <div class="main">
      <div class="topbar">
        <div class="tabs" id="gameTabs"></div>
        <div class="spacer"></div>
        <div class="livechip"><span class="liveDot"></span>Live</div>
        <div class="updatedText" id="updatedText">updated just now</div>
        <button id="refreshBtn" title="Refresh">&#8635;</button>
      </div>

      <div class="summary">
        <div class="chip total"><div class="num" id="numTotal">-</div><div class="lbl">Total</div></div>
        <div class="chip online"><div class="num" id="numOnline">-</div><div class="lbl">Online</div></div>
        <div class="chip offline"><div class="num" id="numOffline">-</div><div class="lbl">Offline</div></div>
      </div>

      <div id="list"></div>

      <footer>
        Auto-refreshes every 15s &middot; <button id="resetKey">reset key</button>
      </footer>
    </div>
  </div>
</div>

<script>
const STORAGE_KEY = "ds_dashboard_key";
const gate = document.getElementById("keyGate");
const app = document.getElementById("app");
const list = document.getElementById("list");
const gameTabsEl = document.getElementById("gameTabs");

let currentFilter = "all";
let currentGame = "all";
let lastData = null;
let lastUpdatedAt = null;

function formatElapsed(s) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60), rm = m % 60;
  return `${h}h ${rm}m`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function updateAgoText() {
  if (!lastUpdatedAt) return;
  const secs = Math.floor((Date.now() - lastUpdatedAt) / 1000);
  document.getElementById("updatedText").textContent =
    secs < 3 ? "updated just now" : `updated ${formatElapsed(secs)} ago`;
}
setInterval(updateAgoText, 1000);

function buildGameTabs(accounts) {
  const counts = {};
  accounts.forEach((a) => { counts[a.game] = (counts[a.game] || 0) + 1; });
  const games = Object.keys(counts).sort();

  if (currentGame !== "all" && !counts[currentGame]) currentGame = "all";

  const tabs = [{ key: "all", label: "All games", count: accounts.length }]
    .concat(games.map((g) => ({ key: g, label: g, count: counts[g] })));

  gameTabsEl.innerHTML = tabs.map((t) => `
    <div class="tab ${t.key === currentGame ? "active" : ""}" data-game="${escapeHtml(t.key)}">
      ${escapeHtml(t.label)} <span class="badge">${t.count}</span>
    </div>
  `).join("");

  gameTabsEl.querySelectorAll(".tab").forEach((el) => {
    el.addEventListener("click", () => {
      currentGame = el.getAttribute("data-game");
      render(lastData);
    });
  });
}

function render(data) {
  lastData = data;
  lastUpdatedAt = Date.now();
  updateAgoText();

  document.getElementById("numTotal").textContent = data.total;
  document.getElementById("numOnline").textContent = data.online;
  document.getElementById("numOffline").textContent = data.offline;
  document.getElementById("navAll").textContent = data.total;
  document.getElementById("navOnline").textContent = data.online;
  document.getElementById("navOffline").textContent = data.offline;

  const accounts = data.accounts || [];
  buildGameTabs(accounts);

  const filtered = accounts.filter((a) => {
    const matchesStatus =
      currentFilter === "all" ? true : currentFilter === "online" ? a.online : !a.online;
    const matchesGame = currentGame === "all" ? true : a.game === currentGame;
    return matchesStatus && matchesGame;
  });

  if (filtered.length === 0) {
    list.innerHTML = `<div class="empty">No accounts match this view.</div>`;
    return;
  }

  list.innerHTML = filtered.map((a) => {
    const dotClass = a.online ? "online" : "offline";
    const statusText = a.online ? "online" : formatElapsed(a.lastSeenSecondsAgo) + " ago";
    const joinBtn = a.online && a.joinUrl ? `<a class="join" href="${a.joinUrl}">Join</a>` : "";
    return `
      <div class="row">
        <div class="dot ${dotClass}"></div>
        <div class="info">
          <div class="name">${escapeHtml(a.name)}</div>
          <div class="meta">${escapeHtml(a.game)} &middot; ${statusText}</div>
        </div>
        ${joinBtn}
      </div>
    `;
  }).join("");
}

async function refresh() {
  const key = localStorage.getItem(STORAGE_KEY);
  if (!key) return;
  try {
    const res = await fetch("/status?key=" + encodeURIComponent(key));
    if (res.status === 401) {
      localStorage.removeItem(STORAGE_KEY);
      showGate();
      return;
    }
    const data = await res.json();
    render(data);
  } catch (e) {
    console.error("refresh failed", e);
  }
}

function showApp() {
  gate.style.display = "none";
  app.style.display = "block";
  refresh();
}

function showGate() {
  gate.style.display = "flex";
  app.style.display = "none";
}

document.getElementById("keySubmit").addEventListener("click", () => {
  const val = document.getElementById("keyInput").value.trim();
  if (!val) return;
  localStorage.setItem(STORAGE_KEY, val);
  showApp();
});

document.getElementById("refreshBtn").addEventListener("click", refresh);
document.getElementById("resetKey").addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  showGate();
});
document.getElementById("resetKeyNav").addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  showGate();
});

document.querySelectorAll(".navitem[data-filter]").forEach((el) => {
  el.addEventListener("click", () => {
    document.querySelectorAll(".navitem[data-filter]").forEach((n) => n.classList.remove("active"));
    el.classList.add("active");
    currentFilter = el.getAttribute("data-filter");
    render(lastData || { total: 0, online: 0, offline: 0, accounts: [] });
  });
});

if (localStorage.getItem(STORAGE_KEY)) {
  showApp();
} else {
  showGate();
}

setInterval(refresh, 15000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((e) => console.error("SW register failed", e));
}
</script>
</body>
</html>
"""'''


def main():
    if len(sys.argv) != 2:
        print("Usage: python patch_dashboard.py path/to/your_bot.py")
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"File not found: {target}")
        sys.exit(1)

    original = target.read_text(encoding="utf-8")

    # Matches PWA_HTML = """ ... """ as a whole, non-greedy, DOTALL so it
    # spans the many lines of HTML/CSS/JS inside it.
    pattern = re.compile(r'PWA_HTML = """.*?"""', re.DOTALL)
    matches = pattern.findall(original)

    if len(matches) == 0:
        print("Could not find a PWA_HTML = \"\"\" ... \"\"\" block in that file. "
              "Nothing was changed.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"Found {len(matches)} PWA_HTML blocks - expected exactly 1. "
              "Nothing was changed, to be safe.")
        sys.exit(1)

    backup_path = target.with_suffix(target.suffix + ".bak")
    backup_path.write_text(original, encoding="utf-8")

    updated = pattern.sub(lambda m: NEW_PWA_HTML, original, count=1)

    target.write_text(updated, encoding="utf-8")
    print(f"Patched {target}")
    print(f"Backup of the original saved as {backup_path}")


if __name__ == "__main__":
    main()
