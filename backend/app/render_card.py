"""
Paylaşılabilir kart (PNG, X/OG için) ve gömülebilir rozet (SVG).

Kart tarama önbelleğinden üretilir; token henüz taranmadıysa genel bir kart
döner. Rozet siteler `<img src=".../badge/MINT.svg">` ile gömer.
"""

from __future__ import annotations

import html
import io
import os
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets"

# tema
_INK = (11, 20, 36)
_PANEL = (23, 38, 63)
_GOLD = (200, 164, 92)
_TEXT = (226, 232, 244)
_MUTED = (135, 151, 179)
_VERDICT_RGB = {
    "bundled": (213, 67, 63),
    "cabaled": (213, 147, 47),
    "organic": (92, 143, 214),
    "inconclusive": (139, 155, 180),
}
_VERDICT_HEX = {
    "bundled": "#d5433f",
    "cabaled": "#d5932f",
    "organic": "#5c8fd6",
    "inconclusive": "#8b9bb4",
}
_LABEL = {
    "bundled": "BUNDLED",
    "cabaled": "CABALED",
    "organic": "ORGANIC",
    "inconclusive": "INCONCLUSIVE",
}


def _short(a: str | None) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else (a or "—")


# ---------- PNG kart -------------------------------------------------------

def _font(name: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(_ASSETS / name), size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def render_png(scan: dict | None, mint: str) -> bytes:
    from PIL import Image, ImageDraw

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), _INK)
    d = ImageDraw.Draw(img)

    # ince guilloche benzeri dikey çizgiler
    for x in range(0, W, 6):
        d.line([(x, 0), (x, H)], fill=(14, 24, 42), width=1)

    kind = (scan or {}).get("verdict", {}).get("kind", "")
    vc = _VERDICT_RGB.get(kind, _GOLD)

    # üst tri-band
    d.rectangle([0, 0, W, 8], fill=_PANEL)
    d.rectangle([0, 0, W // 3, 8], fill=_VERDICT_RGB["bundled"])
    d.rectangle([W // 3, 0, 2 * W // 3, 8], fill=(236, 226, 202))
    d.rectangle([2 * W // 3, 0, W, 8], fill=_VERDICT_RGB["organic"])

    f_kicker = _font("Oswald-SemiBold.ttf", 26)
    f_verdict = _font("Oswald-SemiBold.ttf", 132)
    f_sym = _font("Oswald-SemiBold.ttf", 46)
    f_meta = _font("PlexMono-Medium.ttf", 26)
    f_small = _font("PlexMono-Medium.ttf", 22)
    f_brand = _font("Oswald-SemiBold.ttf", 30)

    d.text((64, 60), "SOLANA · LAUNCH FORENSICS", font=f_kicker, fill=_GOLD)

    if not scan:
        d.text((64, 150), "NOT YET SCANNED", font=f_verdict, fill=_MUTED)
        d.text((64, 320), _short(mint), font=f_meta, fill=_MUTED)
    else:
        tok = scan.get("token") or {}
        v = scan.get("verdict") or {}
        d.text((64, 132), _LABEL.get(kind, kind.upper() or "—"),
               font=f_verdict, fill=vc)

        y = 300
        sym = tok.get("symbol") or ""
        name = tok.get("name") or ""
        d.text((64, y), f"{sym}  {name}".strip()[:38], font=f_sym, fill=_TEXT)
        y += 74
        score = v.get("score", "—")
        conf = v.get("confidence", "—")
        d.text((64, y), f"score {score}   ·   confidence {conf}",
               font=f_meta, fill=_MUTED)
        y += 44
        mcap = tok.get("market_cap")
        mcap_s = f"${mcap:,.0f}" if mcap else "—"
        basis = "launch buyers" if (scan.get("launch") or {}).get("available") \
            else "current holders"
        d.text((64, y), f"mcap {mcap_s}   ·   basis: {basis}",
               font=f_small, fill=_MUTED)
        y += 40
        fired = [s for s in scan.get("signals", []) if s.get("fired")]
        if fired:
            names = ", ".join(s.get("label", s.get("key", "")) for s in fired[:4])
            d.text((64, y), f"fired: {names}"[:78], font=f_small, fill=_MUTED)

    # filigran (dalgalanan ABD bayrağı + altın kartal) + wordmark (sağ alt)
    _draw_watermark(img)
    d.text((64, H - 58), "america", font=f_brand, fill=_TEXT)
    tw = d.textlength("america", font=f_brand)
    d.text((64 + tw + 2, H - 58), ".sx", font=f_brand, fill=_GOLD)
    d.text((64, H - 92), _short(mint), font=f_small, fill=(82, 100, 134))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _frontend_dir() -> Path:
    return Path(
        os.getenv("FRONTEND_DIR") or (Path(__file__).resolve().parents[2] / "frontend")
    )


def _fit_width(im, w: int):
    if im.width == w:
        return im
    return im.resize((w, max(1, round(im.height * w / im.width))))


def _scale_alpha(im, factor: float):
    """RGBA görüntünün alfa kanalını `factor` ile çarpar — silikleştirir."""
    from PIL import Image

    r, g, b, a = im.convert("RGBA").split()
    a = a.point(lambda v: int(v * factor))
    return Image.merge("RGBA", (r, g, b, a))


def _draw_watermark(img) -> None:
    """Sağ alt köşeye silik filigran: dalgalanan ABD bayrağı + altın kartal.

    Görseller (frontend/flag.png, frontend/eagle.png) bulunamazsa sessizce
    atlanır — eski SVG kalkan arması tamamen kaldırıldı.
    """
    from PIL import Image, ImageDraw, ImageFilter

    W, H = img.size
    fdir = _frontend_dir()
    try:
        flag = Image.open(fdir / "flag.png").convert("RGBA")
        eagle = Image.open(fdir / "eagle.png").convert("RGBA")
    except Exception:  # noqa: BLE001
        return

    # bayrak: yumuşak eliptik maskeyle bulanık, çok silik bir yıkama
    flag = _fit_width(flag, int(W * 0.46))
    mask = Image.new("L", flag.size, 0)
    ImageDraw.Draw(mask).ellipse(
        [flag.width * 0.12, flag.height * 0.04,
         flag.width * 1.02, flag.height * 1.05],
        fill=int(255 * 0.10),
    )
    mask = mask.filter(ImageFilter.GaussianBlur(flag.width * 0.12))
    flag.putalpha(mask)
    img.paste(flag, (W - flag.width + 46, H - flag.height + 34), flag)

    # kartal: düz düşük opaklık (altın siluet zaten şeffaf zeminde)
    eagle = _scale_alpha(_fit_width(eagle, int(W * 0.30)), 0.15)
    img.paste(eagle, (W - eagle.width - 8, H - eagle.height + 18), eagle)


# ---------- SVG rozet ----------------------------------------------------

def render_badge_svg(scan: dict | None, mint: str) -> str:
    kind = (scan or {}).get("verdict", {}).get("kind", "")
    label = _LABEL.get(kind, "UNVERIFIED")
    col = _VERDICT_HEX.get(kind, "#8b9bb4")
    score = (scan or {}).get("verdict", {}).get("score")
    right = f"{label}" + (f"  {score}" if score is not None else "")
    right = html.escape(right)
    w = 96 + max(64, len(right) * 9)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="46" role="img" aria-label="america.sx: {right}">
  <rect width="{w}" height="46" rx="3" fill="#0b1424"/>
  <rect x="0.5" y="0.5" width="{w - 1}" height="45" rx="2.5" fill="none" stroke="#283a5c"/>
  <g transform="translate(4,9)" fill="#c8a45c">
    <g transform="scale(0.58)">
      <path d="M32 12 L28 17 L12 15 L22 24 L8 26 L24 31 L14 40 L32 33 L50 40 L40 31 L56 26 L42 24 L52 15 L36 17 Z"/>
      <circle cx="32" cy="13" r="2.4"/>
    </g>
  </g>
  <text x="44" y="19" font-family="Verdana,Segoe UI,sans-serif" font-size="10" fill="#c8a45c" letter-spacing="1">AMERICA.SX</text>
  <text x="44" y="34" font-family="Verdana,Segoe UI,sans-serif" font-size="13" font-weight="bold" fill="{col}">{right}</text>
</svg>"""
