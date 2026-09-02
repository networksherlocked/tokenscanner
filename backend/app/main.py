"""
SolScope API.

Uç noktalar:
    GET  /api/scan/{mint}        Tarama (cache'li)
    POST /api/scan/{mint}/fresh  Cache'i atlayıp yeniden tara
    GET  /api/history/{mint}     Bu tokenın geçmiş kararları
    GET  /api/recent             Son taranan tokenlar
    GET  /api/track              Karne: geçmiş kararlar + sonrasında ne oldu
    GET  /api/health             Sağlayıcı durumu
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .cache import ScanCache
from .engine.scanner import scan_token, TokenTooSmall
from .render_card import render_badge_svg, render_png
from .rpc.market import fetch_market
from .rpc.pool import RpcPool, RpcError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("solscope")

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

state: dict = {}
_inflight: dict[str, asyncio.Task] = {}
_ip_hits: dict[str, list[float]] = {}

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))

# --- Karne (outcome tracking) --------------------------------------------
# Tarama sonrası tokenın market cap'i TRACK_WINDOW_SEC boyunca izlenir.
# Pencerede en düşük noktaya göre düşüş TRACK_DROP_PCT'i geçtiyse "çöktü".
TRACK_WINDOW = int(os.getenv("TRACK_WINDOW_SEC", "1800"))      # 30 dk
TRACK_DROP = float(os.getenv("TRACK_DROP_PCT", "0.35"))        # %35
TRACK_POLL = int(os.getenv("TRACK_POLL_SEC", "120"))
FLAGGED_VERDICTS = {"bundled", "cabaled"}
_last_track_refresh = 0.0


async def refresh_track() -> None:
    """İzlenen tokenların market cap'ini günceller, süresi dolanları sonuçlandırır."""
    cache = state.get("cache")
    if cache is None:
        return
    now = int(time.time())
    pending = cache.track_pending(max_age=TRACK_WINDOW + 3600)
    for row in pending:
        mint = row["mint"]
        mcap_min = row.get("mcap_min")
        try:
            snap = await fetch_market(mint)
            mcap = snap.market_cap
        except Exception:  # noqa: BLE001
            mcap = None
        if mcap:
            mcap_min = mcap if mcap_min is None else min(mcap_min, mcap)
            cache.track_update(mint, mcap, mcap_min, now)

        if now - row["scored_at"] >= TRACK_WINDOW:
            base = row.get("mcap_at_scan")
            drop = (
                max(0.0, (base - mcap_min) / base)
                if base and mcap_min is not None
                else 0.0
            )
            flagged = (row.get("verdict") or "") in FLAGGED_VERDICTS
            crashed = drop >= TRACK_DROP
            if flagged and crashed:
                outcome = "hit"
            elif flagged and not crashed:
                outcome = "no_dump"
            elif not flagged and crashed:
                outcome = "miss"
            else:
                outcome = "clear"
            cache.track_settle(mint, outcome)


