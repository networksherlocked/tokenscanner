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
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .cache import ScanCache
from .engine import registry
from .engine.scanner import scan_token, TokenTooSmall
from .render_card import render_badge_svg, render_png
from . import xpost
from .rpc import trades as rpc_trades
from .rpc.market import fetch_market
from .rpc.pool import RpcError, RpcPool
from .rpc.pool import mask_endpoints as pool_mask

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
TRACK_WINDOW = int(os.getenv("TRACK_WINDOW_SEC", "86400"))     # 24 saat
TRACK_DROP = float(os.getenv("TRACK_DROP_PCT", "0.35"))        # %35
TRACK_POLL = int(os.getenv("TRACK_POLL_SEC", "300"))
FLAGGED_VERDICTS = {"bundled", "cabaled"}
_last_track_refresh = 0.0

# --- Öğrenme: yanlış "organic/inconclusive" kararlardan cüzdan/deployer çıkar --
LEARN_ENABLED = os.getenv("LEARN_ENABLED", "1") != "0"
LEARN_MIN_DROP = float(os.getenv("LEARN_MIN_DROP", "0.55"))   # sadece sert çöküşler
LEARN_MAX_WALLETS = int(os.getenv("LEARN_MAX_WALLETS", "12"))


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
        sym = None
        try:
            snap = await fetch_market(mint)
            mcap = snap.market_cap
            sym = snap.symbol
        except Exception:  # noqa: BLE001
            mcap = None
        if mcap:
            mcap_min = mcap if mcap_min is None else min(mcap_min, mcap)
            cache.track_update(mint, mcap, mcap_min, now, symbol=sym)

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

            if outcome == "miss" and LEARN_ENABLED and drop >= LEARN_MIN_DROP:
                try:
                    learn_from_miss(cache, row, drop)
                except Exception:  # noqa: BLE001
                    log.exception("Ders çıkarılamadı: %s", mint)

    # Sembolü eksik eski kayıtları (settled dahil) tazeden doldur.
    try:
        await _backfill_track_symbols(limit=25)
    except Exception:  # noqa: BLE001
        log.exception("Sembol backfill hatası")


def _cluster_evidence(scan: dict) -> tuple[list[str], str]:
    """Kayıtlı taramada koordinasyon izi var mı? Varsa şüpheli cüzdanları + kısa
    açıklama döndür. YOKSA boş — piyasa çöküşünü bundle sanıp masum cüzdanları
    işaretlemeyelim."""
    launch = scan.get("launch") or {}
    buyers = launch.get("buyers") or []
    signals = {s.get("key"): s for s in scan.get("signals", [])}

    reasons: list[str] = []
    suspects: set[str] = set()

    # 1) ortak fonlayıcı (eşik tutmamış olsa bile)
    funders: dict[str, list[str]] = {}
    for b in buyers:
        f = b.get("funder")
        if f:
            funders.setdefault(f, []).append(b.get("owner"))
    for f, owners in funders.items():
        if len(owners) >= 2:
            reasons.append(f"{len(owners)} lansman alıcısı aynı adresten fonlanmış ({f[:6]}…)")
            suspects.update(o for o in owners if o)
            suspects.add(f)

    # 2) çok-hop fonlama ağacı
    ft = launch.get("funding_tree") or {}
    conv = ft.get("convergence") or {}
    if conv.get("buyers", 0) >= 2 and conv.get("ancestor"):
        reasons.append(
            f"{conv['buyers']} alıcı {conv.get('max_hop', 2)} hop geriden tek "
            f"kaynağa çıkıyor ({conv['ancestor'][:6]}…)"
        )
        suspects.add(conv["ancestor"])
    for gf, ffs in (ft.get("grandfunders") or {}).items():
        if len(ffs) >= 2:
            reasons.append(f"{len(ffs)} fonlayıcı tek üst kaynağa çıkıyor ({gf[:6]}…)")
            suspects.add(gf)
            suspects.update(ffs)

    # 3) taze cüzdan kümesi
    fresh = [b.get("owner") for b in buyers if 0 < (b.get("tx_count") or 0) <= 10]
    if len(fresh) >= 3:
        reasons.append(f"{len(fresh)} lansman alıcısı geçmişsiz (taze) cüzdan")
        suspects.update(o for o in fresh if o)

    # 4) motorun zaten "yakın" olduğu sert sinyaller
    for key in ("common_funder", "same_slot_entry", "fee_fingerprint", "identical_balances"):
        sg = signals.get(key)
        if sg and (sg.get("evidence") or {}).get("cluster_size", 0) >= 2:
            reasons.append(f"{key}: {sg['evidence']['cluster_size']} cüzdanlık küme (eşik altı)")

    return sorted(suspects), " · ".join(reasons)


