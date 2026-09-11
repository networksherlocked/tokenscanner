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
    "wallet_age_batch_span_days": 3.0,  # cüzdanların açıldığı dar pencere
    "wallet_age_batch_min": 6,          # en az kaç cüzdan aynı partide
    "wallet_age_batch_max_days": 45,    # ve hiçbiri bundan eski değil
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
    detail_en: str = ""
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
            "detail_en": self.detail_en,
            "evidence": self.evidence,
            "data_available": self.data_ok,
        }


@dataclass
class SignalContext:
    chain: ChainSnapshot
    market: MarketSnapshot
    launch: object | None = None      # rpc.launch.LaunchSnapshot (duck-typed)
    deployer: object | None = None    # rpc.launch.DeployerInfo
    lp_lock: object | None = None     # rpc.liquidity.LpLockInfo
    launch_ts: int | None = None      # tokenin tahmini doğum zamanı

    @property
    def real_holders(self) -> list[HolderRecord]:
        """Altyapı (CEX, burn, protokol) + program/PDA hesapları (AMM havuz kasası,
        vesting kilidi, çok-imza hazine) dışlanmış MEVCUT holder listesi."""
        return [
            h
            for h in self.chain.holders
            if not (h.owner and registry.is_infrastructure(h.owner))
            and not registry.is_infrastructure(h.token_account)
            and not getattr(h, "is_contract", False)
        ]

    @property
    def launch_ok(self) -> bool:
        return bool(
            self.launch
            and getattr(self.launch, "available", False)
            and len(getattr(self.launch, "buyers", [])) >= 3
        )

    @property
    def bundle_wallets(self) -> list:
        """Bundle sinyallerinin çalışacağı cüzdanlar.

        Lansman verisi varsa İLK ALICILAR (asıl paket burada görünür); yoksa
        mevcut top holder'lara düşülür ve classifier bunu bir caveat'la belirtir.
        """
        if self.launch_ok:
            return list(self.launch.buyers)
        return self.real_holders


