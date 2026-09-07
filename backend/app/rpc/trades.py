"""
Eski / yüksek hacimli tokenlar için lansman verisi — 3. taraf indeksleyiciler.

`collect_launch_snapshot` bonding curve / pair imzalarını ilk bloğa kadar sayar.
Çok eski bir Raydium native pool'da bu sonsuz işlem birikimi demek; bütçe dolar
ve başlangıcı göremeyiz. O zaman bir indeksleyiciden (Birdeye, Bitquery...) en
eski trade'leri çekeriz.

Sağlayıcı-agnostik: `fetch_early_trades()` hangi adaptörün kullanılacağını
`EARLY_TRADES_PROVIDER` env'inden okur. Anahtar tanımlı değilse boş liste döner
ve çağıran taraf sessizce "yalnızca mevcut yapı" moduna düşer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

EARLY_TRADES_PROVIDER = os.getenv("EARLY_TRADES_PROVIDER", "birdeye").lower()
EARLY_TRADES_MAX = int(os.getenv("EARLY_TRADES_MAX", "60"))

BIRDEYE_BASE = os.getenv("BIRDEYE_BASE", "https://public-api.birdeye.so")

# Admin panelinden / DB'den çalışma anında verilen anahtarlar env'i ezer.
_RUNTIME: dict[str, str] = {}


def set_runtime_config(**kw: str | None) -> None:
    """Admin/DB kaynaklı anahtarları canlı ayarlar (ör. birdeye_api_key)."""
    for k, v in kw.items():
        if v:
            _RUNTIME[k] = v.strip()
        else:
            _RUNTIME.pop(k, None)


def _birdeye_key() -> str:
    return _RUNTIME.get("birdeye_api_key") or os.getenv("BIRDEYE_API_KEY", "")


@dataclass
class EarlyTrade:
    owner: str
    amount_raw: int = 0          # + = bu tokenı ALDI, - = sattı
    block_time: int | None = None
    slot: int | None = None
    tx_signature: str | None = None

    @property
    def is_buy(self) -> bool:
        return self.amount_raw > 0


def provider_name() -> str:
    return EARLY_TRADES_PROVIDER


def available() -> bool:
    """Bu sağlayıcı için anahtar/config hazır mı?"""
    if EARLY_TRADES_PROVIDER == "birdeye":
        return bool(_birdeye_key())
    return False  # bilinmeyen / kapalı sağlayıcı


async def fetch_early_trades(
    mint: str, limit: int = EARLY_TRADES_MAX
) -> list[EarlyTrade]:
    """Bir tokenın EN ESKİ ~limit swap'ini (eskiden yeniye) döndürür.

    Anahtar yoksa / hata olursa boş liste — çağıran taraf fallback'e düşsün.
    """
    if not available():
        return []
    try:
        if EARLY_TRADES_PROVIDER == "birdeye":
            trades = await _birdeye_early_trades(mint, limit)
        else:
            log.info("Bilinmeyen EARLY_TRADES_PROVIDER: %s", EARLY_TRADES_PROVIDER)
            return []
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.info("Erken trade çekimi düştü (%s) %s: %s",
                 EARLY_TRADES_PROVIDER, mint, exc)
        return []
    trades.sort(key=lambda t: (t.block_time or 0, t.slot or 0))
    return trades[:limit]


# --- Birdeye adaptörü ------------------------------------------------------
#
# NOT: Birdeye şeması bir anahtarla doğrulanmalı. `_parse_birdeye_item` birden
# çok olası alan adını dener (owner / owner_address, from/to vs base/quote,
# blockUnixTime / blockTime). Anahtar geldiğinde tek bir gerçek şemaya sadeleştir.

async def _birdeye_early_trades(mint: str, limit: int) -> list[EarlyTrade]:
    key = _birdeye_key()
    if not key:
        return []
    headers = {"X-API-KEY": key, "x-chain": "solana"}
    url = f"{BIRDEYE_BASE}/defi/txs/token"
    out: list[EarlyTrade] = []
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        offset = 0
        while len(out) < limit and offset < 240:
            resp = await client.get(
                url,
                params={
                    "address": mint,
                    "tx_type": "swap",
                    "sort_type": "asc",   # en eskiden
                    "offset": offset,
                    "limit": 50,
                },
            )
            if resp.status_code == 401:
                log.warning("Birdeye 401 — BIRDEYE_API_KEY geçersiz.")
                break
            if resp.status_code != 200:
                log.info("Birdeye %s: %s", resp.status_code, resp.text[:200])
                break
            data = (resp.json() or {}).get("data") or {}
            items = data.get("items") or data.get("txs") or []
            if not items:
                break
            for it in items:
                t = _parse_birdeye_item(it, mint)
                if t:
                    out.append(t)
            if not data.get("hasNext", len(items) == 50):
                break
            offset += 50
    return out


def _parse_birdeye_item(it: dict, mint: str) -> EarlyTrade | None:
    owner = (
        it.get("owner")
        or it.get("owner_address")
        or it.get("wallet")
        or it.get("trader")
    )
    if not owner:
        return None

    # Bu token işlemin hangi tarafında? ('to' tarafındaysa alım.)
    sides = []
    for key in ("from", "to", "base", "quote"):
        side = it.get(key)
        if isinstance(side, dict):
            sides.append((key, side))

    amount = 0
    for key, side in sides:
        addr = side.get("address") or side.get("mint")
        if addr != mint:
            continue
        raw = side.get("amount") or side.get("amountRaw") or side.get("ui_amount_raw")
        ui = side.get("uiAmount") or side.get("ui_amount")
        try:
            val = int(raw) if raw is not None else int(float(ui or 0))
        except (TypeError, ValueError):
            val = 0
        # 'from' = tokenı verdi (satış), 'to'/'base' = aldı
        amount = -val if key == "from" else val
        break

    if amount == 0:
        # side ayrımı yoksa 'side' alanına bak
        s = (it.get("side") or it.get("type") or "").lower()
        if s in ("buy", "buys"):
            amount = 1
        elif s in ("sell", "sells"):
            amount = -1

    return EarlyTrade(
        owner=owner,
        amount_raw=amount,
        block_time=it.get("blockUnixTime")
        or it.get("blockTime")
        or it.get("block_unix_time"),
        slot=it.get("slot") or it.get("blockNumber") or it.get("block_number"),
        tx_signature=it.get("txHash") or it.get("tx_hash") or it.get("signature"),
    )
