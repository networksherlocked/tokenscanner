"""
Sinyal motoru.

Tasarım ilkesi: her sinyal bağımsızdır ve tek başına bir karar veremez.
Karar, birden fazla bağımsız sinyalin aynı yönü göstermesinden (convergence)
doğar. Bu yüzden her sinyal sadece kendi gözlemini ve gücünü döndürür;
sınıflandırmayı classifier.py yapar.

Sinyal ağırlıkları burada TANIMLI ama eşikleri tek yerde topladık —
kalibrasyon yaparken sadece SIGNAL_TUNING'e dokunacaksın.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ..rpc.market import MarketSnapshot
from ..rpc.solana import ChainSnapshot, HolderRecord
from . import registry

DAY = 86_400


# --- Kalibrasyon tablosu ----------------------------------------------------

SIGNAL_TUNING = {
    "top10_concentration_warn": 25.0,   # %
    "top10_concentration_high": 45.0,
    "fresh_wallet_ratio_warn": 0.35,    # oran
    "fresh_wallet_ratio_high": 0.60,
    "wallet_age_cluster_hours": 24,     # token doğumundan önceki pencere
    "wallet_age_cluster_min": 4,        # kaç cüzdan aynı pencerede
    "common_funder_min": 3,             # aynı fonlayıcıdan kaç cüzdan
    "same_slot_window": 5,              # slot farkı
    "same_slot_min": 3,
    "identical_balance_tolerance": 0.02,  # %2 fark aynı sayılır
    "identical_balance_min": 3,
    "fee_fingerprint_min": 4,
    "cex_dominance_ratio": 0.60,        # tek borsa payı
    "liquidity_ratio_thin": 0.03,       # likidite / mcap
}


# --- Sinyal veri yapısı -----------------------------------------------------

@dataclass
class Signal:
    key: str
    label: str
    direction: str          # "bundled" | "cabaled" | "organic"
    weight: float           # taban ağırlık (0..1)
    fired: bool = False
    strength: float = 0.0   # 0..1 — ne kadar güçlü tetiklendi
    detail: str = ""
    evidence: dict = field(default_factory=dict)
    data_ok: bool = True    # bu sinyali hesaplayacak veri var mıydı?

    @property
    def contribution(self) -> float:
        return self.weight * self.strength if self.fired else 0.0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "direction": self.direction,
            "fired": self.fired,
            "strength": round(self.strength, 3),
            "weight": self.weight,
            "detail": self.detail,
            "evidence": self.evidence,
            "data_available": self.data_ok,
        }


@dataclass
class SignalContext:
    chain: ChainSnapshot
    market: MarketSnapshot
    launch_ts: int | None = None  # tokenin tahmini doğum zamanı

    @property
    def real_holders(self) -> list[HolderRecord]:
        """Altyapı adreslerini (LP, CEX, burn) dışlanmış holder listesi."""
        return [
            h
            for h in self.chain.holders
            if not (h.owner and registry.is_infrastructure(h.owner))
            and not registry.is_infrastructure(h.token_account)
        ]


def _ramp(value: float, low: float, high: float) -> float:
    """value'yu low..high aralığında 0..1'e eşler."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


# --- Sinyaller --------------------------------------------------------------

def sig_top10_concentration(ctx: SignalContext) -> Signal:
    s = Signal(
        key="top10_concentration",
        label="Top 10 yoğunlaşması",
        direction="cabaled",
        weight=0.7,
    )
    holders = ctx.real_holders[:10]
    if not holders:
        s.data_ok = False
        return s
    pct = sum(h.share for h in holders)
    s.evidence = {"top10_percent": round(pct, 2), "counted": len(holders)}
    if pct >= SIGNAL_TUNING["top10_concentration_warn"]:
        s.fired = True
        s.strength = _ramp(
            pct,
            SIGNAL_TUNING["top10_concentration_warn"],
            SIGNAL_TUNING["top10_concentration_high"],
        )
        s.detail = f"Borsa ve LP dışı ilk 10 cüzdan arzın %{pct:.1f}'ini tutuyor."
    else:
        s.detail = f"İlk 10 cüzdan arzın %{pct:.1f}'i — dağılım makul."
    return s


