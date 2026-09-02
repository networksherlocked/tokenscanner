"""Tarama orkestratörü: veri topla → sinyalleri çalıştır → kararı ver.

İki katmanlı analiz:
  1. LANSMAN  — bonding curve / pool çıpasından ilk ~30 alıcı. Bundle sinyalleri
                (yaş kümesi, ortak fonlayıcı, eşzamanlı giriş, eşit bakiye, ücret
                parmak izi, taze cüzdan) BUNLARIN üzerinde çalışır.
  2. MEVCUT YAPI — getTokenLargestAccounts'tan şu anki top 20. Yoğunlaşma, balina,
                likidite, mint yetkisi buradan.
Lansman verisi çekilemezse (çok yüksek hacimli/eski token, bütçe doldu) bundle
sinyalleri mevcut holder'lara düşer ve sonuçta bu belirtilir.
"""

from __future__ import annotations

import logging
import os
import time

from ..rpc.launch import (
    analyze_deployer,
    build_funding_tree,
    collect_launch_snapshot,
    enrich_launch_buyers,
)
from ..rpc.market import fetch_market
from ..rpc.pool import RpcPool
from ..rpc.pumpfun import fetch_pumpfun
from ..rpc.solana import collect_chain_snapshot
from . import registry
from .classifier import classify
from .signals import SignalContext, run_signals

log = logging.getLogger(__name__)

MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP_USD", "10000"))


class TokenTooSmall(Exception):
    """Market cap eşiğin altında; tarama yapılmadı."""


async def scan_token(pool: RpcPool, mint: str) -> dict:
    started = time.monotonic()

    # 1) Piyasa + pump.fun meta (ikisi de anahtarsız, ucuz).
    market = await fetch_market(mint)
    if not market.market_cap or market.market_cap < MIN_MARKET_CAP:
        seen = f"${market.market_cap:,.0f}" if market.market_cap else "unknown"
        raise TokenTooSmall(
            f"This token's market cap is {seen} — UAVSX only scans tokens above "
            f"${MIN_MARKET_CAP:,.0f}. · Bu tokenın market cap'i {seen}; UAVSX "
            f"yalnızca ${MIN_MARKET_CAP:,.0f} üzerindeki tokenları tarar."
        )

    pump = await fetch_pumpfun(mint)

    # Lansman zamanı: pump.fun created_timestamp en doğrusu; yoksa pair oluşumu.
    launch_ts = (pump.created_ts if pump and pump.created_ts else None) or \
        market.pair_created_at

    # 2) Mevcut yapı — hafif (yalnızca sahip + bakiye).
    chain = await collect_chain_snapshot(pool, mint, deep=False)

    # 3) Lansman anlık görüntüsü.
    anchor, source = None, ""
    if pump and pump.bonding_curve:
        anchor, source = pump.bonding_curve, "bonding_curve"
    elif market.pair_address:
        anchor, source = market.pair_address, "pair"

    # Raydium native pool sonsuza dek işlem biriktirir; çok eskiyse imzaları
    # başa kadar saymak bütçeyi boşa harcar. Bonding curve ise migration'dan
    # sonra dondu — geçmişi sınırlı, yaşı ne olursa olsun denenir.
    age_hours_guess = (time.time() - launch_ts) / 3600 if launch_ts else None
    skip_launch = (
        source == "pair" and age_hours_guess and age_hours_guess > 14 * 24
    )
    launch = None
    if anchor and not skip_launch:
        launch = await collect_launch_snapshot(pool, mint, anchor, source)
        if launch.available:
            await enrich_launch_buyers(pool, launch.buyers)
            launch.funding_tree = await build_funding_tree(pool, launch.buyers)
            if not launch_ts:
                times = [b.first_block_time for b in launch.buyers if b.first_block_time]
                launch_ts = min(times) if times else None

    # 4) Deployer geçmişi.
    deployer = None
    creator = pump.creator if pump else None
    if creator:
        deployer = await analyze_deployer(pool, creator, mint)

    # Lansman da mevcut yapı da holder yaşı vermediyse eski fallback: derin tarama.
    launch_ok = bool(launch and launch.available)
    if not launch_ok:
        chain = await collect_chain_snapshot(pool, mint, deep=True)

    ctx = SignalContext(
        chain=chain,
        market=market,
        launch=launch,
        deployer=deployer,
        launch_ts=launch_ts,
    )
    signals = run_signals(ctx)

    # Güven için "coverage": lansman varsa ilk alıcıların yaş+fonlayıcı çözüm
    # oranı; yoksa mevcut holder taramasının oranı.
    if launch_ok:
        b = launch.buyers
        fields = sum(bool(x.owner_created_at) + bool(x.funder) for x in b)
        coverage = round(fields / (len(b) * 2), 3) if b else 0.0
    else:
        coverage = chain.coverage

    age_hours = (time.time() - launch_ts) / 3600 if launch_ts else None
    verdict = classify(
        signals,
        coverage=coverage,
        market_available=market.available,
        token_age_hours=age_hours,
        launch_available=launch_ok,
    )

    bundle_src = "launch" if launch_ok else "current_holders"
    launch_buyers_out = []
    if launch_ok:
        launch_buyers_out = [
            {
                "owner": b.owner,
                "amount": b.amount_raw / (10 ** chain.mint_info.decimals)
                if chain.mint_info.decimals
                else b.amount_raw,
                "share": round(b.share, 2),
                "slot": b.first_slot,
                "tx_count": b.owner_tx_count,
                "created_at": b.owner_created_at,
                "funder": b.funder,
                "funder_tag": registry.classify_address(b.funder)["name"]
                if b.funder
                else None,
            }
            for b in launch.buyers
        ]

    return {
        "mint": mint,
        "scanned_at": int(time.time()),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "token": {
            "name": market.name,
            "symbol": market.symbol,
            "decimals": chain.mint_info.decimals,
            "supply": chain.mint_info.supply,
            "price_usd": market.price_usd,
            "market_cap": market.market_cap,
            "liquidity_usd": market.liquidity_usd,
            "volume_24h": market.volume_24h,
            "dex": market.dex,
            "age_hours": round(age_hours, 1) if age_hours else None,
            "socials": market.socials,
        },
        "verdict": verdict.to_dict(),
        "signals": [s.to_dict() for s in signals],
        "holders": [
            {
                "owner": h.owner,
                "token_account": h.token_account,
                "amount": h.ui_amount,
                "share": round(h.share, 3),
                "tx_count": h.owner_tx_count,
                "created_at": h.owner_created_at,
                "funder": h.funder,
                "funder_tag": registry.classify_address(h.funder)["name"]
                if h.funder
                else None,
                "entry_slot": h.first_slot,
                "tag": registry.classify_address(h.owner)["kind"]
                if h.owner
                else "unknown",
            }
            for h in chain.holders
        ],
        "launch": {
            "source": bundle_src,
            "available": launch_ok,
            "buyer_count": len(launch_buyers_out),
            "buyers": launch_buyers_out,
            "deployer": {
                "address": deployer.address if deployer else None,
                "prior_tokens": deployer.prior_tokens if deployer else 0,
                "checked": deployer.checked if deployer else False,
                "checked_tokens": deployer.checked_tokens if deployer else 0,
                "dead_tokens": deployer.dead_tokens if deployer else 0,
                "dead_rate": round(deployer.dead_rate, 2) if deployer else 0.0,
            },
            "funding_tree": (launch.funding_tree if launch_ok else {}),
        },
        "data_quality": {
            "chain_coverage": chain.coverage,
            "market_available": market.available,
            "bundle_source": bundle_src,
            "errors": chain.errors,
        },
    }
