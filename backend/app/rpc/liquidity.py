"""
LP (likidite havuzu) kilit durumu.

Bir tokenın likiditesi çekilebiliyorsa (LP tokenları geliştiricinin cüzdanında)
"rug" bir imza tuşu uzaklıktadır. Kilitli/yakılmışsa likidite kalıcıdır.

Tespit sırası (ucuzdan pahalıya):
  1. pump.fun bonding curve       → likidite eğride, migration'a kadar kilitli
  2. pump.fun/PumpSwap AMM         → migration'da LP protokolce kilitlenir
  3. Raydium API (anahtarsız)      → `burnPercent` doğrudan gelir, RPC harcamaz
  4. LP mint analizi (RPC, ~3-4)   → en büyük LP sahibinin authority'si bir
                                      program PDA'sı mı (kilitli) yoksa düz
                                      cüzdan mı (kilitsiz)?
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..engine import registry
from .pool import RpcPool

log = logging.getLogger(__name__)

_RAYDIUM_POOL_API = "https://api-v3.raydium.io/pools/info/ids?ids={ids}"

# Bu oranın üstünde yakılmış+kilitli LP → likidite kalıcı sayılır.
LOCK_SAFE_PCT = 0.90
# Tek bir düz cüzdanda bu kadar LP → geliştirici likiditeyi çekebilir.
DEV_HELD_WARN_PCT = 0.40


@dataclass
class LpLockInfo:
    checked: bool = False
    status: str = "unknown"
    # "burned" | "locked" | "protocol_locked" | "bonding_curve"
    # | "unlocked" | "partial" | "unverified" | "unknown"
    burned_pct: float = 0.0        # LP arzının yakılmış oranı (0..1)
    locked_pct: float = 0.0        # bir program PDA'sında tutulan oran
    dev_held_pct: float = 0.0      # deployer/tek düz cüzdanda tutulan oran
    top_holder_pct: float = 0.0    # en büyük tekil LP sahibinin payı
    lp_mint: str | None = None
    holder_program: str | None = None  # LP'yi tutan PDA'nın sahibi program
    pool_creator: str | None = None    # havuzu açan cüzdan (PumpSwap parse'ından)
    source: str = ""              # pumpfun | raydium_api | pumpswap_pool | lp_mint_analysis
    detail: str = ""

    @property
    def locked_or_burned_pct(self) -> float:
        return min(1.0, self.burned_pct + self.locked_pct)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "status": self.status,
            "burned_pct": round(self.burned_pct, 4),
            "locked_pct": round(self.locked_pct, 4),
            "dev_held_pct": round(self.dev_held_pct, 4),
            "top_holder_pct": round(self.top_holder_pct, 4),
            "lp_mint": self.lp_mint,
            "holder_program": self.holder_program,
            "pool_creator": self.pool_creator,
            "source": self.source,
            "detail": self.detail,
        }


def _addr(v) -> str | None:
    if isinstance(v, dict):
        return v.get("address")
    return v if isinstance(v, str) else None


def _classify(info: LpLockInfo) -> None:
    """burned/locked/dev_held oranlarına bakıp status + detail yazar."""
    lb = info.locked_or_burned_pct
    if info.burned_pct >= LOCK_SAFE_PCT:
        info.status = "burned"
        info.detail = (
            f"LP arzının ~%{info.burned_pct * 100:.0f}'i yakılmış — "
            "likidite kalıcı, çekilemez."
        )
    elif lb >= LOCK_SAFE_PCT:
        prog = (
            registry.LP_LOCKER_PROGRAMS.get(info.holder_program)
            or (info.holder_program[:6] + "…" if info.holder_program else "bir kontrat")
        )
        info.status = "locked"
        info.detail = (
            f"LP'nin ~%{lb * 100:.0f}'i kilitli ({prog}) — "
            "geliştirici likiditeyi çekemez."
        )
    elif info.dev_held_pct >= DEV_HELD_WARN_PCT or (
        info.top_holder_pct >= DEV_HELD_WARN_PCT and info.locked_pct < 0.5
    ):
        pct = max(info.dev_held_pct, info.top_holder_pct)
        info.status = "unlocked"
        info.detail = (
            f"LP tokenlarının ~%{pct * 100:.0f}'i tek bir düz cüzdanda — "
            "geliştirici likiditeyi istediği an çekebilir (rug riski)."
        )
    elif lb > 0.01:
        info.status = "partial"
        info.detail = (
            f"LP'nin ~%{lb * 100:.0f}'i yakılmış/kilitli, kalanı dağınık — "
            "kısmi koruma."
        )
    else:
        info.status = "unlocked"
        info.detail = (
            "LP'nin yakıldığına/kilitlendiğine dair kanıt yok — likidite "
            "çekilebilir (rug riski)."
        )


async def _raydium_pool(pair: str, timeout: float = 8.0) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(_RAYDIUM_POOL_API.format(ids=pair))
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Raydium pool API düştü %s: %s", pair, exc)
        return None
    rows = (data or {}).get("data") or []
    return rows[0] if rows and rows[0] else None


async def _lp_mint_analysis(
    pool: RpcPool, info: LpLockInfo, creator: str | None
) -> None:
    """LP mint'in arzı + en büyük sahiplerinin authority türü."""
    lp = info.lp_mint
    if not lp:
        return
    # Not: info.checked yalnızca analiz TAM bittiğinde True yapılır — bir RPC
    # çağrısı ortada düşerse sinyal "veri yok" kalsın, yanlışlıkla "unlocked"
    # demesin.
    acc = await pool.call("getAccountInfo", [lp, {"encoding": "jsonParsed"}])
    parsed = ((acc or {}).get("value") or {}).get("data", {}).get("parsed", {})
    supply = int(parsed.get("info", {}).get("supply", 0) or 0)
    if supply <= 0:
        info.burned_pct = 1.0
        info.checked = True
        _classify(info)
        return

    largest = await pool.call("getTokenLargestAccounts", [lp])
    rows = [r for r in (largest or {}).get("value", []) if int(r.get("amount", 0)) > 0]
    if not rows:
        info.burned_pct = 1.0
        info.checked = True
        _classify(info)
        return

    owners_res = await pool.call(
        "getMultipleAccounts",
        [[r["address"] for r in rows], {"encoding": "jsonParsed"}],
    )
    owner_vals = (owners_res or {}).get("value", [])

    # LP token hesaplarının authority'lerini çöz, sonra o authority'lerin
    # System Program dışında bir programca sahiplenilip sahiplenilmediğine bak.
    authorities: dict[str, float] = {}
    burned_here = 0.0
    for r, acc in zip(rows, owner_vals):
        frac = int(r["amount"]) / supply
        info.top_holder_pct = max(info.top_holder_pct, frac)
        auth = None
        if acc:
            auth = acc.get("data", {}).get("parsed", {}).get("info", {}).get("owner")
        if auth in registry.BURN_ADDRESSES:
            burned_here += frac
        elif auth:
            authorities[auth] = authorities.get(auth, 0.0) + frac
    info.burned_pct = max(info.burned_pct, burned_here)

    # Authority hesaplarının sahip programını tek çağrıda çek.
    if authorities:
        auth_list = list(authorities)
        acc_res = await pool.call(
            "getMultipleAccounts", [auth_list, {"encoding": "base64"}]
        )
        for a, acc in zip(auth_list, (acc_res or {}).get("value", [])):
            frac = authorities[a]
            prog = (acc or {}).get("owner") if acc else None
            if prog and prog not in (
                registry.SYSTEM_PROGRAM,
                registry.TOKEN_PROGRAM,
                registry.TOKEN_2022_PROGRAM,
            ):
                info.locked_pct += frac
                info.holder_program = info.holder_program or prog
            elif creator and a == creator:
                info.dev_held_pct += frac
            elif frac >= DEV_HELD_WARN_PCT:
                info.dev_held_pct += frac
    info.checked = True
    _classify(info)


