"""Theme color math, resolve_theme(), and accent-colored icon generation."""
import io
from pathlib import Path

from .config import DEFAULT_THEME, PALETTES, STATUS_COLORS, ICON_VERSION
from . import state

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def darken_hex(hex_color: str, factor: float = 0.3) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r, g, b = (max(0, int(c * (1 - factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def lighten_hex(hex_color: str, factor: float = 0.25) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r, g, b = (min(255, int(c + (255 - c) * factor)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def contrast_text_color(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1a1005" if luminance > 0.6 else "#ffffff"


def blend_hex(fg_hex: str, bg_hex: str, alpha: float) -> str:
    fg_hex, bg_hex = fg_hex.lstrip("#"), bg_hex.lstrip("#")
    fr, fg_, fb = int(fg_hex[0:2], 16), int(fg_hex[2:4], 16), int(fg_hex[4:6], 16)
    br, bg_, bb = int(bg_hex[0:2], 16), int(bg_hex[2:4], 16), int(bg_hex[4:6], 16)
    r = round(fr * alpha + br * (1 - alpha))
    g = round(fg_ * alpha + bg_ * (1 - alpha))
    b = round(fb * alpha + bb * (1 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


def resolve_theme() -> dict:
    accent = state.theme.get("accent", DEFAULT_THEME["accent"])
    mode = state.theme.get("mode", DEFAULT_THEME["mode"])
    palette = PALETTES.get(mode, PALETTES["dark"])
    return {
        "accent": accent,
        "accent2": darken_hex(accent, 0.3),
        "accentTint": blend_hex(accent, palette["sidebar"], 0.14),
        "accentText": contrast_text_color(accent),
        "mode": mode,
        **palette,
        **STATUS_COLORS,
    }


def _hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip("#")
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


def _load_icon_font(size: int):
    font_size = max(12, int(size * 0.42))
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=font_size)
    except TypeError:
        return ImageFont.load_default()


def generate_default_icon(size: int, accent_hex: str) -> bytes:
    """Gradient rounded DS icon derived from the saved accent color."""
    top = _hex_to_rgb(lighten_hex(accent_hex, 0.22))
    bot = _hex_to_rgb(darken_hex(accent_hex, 0.28))
    text_fill = contrast_text_color(accent_hex)
    shadow_fill = (0, 0, 0, 90) if text_fill == "#1a1005" else (0, 0, 0, 70)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)

    radius = int(size * 0.22)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    img.putalpha(mask)

    rim = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rim_draw = ImageDraw.Draw(rim)
    inset = max(1, size // 64)
    rim_draw.rounded_rectangle(
        (inset, inset, size - 1 - inset, size - 1 - inset),
        radius=max(1, radius - inset),
        outline=(255, 255, 255, 70),
        width=max(1, size // 90),
    )
    img = Image.alpha_composite(img, rim)

    draw = ImageDraw.Draw(img)
    text = "DS"
    font = _load_icon_font(size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size - th) / 2 - bbox[1] - size * 0.02

    shadow_off = max(1, size // 64)
    draw.text((tx, ty + shadow_off), text, font=font, fill=shadow_fill)
    draw.text((tx, ty), text, font=font, fill=text_fill)
    img.putalpha(mask)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_icon_version() -> str:
    if HAS_PIL:
        accent = state.theme.get("accent", DEFAULT_THEME["accent"]).lstrip("#").lower()
        return f"accent-{accent}-v{ICON_VERSION}"
    return f"fixed-{ICON_VERSION}"


def build_manifest_json() -> str:
    import json
    resolved = resolve_theme()
    version = get_icon_version()
    return json.dumps({
        "name": "DivineSoul Dashboard",
        "short_name": "DS",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "background_color": resolved["bgmain"],
        "theme_color": resolved["bgmain"],
        "icons": [
            {"src": f"/icon-192.png?v={version}", "sizes": "192x192", "type": "image/png"},
            {"src": f"/icon-512.png?v={version}", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })
