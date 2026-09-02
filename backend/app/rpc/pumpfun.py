"""
pump.fun meta verisi — anahtarsız, server-side çalışıyor.

Bize üç kritik şeyi verir:
    creator            — deployer cüzdanı (geçmiş rug'ları için)
    created_timestamp  — GERÇEK lansman zamanı (DexScreener'ın pairCreatedAt'i
                          çoğu zaman migration zamanıdır, daha geç)
    bonding_curve      — lansman işlemlerinin çıpası; ilk alıcıları buradan çekeriz
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_ENDPOINT = "https://frontend-api-v3.pump.fun/coins/{mint}"
_UA = "Mozilla/5.0 (compatible; UAVSX/1.0)"


@dataclass
class PumpMeta:
    creator: str | None = None
    created_ts: int | None = None       # unix saniye
    bonding_curve: str | None = None
    complete: bool = False              # bonding curve doldu / Raydium'a taşındı
    total_supply_raw: int | None = None


async def fetch_pumpfun(mint: str, timeout: float = 10.0) -> PumpMeta | None:
    """pump.fun tokeni değilse (veya API düşükse) None döner."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(_ENDPOINT.format(mint=mint))
        if resp.status_code != 200:
            return None
        d = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("pump.fun verisi alınamadı %s: %s", mint, exc)
        return None
    if not isinstance(d, dict) or not d.get("mint"):
        return None

    ts = d.get("created_timestamp")
    return PumpMeta(
        creator=d.get("creator"),
        created_ts=int(ts) // 1000 if ts else None,
        bonding_curve=d.get("bonding_curve"),
        complete=bool(d.get("complete")),
        total_supply_raw=int(d["total_supply"]) if d.get("total_supply") else None,
    )
