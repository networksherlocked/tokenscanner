"""
Fiyat grafiği verisi — GeckoTerminal OHLCV API (anahtarsız, ücretsiz).

Sonuç ekranındaki grafik artık 3. taraf iframe değil; kendi SVG grafiğimizi
bu uçtan gelen mumlarla çiziyoruz. GeckoTerminal free API ~30 istek/dk, bu
yüzden hem havuz çözümü hem OHLCV kısa süreli önbelleğe alınır.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

_GT = "https://api.geckoterminal.com/api/v2"
# Render'ın veri merkezi IP'si "bot" User-Agent'larda daha çok 429/403 yiyor —
# gerçek tarayıcı başlıkları kullan (DexScreener/pump.fun ile aynı numara).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# preset -> (timeframe, aggregate, limit)
_PRESETS: dict[str, tuple[str, int, int]] = {
    # çizgi
    "1h":   ("minute", 1, 60),
    "24h":  ("minute", 15, 96),
    "7d":   ("hour", 1, 168),
    "30d":  ("hour", 12, 60),
    # mum
    "c1m":  ("minute", 1, 120),
    "c5m":  ("minute", 5, 120),
    "c15m": ("minute", 15, 120),
    "c1h":  ("hour", 1, 120),
}
DEFAULT_PRESET = "24h"

_POOL_TTL = 1800
_OHLCV_TTL = 45
_pool_cache: dict[str, tuple[tuple[str | None, str | None, str | None], float]] = {}
_ohlcv_cache: dict[tuple, tuple[tuple, float]] = {}


async def _get(
    client: httpx.AsyncClient, path: str, params: dict | None = None, tries: int = 3
):
    r = None
    for i in range(tries):
        r = await client.get(_GT + path, params=params)
        if r.status_code == 429 and i < tries - 1:
            await asyncio.sleep(1.5 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    if r is not None:
        r.raise_for_status()


async def _resolve_pool(
    client: httpx.AsyncClient, mint: str
) -> tuple[str | None, str | None, str | None]:
    """(pool_address, base_symbol, dex) — en yüksek likiditeli havuz."""
    now = time.time()
    hit = _pool_cache.get(mint)
    if hit and now - hit[1] < _POOL_TTL:
        return hit[0]
    try:
        data = await _get(
            client, f"/networks/solana/tokens/{mint}/pools", {"page": 1}
        )
    except Exception as exc:  # noqa: BLE001
        log.info("havuz listesi alınamadı %s: %s", mint, exc)
        return (hit[0] if hit else (None, None, None))

    pools = data.get("data") or []
    best, best_rsv = None, -1.0
    for p in pools:
        a = p.get("attributes") or {}
        try:
            rsv = float(a.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            rsv = 0.0
        if rsv > best_rsv and a.get("address"):
            best, best_rsv = p, rsv
    if not best:
        return (None, None, None)

    a = best["attributes"]
    name = (a.get("name") or "").split("/")[0].strip() or None
    dex = None
    rel = (best.get("relationships") or {}).get("dex") or {}
    did = (rel.get("data") or {}).get("id")
    if did:
        dex = did.replace("_", " ")
    out = (a.get("address"), name, dex)
    _pool_cache[mint] = (out, now)
    return out


def _parse_candles(data) -> list[dict]:
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for r in rows:
        if not r or len(r) < 5 or r[1] is None:
            continue
        try:
            out.append({
                "t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]),
                "v": float(r[5]) if len(r) > 5 and r[5] is not None else 0.0,
            })
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out


async def fetch_chart(
    mint: str, preset: str = DEFAULT_PRESET, pair: str | None = None
) -> dict:
    preset = preset if preset in _PRESETS else DEFAULT_PRESET
    tf, agg, lim = _PRESETS[preset]
    ck = (mint, tf, agg, lim)
    now = time.time()

    hit = _ohlcv_cache.get(ck)
    if hit and now - hit[1] < _OHLCV_TTL:
        candles, sym, dex, pool = hit[0]
        return {"live": len(candles) >= 2, "candles": candles,
                "symbol": sym, "dex": dex, "pool": pool, "preset": preset}

    def _stale():
        if hit:
            c, s, d, p = hit[0]
            return {"live": len(c) >= 2, "candles": c, "symbol": s, "dex": d,
                    "pool": p, "preset": preset, "stale": True}
        return None

    try:
        async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as client:
            # 1) pool: taramanın verdiği pair adresi (bir GeckoTerminal isteği
            #    daha az → daha az 429). Yoksa / işe yaramazsa havuz çözümü.
            pool, sym, dex = (pair or None), None, None
            candles: list[dict] = []
            if pool:
                try:
                    data = await _get(
                        client, f"/networks/solana/pools/{pool}/ohlcv/{tf}",
                        {"aggregate": agg, "limit": lim},
                    )
                    candles = _parse_candles(data)
                except Exception:  # noqa: BLE001
                    candles = []
            if len(candles) < 2:
                rpool, sym, dex = await _resolve_pool(client, mint)
                if rpool and rpool != pool:
                    pool = rpool
                    data = await _get(
                        client, f"/networks/solana/pools/{pool}/ohlcv/{tf}",
                        {"aggregate": agg, "limit": lim},
                    )
                    candles = _parse_candles(data)
    except Exception as exc:  # noqa: BLE001
        log.info("grafik verisi alınamadı %s: %s", mint, exc)
        return _stale() or {"live": False, "reason": "unavailable",
                            "candles": [], "pool": pair or None}

    if not pool:
        return _stale() or {"live": False, "reason": "no_pool", "candles": [], "pool": None}

    _ohlcv_cache[ck] = ((candles, sym, dex, pool), now)
    return {"live": len(candles) >= 2, "candles": candles,
            "symbol": sym, "dex": dex, "pool": pool, "preset": preset,
            **({} if len(candles) >= 2 else {"reason": "no_ohlcv"})}
