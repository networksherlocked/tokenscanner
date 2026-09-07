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
    complete: bool = False              # bonding curve doldu / PumpSwap'e taşındı
    pool_address: str | None = None     # graduation sonrası PumpSwap havuzu
    total_supply_raw: int | None = None


async def fetch_pumpfun(mint: str, timeout: float = 10.0) -> PumpMeta | None:
    """pump.fun'da BASILMIŞ bir token değilse (veya API düşükse) None döner.

    pump.fun v3 API'si artık harici tokenları da indeksliyor (`protocol` =
    "non_launchpad", `virtual_sol_reserves` = null). Bunları pump.fun lansmanı
    saymak lansman analizini yanlış çıpaya yönlendirir — bu yüzden yalnızca
    `protocol`/`program` == "pump" olanları kabul ediyoruz.
    """
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

    launchpad = d.get("protocol") or d.get("program")
    is_pump = launchpad == "pump" or (
        launchpad is None
        and str(mint).endswith("pump")
        and d.get("virtual_sol_reserves") is not None
    )
    if not is_pump:
        return None

    ts = d.get("created_timestamp")
    return PumpMeta(
        creator=d.get("creator"),
        created_ts=int(ts) // 1000 if ts else None,
        bonding_curve=d.get("bonding_curve"),
        complete=bool(d.get("complete")),
        pool_address=d.get("pump_swap_pool") or d.get("raydium_pool"),
        total_supply_raw=int(d["total_supply"]) if d.get("total_supply") else None,
    )