def sig_fresh_wallets(ctx: SignalContext) -> Signal:
    s = Signal(
        key="fresh_wallets",
        label="Taze cüzdan oranı",
        direction="bundled",
        weight=0.8,
    )
    holders = [h for h in ctx.real_holders if h.owner_tx_count]
    if len(holders) < 4:
        s.data_ok = False
        return s
    fresh = [h for h in holders if h.owner_tx_count <= 10]
    ratio = len(fresh) / len(holders)
    s.evidence = {"fresh": len(fresh), "total": len(holders), "ratio": round(ratio, 3)}
    if ratio >= SIGNAL_TUNING["fresh_wallet_ratio_warn"]:
        s.fired = True
        s.strength = _ramp(
            ratio,
            SIGNAL_TUNING["fresh_wallet_ratio_warn"],
            SIGNAL_TUNING["fresh_wallet_ratio_high"],
        )
        s.detail = (
            f"{len(fresh)}/{len(holders)} büyük cüzdanın toplam işlem geçmişi "
            f"10'un altında — geçmişsiz cüzdanlar."
        )
    else:
        s.detail = f"Cüzdanların {len(holders) - len(fresh)}'inin gerçek işlem geçmişi var."
    return s


def sig_wallet_age_cluster(ctx: SignalContext) -> Signal:
    s = Signal(
        key="wallet_age_cluster",
        label="Cüzdan doğum kümesi",
        direction="bundled",
        weight=1.0,
    )
    if not ctx.launch_ts:
        s.data_ok = False
        return s
    ages = [h.owner_created_at for h in ctx.real_holders if h.owner_created_at]
    if len(ages) < 4:
        s.data_ok = False
        return s

    window = SIGNAL_TUNING["wallet_age_cluster_hours"] * 3600
    born_just_before = [t for t in ages if 0 <= ctx.launch_ts - t <= window]
    s.evidence = {
        "clustered": len(born_just_before),
        "total": len(ages),
        "window_hours": SIGNAL_TUNING["wallet_age_cluster_hours"],
    }
    if len(born_just_before) >= SIGNAL_TUNING["wallet_age_cluster_min"]:
        s.fired = True
        s.strength = _ramp(len(born_just_before) / len(ages), 0.3, 0.75)
        s.detail = (
            f"{len(born_just_before)} cüzdan token doğmadan önceki "
            f"{SIGNAL_TUNING['wallet_age_cluster_hours']} saat içinde açılmış."
        )
    else:
        median_age = (ctx.launch_ts - statistics.median(ages)) / DAY
        s.detail = f"Cüzdan yaşları dağınık (medyan {median_age:.0f} gün önce açılmış)."
    return s


def sig_common_funder(ctx: SignalContext) -> Signal:
    s = Signal(
        key="common_funder",
        label="Ortak fonlayıcı",
        direction="bundled",
        weight=1.0,
    )
    resolved = [h.funder for h in ctx.real_holders if h.funder]
    if len(resolved) < 3:
        s.data_ok = False
        return s

    # Borsa ve protokol adresleri "ortak fonlayıcı" sayılmaz — binlerce cüzdan
    # aynı Binance sıcak cüzdanından fonlanır, bu koordinasyon kanıtı değildir.
    # Ama bunu "veri yok" saymak da yanlış: sinyali hesapladık, tetiklenmedi.
    funders = [f for f in resolved if not registry.is_infrastructure(f)]
    if not funders:
        s.detail = "Tüm fonlamalar bilinen borsa adreslerinden — özel ortak fonlayıcı yok."
        s.evidence = {"resolved": len(resolved), "private_funders": 0}
        return s

    counts = Counter(funders)
    top_funder, n = counts.most_common(1)[0]
    s.evidence = {
        "funder": top_funder,
        "wallets": n,
        "resolved": len(funders),
        "distinct_funders": len(counts),
    }
    if n >= SIGNAL_TUNING["common_funder_min"]:
        s.fired = True
        s.strength = _ramp(n / len(funders), 0.25, 0.7)
        s.detail = (
            f"{n} büyük cüzdanın ilk SOL'u aynı adresten geldi "
            f"({top_funder[:6]}…{top_funder[-4:]})."
        )
    else:
        s.detail = f"{len(counts)} farklı fonlama kaynağı — ortak fonlayıcı yok."
    return s


def sig_same_slot_entry(ctx: SignalContext) -> Signal:
    s = Signal(
        key="same_slot_entry",
        label="Eşzamanlı giriş",
        direction="bundled",
        weight=1.0,
    )
    slots = sorted(h.first_slot for h in ctx.real_holders if h.first_slot)
    if len(slots) < 3:
        s.data_ok = False
        return s

    window = SIGNAL_TUNING["same_slot_window"]
    best: list[int] = []
    for i, start in enumerate(slots):
        group = [x for x in slots[i:] if x - start <= window]
        if len(group) > len(best):
            best = group
    s.evidence = {
        "cluster_size": len(best),
        "total": len(slots),
        "slot_window": window,
        "slot_range": [best[0], best[-1]] if best else None,
    }
    if len(best) >= SIGNAL_TUNING["same_slot_min"]:
        s.fired = True
        s.strength = _ramp(len(best) / len(slots), 0.25, 0.8)
        s.detail = (
            f"{len(best)} cüzdan tokena {window} slot (~{window * 0.4:.0f} sn) "
            f"içinde girmiş — tek işlem paketi imzası."
        )
    else:
        s.detail = "Girişler zamana yayılmış."
    return s