def learn_from_miss(cache: ScanCache, row: dict, drop: float) -> None:
    mint = row["mint"]
    scan = cache.scan_payload(mint)
    if not scan:
        cache.add_lesson(
            mint=mint, symbol=row.get("symbol"), verdict_was=row.get("verdict"),
            outcome="miss", drop_pct=round(drop, 3), scored_at=row.get("scored_at"),
            learned_at=int(time.time()), wallets_flagged=0, deployer=None,
            detail="Orijinal tarama verisi yok — ders çıkarılamadı.",
        )
        return

    suspects, why = _cluster_evidence(scan)
    deployer = ((scan.get("launch") or {}).get("deployer") or {}).get("address")
    pct = f"−%{drop * 100:.0f}"
    flagged_n = 0

    if suspects or deployer:
        note = f"{row.get('symbol') or mint[:6]} '{row.get('verdict')}' dendi, {pct} çöktü"
        for addr in suspects[:LEARN_MAX_WALLETS]:
            if addr and not registry.is_infrastructure(addr):
                cache.flagged_add(addr, note, via=mint, kind="wallet", bump=True)
                flagged_n += 1
        if deployer and not registry.is_infrastructure(deployer):
            cache.flagged_add(
                deployer, note + " (deployer)", via=mint, kind="deployer", bump=True
            )
        _reload_flagged()

    detail = why or "Koordinasyon izi bulunamadı — muhtemelen piyasa çöküşü."
    cache.add_lesson(
        mint=mint, symbol=row.get("symbol"), verdict_was=row.get("verdict"),
        outcome="miss", drop_pct=round(drop, 3), scored_at=row.get("scored_at"),
        learned_at=int(time.time()), wallets_flagged=flagged_n, deployer=deployer,
        detail=detail,
    )
    log.info(
        "DERS: %s (%s → %s) · %s cüzdan işaretlendi · %s",
        mint, row.get("verdict"), pct, flagged_n, detail[:120],
    )


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
    dsn = os.getenv("DATABASE_URL") or None
    state["cache"] = ScanCache(
        path=os.getenv("CACHE_PATH", "data/scans.db"),
        ttl=int(os.getenv("CACHE_TTL", "900")),
        dsn=dsn,
    )
    log.info(
        "Havuz hazır: %s sağlayıcı · depo: %s",
        len(state["pool"].providers),
        "Postgres" if dsn else "SQLite",
    )
    try:
        rows = {r["address"]: (r["note"] or "flagged") for r in state["cache"].flagged_list()}
        registry.set_runtime_flagged(rows)
        if rows:
            log.info("İşaretli cüzdan yüklendi: %s", len(rows))
    except Exception:  # noqa: BLE001
        log.exception("İşaretli cüzdan listesi yüklenemedi")
    try:
        bkey = state["cache"].config_get("birdeye_api_key")
        rpc_trades.set_runtime_config(birdeye_api_key=bkey)
        if bkey:
            log.info("Birdeye anahtarı DB'den yüklendi.")
    except Exception:  # noqa: BLE001
        log.exception("Entegrasyon anahtarları yüklenemedi")
    try:
        rpc_db = state["cache"].config_get("rpc_endpoints")
        if rpc_db:
            state["pool"].reconfigure(rpc_db)
            log.info("RPC uç noktaları DB'den yüklendi.")
    except Exception:  # noqa: BLE001
        log.exception("RPC uç noktaları DB'den yüklenemedi (env'e düşülüyor)")
    try:
        _reconfigure_x()
        if xpost.public_status()["enabled"]:
            log.info("X otomatik paylaşım AÇIK.")
    except Exception:  # noqa: BLE001
        log.exception("X paylaşım yapılandırması yüklenemedi")
    try:
        ttl_db = state["cache"].config_get("cache_ttl_sec")
        if ttl_db and int(ttl_db) > 0:
            state["cache"].ttl = int(ttl_db)
            log.info("Önbellek süresi DB'den: %s sn", ttl_db)
    except Exception:  # noqa: BLE001
        log.exception("Önbellek süresi yüklenemedi")
    track_task = asyncio.create_task(_track_loop())
    yield
    track_task.cancel()
    await state["pool"].aclose()
    state["cache"].close()


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
            result = await scan_token(state["pool"], mint, state["cache"])
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
            # X otomatik paylaşım — bloklamaz, hata taramayı etkilemez.
            asyncio.create_task(xpost.maybe_autopost(result, state["cache"]))
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