def _ramp(value: float, low: float, high: float) -> float:
    """value'yu low..high aralığında 0..1'e eşler."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _obscured_funding(ctx: SignalContext) -> bool:
    """Fonlama ağacı 2+ alıcıyı tek bir gizli ataya bağlıyor mu? Öyleyse
    'her cüzdan ayrı fonlayıcı' bir kamuflajdır — 'bot yarışı' sayma."""
    if not (ctx.launch and getattr(ctx.launch, "available", False)):
        return False
    conv = (getattr(ctx.launch, "funding_tree", {}) or {}).get("convergence") or {}
    return conv.get("buyers", 0) >= 2


def _count_strength(
    count: int, min_count: int, span: int = 4, floor: float = 0.5
) -> float:
    """Eşik-tabanlı bir sinyalin gücü.

    Eşiği geçen bir sinyal EN AZ `floor` güç taşır — böylece "tetiklendi ama
    strength 0, katkı 0" durumu oluşmaz. `span` kadar fazla eşleşme 1.0 yapar.
    Not: pay/toplam oranı DEĞİL, mutlak eşleşme sayısı kullanılır: 20 holder'ın
    5'inin birebir aynı bot ücretini ödemesi, diğer 15'ten bağımsız olarak
    güçlü bir imzadır.
    """
    over = max(0, count - min_count)
    return max(floor, min(1.0, floor + (1.0 - floor) * over / span))


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
        s.detail_en = f"The top 10 non-exchange, non-LP wallets hold {pct:.1f}% of supply."
    else:
        s.detail = f"İlk 10 cüzdan arzın %{pct:.1f}'i — dağılım makul."
        s.detail_en = f"Top 10 wallets hold {pct:.1f}% of supply — reasonable spread."
    return s


def sig_supply_whale(ctx: SignalContext) -> Signal:
    """Tek bir borsa/LP dışı cüzdanda toplanmış arz.

    getTokenLargestAccounts eski tokenlarda lansmandaki paket cüzdanlarını
    göstermez; ama tek bir 'unknown' cüzdanın arzın büyük kısmını tutması,
    dağıtımın organik OLMADIĞININ güçlü ve yaştan bağımsız bir kanıtıdır.
    """
    s = Signal(
        key="supply_whale",
        label="Tek cüzdan baskınlığı",
        direction="bundled",
        weight=1.0,
    )
    holders = [h for h in ctx.real_holders if h.amount_raw > 0]
    if not holders:
        s.data_ok = False
        return s
    top = max(holders, key=lambda h: h.share)
    # top2: en büyük iki 'unknown' cüzdanın toplamı
    top2 = sum(sorted((h.share for h in holders), reverse=True)[:2])
    s.evidence = {
        "top_owner": top.owner,
        "top_share": round(top.share, 2),
        "top2_share": round(top2, 2),
        "top_tag": top.tag,
    }
    WARN, HIGH = 18.0, 40.0
    if top.share >= WARN:
        s.fired = True
        s.strength = max(0.5, _ramp(top.share, WARN, HIGH))
        s.detail = (
            f"Tek bir borsa/LP dışı cüzdan ({top.owner[:6]}…{top.owner[-4:]}) "
            f"arzın %{top.share:.1f}'ini tutuyor — dağıtım tek elde toplanmış."
        )
        s.detail_en = (
            f"A single non-exchange, non-LP wallet ({top.owner[:6]}…{top.owner[-4:]}) "
            f"holds {top.share:.1f}% of supply — distribution has piled up in one hand."
        )
    elif top2 >= 30.0:
        s.fired = True
        s.strength = max(0.45, _ramp(top2, 30.0, 55.0))
        s.detail = f"En büyük iki cüzdan birlikte arzın %{top2:.1f}'ini tutuyor."
        s.detail_en = f"The top two wallets together hold {top2:.1f}% of supply."
    else:
        s.detail = f"En büyük tekil cüzdan arzın %{top.share:.1f}'i — aşırı yoğunlaşma yok."
        s.detail_en = f"Largest single wallet holds {top.share:.1f}% of supply — no excess concentration."
    return s


def sig_launch_dominance(ctx: SignalContext) -> Signal:
    """Lansmanda (bonding curve / havuz açılışı) tek bir cüzdanın arzın büyük
    kısmını kapması.

    supply_whale'in TERSİNE şu anki değil LANSMAN ANINDAKİ payı ölçer: o
    cüzdan sonradan tamamen satıp çıksa ve artık top holder listesinde hiç
    görünmese bile bu sinyal yakalar. Küçük alıcı sayısı (n) bu tür tekil
    süpürmelerde normaldir — çoklu-cüzdan koordinasyon sinyallerinin aksine
    az veriyle de güvenilir çalışır, bu yüzden ctx.launch_ok'un ötesinde ayrı
    bir minimum ARAMAZ.
    """
    s = Signal(
        key="launch_dominance",
        label="Lansmanda tekil hakimiyet",
        direction="bundled",
        weight=1.0,
    )
    if not ctx.launch_ok:
        s.data_ok = False
        return s
    buyers = [b for b in ctx.launch.buyers if b.amount_raw > 0]
    if len(buyers) < 3:
        s.data_ok = False
        return s
    top = max(buyers, key=lambda b: b.share)
    s.evidence = {
        "top_owner": top.owner,
        "top_share": round(top.share, 2),
        "buyers": len(buyers),
    }
    WARN, HIGH = 35.0, 65.0
    if top.share >= WARN:
        s.fired = True
        s.strength = max(0.5, _ramp(top.share, WARN, HIGH))
        s.detail = (
            f"Lansmanda tek bir cüzdan ({top.owner[:6]}…{top.owner[-4:]}) "
            f"arzın %{top.share:.1f}'ini almış — o cüzdan artık elinde "
            f"tutmasa bile bu, lansmanın adil dağıtılmadığının kanıtıdır."
        )
        s.detail_en = (
            f"A single wallet ({top.owner[:6]}…{top.owner[-4:]}) took "
            f"{top.share:.1f}% of supply at launch — even if that wallet no "
            f"longer holds it, this proves the launch wasn't distributed fairly."
        )
    else:
        s.detail = f"Lansmanda en büyük tekil alım %{top.share:.1f} — aşırı yoğunlaşma yok."
        s.detail_en = f"Largest single launch buy was {top.share:.1f}% — no excess concentration."
    return s


def sig_fresh_wallets(ctx: SignalContext) -> Signal:
    s = Signal(
        key="fresh_wallets",
        label="Taze cüzdan oranı",
        direction="bundled",
        weight=0.8,
    )
    holders = [h for h in ctx.bundle_wallets if h.owner_tx_count]
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
        s.detail_en = (
            f"{len(fresh)}/{len(holders)} large wallets have fewer than 10 total "
            f"transactions — histories look brand new."
        )
    else:
        s.detail = f"Cüzdanların {len(holders) - len(fresh)}'inin gerçek işlem geçmişi var."
        s.detail_en = f"{len(holders) - len(fresh)} of the wallets have a real transaction history."
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
    ages = [h.owner_created_at for h in ctx.bundle_wallets if h.owner_created_at]
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
        s.detail_en = (
            f"{len(born_just_before)} wallets were created within "
            f"{SIGNAL_TUNING['wallet_age_cluster_hours']}h before the token's birth."
        )
    else:
        median_age = (ctx.launch_ts - statistics.median(ages)) / DAY
        if median_age < 0:
            s.detail = (
                "Büyük cüzdanların çoğu tokendan SONRA açılmış — lansman kümesi yok."
            )
            s.detail_en = (
                "Most large wallets were created AFTER the token — no launch cluster."
            )
        else:
            s.detail = (
                f"Cüzdan yaşları dağınık (medyan {median_age:.0f} gün önce açılmış)."
            )
            s.detail_en = (
                f"Wallet ages are spread out (median created {median_age:.0f} days earlier)."
            )
    return s


def sig_wallet_age_batch(ctx: SignalContext) -> Signal:
    """Önceden hazırlanıp bekletilmiş cüzdan partisi.

    `wallet_age_cluster` yalnızca lansmandan önceki 24 saate bakar; sofistike
    bir paketleyici cüzdanları günler/haftalar önce açıp "yaşlandırır" ve o
    pencere sinyalini atlatır. Ama hazırlanmış bir parti hâlâ iki ize sahiptir:
    (a) cüzdanların hepsi birbirine çok yakın tarihlerde açılmıştır ve
    (b) hiçbiri gerçekten eski değildir. Dar açılış penceresi + genç yaş,
    24 saatlik kümeden bağımsız bir paket imzasıdır.
    """
    s = Signal(
        key="wallet_age_batch",
        label="Hazırlanmış cüzdan partisi",
        direction="bundled",
        weight=1.0,
    )
    if not ctx.launch_ts:
        s.data_ok = False
        return s
    days_before = sorted(
        (ctx.launch_ts - h.owner_created_at) / DAY
        for h in ctx.bundle_wallets
        if h.owner_created_at and h.owner_created_at <= ctx.launch_ts
    )
    need = SIGNAL_TUNING["wallet_age_batch_min"]
    if len(days_before) < need:
        s.data_ok = False
        return s

    span = SIGNAL_TUNING["wallet_age_batch_span_days"]
    max_days = SIGNAL_TUNING["wallet_age_batch_max_days"]
    # en büyük "span günlük pencere" kümesi
    best: list[float] = []
    for i, base in enumerate(days_before):
        grp = [d for d in days_before[i:] if d - base <= span]
        if len(grp) > len(best):
            best = grp

    window_days = (best[-1] - best[0]) if best else 0.0
    median_days = statistics.median(best) if best else 0.0
    s.evidence = {
        "batch_size": len(best),
        "resolved_ages": len(days_before),
        "window_days": round(window_days, 2),
        "median_days_before": round(median_days, 1),
        "oldest_days": round(days_before[-1], 1),
    }

    # medyan < 1 gün ise bu zaten "taze kohort" — onu wallet_age_cluster görür,
    # burada tekrar saymayalım.
    fresh_cohort = median_days < 1.0
    is_batch = (
        len(best) >= need
        and len(best) >= len(days_before) * 0.6
        and best[-1] <= max_days
        and not fresh_cohort
    )
    if is_batch:
        s.fired = True
        s.strength = _count_strength(len(best), need, span=10)
        s.detail = (
            f"{len(best)} lansman cüzdanı, lansmandan medyan {median_days:.0f} gün "
            f"önce ve {window_days:.1f} günlük dar bir pencerede açılmış — önceden "
            "hazırlanıp bekletilmiş bir cüzdan partisi."
        )
        s.detail_en = (
            f"{len(best)} launch wallets were created a median of {median_days:.0f} "
            f"days before launch, within a tight {window_days:.1f}-day window — "
            "a batch of wallets prepared and held in advance."
        )
    else:
        s.detail = "Lansman cüzdanlarının açılış tarihleri bir parti oluşturmuyor."
        s.detail_en = "Launch wallets' creation dates don't form a batch."
    return s


def sig_common_funder(ctx: SignalContext) -> Signal:
    s = Signal(
        key="common_funder",
        label="Ortak fonlayıcı",
        direction="bundled",
        weight=1.0,
    )
    resolved = [h.funder for h in ctx.bundle_wallets if h.funder]
    if len(resolved) < 2:
        s.data_ok = False
        return s

    # Borsa ve protokol adresleri "ortak fonlayıcı" sayılmaz — binlerce cüzdan
    # aynı Binance sıcak cüzdanından fonlanır, bu koordinasyon kanıtı değildir.
    # Ama bunu "veri yok" saymak da yanlış: sinyali hesapladık, tetiklenmedi.
    funders = [f for f in resolved if not registry.is_infrastructure(f)]
    if not funders:
        s.detail = "Tüm fonlamalar bilinen borsa adreslerinden — özel ortak fonlayıcı yok."
        s.detail_en = "All funding came from known exchange addresses — no private common funder."
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
    # Ortak fonlayıcının fonladığı cüzdanlar arzın ne kadarını tutuyor?
    shared_share = sum(
        h.share for h in ctx.bundle_wallets if h.funder == top_funder
    )
    s.evidence["shared_holder_share"] = round(shared_share, 2)
    # Normalde 3 cüzdan; ama ortak fonlayıcı büyük bir payı (>%10) besliyorsa
    # 2 cüzdan da yeterli.
    threshold = SIGNAL_TUNING["common_funder_min"]
    if n >= 2 and shared_share >= 10.0:
        threshold = 2
    if n >= threshold:
        s.fired = True
        s.strength = _count_strength(n, threshold, span=4)
        s.detail = (
            f"{n} büyük cüzdanın ilk SOL'u aynı adresten geldi "
            f"({top_funder[:6]}…{top_funder[-4:]}) — birlikte arzın "
            f"%{shared_share:.1f}'i."
        )
        s.detail_en = (
            f"{n} large wallets got their first SOL from the same address "
            f"({top_funder[:6]}…{top_funder[-4:]}) — together they hold "
            f"{shared_share:.1f}% of supply."
        )
    else:
        s.detail = f"{len(counts)} farklı fonlama kaynağı — ortak fonlayıcı yok."
        s.detail_en = f"{len(counts)} distinct funding sources — no common funder."
    return s


def sig_same_slot_entry(ctx: SignalContext) -> Signal:
    """Eşzamanlı giriş. Ama dikkat: hype'lı bir lansmanda birbirinden bağımsız
    onlarca bot/sniper (Photon, BonkBot, Trojan…) aynı slotta iner. Bu, market
    yapısıdır — koordinasyon değil. Ayırt eden şeyler:
      • TEK işlemde birden çok alım → tartışmasız paket (operatör imzası)
      • slot kümesinin ne kadar sıkı olduğu (1 slot vs 5 slot)
      • kümedeki cüzdanların ayrı ayrı fonlayıcı/imza kullanması → botsu yarış
    """
    s = Signal(
        key="same_slot_entry",
        label="Eşzamanlı giriş",
        direction="bundled",
        weight=1.0,
    )
    wl = sorted(
        (h for h in ctx.bundle_wallets if h.first_slot),
        key=lambda h: h.first_slot,
    )
    if len(wl) < 3:
        s.data_ok = False
        return s

    window = SIGNAL_TUNING["same_slot_window"]
    best: list = []
    for i in range(len(wl)):
        grp = [h for h in wl[i:] if h.first_slot - wl[i].first_slot <= window]
        if len(grp) > len(best):
            best = grp
    n = len(best)
    if n < SIGNAL_TUNING["same_slot_min"]:
        s.evidence = {"cluster_size": n, "total": len(wl)}
        s.detail = "Girişler zamana yayılmış."
        s.detail_en = "Entries are spread out over time."
        return s

    slot_span = best[-1].first_slot - best[0].first_slot
    sig_groups: dict[str, int] = {}
    for h in best:
        sg = getattr(h, "first_signature", None)
        if sg:
            sig_groups[sg] = sig_groups.get(sg, 0) + 1
    same_tx = sum(c for c in sig_groups.values() if c >= 2)
    distinct_funders = len({h.funder for h in best if h.funder})
    distinct_sigs = len(sig_groups)

    s.evidence = {
        "cluster_size": n,
        "total": len(wl),
        "slot_window": window,
        "slot_range": [best[0].first_slot, best[-1].first_slot],
        "same_tx_buyers": same_tx,
        "distinct_funders": distinct_funders,
    }
    s.fired = True

    # 1) Tek işlemde ≥2 alım — operatörün bir cüzdandan birçok cüzdana alması.
    if same_tx >= 2:
        s.strength = min(1.0, 0.72 + 0.06 * (same_tx - 2))
        s.detail = (
            f"{same_tx} lansman alıcısı TEK işlemde alım yapmış — operatör bir "
            "cüzdandan birden çok cüzdana aldı; tartışmasız paket imzası."
        )
        s.detail_en = (
            f"{same_tx} launch buyers bought in a SINGLE transaction — an "
            "operator bought into multiple wallets from one; an unmistakable bundle signature."
        )
        return s

    # 2) Saf eşzamanlılık — sıkılığa göre ölçekle, botsu yarışsa iskonto et.
    base = _count_strength(n, SIGNAL_TUNING["same_slot_min"], span=5)
    tightness = 1.0 - min(1.0, slot_span / max(1, window))  # 0 slot → 1.0
    mult = 0.55 + 0.45 * tightness
    # Yarış: giriş ≥2 slota YAYILMIŞ (tek Jito paketi değil) + her cüzdan ayrı
    # işlem ve ayrı fonlayıcı. Fonlama ağacı tek kaynağa çıkıyorsa "ayrı
    # fonlayıcı" bir kamuflajdır — yarış sayma.
    race = (
        not _obscured_funding(ctx)
        and slot_span >= 2
        and distinct_sigs >= n * 0.9
        and distinct_funders >= max(3, int(n * 0.7))
    )
    if race:
        mult *= 0.5
    s.strength = round(base * mult, 3)

    if s.strength < 0.12:
        s.fired = False
        s.detail = (
            f"{n} cüzdan {window} slot içinde girdi ama ayrı işlem ve ayrı "
            "fonlayıcılarla — koordinasyondan çok sniper/bot yarışı."
        )
        s.detail_en = (
            f"{n} wallets entered within {window} slots but with separate "
            "transactions and funders — looks more like a sniper/bot race than coordination."
        )
        return s
    if race:
        s.detail = (
            f"{n} cüzdan {slot_span} slot içinde girmiş ama her biri ayrı "
            "fonlayıcı/işlem kullanmış — kısmi koordinasyon işareti."
        )
        s.detail_en = (
            f"{n} wallets entered within {slot_span} slots but each used a "
            "separate funder/transaction — a partial coordination signal."
        )
    else:
        s.detail = (
            f"{n} cüzdan tokena {slot_span} slot (~{slot_span * 0.4:.1f} sn) "
            "içinde girmiş — işlem paketi imzası."
        )
        s.detail_en = (
            f"{n} wallets entered the token within {slot_span} "
            f"slot{'s' if slot_span != 1 else ''} (~{slot_span * 0.4:.1f}s) — "
            "a transaction-bundle signature."
        )
    return s


def sig_identical_balances(ctx: SignalContext) -> Signal:
    """Tekdüze allocation. Elle alımda insanlar $10, $200, $3000 alır — dağılım
    geniştir. Bir operatör arzı böldüğünde miktarlar dar bir banda oturur.

    İki test: (a) dar ±%2 küme (tam kopya botları), (b) tüm setin varyasyon
    katsayısı — jitter'lı paketleri de yakalar (vc.fun / KAMUFLE PAKET).
    """
    s = Signal(
        key="identical_balances",
        label="Tekdüze bakiye dağılımı",
        direction="bundled",
        weight=0.9,
    )
    amounts = sorted(h.amount_raw for h in ctx.bundle_wallets if h.amount_raw > 0)
    if len(amounts) < 4:
        s.data_ok = False
        return s

    tol = SIGNAL_TUNING["identical_balance_tolerance"]
    tight: list[int] = []
    for i, base in enumerate(amounts):
        grp = [a for a in amounts[i:] if abs(a - base) <= base * tol]
        if len(grp) > len(tight):
            tight = grp

    # Varyasyon katsayısı — en büyüğü (çoğu zaman deployer) hariç.
    body = amounts[:-1] if len(amounts) >= 6 else amounts
    mean = statistics.mean(body)
    cv = statistics.pstdev(body) / mean if mean else 1.0
    s.evidence = {
        "tight_cluster": len(tight),
        "total": len(amounts),
        "cv": round(cv, 3),
        "cv_n": len(body),
    }

    tight_hit = len(tight) >= SIGNAL_TUNING["identical_balance_min"]
    cv_hit = len(body) >= 5 and cv < 0.22

    if not tight_hit and not cv_hit:
        s.detail = f"Bakiyeler doğal biçimde farklı (varyasyon katsayısı {cv:.2f})."
        s.detail_en = f"Balances vary naturally (coefficient of variation {cv:.2f})."
        return s

    s.fired = True
    st_tight = _count_strength(len(tight), SIGNAL_TUNING["identical_balance_min"], span=4) if tight_hit else 0.0
    st_cv = _ramp(0.22 - cv, 0.0, 0.17) if cv_hit else 0.0   # cv 0.22→0, cv 0.05→1
    s.strength = round(max(st_tight, 0.4 + 0.6 * st_cv if cv_hit else st_tight), 3)
    if cv_hit and not tight_hit:
        s.detail = (
            f"{len(body)} lansman cüzdanının alım miktarları dar bir banda oturmuş "
            f"(varyasyon katsayısı {cv:.2f}) — elle alımda beklenmeyen tekdüzelik, "
            "tek elden allocation işareti."
        )
        s.detail_en = (
            f"{len(body)} launch wallets' buy sizes sit in a tight band "
            f"(coefficient of variation {cv:.2f}) — unexpected uniformity for "
            "manual buying, a sign of single-source allocation."
        )
    else:
        s.detail = (
            f"{len(tight)} cüzdanın bakiyesi birbirinin %{tol * 100:.0f}'i içinde"
            + (f"; tüm setin varyasyon katsayısı {cv:.2f}" if cv_hit else "")
            + " — elle alımda beklenmeyen eşitlik."
        )
        s.detail_en = (
            f"{len(tight)} wallets' balances are within {tol * 100:.0f}% of each other"
            + (f"; whole-set coefficient of variation {cv:.2f}" if cv_hit else "")
            + " — unexpected uniformity for manual buying."
        )
    return s


def sig_fee_fingerprint(ctx: SignalContext) -> Signal:
    s = Signal(
        key="fee_fingerprint",
        label="İşlem ücreti parmak izi",
        direction="bundled",
        weight=0.7,
    )
    wl = [h for h in ctx.bundle_wallets if h.entry_fee]
    if len(wl) < 4:
        s.data_ok = False
        return s
    counts = Counter(h.entry_fee for h in wl)
    fee, n = counts.most_common(1)[0]
    matching = [h for h in wl if h.entry_fee == fee]
    distinct_funders = len({h.funder for h in matching if h.funder})
    s.evidence = {
        "fee_lamports": fee,
        "wallets": n,
        "total": len(wl),
        "distinct_funders": distinct_funders,
    }
    if n < SIGNAL_TUNING["fee_fingerprint_min"]:
        s.detail = "Giriş ücretleri farklı — tek bir otomasyon izi yok."
        s.detail_en = "Entry fees vary — no single automation fingerprint."
        return s

    s.fired = True
    s.strength = _count_strength(n, SIGNAL_TUNING["fee_fingerprint_min"], span=4)
    # Aynı ücreti ödeyen cüzdanlar ayrı ayrı fonlanmışsa bu bir operatör imzası
    # değil, ortak bir bot/router'ın varsayılan öncelik ücreti olabilir.
    if distinct_funders >= max(3, int(n * 0.7)) and not _obscured_funding(ctx):
        s.strength = round(s.strength * 0.45, 3)
        s.detail = (
            f"{n} işlem aynı öncelik ücretini ({fee} lamports) ödemiş ama ayrı "
            "fonlayıcılarla — muhtemelen ortak bir botun/router'ın varsayılanı."
        )
        s.detail_en = (
            f"{n} transactions paid the same priority fee ({fee} lamports) but "
            "with separate funders — likely a shared bot's/router's default."
        )
        if s.strength < 0.12:
            s.fired = False
    else:
        s.detail = (
            f"{n} giriş işlemi birebir aynı öncelik ücretini ödemiş "
            f"({fee} lamports) — aynı botun imzası."
        )
        s.detail_en = (
            f"{n} entry transactions paid the exact same priority fee "
            f"({fee} lamports) — the same bot's signature."
        )
    return s


def sig_funding_profile(ctx: SignalContext) -> Signal:
    s = Signal(
        key="funding_profile",
        label="Fonlama kaynağı profili",
        direction="cabaled",
        weight=0.9,
    )
    funders = [h.funder for h in ctx.bundle_wallets if h.funder]
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
        # Büyük borsa mı (Binance/Coinbase — binlerce gerçek kullanıcı buradan
        # çeker) yoksa bölgesel/düşük-KYC mi?
        top_tier = None
        for f in funders:
            m = registry.classify_address(f)
            if m["kind"] == "cex" and m["name"] == top_ex:
                top_tier = m["tier"]
                break
        if dominance >= SIGNAL_TUNING["cex_dominance_ratio"] and top_n >= 3:
            if top_tier == "major":
                # Zayıf sinyal — yalnızca neredeyse tümü tek büyük borsadan.
                if dominance >= 0.85 and top_n >= 5:
                    s.fired = True
                    s.strength = round(_ramp(dominance, 0.85, 1.0) * 0.45, 3)
                    s.detail = (
                        f"Cüzdanların %{dominance * 100:.0f}'i tek bir büyük "
                        f"borsadan ({top_ex}) — olağandışı derecede tek kaynak "
                        "(zayıf işaret)."
                    )
                    s.detail_en = (
                        f"{dominance * 100:.0f}% of wallets were funded from a "
                        f"single major exchange ({top_ex}) — unusually single-sourced "
                        "(weak signal)."
                    )
                    if s.strength < 0.12:
                        s.fired = False
                    return s
            else:
                s.fired = True
                s.strength = _ramp(dominance, 0.6, 0.9)
                s.detail = (
                    f"Cüzdanların %{dominance * 100:.0f}'i tek bir borsadan "
                    f"fonlanmış ({top_ex}) — tek elden dağıtım işareti."
                )
                s.detail_en = (
                    f"{dominance * 100:.0f}% of wallets were funded from a "
                    f"single exchange ({top_ex}) — a sign of single-source distribution."
                )
                return s

    if tiers["low_trust"]:
        s.fired = True
        s.direction = "bundled"
        s.strength = _ramp(tiers["low_trust"] / len(funders), 0.1, 0.5)
        s.detail = f"{tiers['low_trust']} cüzdan düşük güvenli kaynaktan fonlanmış."
        s.detail_en = f"{tiers['low_trust']} wallets were funded from a low-trust source."
        return s

    if cex_total >= 3 and tiers["major"] >= tiers["regional"]:
        s.direction = "organic"
        s.detail = (
            f"{tiers['major']} cüzdan büyük borsalardan fonlanmış — "
            "dağıtım doğal görünüyor."
        )
        s.detail_en = (
            f"{tiers['major']} wallets were funded from major exchanges — "
            "distribution looks natural."
        )
    else:
        s.detail = f"{tiers['unknown']} cüzdanın fonlama kaynağı tanımlı listede yok."
        s.detail_en = f"{tiers['unknown']} wallets' funding source isn't in our known list."
    return s


def sig_flagged_wallets(ctx: SignalContext) -> Signal:
    s = Signal(
        key="flagged_wallets",
        label="Daha önce işaretlenmiş cüzdanlar",
        direction="bundled",
        weight=1.0,
    )
    hits = []
    for h in ctx.bundle_wallets:
        for addr in (h.owner, h.funder):
            if addr and registry.is_flagged(addr):
                hits.append(addr)
    s.evidence = {"hits": sorted(set(hits))}
    if hits:
        s.fired = True
        s.strength = _ramp(len(set(hits)), 1, 4)
        s.detail = f"{len(set(hits))} adres kendi kayıtlarımızda daha önce işaretlenmiş."
        s.detail_en = f"{len(set(hits))} address(es) were previously flagged in our own records."
    else:
        s.detail = "Bilinen işaretli cüzdan yok."
        s.detail_en = "No known flagged wallets."
    return s


def sig_deployer_history(ctx: SignalContext) -> Signal:
    """Deployer cüzdanının geçmiş token sayısı (Helius DAS ile).

    Seri token basan bir cüzdan tek başına suç değil ama güçlü bir bağlam
    sinyalidir — özellikle diğer bundle işaretleriyle birlikte.
    """
    s = Signal(
        key="deployer_history",
        label="Deployer geçmişi",
        direction="cabaled",
        weight=0.8,
    )
    dep = ctx.deployer
    addr = getattr(dep, "address", None) if dep else None

    # Öğrenme: bu deployer daha önce bir "miss"te işaretlenmişse anında sert sinyal.
    if addr and registry.is_flagged(addr):
        s.fired = True
        s.direction = "bundled"
        s.strength = 0.85
        s.evidence = {"deployer": addr, "flagged": True}
        s.detail = (
            f"Deployer ({addr[:6]}…{addr[-4:]}) daha önce çöken bir tokende "
            "işaretlenmiş — kendi kayıtlarımızda seri rug profili."
        )
        s.detail_en = (
            f"Deployer ({addr[:6]}…{addr[-4:]}) was flagged before on a token "
            "that later collapsed — a serial-rug profile in our own records."
        )
        return s

    if not dep or not getattr(dep, "checked", False):
        s.data_ok = False
        s.detail = "Deployer geçmişi çıkarılamadı (Helius DAS gerekli)."
        s.detail_en = "Deployer history could not be retrieved (Helius DAS required)."
        return s
    n = getattr(dep, "prior_tokens", 0)
    checked = getattr(dep, "checked_tokens", 0)
    dead = getattr(dep, "dead_tokens", 0)
    rate = getattr(dep, "dead_rate", 0.0)
    s.evidence = {
        "deployer": addr,
        "prior_tokens": n,
        "checked": checked,
        "dead": dead,
        "dead_rate": round(rate, 2),
    }
    if n == 0:
        s.detail = "Deployer'ın ilk tokeni."
        s.detail_en = "This is the deployer's first token."
        return s

    # Seri lansman + yüksek ölüm oranı = güçlü kırmızı bayrak.
    if checked >= 3 and rate >= 0.6:
        s.fired = True
        s.direction = "bundled"
        s.strength = max(0.6, _ramp(rate, 0.5, 1.0))
        s.detail = (
            f"Deployer {n} token basmış; kontrol edilen {checked}'inin "
            f"{dead}'i (%{rate * 100:.0f}) ölmüş/likidite çekilmiş — seri rug profili."
        )
        s.detail_en = (
            f"Deployer has minted {n} tokens; {dead} of the {checked} checked "
            f"({rate * 100:.0f}%) are dead/had liquidity pulled — a serial-rug profile."
        )
    elif n >= 15:
        s.fired = True
        s.direction = "bundled"
        s.strength = _ramp(n, 15, 50)
        s.detail = f"Deployer daha önce {n} token basmış — seri lansman cüzdanı."
        s.detail_en = f"Deployer has minted {n} prior tokens — a serial-launch wallet."
    elif n >= 5:
        s.fired = True
        s.strength = _ramp(n, 5, 20)
        extra = f", kontrol edilen {checked}'ten {dead}'i ölü" if checked else ""
        extra_en = f", {dead} of {checked} checked are dead" if checked else ""
        s.detail = f"Deployer daha önce {n} token basmış{extra}."
        s.detail_en = f"Deployer has minted {n} prior tokens{extra_en}."
    else:
        s.detail = f"Deployer'ın {n} önceki tokeni var — memecoin'de olağan."
        s.detail_en = f"Deployer has {n} prior token(s) — normal for memecoins."
    return s


def sig_funding_tree(ctx: SignalContext) -> Signal:
    """Çok-hop fonlama: farklı direkt fonlayıcılar 2-3 hop geriden tek bir
    kaynağa çıkıyorsa, cüzdanlar bağımsız görünse bile koordinasyon vardır.
    Araya cüzdan koyarak (A→B→C) gizlenen paketleri bu yakalar."""
    s = Signal(
        key="funding_tree",
        label="Fonlama ağacı (çok-hop)",
        direction="bundled",
        weight=1.0,
    )
    tree = {}
    if ctx.launch and getattr(ctx.launch, "available", False):
        tree = getattr(ctx.launch, "funding_tree", {}) or {}
    grand = tree.get("grandfunders") or {}
    fb = tree.get("funder_buyers") or {}
    conv = tree.get("convergence") or {}
    hops = tree.get("hops", 2)
    if not fb:
        s.data_ok = False
        s.detail = "Fonlama ağacı çıkarılamadı."
        s.detail_en = "Funding tree could not be resolved."
        return s

    # 1) Herhangi bir hop'ta (>=2) ortak ata — en güçlü, en derin sinyal.
    if conv and conv.get("buyers", 0) >= 2:
        n = conv["buyers"]
        anc = conv["ancestor"]
        max_hop = conv.get("max_hop", 2)
        min_hop = conv.get("min_hop", 2)
        s.evidence = {
            "ancestor": anc,
            "buyers_reached": n,
            "min_hop": min_hop,
            "max_hop": max_hop,
        }
        s.fired = True
        s.strength = _count_strength(n, 2, span=5)
        if min_hop >= 3:
            s.strength = min(1.0, s.strength + 0.1)  # daha derin gizleme
        s.detail = (
            f"{n} lansman alıcısının fonlaması {max_hop} hop geriden aynı adrese "
            f"({anc[:6]}…{anc[-4:]}) çıkıyor — araya cüzdan koyarak gizlenmiş "
            "ortak kaynak."
        )
        s.detail_en = (
            f"{n} launch buyers' funding traces back {max_hop} hops to the same "
            f"address ({anc[:6]}…{anc[-4:]}) — a common source hidden behind "
            "intermediary wallets."
        )
        return s

    # 2) Fallback: hop 2 grandfunder yoğunlaşması.
    best_gf, best_funders = None, []
    for gf, funders in grand.items():
        if len(funders) > len(best_funders):
            best_gf, best_funders = gf, funders
    reached = sum(len(fb.get(f, [])) for f in best_funders)
    s.evidence = {
        "grandfunder": best_gf,
        "direct_funders": len(best_funders),
        "buyers_reached": reached,
    }
    if len(best_funders) >= 2 and reached >= 3:
        s.fired = True
        s.strength = _count_strength(reached, 3, span=5)
        s.detail = (
            f"{len(best_funders)} farklı fonlayıcı tek bir üst kaynaktan "
            f"({best_gf[:6]}…{best_gf[-4:]}) besleniyor — bu fonlayıcılar "
            f"{reached} lansman alıcısına para göndermiş."
        )
        s.detail_en = (
            f"{len(best_funders)} distinct funders are fed by a single upstream "
            f"source ({best_gf[:6]}…{best_gf[-4:]}) — these funders sent money "
            f"to {reached} launch buyers."
        )
    else:
        s.detail = (
            f"Fonlayıcılar ayrı üst kaynaklardan — {hops}-hop koordinasyon yok."
        )
        s.detail_en = (
            f"Funders come from separate upstream sources — no {hops}-hop coordination."
        )
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
    risks_en = []
    if mi.mint_authority:
        risks.append(("mint", "arz sonradan artırılabilir"))
        risks_en.append(("mint", "supply can be increased later"))
    if mi.freeze_authority:
        risks.append(("freeze", "cüzdanlar dondurulup satış engellenebilir"))
        risks_en.append(("freeze", "wallets can be frozen, blocking sales"))
    if risks:
        s.fired = True
        s.strength = 1.0 if len(risks) == 2 else 0.6
        names = " ve ".join(r[0] for r in risks)
        effects = "; ".join(r[1] for r in risks)
        s.detail = f"{names} yetkisi hâlâ açık — {effects}."
        names_en = " and ".join(r[0] for r in risks_en)
        effects_en = "; ".join(r[1] for r in risks_en)
        s.detail_en = f"{names_en} authority is still live — {effects_en}."
    else:
        s.direction = "organic"
        s.detail = "Mint ve freeze yetkileri devredilmiş."
        s.detail_en = "Mint and freeze authority have been revoked."
    return s


def sig_lp_lock(ctx: SignalContext) -> Signal:
    """LP tokenları yakılmış/kilitli mi, yoksa geliştirici çekebilir mi?

    Bu bir dağıtım sinyali değil, bir güven/rug-riski sinyalidir; ama verdict
    ekseninde kilitsiz LP "insider kontrolü" (cabaled), kilitli LP ise
    organiklik lehine bir puan taşır.
    """
    s = Signal(
        key="lp_lock",
        label="LP kilit durumu",
        direction="cabaled",
        weight=0.7,
    )
    lp = ctx.lp_lock
    if not lp or not getattr(lp, "checked", False):
        s.data_ok = False
        s.detail = "LP kilit durumu çıkarılamadı."
        s.detail_en = "LP lock status could not be retrieved."
        return s

    st = getattr(lp, "status", "unknown")
    burned = getattr(lp, "burned_pct", 0.0)
    locked = getattr(lp, "locked_pct", 0.0)
    dev = getattr(lp, "dev_held_pct", 0.0)
    top = getattr(lp, "top_holder_pct", 0.0)
    s.evidence = {
        "status": st,
        "burned_pct": round(burned, 3),
        "locked_pct": round(locked, 3),
        "dev_held_pct": round(dev, 3),
        "top_holder_pct": round(top, 3),
        "source": getattr(lp, "source", ""),
    }
    detail = getattr(lp, "detail", "") or ""
    detail_en = getattr(lp, "detail_en", "") or ""

    if st in ("burned", "locked", "protocol_locked", "bonding_curve"):
        s.direction = "organic"
        s.fired = True
        s.strength = 0.6 if st in ("burned", "locked") else 0.4
        s.detail = detail
        s.detail_en = detail_en
    elif st == "unlocked":
        s.direction = "cabaled"
        s.fired = True
        s.strength = max(0.5, _ramp(max(dev, top), 0.4, 0.9))
        s.detail = detail or "LP kilitli değil — likidite çekilebilir."
        s.detail_en = detail_en or "LP is not locked — liquidity can be pulled."
    elif st == "unverified":
        # Doğrulanamayan LP bir risktir — "güvenli" sayma. Hafif bir cabaled
        # dürtüsü + sonuç ekranında belirgin uyarı (classifier caveat'ı).
        s.direction = "cabaled"
        s.fired = True
        s.strength = 0.3
        s.detail = detail or "LP kilit durumu doğrulanamadı — çekilebilir olabilir."
        s.detail_en = detail_en or "LP lock status could not be verified — it may be pullable."
    elif st == "partial":
        s.detail = detail or "LP kısmen yakılmış/kilitli."
        s.detail_en = detail_en or "LP is partially burned/locked."
    else:
        s.data_ok = False
        s.detail = detail or "LP kilit durumu belirsiz."
        s.detail_en = detail_en or "LP lock status is unclear."
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
        s.detail_en = (
            f"Liquidity is only {ratio * 100:.1f}% of market cap — "
            "large sells will crash the price."
        )
    else:
        s.direction = "organic"
        s.detail = f"Likidite / piyasa değeri oranı %{ratio * 100:.1f}."
        s.detail_en = f"Liquidity / market-cap ratio is {ratio * 100:.1f}%."
    return s


def sig_wallet_age_diversity(ctx: SignalContext) -> Signal:
    """Pozitif sinyal: cüzdan yaşları gerçekten dağınıksa organikliğe puan."""
    s = Signal(
        key="wallet_age_diversity",
        label="Cüzdan yaşı çeşitliliği",
        direction="organic",
        weight=0.8,
    )
    ages = [h.owner_created_at for h in ctx.bundle_wallets if h.owner_created_at]
    # Cüzdanların yarısından azının gerçek yaşını çözebildiysek "organik" demek
    # için yeterli veri yok — sessizce organiğe puan vermeyelim.
    need = max(5, int(len(ctx.bundle_wallets) * 0.5))
    if len(ages) < need or not ctx.launch_ts:
        s.data_ok = False
        s.detail = (
            "Cüzdanların yeterince çoğunun gerçek yaşı çözülemedi — "
            "organiklik puanı verilemiyor."
        )
        s.detail_en = (
            "Real age could not be resolved for enough wallets — "
            "no organic-ness score can be given."
        )
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
        s.detail_en = (
            f"{older_than_month}/{len(days)} wallets were created at least a "
            "month before the token — age spread is wide."
        )
    else:
        s.detail = "Yaş dağılımı organik sayılacak kadar geniş değil."
        s.detail_en = "Age spread isn't wide enough to count as organic."
    return s


ALL_SIGNALS: list[Callable[[SignalContext], Signal]] = [
    sig_wallet_age_cluster,
    sig_wallet_age_batch,
    sig_common_funder,
    sig_same_slot_entry,
    sig_identical_balances,
    sig_fee_fingerprint,
    sig_fresh_wallets,
    sig_flagged_wallets,
    sig_supply_whale,
    sig_launch_dominance,
    sig_top10_concentration,
    sig_funding_profile,
    sig_funding_tree,
    sig_deployer_history,
    sig_mint_authority,
    sig_lp_lock,
    sig_liquidity_health,
    sig_wallet_age_diversity,
]


def run_signals(ctx: SignalContext) -> list[Signal]:
    out = []
    for fn in ALL_SIGNALS:
        try:
            s = fn(ctx)
            # Eşiği kıl payı geçip gücü ~0 kalan sinyal "tetiklendi" görünmesin
            # (kullanıcı kararın yok saydığı kırmızı noktalar görüyor).
            if s.fired and s.strength < 0.06:
                s.fired = False
            out.append(s)
        except Exception as exc:  # noqa: BLE001
            nice = fn.__name__.replace("sig_", "").replace("_", " ").capitalize()
            out.append(Signal(
                key=fn.__name__.replace("sig_", ""),
                label=nice,
                direction="cabaled",
                weight=0.0,
                data_ok=False,
                detail=f"Sinyal hesaplanamadı: {exc}",
                detail_en=f"Signal could not be computed: {exc}",
            ))
    return out
