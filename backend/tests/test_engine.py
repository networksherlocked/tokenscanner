"""
Sentetik senaryolarla motor doğrulaması.

Bunu RPC'ye hiç gitmeden çalıştırabiliyor olman önemli: sinyal eşiklerini
kalibre ederken her denemede kredi harcamak istemezsin.

Çalıştır:  python -m tests.test_engine   (backend/ dizininden)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.classifier import classify  # noqa: E402
from app.engine.signals import SignalContext, run_signals  # noqa: E402
from app.rpc.market import MarketSnapshot  # noqa: E402
from app.rpc.solana import ChainSnapshot, HolderRecord, MintInfo  # noqa: E402

NOW = int(time.time())
LAUNCH = NOW - 3600 * 8  # 8 saat önce doğmuş token
SUPPLY = 1_000_000_000_000_000  # 1B token, 6 decimals


def _mint(mint_auth=None, freeze_auth=None) -> MintInfo:
    return MintInfo(
        mint="So11111111111111111111111111111111111111112",
        decimals=6,
        supply_raw=SUPPLY,
        mint_authority=mint_auth,
        freeze_authority=freeze_auth,
    )


def _market(liq=60_000.0, mcap=800_000.0) -> MarketSnapshot:
    return MarketSnapshot(
        available=True,
        name="Test Token",
        symbol="TEST",
        price_usd=0.0008,
        market_cap=mcap,
        liquidity_usd=liq,
        volume_24h=250_000.0,
        pair_created_at=LAUNCH,
        dex="raydium",
    )


def bundled_case() -> tuple[ChainSnapshot, MarketSnapshot]:
    """Klasik bundle: 8 taze cüzdan, aynı fonlayıcı, aynı slot, eşit bakiye."""
    holders = []
    funder = "BUNDLEFUNDERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    for i in range(8):
        holders.append(
            HolderRecord(
                token_account=f"TA{i:040d}",
                owner=f"OW{i:040d}",
                amount_raw=int(SUPPLY * 0.031) + i * 900,  # ~%3.1, birbirine çok yakın
                first_slot=280_000_000 + (i % 3),          # aynı slot penceresi
                first_block_time=LAUNCH + 12,
                entry_fee=105_000,                          # birebir aynı ücret
                token_tx_count=2,
                owner_created_at=LAUNCH - 3600 * 2,         # launch'tan 2 saat önce
                owner_tx_count=4,
                funder=funder,
            )
        )
    # Bir de LP hesabı
    holders.append(
        HolderRecord(
            token_account="LPACCOUNT",
            owner="5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j",
            amount_raw=int(SUPPLY * 0.30),
        )
    )
    for h in holders:
        h.share = h.amount_raw / SUPPLY * 100
    snap = ChainSnapshot(mint_info=_mint(mint_auth="SOMEAUTH"), holders=holders, coverage=0.95)
    return snap, _market(liq=15_000.0, mcap=900_000.0)


def organic_case() -> tuple[ChainSnapshot, MarketSnapshot]:
    """Dağınık yaşlar, farklı borsalardan fonlama, makul dağılım."""
    cex = [
        "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
        "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
        "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5",
        "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD",
    ]
    ages = [400, 220, 95, 640, 48, 310, 150, 900]  # gün
    holders = []
    for i in range(8):
        holders.append(
            HolderRecord(
                token_account=f"TA{i:040d}",
                owner=f"OW{i:040d}",
                amount_raw=int(SUPPLY * (0.021 - i * 0.002)),
                first_slot=280_000_000 + i * 4_000,
                first_block_time=LAUNCH + i * 1800,
                entry_fee=5_000 + i * 1_137,
                token_tx_count=6 + i,
                owner_created_at=LAUNCH - ages[i] * 86_400,
                owner_tx_count=180 + i * 40,
                funder=cex[i % len(cex)],
            )
        )
    for h in holders:
        h.share = h.amount_raw / SUPPLY * 100
    snap = ChainSnapshot(mint_info=_mint(), holders=holders, coverage=0.98)
    return snap, _market()


def cabaled_case() -> tuple[ChainSnapshot, MarketSnapshot]:
    """Tek borsa baskın + yüksek yoğunlaşma, ama bundle imzası yok."""
    holders = []
    for i in range(8):
        holders.append(
            HolderRecord(
                token_account=f"TA{i:040d}",
                owner=f"OW{i:040d}",
                amount_raw=int(SUPPLY * (0.075 - i * 0.004)),
                first_slot=280_000_000 + i * 9_000,
                first_block_time=LAUNCH + i * 4200,
                entry_fee=7_000 + i * 2_311,
                token_tx_count=9,
                owner_created_at=LAUNCH - (30 + i * 11) * 86_400,
                owner_tx_count=60 + i * 9,
                funder="ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ",  # hepsi MEXC
            )
        )
    for h in holders:
        h.share = h.amount_raw / SUPPLY * 100
    snap = ChainSnapshot(mint_info=_mint(freeze_auth="FRZ"), holders=holders, coverage=0.9)
    return snap, _market(liq=45_000.0, mcap=1_200_000.0)


def aged_whale_case() -> tuple[ChainSnapshot, MarketSnapshot]:
    """Eski token (77 gün). getTokenLargestAccounts lansman paketini göstermez;
    ama tek cüzdan arzın ~%49'unu tutuyor + botsu ücret imzası + ince likidite.
    getSignaturesForAddress bütçesi dolduğu için çoğu cüzdanın yaşı/fonlayıcısı
    çözülemedi (owner_created_at=None, funder=None)."""
    holders = []
    shares = [49.2, 9.2, 2.4, 1.4, 1.0, 0.9, 0.5, 0.4, 0.4, 0.4,
              0.4, 0.35, 0.34, 0.33, 0.31, 0.3, 0.28, 0.27, 0.26]
    for i, sh in enumerate(shares):
        holders.append(
            HolderRecord(
                token_account=f"TA{i:040d}",
                owner=f"OW{i:040d}",
                amount_raw=int(SUPPLY * sh / 100),
                first_slot=429_000_000 + i * 7_000,
                first_block_time=LAUNCH + i * 90_000,
                entry_fee=79_999 if i < 5 else 12_000 + i * 813,
                token_tx_count=3000,
                owner_created_at=None,   # bütçe doldu — yaş çözülemedi
                owner_tx_count=3000,
                funder=None,             # fonlayıcı çözülemedi
            )
        )
    for h in holders:
        h.share = h.amount_raw / SUPPLY * 100
    snap = ChainSnapshot(mint_info=_mint(), holders=holders, coverage=0.33)
    return snap, _market(liq=2_900_000.0, mcap=269_000_000.0)


def run(name: str, builder, age_hours: float = 8.0) -> str:
    chain, market = builder()
    ctx = SignalContext(chain=chain, market=market, launch_ts=LAUNCH)
    signals = run_signals(ctx)
    verdict = classify(
        signals,
        coverage=chain.coverage,
        market_available=market.available,
        token_age_hours=age_hours,
    )
    print(f"\n{'=' * 66}\n{name}\n{'=' * 66}")
    print(f"  KARAR: {verdict.label}  |  skor {verdict.score}  |  güven {verdict.confidence} ({verdict.confidence_label})")
    print("  Tetiklenen sinyaller:")
    for s in signals:
        mark = "●" if s.fired else ("○" if s.data_ok else "×")
        print(f"    {mark} {s.label:32s} {s.detail}")
    for c in verdict.caveats:
        print(f"  ! {c}")
    return verdict.kind


if __name__ == "__main__":
    results = {
        "BUNDLE SENARYOSU": run("BUNDLE SENARYOSU", bundled_case),
        "ORGANİK SENARYO": run("ORGANİK SENARYO", organic_case),
        "CABAL SENARYOSU": run("CABAL SENARYOSU", cabaled_case),
        "ESKİ TOKEN + BALİNA": run("ESKİ TOKEN + BALİNA", aged_whale_case, age_hours=1847.0),
    }
    expected = {
        "BUNDLE SENARYOSU": "bundled",
        "ORGANİK SENARYO": "organic",
        "CABAL SENARYOSU": "cabaled",
        "ESKİ TOKEN + BALİNA": "bundled",
    }
    print(f"\n{'=' * 66}")
    ok = True
    for k, got in results.items():
        want = expected[k]
        status = "GEÇTİ" if got == want else f"KALDI (beklenen {want}, çıkan {got})"
        ok &= got == want
        print(f"  {k:20s} → {got:9s} {status}")
    sys.exit(0 if ok else 1)