@app.get("/api/organic")
async def organic(limit: int = 30):
    """Organic kararlı, çökmemiş tokenlar — karneden ayrı ve daha uzun liste."""
    return {
        "records": state["cache"].organic_list(min(limit, 60)),
        "window_sec": TRACK_WINDOW,
    }


async def _backfill_track_symbols(limit: int = 25, budget: float = 6.0) -> None:
    """Sembolü eksik karne kayıtlarını (settled dahil) DexScreener'dan doldurur.

    Eski kayıtlarda `symbol` çoğu zaman NULL; kayıtlı tarama payload'ı da
    silinmiş olabilir. Burada tazeden çekip kalıcı yazıyoruz. Eşzamanlı ve
    süre sınırlı — /api/track yanıtını fazla bekletmesin.
    """
    cache = state.get("cache")
    if cache is None:
        return
    mints = cache.track_missing_symbol(limit)
    if not mints:
        return

    async def one(mint: str) -> None:
        try:
            snap = await fetch_market(mint)
        except Exception:  # noqa: BLE001
            return
        if snap.symbol:
            cache.track_set_symbol(mint, snap.symbol)

    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(m) for m in mints)), timeout=budget
        )
    except asyncio.TimeoutError:
        pass


@app.get("/api/track")
async def track(limit: int = 20):
    # Instance yeni uyandıysa arka plan döngüsü henüz dönmemiş olabilir —
    # sayfa açılışında bir kez tetikle (throttle'lı, bloklamadan).
    global _last_track_refresh
    now = time.time()
    if now - _last_track_refresh > 45:
        _last_track_refresh = now
        asyncio.create_task(refresh_track())
    await _backfill_track_symbols(limit=min(limit, 50))
    return {
        "records": state["cache"].track_list(min(limit, 50)),
        "window_sec": TRACK_WINDOW,
        "drop_pct": TRACK_DROP,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "store": "postgres" if state["cache"].pg else "sqlite",
        "admin": bool(ADMIN_TOKEN),
        "providers": state["pool"].stats(),
    }


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
        desc = "Paste a Solana mint. Sixteen independent on-chain signals decide."

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


# --- Admin paneli --------------------------------------------------------

import hashlib

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or ""
RPC_QUOTA = int(os.getenv("RPC_MONTHLY_QUOTA", "0"))  # 0 = bilinmiyor


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _admin_enabled() -> bool:
    if ADMIN_TOKEN:
        return True
    try:
        return bool(state["cache"].config_get("admin_token_sha256"))
    except Exception:  # noqa: BLE001
        return False


def _admin_ok(given: str) -> bool:
    if not given:
        return False
    db_hash = None
    try:
        db_hash = state["cache"].config_get("admin_token_sha256")
    except Exception:  # noqa: BLE001
        pass
    if db_hash:
        return secrets.compare_digest(_sha(given), db_hash)
    return bool(ADMIN_TOKEN) and secrets.compare_digest(given, ADMIN_TOKEN)