async def _pumpswap_pool(pool: RpcPool, info: LpLockInfo, pair: str, mint: str) -> None:
    """PumpSwap havuz hesabını parse eder: creator + lp_mint.

    Anchor `Pool` layout: 8 disc · 1 pool_bump · 2 index · 32 creator ·
    32 base_mint · 32 quote_mint · 32 lp_mint · …
    """
    acc = await pool.call("getAccountInfo", [pair, {"encoding": "base64"}])
    val = (acc or {}).get("value") or {}
    if (val.get("owner") or "") != registry.PUMPSWAP_PROGRAM:
        return
    data = val.get("data")
    raw = data[0] if isinstance(data, list) else data
    if not raw:
        return
    import base64
    try:
        b = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        return
    if len(b) < 139:
        return
    base_mint = registry.b58encode(b[43:75])
    quote_mint = registry.b58encode(b[75:107])
    # layout doğrulama: taranan mint havuzun bir tarafı olmalı
    if mint not in (base_mint, quote_mint):
        log.info("PumpSwap layout eşleşmedi %s (base=%s quote=%s)", pair, base_mint, quote_mint)
        return
    info.pool_creator = registry.b58encode(b[11:43])
    info.lp_mint = registry.b58encode(b[107:139])
    info.source = "pumpswap_pool"


