"""
Piyasa verisi — DexScreener (anahtarsız, ücretsiz, ~300 istek/dk).

Tek bağımlı olma: bir sağlayıcı düşerse tarama tamamen ölmemeli, sadece
ilgili sinyaller "veri yok" durumuna geçmeli.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


@dataclass
class MarketSnapshot:
    available: bool = False
    name: str | None = None
    symbol: str | None = None
    price_usd: float | None = None
    market_cap: float | None = None
    liquidity_usd: float | None = None
    volume_24h: float | None = None
    pair_created_at: int | None = None  # unix ts (saniye)
    dex: str | None = None
    pair_address: str | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    socials: list[str] = field(default_factory=list)

    @property
    def liquidity_ratio(self) -> float | None:
        """Likidite / piyasa değeri. Düşükse çıkış zor demektir."""
        if not self.market_cap or not self.liquidity_usd:
            return None
        return self.liquidity_usd / self.market_cap


async def fetch_market(mint: str, timeout: float = 12.0) -> MarketSnapshot:
    snap = MarketSnapshot()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(DEXSCREENER.format(mint=mint))
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Piyasa verisi alınamadı %s: %s", mint, exc)
        return snap

    pairs = data.get("pairs") or []
    if not pairs:
        return snap

    # En derin likiditeye sahip çifti birincil kabul et.
    pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    base = pair.get("baseToken") or {}
    txns = (pair.get("txns") or {}).get("h24") or {}

    snap.available = True
    snap.name = base.get("name")
    snap.symbol = base.get("symbol")
    snap.price_usd = _f(pair.get("priceUsd"))
    snap.market_cap = _f(pair.get("marketCap") or pair.get("fdv"))
    snap.liquidity_usd = _f((pair.get("liquidity") or {}).get("usd"))
    snap.volume_24h = _f((pair.get("volume") or {}).get("h24"))
    snap.dex = pair.get("dexId")
    snap.pair_address = pair.get("pairAddress")
    snap.buys_24h = txns.get("buys")
    snap.sells_24h = txns.get("sells")

    created = pair.get("pairCreatedAt")
    if created:
        snap.pair_created_at = int(created) // 1000  # ms -> s

    info = pair.get("info") or {}
    snap.socials = [
        s.get("url") for s in (info.get("socials") or []) if s.get("url")
    ] + [w.get("url") for w in (info.get("websites") or []) if w.get("url")]

    return snap


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
