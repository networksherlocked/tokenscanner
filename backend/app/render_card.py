"""
Paylaşılabilir kart (PNG, X/OG için) ve gömülebilir rozet (SVG).

Kart, sonuç sayfasındaki "karar" panelinin YATAY (1200×630) versiyonudur;
sayfa diline göre EN veya TR üretilir (`/card/MINT.png?lang=tr`). Token
henüz taranmadıysa genel bir kart döner.
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
_DIM = (82, 100, 134)
_TRACK = (40, 58, 92)
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

# --- diller --------------------------------------------------------------
_T = {
    "en": {
        "kicker": "DETERMINATION",
        "score": "MATCH SCORE",
        "conf": "CONFIDENCE",
        "mcap": "Market cap",
        "liq": "Liquidity",
        "age": "Age",
        "basis": "Analysis basis",
        "launch": "launch buyers",
        "holders": "current holders",
        "age_fmt": lambda h: (f"{h/24:.1f} d" if h and h >= 48 else f"{h:.0f} h") if h else "—",
        "not_scanned": "NOT YET SCANNED",
        "conf_lbl": {"yüksek": "HIGH", "orta": "MEDIUM", "düşük": "LOW", "çok düşük": "VERY LOW"},
        "summary": {
            "bundled": "Supply distribution looks manufactured — coordinated buying patterns detected.",
            "cabaled": "Distribution is insider-heavy or unusual, without the hard signatures of a bundle.",
            "organic": 'No coordinated distribution pattern found. This does not mean "safe" or "will go up".',
            "inconclusive": "Not enough on-chain data was collected to make a firm call.",
        },
    },
    "tr": {
        "kicker": "KARAR",
        "score": "UYUM SKORU",
        "conf": "GÜVEN",
        "mcap": "Piyasa değeri",
        "liq": "Likidite",
        "age": "Yaş",
        "basis": "Analiz temeli",
        "launch": "lansman alıcıları",
        "holders": "mevcut holder'lar",
        "age_fmt": lambda h: (f"{h/24:.1f} gün" if h and h >= 48 else f"{h:.0f} saat") if h else "—",
        "not_scanned": "HENÜZ TARANMADI",
        "conf_lbl": {"yüksek": "YÜKSEK", "orta": "ORTA", "düşük": "DÜŞÜK", "çok düşük": "ÇOK DÜŞÜK"},
        "summary": {},  # TR: taramanın kendi Türkçe özeti kullanılır
    },
}


def _short(a: str | None) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else (a or "—")


def _money(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    return f"${n:,.0f}"


# ---------- PNG kart -------------------------------------------------------

def _font(name: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(_ASSETS / name), size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int, max_lines: int = 3) -> list[str]:
    out: list[str] = []
    cur = ""
    for word in (text or "").split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
            if len(out) == max_lines:
                break
    if cur and len(out) < max_lines:
        out.append(cur)
    if out and draw.textlength(out[-1], font=font) > max_w:
        while out[-1] and draw.textlength(out[-1] + "…", font=font) > max_w:
            out[-1] = out[-1][:-1]
        out[-1] += "…"
    return out


def render_png(scan: dict | None, mint: str, lang: str = "en") -> bytes:
    from PIL import Image, ImageDraw

    T = _T.get(lang if lang in _T else "en")
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), _INK)
    d = ImageDraw.Draw(img)

    # ince dikey guilloche
    for x in range(0, W, 6):
        d.line([(x, 0), (x, H)], fill=(14, 24, 42), width=1)

    kind = (scan or {}).get("verdict", {}).get("kind", "")
    vc = _VERDICT_RGB.get(kind, _GOLD)

    # üst tri-band
    d.rectangle([0, 0, W, 8], fill=_PANEL)
    d.rectangle([0, 0, W // 3, 8], fill=_VERDICT_RGB["bundled"])
    d.rectangle([W // 3, 0, 2 * W // 3, 8], fill=(236, 226, 202))
    d.rectangle([2 * W // 3, 0, W, 8], fill=_VERDICT_RGB["organic"])

    f_kicker = _font("Oswald-SemiBold.ttf", 24)
    f_verdict = _font("Oswald-SemiBold.ttf", 104)
    f_sum = _font("PlexMono-Medium.ttf", 25)
    f_lbl = _font("Oswald-SemiBold.ttf", 20)
    f_val = _font("PlexMono-Medium.ttf", 26)
    f_sym = _font("Oswald-SemiBold.ttf", 40)
    f_small = _font("PlexMono-Medium.ttf", 21)
    f_brand = _font("Oswald-SemiBold.ttf", 28)

    PAD = 64
    LW = 632                      # sol blok genişliği
    d.text((PAD, 52), "SOLANA · LAUNCH FORENSICS", font=f_kicker, fill=_GOLD)
    d.text((PAD, 90), T["kicker"], font=f_kicker, fill=_GOLD)

    if not scan:
        d.text((PAD, 128), T["not_scanned"], font=f_verdict, fill=_MUTED)
        d.text((PAD, 300), _short(mint), font=f_val, fill=_MUTED)
    else:
        tok = scan.get("token") or {}
        v = scan.get("verdict") or {}

        d.text((PAD, 118), _LABEL.get(kind, (kind or "—").upper()),
               font=f_verdict, fill=vc)

        # özet — EN: kendi tablomuz, TR: taramanın Türkçe özeti
        summary = (T["summary"].get(kind) if lang == "en" else None) \
            or v.get("summary") or ""
        y = 250
        for line in _wrap(d, summary, f_sum, LW, 3):
            d.text((PAD, y), line, font=f_sum, fill=_MUTED)
            y += 34

        # skor + güven barları
        def bar(y0, label, value, pct, extra=""):
            d.text((PAD, y0), label, font=f_lbl, fill=_DIM)
            rt = f"{value}" + (f"  ·  {extra}" if extra else "")
            d.text((PAD + LW - d.textlength(rt, font=f_val), y0 - 2), rt,
                   font=f_val, fill=_TEXT)
            by = y0 + 30
            d.rounded_rectangle([PAD, by, PAD + LW, by + 9], radius=4, fill=_TRACK)
            fw = max(8, int(LW * min(100, max(0, pct)) / 100))
            d.rounded_rectangle([PAD, by, PAD + fw, by + 9], radius=4, fill=vc)

        score = v.get("score")
        conf = v.get("confidence")
        clbl = T["conf_lbl"].get(v.get("confidence_label"), "")
        bar(398, T["score"], score if score is not None else "—",
            score if isinstance(score, (int, float)) else 0)
        bar(468, T["conf"], conf if conf is not None else "—",
            conf if isinstance(conf, (int, float)) else 0, clbl)

        # sağ blok — token bilgileri
        rx = PAD + LW + 60
        sym = (tok.get("symbol") or "").upper()
        name = tok.get("name") or ""
        d.text((rx, 118), (sym or _short(mint))[:16], font=f_sym, fill=_TEXT)
        if name and name.upper() != sym:
            d.text((rx, 168), name[:26], font=f_small, fill=_MUTED)

        rows = [
            (T["mcap"], _money(tok.get("market_cap"))),
            (T["liq"], _money(tok.get("liquidity_usd"))),
            (T["age"], T["age_fmt"](tok.get("age_hours"))),
        ]
        basis_key = "launch" if (scan.get("launch") or {}).get("available") else "holders"
        bcount = (scan.get("launch") or {}).get("buyer_count")
        basis_val = T[basis_key] + (f" ({bcount})" if basis_key == "launch" and bcount else "")
        rows.append((T["basis"], basis_val))

        ry = 220
        for lab, val in rows:
            d.text((rx, ry), lab, font=f_lbl, fill=_GOLD)
            d.text((rx, ry + 26), str(val)[:26], font=f_val, fill=_TEXT)
            ry += 78

    # filigran + wordmark
    _draw_watermark(img)
    d.text((PAD, H - 56), "america", font=f_brand, fill=_TEXT)
    tw = d.textlength("america", font=f_brand)
    d.text((PAD + tw + 2, H - 56), ".sx", font=f_brand, fill=_GOLD)
    d.text((PAD, H - 88), _short(mint), font=f_small, fill=_DIM)

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
    atlanır.
    """
    from PIL import Image, ImageDraw, ImageFilter

    W, H = img.size
    fdir = _frontend_dir()
    try:
        flag = Image.open(fdir / "flag.png").convert("RGBA")
        eagle = Image.open(fdir / "eagle.png").convert("RGBA")
    except Exception:  # noqa: BLE001
        return

    flag = _fit_width(flag, int(W * 0.42))
    mask = Image.new("L", flag.size, 0)
    ImageDraw.Draw(mask).ellipse(
        [flag.width * 0.12, flag.height * 0.04,
         flag.width * 1.02, flag.height * 1.05],
        fill=int(255 * 0.09),
    )
    mask = mask.filter(ImageFilter.GaussianBlur(flag.width * 0.12))
    flag.putalpha(mask)
    img.paste(flag, (W - flag.width + 40, H - flag.height + 30), flag)

    eagle = _scale_alpha(_fit_width(eagle, int(W * 0.26)), 0.14)
    img.paste(eagle, (W - eagle.width - 10, H - eagle.height + 14), eagle)


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