async def analyze_lp_lock(
    pool: RpcPool, mint: str, market, pump, creator: str | None = None
) -> LpLockInfo:
    info = LpLockInfo()
    dex = (getattr(market, "dex", None) or "").lower()
    pair = getattr(market, "pair_address", None)

    # 1) pump.fun bonding curve — likidite eğride (henüz mezun değil)
    if pump and getattr(pump, "bonding_curve", None) and not getattr(
        pump, "complete", False
    ):
        info.checked = True
        info.status = "bonding_curve"
        info.source = "pumpfun"
        info.detail = (
            "Token hâlâ pump.fun bonding curve'ünde — likidite eğride, "
            "migration'a kadar geliştirici çekemez."
        )
        return info

    # 2) GERÇEK pump.fun mezunu — LP protokolce yakılır. Yalnızca pump.fun
    #    API'si onayladığında (pump.complete). DEX adının "pumpswap" olması
    #    TEK BAŞINA yetmez: PumpSwap izinsiz bir AMM, herkes havuz açabilir.
    if pump and getattr(pump, "complete", False):
        info.checked = True
        info.status = "protocol_locked"
        info.source = "pumpfun"
        info.detail = (
            "Token pump.fun'da mezun oldu — likidite PumpSwap'e taşınırken LP "
            "protokol tarafından yakıldı; geliştirici likiditeyi çekemez."
        )
        return info

    if not pair:
        info.status = "unverified"
        info.detail = (
            "LP havuzu adresi bulunamadı — likidite kilit durumu doğrulanamadı. "
            "Kilitli olduğunu VARSAYMAYIN."
        )
        return info

    # 3) LP mint'i çöz: (a) Raydium API  (b) PumpSwap havuz hesabı
    row = await _raydium_pool(pair)
    if row:
        info.lp_mint = _addr(row.get("lpMint"))
        info.source = "raydium_api"
        bp = row.get("burnPercent")
        if bp is not None:
            info.checked = True
            info.burned_pct = max(0.0, min(1.0, float(bp) / 100.0))
            if info.burned_pct >= LOCK_SAFE_PCT:
                _classify(info)
                return info

    if not info.lp_mint and dex in registry.PUMP_AMM_DEXES:
        try:
            await _pumpswap_pool(pool, info, pair, mint)
        except Exception as exc:  # noqa: BLE001
            log.info("PumpSwap havuz parse düştü %s: %s", pair, exc)

    # 4) LP mint holder analizi — herhangi bir fungible LP mint için çalışır
    if info.lp_mint:
        try:
            await _lp_mint_analysis(pool, info, creator or info.pool_creator)
        except Exception as exc:  # noqa: BLE001
            log.info("LP mint analizi düştü %s: %s", info.lp_mint, exc)
        if info.checked:
            return info

    # 5) Doğrulanamadı — bu bir RİSKTİR. "Güvenli / kilitli" DEME.
    info.status = "unverified"
    info.source = info.source or dex
    info.detail = (
        f"LP kilit durumu otomatik doğrulanamadı ({dex or 'bu havuz tipi'} — "
        "ör. Meteora / Orca yoğunlaşmış likidite, pozisyonlar NFT). Likiditenin "
        "çekilebilir olduğunu varsayın; token yaratıcısı likiditeyi çekerse "
        "token dağıtımından bağımsız olarak çöker. Sistem bu tokenı çekilme için "
        "izlemeye alır — çekilirse yaratıcısı kara listeye eklenir."
    )
    return info