def _admin(request: Request) -> None:
    if not _admin_enabled():
        raise HTTPException(404, "Admin paneli kapalı (ADMIN_TOKEN tanımsız).")
    given = request.headers.get("X-Admin-Token") or ""
    if not given:
        auth = request.headers.get("Authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not _admin_ok(given):
        raise HTTPException(401, "Yetkisiz.")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    f = _FRONTEND_DIR / "admin.html"
    if not f.is_file():
        raise HTTPException(404, "admin.html yok")
    return HTMLResponse(f.read_text(encoding="utf-8"))


@app.get("/api/admin/overview", dependencies=[Depends(_admin)])
async def admin_overview():
    return {
        "stats": state["cache"].stats(),
        "providers": state["pool"].stats(),
        "config": {
            "track_window_sec": TRACK_WINDOW,
            "track_drop_pct": TRACK_DROP,
            "min_market_cap": float(os.getenv("MIN_MARKET_CAP_USD", "10000")),
            "rate_limit_per_min": RATE_LIMIT,
            "rpc_quota": RPC_QUOTA,
            "cache_ttl_hours": round(state["cache"].ttl / 3600, 2),
        },
    }


@app.post("/api/admin/password", dependencies=[Depends(_admin)])
async def admin_password(payload: dict = Body(...)):
    new = str(payload.get("new", "")).strip()
    if len(new) < 8:
        raise HTTPException(422, "Yeni şifre en az 8 karakter olmalı.")
    state["cache"].config_set("admin_token_sha256", _sha(new))
    log.info("Admin şifresi değiştirildi.")
    return {"ok": True}


@app.get("/api/admin/appeals", dependencies=[Depends(_admin)])
async def admin_appeals(status: str | None = None):
    return {"appeals": state["cache"].appeals_list(status)}


@app.post("/api/admin/appeals/{appeal_id}", dependencies=[Depends(_admin)])
async def admin_appeal_update(appeal_id: int, payload: dict = Body(...)):
    status = str(payload.get("status", "")).strip()
    if status not in ("open", "resolved", "dismissed"):
        raise HTTPException(422, "status: open | resolved | dismissed")
    state["cache"].appeal_set_status(appeal_id, status, payload.get("note"))
    return {"ok": True}


@app.get("/api/admin/track", dependencies=[Depends(_admin)])
async def admin_track():
    return {"records": state["cache"].track_list(200), "window_sec": TRACK_WINDOW}


@app.delete("/api/admin/track/{mint}", dependencies=[Depends(_admin)])
async def admin_track_delete(mint: str):
    state["cache"].track_delete(_validate(mint))
    return {"ok": True}


@app.delete("/api/admin/cache/{mint}", dependencies=[Depends(_admin)])
async def admin_cache_delete(mint: str):
    state["cache"].cache_delete(_validate(mint))
    return {"ok": True}


@app.post("/api/admin/rescan/{mint}", dependencies=[Depends(_admin)])
async def admin_rescan(mint: str):
    mint = _validate(mint)
    state["cache"].cache_delete(mint)
    try:
        return await _run_scan(mint)
    except TokenTooSmall as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, RpcError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/admin/flagged", dependencies=[Depends(_admin)])
async def admin_flagged():
    return {"flagged": state["cache"].flagged_list()}


@app.post("/api/admin/flagged", dependencies=[Depends(_admin)])
async def admin_flagged_add(payload: dict = Body(...)):
    addr = str(payload.get("address", "")).strip()
    if not BASE58.match(addr):
        raise HTTPException(422, "Geçersiz Solana adresi.")
    state["cache"].flagged_add(addr, payload.get("note"))
    _reload_flagged()
    return {"ok": True}


@app.delete("/api/admin/flagged/{address}", dependencies=[Depends(_admin)])
async def admin_flagged_remove(address: str):
    state["cache"].flagged_remove(address.strip())
    _reload_flagged()
    return {"ok": True}


@app.get("/api/admin/lessons", dependencies=[Depends(_admin)])
async def admin_lessons():
    return {"lessons": state["cache"].lessons_list()}


@app.post("/api/admin/lessons/{mint}/undo", dependencies=[Depends(_admin)])
async def admin_lesson_undo(mint: str):
    removed = state["cache"].lesson_undo(_validate(mint))
    _reload_flagged()
    return {"ok": True, "removed": removed}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "…" + key[-4:]


@app.get("/api/admin/settings", dependencies=[Depends(_admin)])
async def admin_settings():
    """Panelden ayarlanabilen tüm yapılandırma tek yerde."""
    cache = state["cache"]
    pool = state["pool"]
    db_rpc = cache.config_get("rpc_endpoints") or ""
    db_bkey = cache.config_get("birdeye_api_key") or ""
    env_bkey = os.getenv("BIRDEYE_API_KEY", "")
    return {
        # --- düzenlenebilir ---
        "cache_ttl_hours": round(cache.ttl / 3600, 3),
        "cache_ttl_source": "db" if cache.config_get("cache_ttl_sec") else "env",
        "rpc_endpoints_masked": pool_mask(pool.raw),
        "rpc_endpoints_source": "db" if db_rpc else "env",
        "rpc_provider_count": len(pool.providers),
        "rpc_has_das": pool.has_das,
        "birdeye": {
            "provider": rpc_trades.provider_name(),
            "configured": rpc_trades.available(),
            "source": "db" if db_bkey else ("env" if env_bkey else "none"),
            "masked": _mask_key(db_bkey or env_bkey),
        },
        "x_autopost": xpost.public_status(),
        # --- yalnızca env (bilgi amaçlı) ---
        "env_only": {
            "min_market_cap": float(os.getenv("MIN_MARKET_CAP_USD", "10000")),
            "rate_limit_per_min": RATE_LIMIT,
            "track_window_sec": TRACK_WINDOW,
            "track_drop_pct": TRACK_DROP,
            "rpc_quota": RPC_QUOTA,
            "database": "postgres" if cache.pg else "sqlite",
        },
    }


@app.post("/api/admin/settings", dependencies=[Depends(_admin)])
async def admin_settings_set(payload: dict = Body(...)):
    """Panelden gelen ayarları uygular (verilen alanlar). Hepsi anında geçerli,
    DB'ye yazılır; ilgili env değişkeni yalnızca başlangıç varsayılanı olur."""
    cache = state["cache"]
    changed: list[str] = []

    if "cache_ttl_hours" in payload:
        try:
            hours = float(payload["cache_ttl_hours"])
        except (TypeError, ValueError):
            raise HTTPException(422, "cache_ttl_hours bir sayı olmalı.") from None
        if not 0 < hours <= 168:
            raise HTTPException(422, "Önbellek süresi 0–168 saat arasında olmalı.")
        sec = int(round(hours * 3600))
        cache.config_set("cache_ttl_sec", str(sec))
        cache.ttl = sec
        changed.append("cache_ttl")

    if "rpc_endpoints" in payload:
        raw = str(payload.get("rpc_endpoints") or "").strip()
        if not raw:
            raise HTTPException(422, "RPC uç noktası boş olamaz.")
        try:
            state["pool"].reconfigure(raw)
        except RpcError as exc:
            raise HTTPException(422, f"Geçersiz RPC yapılandırması: {exc}") from exc
        cache.config_set("rpc_endpoints", raw)
        changed.append("rpc_endpoints")

    if "birdeye_api_key" in payload:
        key = str(payload.get("birdeye_api_key") or "").strip()
        cache.config_set("birdeye_api_key", key)
        rpc_trades.set_runtime_config(birdeye_api_key=key or None)
        changed.append("birdeye")

    if "public_base_url" in payload:
        url = str(payload.get("public_base_url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(422, "public_base_url http(s):// ile başlamalı.")
        cache.config_set("public_base_url", url)
        changed.append("public_base_url")

    # --- X otomatik paylaşım ---
    if "x_autopost_enabled" in payload:
        cache.config_set(
            "x_autopost_enabled", "1" if payload.get("x_autopost_enabled") else "0"
        )
        changed.append("x_enabled")

    for fld, key in (
        ("x_api_key", "x_api_key"), ("x_api_secret", "x_api_secret"),
        ("x_access_token", "x_access_token"), ("x_access_secret", "x_access_secret"),
    ):
        if fld in payload:
            cache.config_set(key, str(payload.get(fld) or "").strip())
            changed.append(fld)

    if "x_autopost_config" in payload and isinstance(
        payload["x_autopost_config"], dict
    ):
        c = payload["x_autopost_config"]
        allowed = {
            "verdicts", "min_mcap", "min_score", "min_confidence",
            "cooldown_h", "max_per_day", "media", "lang",
        }
        clean = {k: v for k, v in c.items() if k in allowed}
        if "verdicts" in clean:
            clean["verdicts"] = [
                x for x in clean["verdicts"]
                if x in ("bundled", "cabaled", "organic", "inconclusive")
            ] or ["bundled"]
        if "lang" in clean and clean["lang"] not in ("en", "tr"):
            clean["lang"] = "en"
        cache.config_set("x_autopost_config", json.dumps(clean))
        changed.append("x_config")

    if not changed:
        raise HTTPException(422, "Değiştirilecek bir alan gönderilmedi.")

    if any(x.startswith("x_") for x in changed) or "public_base_url" in changed:
        _reconfigure_x()

    log.info("Admin ayar değişikliği: %s", ", ".join(changed))
    return {"ok": True, "changed": changed}


@app.post("/api/admin/x/test", dependencies=[Depends(_admin)])
async def admin_x_test(payload: dict = Body(default={})):
    """Manuel test tweet'i — eşik kontrolü yok, sadece kimlik denemesi.

    payload.mint verilirse o tokenın kayıtlı taramasıyla gerçek biçimde atar.
    """
    cache = state["cache"]
    sample = None
    mint = str((payload or {}).get("mint") or "").strip()
    if mint:
        try:
            mint = _validate(mint)
        except HTTPException:
            raise HTTPException(422, "Geçersiz mint.") from None
        sample = cache.scan_payload(mint)
        if not sample:
            raise HTTPException(404, "Bu mint için kayıtlı tarama yok — önce tara.")
    try:
        res = await xpost.send_test_tweet(cache, sample)
    except xpost.XError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"X hatası: {exc}") from exc
    return {"ok": True, **res}


@app.get("/api/admin/x/posts", dependencies=[Depends(_admin)])
async def admin_x_posts():
    return {"posts": state["cache"].x_posts_recent(30)}


def _reload_flagged() -> None:
    rows = {
        r["address"]: (r["note"] or "flagged")
        for r in state["cache"].flagged_list()
    }
    registry.set_runtime_flagged(rows)


def _reconfigure_x() -> None:
    """DB config + env → xpost.configure(). Her admin değişikliğinden sonra çağır."""
    c = state["cache"]
    raw = c.config_get("x_autopost_config")
    extra: dict = {}
    if raw:
        try:
            extra = json.loads(raw) or {}
        except (TypeError, ValueError):
            extra = {}
    xpost.configure({
        "enabled": c.config_get("x_autopost_enabled") == "1",
        "api_key": c.config_get("x_api_key") or os.getenv("X_API_KEY", ""),
        "api_secret": c.config_get("x_api_secret") or os.getenv("X_API_SECRET", ""),
        "access_token": c.config_get("x_access_token")
        or os.getenv("X_ACCESS_TOKEN", ""),
        "access_secret": c.config_get("x_access_secret")
        or os.getenv("X_ACCESS_SECRET", ""),
        "base_url": c.config_get("public_base_url")
        or os.getenv("PUBLIC_BASE_URL", ""),
        **extra,
    })


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
