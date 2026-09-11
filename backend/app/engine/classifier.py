"""
Sınıflandırıcı.

Tek bir sinyal asla karar veremez. Karar, birbirinden bağımsız sinyallerin
aynı yönü göstermesinden doğar. Bu dosyadaki kural seti bilerek okunabilir
tutuldu — kalibrasyon yaparken burayı ve signals.SIGNAL_TUNING'i düzenle.

Çıktı iki ayrı sayı verir:
  score      — token atandığı kategoriye ne kadar iyi uyuyor (0-100).
               Fiyat tahmini DEĞİL.
  confidence — bu kararı verirken elimizde ne kadar veri vardı (0-100).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .signals import Signal

# Sinyal etiketlerinin İngilizcesi — yalnızca caveat metinlerinde sinyal
# adı gömülü olduğu için lazım (frontend'deki SIG_EN ile aynı tutulmalı).
SIG_LABEL_EN = {
    "wallet_age_cluster": "Wallet birth cluster",
    "wallet_age_batch": "Prepared wallet batch",
    "common_funder": "Common funder",
    "funding_tree": "Funding tree (multi-hop)",
    "same_slot_entry": "Simultaneous entry",
    "identical_balances": "Uniform balance spread",
    "fee_fingerprint": "Fee fingerprint",
    "fresh_wallets": "Fresh-wallet ratio",
    "deployer_history": "Deployer history",
    "flagged_wallets": "Previously flagged wallets",
    "supply_whale": "Single-wallet dominance",
    "launch_dominance": "Single-wallet launch dominance",
    "top10_concentration": "Top-10 concentration",
    "funding_profile": "Funding-source profile",
    "mint_authority": "Mint / freeze authority",
    "lp_lock": "LP lock status",
    "liquidity_health": "Liquidity depth",
    "wallet_age_diversity": "Wallet-age diversity",
}

# "Sert" sinyaller: tek başına değil ama birkaçı birleşince Bundled kararı verir.
HARD_SIGNALS = {
    "wallet_age_cluster",
    "wallet_age_batch",
    "common_funder",
    "same_slot_entry",
    "identical_balances",
    "fee_fingerprint",
    "flagged_wallets",
    "supply_whale",
    "launch_dominance",
    "funding_tree",
}

# Danışma sinyalleri: karara (bundled/cabaled/organic) AĞIRLIK KATMAZLAR — çünkü
# "dağıtım nasıl yapıldı" sorusuyla ilgili değiller, rug/güvenlik bağlamıdırlar.
# Sonuç ekranında ayrı bir "Risk bayrakları" bölümünde gösterilirler.
# (LP kilit durumu %100 organik dağıtımlı bir tokende de kötü olabilir — bu onu
#  "cabaled" yapmaz.)
ADVISORY_SIGNALS = {"lp_lock"}

# Kütle eşzamanlı giriş: lansman alıcılarının ezici çoğunluğu tek-iki slotta
# girdiyse bu tek başına bir paket imzasıdır — bir sniper sürüsü zamana yayılır.
MASS_SLOT_MIN_WALLETS = 10
MASS_SLOT_MIN_RATIO = 0.8
MASS_SLOT_MAX_SPAN = 2   # slot

# Klasik yol: taze lansman paketleri — çok sayıda sert sinyal.
BUNDLED_MIN_HARD = 3
BUNDLED_MIN_WEIGHT = 1.8

# Alternatif yol: eski/konsolide olmuş tokenlar. getTokenLargestAccounts
# lansman cüzdanlarını göstermez ama yoğunlaşma + botsu izler hâlâ görülür.
BUNDLED_ALT_HARD = 2
BUNDLED_ALT_COMBO = 2.0   # bundled_weight + 0.6 * cabaled_weight

CABALED_MIN_WEIGHT = 0.9

# Bu kadar sert sinyal veri yokluğundan hesaplanamadıysa "Organic" deme.
INCONCLUSIVE_BLIND_HARD = 3
INCONCLUSIVE_COVERAGE = 0.4

WEEK_HOURS = 24 * 7

VERDICTS = {
    "bundled": {
        "label": "Bundled",
        "color": "#ff4d5e",
        "summary": "Arz dağıtımı üretilmiş görünüyor — koordineli alım desenleri tespit edildi.",
    },
    "cabaled": {
        "label": "Cabaled",
        "color": "#ffa726",
        "summary": "Dağıtım insider ağırlıklı ya da olağandışı; Bundled'ın sert işaretleri yok.",
    },
    "organic": {
        "label": "Organic",
        "color": "#4d9fff",
        "summary": "Koordineli dağıtım deseni bulunamadı. Bu 'güvenli' demek değildir.",
    },
    "inconclusive": {
        "label": "Inconclusive",
        "color": "#8b9bb4",
        "summary": "Karar vermeye yetecek zincir verisi toplanamadı — sonuç belirsiz.",
    },
}


@dataclass
class Verdict:
    kind: str                       # bundled | cabaled | organic
    label: str
    color: str
    summary: str
    score: int                      # 0-100, kategoriye uyum gücü
    confidence: int                 # 0-100, veri yeterliliği
    confidence_label: str
    fired: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    caveats_en: list[str] = field(default_factory=list)
    risk_flags: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "color": self.color,
            "summary": self.summary,
            "score": self.score,
            "confidence": self.confidence,
            "confidence_label": self.confidence_label,
            "fired_signals": self.fired,
            "reasons": self.reasons,
            "caveats": self.caveats,
            "caveats_en": self.caveats_en,
            "risk_flags": self.risk_flags,
        }


def _confidence_label(pct: int) -> str:
    if pct >= 75:
        return "yüksek"
    if pct >= 50:
        return "orta"
    if pct >= 25:
        return "düşük"
    return "çok düşük"


def classify(
    signals: list[Signal],
    coverage: float,
    market_available: bool,
    token_age_hours: float | None,
    launch_available: bool = True,
    launch_buyer_count: int | None = None,
) -> Verdict:
    fired = [s for s in signals if s.fired]
    hard_fired = [s for s in fired if s.key in HARD_SIGNALS]
    # Danışma sinyalleri ağırlık matematiğine girmez.
    weighed = [s for s in fired if s.key not in ADVISORY_SIGNALS]

    bundled_weight = sum(s.contribution for s in weighed if s.direction == "bundled")
    cabaled_weight = sum(s.contribution for s in weighed if s.direction == "cabaled")
    organic_weight = sum(s.contribution for s in weighed if s.direction == "organic")
    combo = bundled_weight + 0.6 * cabaled_weight

    blind_hard = sum(
        1 for s in signals if s.key in HARD_SIGNALS and not s.data_ok
    )

    strong_bundle = (
        len(hard_fired) >= BUNDLED_MIN_HARD and bundled_weight >= BUNDLED_MIN_WEIGHT
    )
    alt_bundle = len(hard_fired) >= BUNDLED_ALT_HARD and combo >= BUNDLED_ALT_COMBO

    # Kütle eşzamanlı giriş: same_slot_entry lansman alıcılarının ≥%80'ini ve
    # ≥10 cüzdanı, ≤2 slotluk bir aralıkta yakaladıysa. Tek başına karar vermesin
    # ("no single signal") — en az bir doğrulayıcı sinyal daha gerekli.
    ss = next((s for s in fired if s.key == "same_slot_entry"), None)
    mass_same_slot = False
    if ss:
        ev = ss.evidence or {}
        cs = ev.get("cluster_size", 0) or 0
        tot = ev.get("total", 0) or 1
        rng = ev.get("slot_range") or [0, 0]
        slot_span = (rng[1] - rng[0]) if isinstance(rng, list) and len(rng) == 2 else 99
        mass_same_slot = (
            cs >= MASS_SLOT_MIN_WALLETS
            and cs / tot >= MASS_SLOT_MIN_RATIO
            and slot_span <= MASS_SLOT_MAX_SPAN
        )
    corroborating = [
        s for s in fired
        if s.key != "same_slot_entry"
        and (
            s.key in HARD_SIGNALS
            or s.key in ("top10_concentration", "funding_profile", "deployer_history")
        )
    ]
    mass_slot_bundle = mass_same_slot and len(corroborating) >= 1

    # --- Karar kuralı --------------------------------------------------
    if strong_bundle or alt_bundle or mass_slot_bundle:
        kind = "bundled"
        # Skor = "ne kadar eminiz". Kaç sert sinyal (4 → tam) × ortalama güçleri,
        # + yoğunlaşma/insider desteği. Kör sinyaller paydayı şişirmez.
        avg_hard = (
            sum(s.strength for s in hard_fired) / len(hard_fired)
            if hard_fired else 0.0
        )
        score_frac = min(1.0, len(hard_fired) / 4.0) * (0.45 + 0.55 * avg_hard)
        score_frac += 0.12 * min(1.0, cabaled_weight)
        if mass_same_slot:
            score_frac = max(score_frac, 0.62)
        if len(hard_fired) >= 5:
            score_frac = max(score_frac, 0.80)
        score_frac = min(1.0, score_frac)
    elif combo >= CABALED_MIN_WEIGHT:
        kind = "cabaled"
        # combo 0.9 (eşik) → ~42 ; combo 2.4+ → ~78
        score_frac = min(1.0, 0.42 + 0.30 * (combo - CABALED_MIN_WEIGHT) / 1.5)
    elif blind_hard >= INCONCLUSIVE_BLIND_HARD or coverage < INCONCLUSIVE_COVERAGE:
        kind = "inconclusive"
        score_frac = 0.0
    else:
        kind = "organic"
        ceiling = sum(s.weight for s in weighed if s.direction == "organic") + 0.5
        score_frac = (organic_weight + 0.5) / ceiling if ceiling else 0.0

    score = int(round(min(1.0, score_frac) * 100))
    if kind == "bundled":
        score = max(score, 45)
    elif kind == "cabaled":
        score = max(score, 38)
    elif kind == "organic":
        score = max(score, 35)

    # --- Güven ----------------------------------------------------------
    computable = [s for s in signals if s.data_ok]
    signal_coverage = len(computable) / len(signals) if signals else 0
    conf = 0.55 * signal_coverage + 0.30 * coverage + 0.15 * (1.0 if market_available else 0.0)

    caveats: list[str] = []
    caveats_en: list[str] = []
    # Küçük örneklem cezası: "koordineli desen bulunamadı" iddiası 3-4
    # lansman alıcısı üzerinden kurulmuşsa, 50+ alıcılı bir lansmana göre çok
    # daha zayıf bir kanıttır — signal_coverage/chain_coverage bunu yakalamaz
    # (veri "çözülebilir" olabilir ama az olabilir).
    if launch_available and launch_buyer_count is not None and launch_buyer_count < 8:
        conf *= 0.55 + 0.45 * min(1.0, launch_buyer_count / 8)
        if launch_buyer_count < 6:
            caveats.append(
                f"Lansmanda yalnızca {launch_buyer_count} alıcı bulundu — "
                "koordinasyon sinyalleri bu kadar küçük bir örneklemde "
                "istatistiksel olarak güçsüzdür, güven buna göre düşürüldü."
            )
            caveats_en.append(
                f"Only {launch_buyer_count} buyers were found at launch — "
                "coordination signals are statistically weak on such a small "
                "sample, confidence was lowered accordingly."
            )
    if kind == "bundled" and mass_slot_bundle and not strong_bundle:
        caveats.append(
            "Lansman alıcılarının neredeyse tamamı tek-iki slot içinde girmiş "
            "(paket imzası), ancak fonlama ve cüzdan-hazırlık izleri gizlenmiş — "
            "parmak izini bilinçli olarak örten bir paket olabilir; klasik "
            "paketlerin tüm sert sinyalleri tetiklenmedi."
        )
        caveats_en.append(
            "Nearly all launch buyers entered within one or two slots (a bundle "
            "signature), but the funding and wallet-prep traces are hidden — "
            "this may be a bundle that deliberately covers its fingerprint; not "
            "every hard signal a classic bundle fires was triggered."
        )
    if not launch_available:
        conf *= 0.8
        caveats.append(
            "Lansman işlem verisi çekilemedi (çok yüksek hacimli / eski token). "
            "Bundle sinyalleri lansmandaki ilk alıcılar yerine ŞU ANKİ en büyük "
            "cüzdanlar üzerinde çalıştı — koordineli bir lansmanı kaçırmış olabiliriz."
        )
        caveats_en.append(
            "Launch transaction data could not be retrieved (very high volume / "
            "old token). Bundle signals ran on the CURRENT top holders instead of "
            "the original launch buyers — we may have missed a coordinated launch."
        )
    if token_age_hours is not None and token_age_hours < 6:
        conf *= 0.7
        caveats.append(
            "Token 6 saatten yeni — işlem geçmişi bir desen çıkarmaya yetmeyebilir."
        )
        caveats_en.append(
            "Token is less than 6 hours old — transaction history may not be "
            "enough to establish a pattern."
        )
    whale = next((s for s in signals if s.key == "supply_whale" and s.fired), None)
    if whale:
        caveats.append(
            "Baskın cüzdan bir borsa soğuk cüzdanı, hazine ya da kilitli vesting "
            "kontratı da olabilir — etiketleyemedik."
        )
        caveats_en.append(
            "The dominant wallet could also be an exchange cold wallet, a "
            "treasury, or a locked vesting contract — we couldn't label it."
        )
    if not market_available:
        caveats.append("Piyasa verisi alınamadı; likidite sinyalleri hesaplanmadı.")
        caveats_en.append("Market data could not be retrieved; liquidity signals were not computed.")
    missing = [s.label for s in signals if not s.data_ok]
    if missing:
        caveats.append(f"Veri yetersizliği nedeniyle hesaplanamayan sinyaller: {', '.join(missing)}.")
        missing_en = [SIG_LABEL_EN.get(s.key, s.label) for s in signals if not s.data_ok]
        caveats_en.append(f"Signals that couldn't be computed due to insufficient data: {', '.join(missing_en)}.")

    # --- Risk bayrakları (karara girmez, ayrı gösterilir) --------------
    risk_flags: list[dict] = []
    lp = next((s for s in signals if s.key == "lp_lock" and s.fired), None)
    if lp:
        st = (lp.evidence or {}).get("status")
        if st in ("unlocked", "unverified"):
            msg = lp.detail or (
                "LP kilit durumu doğrulanamadı — çekilebilir varsayın."
                if st == "unverified"
                else "LP kilitli/yakılmış değil — geliştirici çekebilir."
            )
            msg_en = lp.detail_en or (
                "LP lock status could not be verified — assume it can be pulled."
                if st == "unverified"
                else "LP is not locked/burned — the developer can pull it."
            )
            risk_flags.append({
                "key": "lp", "severity": "high",
                "label": "Likidite çekilebilir", "detail": msg, "detail_en": msg_en,
            })
            caveats.append("⚠ " + msg)
            caveats_en.append("⚠ " + msg_en)
    ma = next((s for s in signals if s.key == "mint_authority" and s.fired
               and s.direction != "organic"), None)
    if ma:
        risk_flags.append({
            "key": "authority", "severity": "high",
            "label": "Mint/freeze yetkisi açık", "detail": ma.detail, "detail_en": ma.detail_en,
        })
    lh = next((s for s in signals if s.key == "liquidity_health" and s.fired
               and s.direction == "cabaled"), None)
    if lh:
        risk_flags.append({
            "key": "liquidity", "severity": "medium",
            "label": "İnce likidite", "detail": lh.detail, "detail_en": lh.detail_en,
        })
    dh = next((s for s in signals if s.key == "deployer_history" and s.fired
               and s.direction == "bundled"), None)
    if dh:
        risk_flags.append({
            "key": "deployer", "severity": "high",
            "label": "Deployer rug/seri lansman geçmişi", "detail": dh.detail, "detail_en": dh.detail_en,
        })
    fw = next((s for s in signals if s.key == "flagged_wallets" and s.fired), None)
    if fw:
        risk_flags.append({
            "key": "flagged", "severity": "high",
            "label": "Kara listedeki cüzdanlar", "detail": fw.detail, "detail_en": fw.detail_en,
        })

    confidence = int(round(max(0.0, min(1.0, conf)) * 100))

    meta = VERDICTS[kind]
    return Verdict(
        kind=kind,
        label=meta["label"],
        color=meta["color"],
        summary=meta["summary"],
        score=score,
        confidence=confidence,
        confidence_label=_confidence_label(confidence),
        fired=[s.key for s in fired],
        reasons=[s.detail for s in fired if s.detail],
        caveats=caveats,
        caveats_en=caveats_en,
        risk_flags=risk_flags,
    )