async def _track_loop() -> None:
    while True:
        try:
            await refresh_track()
        except Exception:  # noqa: BLE001
            log.exception("Karne yenileme hatası")
        await asyncio.sleep(TRACK_POLL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pool"] = RpcPool()
    state["cache"] = ScanCache(
        path=os.getenv("CACHE_PATH", "data/scans.db"),
        ttl=int(os.getenv("CACHE_TTL", "900")),
    )
    log.info(
        "Havuz hazır: %s sağlayıcı", len(state["pool"].providers)
    )
    track_task = asyncio.create_task(_track_loop())
    yield
    track_task.cancel()
    await state["pool"].aclose()


app = FastAPI(title="america.sx", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < 60]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(429, "Dakikadaki tarama limitini aştın. Biraz bekle.")
    hits.append(now)
    _ip_hits[ip] = hits


def _validate(mint: str) -> str:
    mint = mint.strip()
    if not BASE58.match(mint):
        raise HTTPException(400, "Geçersiz Solana adresi.")
    return mint


async def _run_scan(mint: str) -> dict:
    """Aynı token için eşzamanlı istekleri tek taramada birleştirir."""
    if mint in _inflight:
        return await _inflight[mint]

    async def work() -> dict:
        try:
            result = await scan_token(state["pool"], mint)
            state["cache"].put(mint, result)
            token = result.get("token") or {}
            verdict = result.get("verdict") or {}
            # "inconclusive" bir tahmin değil — karneye alma.
            if verdict.get("kind") != "inconclusive":
                state["cache"].track_start(
                    mint,
                    token.get("symbol"),
                    verdict.get("kind"),
                    verdict.get("score"),
                    token.get("market_cap"),
                )
            return result
        finally:
            _inflight.pop(mint, None)

    task = asyncio.create_task(work())
    _inflight[mint] = task
    return await task


@app.get("/api/scan/{mint}")
async def scan(mint: str, request: Request):
    mint = _validate(mint)
    cached = state["cache"].get(mint)
    if cached:
        return cached
    _check_rate(request)
    try:
        return await _run_scan(mint)
    except TokenTooSmall as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RpcError as exc:
        raise HTTPException(
            503, f"Zincir verisi şu an alınamıyor: {exc}"
        ) from exc


@app.post("/api/scan/{mint}/fresh")
async def rescan(mint: str, request: Request):
    mint = _validate(mint)
    _check_rate(request)
    try:
        return await _run_scan(mint)
    except TokenTooSmall as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RpcError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/history/{mint}")
async def history(mint: str):
    return {"mint": _validate(mint), "history": state["cache"].history(_validate(mint))}


@app.get("/api/recent")
async def recent(limit: int = 20):
    return {"scans": state["cache"].recent(min(limit, 50))}


@app.get("/api/track")
async def track(limit: int = 20):
    # Instance yeni uyandıysa arka plan döngüsü henüz dönmemiş olabilir —
    # sayfa açılışında bir kez tetikle (throttle'lı, bloklamadan).
    global _last_track_refresh
    now = time.time()
    if now - _last_track_refresh > 45:
        _last_track_refresh = now
        asyncio.create_task(refresh_track())
    return {
        "records": state["cache"].track_list(min(limit, 50)),
        "window_sec": TRACK_WINDOW,
        "drop_pct": TRACK_DROP,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "providers": state["pool"].stats()}


# --- Paylaşım: kart, rozet, OG sayfası -----------------------------------

_VLABEL = {
    "bundled": "Bundled", "cabaled": "Cabaled",
    "organic": "Organic", "inconclusive": "Inconclusive",
}


def _cached_scan(mint: str) -> dict | None:
    try:
        mint = _validate(mint)
    except HTTPException:
        return None
    return state["cache"].get(mint)


@app.get("/card/{mint}.png")
async def card_png(mint: str):
    scan = _cached_scan(mint)
    png = render_png(scan, mint)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/badge/{mint}.svg")
async def badge_svg(mint: str):
    scan = _cached_scan(mint)
    return Response(
        content=render_badge_svg(scan, mint),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/t/{mint}", response_class=HTMLResponse)
async def share_page(mint: str, request: Request):
    """Arayüzün aynısı ama <head>'e o tokenın OG etiketleri enjekte edilmiş."""
    if not _FRONTEND_DIR.is_dir():
        raise HTTPException(404, "frontend yok")
    doc = (_FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    scan = _cached_scan(mint)
    base = str(request.base_url).rstrip("/")

    # mint _validate'ten geçti — yalnızca base58, HTML/JS'e güvenli.
    mint = _validate(mint)
    if scan:
        tok = scan.get("token") or {}
        v = scan.get("verdict") or {}
        label = _VLABEL.get(v.get("kind"), "Scanned")
        sym = tok.get("symbol") or mint[:6]
        title = f"{label} — {sym} · america.sx"
        desc = (
            f"{label} · score {v.get('score')} · confidence {v.get('confidence')}. "
            f"{v.get('summary', '')}"
        )[:200]
    else:
        title = "america.sx — Solana launch forensics"
        desc = "Paste a Solana mint. Fifteen independent on-chain signals decide."

    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
        )

    og = (
        f'<meta property="og:type" content="website">'
        f'<meta property="og:title" content="{esc(title)}">'
        f'<meta property="og:description" content="{esc(desc)}">'
        f'<meta property="og:image" content="{base}/card/{mint}.png">'
        f'<meta property="og:url" content="{base}/t/{mint}">'
        f'<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{esc(title)}">'
        f'<meta name="twitter:description" content="{esc(desc)}">'
        f'<meta name="twitter:image" content="{base}/card/{mint}.png">'
        f'<script>window.__PREFILL_MINT__="{mint}";</script>'
    )
    doc = doc.replace("</head>", og + "</head>", 1)
    return HTMLResponse(doc)


@app.post("/api/appeal")
async def appeal(request: Request, payload: dict = Body(...)):
    mint = _validate(str(payload.get("mint", "")))
    body = str(payload.get("body", "")).strip()
    contact = str(payload.get("contact", "")).strip() or None
    if len(body) < 20:
        raise HTTPException(422, "Lütfen itirazını biraz daha açık yaz (en az 20 karakter).")
    if len(body) > 4000:
        body = body[:4000]
    ip = request.client.host if request.client else "unknown"
    cache = state["cache"]
    if cache.appeals_today(contact, ip) >= 5:
        raise HTTPException(429, "Bugünlük itiraz limitine ulaştın. Yarın tekrar dene.")
    cached = cache.get(mint)
    verdict = (cached or {}).get("verdict", {}).get("kind")
    # IP'yi contact yoksa istismar anahtarı olarak sakla (e-posta değil)
    aid = cache.add_appeal(mint, verdict, contact or ip, body)
    log.info("İtiraz #%s — %s (%s)", aid, mint, verdict)
    return {"ok": True, "id": aid}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Beklenmeyen hata: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Tarama tamamlanamadı. Tekrar dene."},
    )


# --- Statik arayüz ---------------------------------------------------------
# API rotalarından SONRA bağlanmalı: "/" catch-all olduğu için önce eklenirse
# /api/* isteklerini gölgeler. FRONTEND_DIR yoksa (yalnız-API dağıtımı) atlanır.
_FRONTEND_DIR = Path(
    os.getenv("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend")
)
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    log.info("Arayüz sunuluyor: %s", _FRONTEND_DIR)
else:
    log.warning("Arayüz klasörü bulunamadı (%s) — yalnızca API aktif.", _FRONTEND_DIR)
