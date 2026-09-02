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

# "Sert" sinyaller: tek başına değil ama birkaçı birleşince Bundled kararı verir.
HARD_SIGNALS = {
    "wallet_age_cluster",
    "common_funder",
    "same_slot_entry",
    "identical_balances",
    "fee_fingerprint",
    "flagged_wallets",
    "supply_whale",
    "funding_tree",
}

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
) -> Verdict:
    fired = [s for s in signals if s.fired]
    hard_fired = [s for s in fired if s.key in HARD_SIGNALS]

    bundled_weight = sum(s.contribution for s in fired if s.direction == "bundled")
    cabaled_weight = sum(s.contribution for s in fired if s.direction == "cabaled")
    organic_weight = sum(s.contribution for s in fired if s.direction == "organic")
    combo = bundled_weight + 0.6 * cabaled_weight

    blind_hard = sum(
        1 for s in signals if s.key in HARD_SIGNALS and not s.data_ok
    )

    strong_bundle = (
        len(hard_fired) >= BUNDLED_MIN_HARD and bundled_weight >= BUNDLED_MIN_WEIGHT
    )
    alt_bundle = len(hard_fired) >= BUNDLED_ALT_HARD and combo >= BUNDLED_ALT_COMBO

    # --- Karar kuralı --------------------------------------------------
    if strong_bundle or alt_bundle:
        kind = "bundled"
        # Tetiklenen bundled ağırlığı / veri BULUNAN bundled sinyallerin tavanı
        # (kör sinyaller skoru boşuna düşürmesin). Yoğunlaşma ağırlığını da bir
        # miktar kredi olarak ekle.
        avail_ceiling = sum(
            s.weight for s in signals if s.direction == "bundled" and s.data_ok
        ) or 1.0
        score_frac = min(1.0, bundled_weight / avail_ceiling + 0.15 * cabaled_weight)
    elif combo >= CABALED_MIN_WEIGHT:
        kind = "cabaled"
        ceiling = sum(
            s.weight for s in signals if s.direction in ("bundled", "cabaled")
        )
        score_frac = (bundled_weight + cabaled_weight) / ceiling if ceiling else 0.0
    elif blind_hard >= INCONCLUSIVE_BLIND_HARD or coverage < INCONCLUSIVE_COVERAGE:
        kind = "inconclusive"
        score_frac = 0.0
    else:
        kind = "organic"
        ceiling = sum(s.weight for s in signals if s.direction == "organic") + 0.5
        score_frac = (organic_weight + 0.5) / ceiling if ceiling else 0.0

    score = int(round(min(1.0, score_frac) * 100))
    if kind in ("bundled", "cabaled"):
        score = max(score, 40)
    elif kind == "organic":
        score = max(score, 35)

    # --- Güven ----------------------------------------------------------
    computable = [s for s in signals if s.data_ok]
    signal_coverage = len(computable) / len(signals) if signals else 0
    conf = 0.55 * signal_coverage + 0.30 * coverage + 0.15 * (1.0 if market_available else 0.0)

    caveats: list[str] = []
    if not launch_available:
        conf *= 0.8
        caveats.append(
            "Lansman işlem verisi çekilemedi (çok yüksek hacimli / eski token). "
            "Bundle sinyalleri lansmandaki ilk alıcılar yerine ŞU ANKİ en büyük "
            "cüzdanlar üzerinde çalıştı — koordineli bir lansmanı kaçırmış olabiliriz."
        )
    if token_age_hours is not None and token_age_hours < 6:
        conf *= 0.7
        caveats.append(
            "Token 6 saatten yeni — işlem geçmişi bir desen çıkarmaya yetmeyebilir."
        )
    whale = next((s for s in signals if s.key == "supply_whale" and s.fired), None)
    if whale:
        caveats.append(
            "Baskın cüzdan bir borsa soğuk cüzdanı, hazine ya da kilitli vesting "
            "kontratı da olabilir — etiketleyemedik."
        )
    if not market_available:
        caveats.append("Piyasa verisi alınamadı; likidite sinyalleri hesaplanmadı.")
    missing = [s.label for s in signals if not s.data_ok]
    if missing:
        caveats.append(f"Veri yetersizliği nedeniyle hesaplanamayan sinyaller: {', '.join(missing)}.")

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
    )
