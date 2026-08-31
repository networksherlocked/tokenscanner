"""Tarama orkestratörü: veri topla → sinyalleri çalıştır → kararı ver."""

from __future__ import annotations

import asyncio
import logging
import time

from ..rpc.market import fetch_market
from ..rpc.pool import RpcPool
from ..rpc.solana import collect_chain_snapshot
from . import registry
from .classifier import classify
from .signals import SignalContext, run_signals

log = logging.getLogger(__name__)


async def scan_token(pool: RpcPool, mint: str) -> dict:
    started = time.monotonic()

    chain, market = await asyncio.gather(
        collect_chain_snapshot(pool, mint),
        fetch_market(mint),
    )

    # Tokenin doğum zamanı: pazar çiftinin oluşumu, yoksa en erken holder girişi.
    launch_ts = market.pair_created_at
    if not launch_ts:
        times = [h.first_block_time for h in chain.holders if h.first_block_time]
        launch_ts = min(times) if times else None

    ctx = SignalContext(chain=chain, market=market, launch_ts=launch_ts)
    signals = run_signals(ctx)

    age_hours = (time.time() - launch_ts) / 3600 if launch_ts else None
    verdict = classify(
        signals,
        coverage=chain.coverage,
        market_available=market.available,
        token_age_hours=age_hours,
    )

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
        "data_quality": {
            "chain_coverage": chain.coverage,
            "market_available": market.available,
            "errors": chain.errors,
        },
    }
