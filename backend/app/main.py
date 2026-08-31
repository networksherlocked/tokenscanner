"""
SolScope API.

Uç noktalar:
    GET  /api/scan/{mint}        Tarama (cache'li)
    POST /api/scan/{mint}/fresh  Cache'i atlayıp yeniden tara
    GET  /api/history/{mint}     Bu tokenın geçmiş kararları
    GET  /api/recent             Son taranan tokenlar
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .cache import ScanCache
from .engine.scanner import scan_token
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
    yield
    await state["pool"].aclose()


app = FastAPI(title="SolScope", version="0.1.0", lifespan=lifespan)
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


@app.get("/api/health")
async def health():
    return {"status": "ok", "providers": state["pool"].stats()}


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