def sig_identical_balances(ctx: SignalContext) -> Signal:
    s = Signal(
        key="identical_balances",
        label="Birebir eşit bakiyeler",
        direction="bundled",
        weight=0.9,
    )
    holders = [h for h in ctx.real_holders if h.amount_raw > 0]
    if len(holders) < 4:
        s.data_ok = False
        return s

    tol = SIGNAL_TUNING["identical_balance_tolerance"]
    amounts = sorted(h.amount_raw for h in holders)
    best: list[int] = []
    for i, base in enumerate(amounts):
        group = [a for a in amounts[i:] if abs(a - base) <= base * tol]
        if len(group) > len(best):
            best = group
    s.evidence = {"cluster_size": len(best), "total": len(holders)}
    if len(best) >= SIGNAL_TUNING["identical_balance_min"]:
        s.fired = True
        s.strength = _ramp(len(best) / len(holders), 0.25, 0.7)
        s.detail = (
            f"{len(best)} cüzdanın bakiyesi birbirinin %{tol * 100:.0f}'i içinde — "
            "elle alımda beklenmeyen bir eşitlik."
        )
    else:
        s.detail = "Bakiyeler doğal biçimde farklı."
    return s


def sig_fee_fingerprint(ctx: SignalContext) -> Signal:
    s = Signal(
        key="fee_fingerprint",
        label="İşlem ücreti parmak izi",
        direction="bundled",
        weight=0.7,
    )
    fees = [h.entry_fee for h in ctx.real_holders if h.entry_fee]
    if len(fees) < 4:
        s.data_ok = False
        return s
    counts = Counter(fees)
    fee, n = counts.most_common(1)[0]
    s.evidence = {"fee_lamports": fee, "wallets": n, "total": len(fees)}
    if n >= SIGNAL_TUNING["fee_fingerprint_min"]:
        s.fired = True
        s.strength = _ramp(n / len(fees), 0.3, 0.8)
        s.detail = (
            f"{n} giriş işlemi birebir aynı öncelik ücretini ödemiş "
            f"({fee} lamports) — aynı botun imzası."
        )
    else:
        s.detail = "Giriş ücretleri farklı — tek bir otomasyon izi yok."
    return s


def sig_funding_profile(ctx: SignalContext) -> Signal:
    s = Signal(
        key="funding_profile",
        label="Fonlama kaynağı profili",
        direction="cabaled",
        weight=0.9,
    )
    funders = [h.funder for h in ctx.real_holders if h.funder]
    if len(funders) < 3:
        s.data_ok = False
        return s

    tiers = Counter()
    named = Counter()
    for f in funders:
        meta = registry.classify_address(f)
        if meta["kind"] == "cex":
            tiers[meta["tier"]] += 1
            named[meta["name"]] += 1
        else:
            tiers["unknown"] += 1

    cex_total = sum(v for k, v in tiers.items() if k != "unknown")
    s.evidence = {
        "resolved": len(funders),
        "by_tier": dict(tiers),
        "by_exchange": dict(named),
    }

    if named:
        top_ex, top_n = named.most_common(1)[0]
        dominance = top_n / len(funders)
        if dominance >= SIGNAL_TUNING["cex_dominance_ratio"] and top_n >= 3:
            s.fired = True
            s.strength = _ramp(dominance, 0.6, 0.9)
            s.detail = (
                f"Cüzdanların %{dominance * 100:.0f}'i tek bir borsadan fonlanmış "
                f"({top_ex}) — tek elden dağıtım işareti."
            )
            return s

    if tiers["low_trust"]:
        s.fired = True
        s.direction = "bundled"
        s.strength = _ramp(tiers["low_trust"] / len(funders), 0.1, 0.5)
        s.detail = f"{tiers['low_trust']} cüzdan düşük güvenli kaynaktan fonlanmış."
        return s

    if cex_total >= 3 and tiers["major"] >= tiers["regional"]:
        s.direction = "organic"
        s.detail = (
            f"{tiers['major']} cüzdan büyük borsalardan fonlanmış — "
            "dağıtım doğal görünüyor."
        )
    else:
        s.detail = f"{tiers['unknown']} cüzdanın fonlama kaynağı tanımlı listede yok."
    return s


def sig_flagged_wallets(ctx: SignalContext) -> Signal:
    s = Signal(
        key="flagged_wallets",
        label="Daha önce işaretlenmiş cüzdanlar",
        direction="bundled",
        weight=1.0,
    )
    hits = []
    for h in ctx.real_holders:
        for addr in (h.owner, h.funder):
            if addr and addr in registry.FLAGGED_WALLETS:
                hits.append(addr)
    s.evidence = {"hits": sorted(set(hits))}
    if hits:
        s.fired = True
        s.strength = _ramp(len(set(hits)), 1, 4)
        s.detail = f"{len(set(hits))} adres kendi kayıtlarımızda daha önce işaretlenmiş."
    else:
        s.detail = "Bilinen işaretli cüzdan yok."
    return s


def sig_mint_authority(ctx: SignalContext) -> Signal:
    s = Signal(
        key="mint_authority",
        label="Mint / freeze yetkisi",
        direction="cabaled",
        weight=0.8,
    )
    mi = ctx.chain.mint_info
    s.evidence = {
        "mint_authority": mi.mint_authority,
        "freeze_authority": mi.freeze_authority,
    }
    risks = []
    if mi.mint_authority:
        risks.append(("mint", "arz sonradan artırılabilir"))
    if mi.freeze_authority:
        risks.append(("freeze", "cüzdanlar dondurulup satış engellenebilir"))
    if risks:
        s.fired = True
        s.strength = 1.0 if len(risks) == 2 else 0.6
        names = " ve ".join(r[0] for r in risks)
        effects = "; ".join(r[1] for r in risks)
        s.detail = f"{names} yetkisi hâlâ açık — {effects}."
    else:
        s.direction = "organic"
        s.detail = "Mint ve freeze yetkileri devredilmiş."
    return s


def sig_liquidity_health(ctx: SignalContext) -> Signal:
    s = Signal(
        key="liquidity_health",
        label="Likidite derinliği",
        direction="cabaled",
        weight=0.6,
    )
    if not ctx.market.available or not ctx.market.liquidity_ratio:
        s.data_ok = False
        return s
    ratio = ctx.market.liquidity_ratio
    s.evidence = {
        "liquidity_usd": ctx.market.liquidity_usd,
        "market_cap": ctx.market.market_cap,
        "ratio": round(ratio, 4),
    }
    if ratio < SIGNAL_TUNING["liquidity_ratio_thin"]:
        s.fired = True
        s.strength = _ramp(SIGNAL_TUNING["liquidity_ratio_thin"] - ratio, 0.0, 0.025)
        s.detail = (
            f"Likidite piyasa değerinin sadece %{ratio * 100:.1f}'i — "
            "büyük satışlar fiyatı çökertir."
        )
    else:
        s.direction = "organic"
        s.detail = f"Likidite / piyasa değeri oranı %{ratio * 100:.1f}."
    return s


def sig_wallet_age_diversity(ctx: SignalContext) -> Signal:
    """Pozitif sinyal: cüzdan yaşları gerçekten dağınıksa organikliğe puan."""
    s = Signal(
        key="wallet_age_diversity",
        label="Cüzdan yaşı çeşitliliği",
        direction="organic",
        weight=0.8,
    )
    ages = [h.owner_created_at for h in ctx.real_holders if h.owner_created_at]
    if len(ages) < 5 or not ctx.launch_ts:
        s.data_ok = False
        return s
    days = [(ctx.launch_ts - t) / DAY for t in ages]
    spread = statistics.pstdev(days)
    older_than_month = sum(1 for d in days if d > 30)
    s.evidence = {
        "stdev_days": round(spread, 1),
        "older_than_30d": older_than_month,
        "total": len(days),
    }
    if spread > 45 and older_than_month >= len(days) * 0.5:
        s.fired = True
        s.strength = _ramp(spread, 45, 200)
        s.detail = (
            f"Cüzdanların {older_than_month}/{len(days)}'i tokendan en az bir ay "
            "önce açılmış, yaş dağılımı geniş."
        )
    else:
        s.detail = "Yaş dağılımı organik sayılacak kadar geniş değil."
    return s


ALL_SIGNALS: list[Callable[[SignalContext], Signal]] = [
    sig_wallet_age_cluster,
    sig_common_funder,
    sig_same_slot_entry,
    sig_identical_balances,
    sig_fee_fingerprint,
    sig_fresh_wallets,
    sig_flagged_wallets,
    sig_top10_concentration,
    sig_funding_profile,
    sig_mint_authority,
    sig_liquidity_health,
    sig_wallet_age_diversity,
]


def run_signals(ctx: SignalContext) -> list[Signal]:
    out = []
    for fn in ALL_SIGNALS:
        try:
            out.append(fn(ctx))
        except Exception as exc:  # noqa: BLE001
            broken = Signal(
                key=fn.__name__,
                label=fn.__name__,
                direction="cabaled",
                weight=0.0,
                data_ok=False,
                detail=f"Sinyal hesaplanamadı: {exc}",
            )
            out.append(broken)
    return out
