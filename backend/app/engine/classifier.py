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

# Bundled kararı için gereken minimum "sert" sinyal sayısı.
HARD_SIGNALS = {
    "wallet_age_cluster",
    "common_funder",
    "same_slot_entry",
    "identical_balances",
    "fee_fingerprint",
    "flagged_wallets",
}
BUNDLED_MIN_HARD = 3
BUNDLED_MIN_WEIGHT = 2.0

CABALED_MIN_WEIGHT = 0.9

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
) -> Verdict:
    fired = [s for s in signals if s.fired]
    hard_fired = [s for s in fired if s.key in HARD_SIGNALS]

    bundled_weight = sum(s.contribution for s in fired if s.direction == "bundled")
    cabaled_weight = sum(s.contribution for s in fired if s.direction == "cabaled")
    organic_weight = sum(s.contribution for s in fired if s.direction == "organic")

    # --- Karar kuralı --------------------------------------------------
    if len(hard_fired) >= BUNDLED_MIN_HARD and bundled_weight >= BUNDLED_MIN_WEIGHT:
        kind = "bundled"
        raw = bundled_weight
        ceiling = sum(s.weight for s in signals if s.direction == "bundled")
    elif bundled_weight + cabaled_weight >= CABALED_MIN_WEIGHT:
        kind = "cabaled"
        raw = bundled_weight + cabaled_weight
        ceiling = sum(
            s.weight for s in signals if s.direction in ("bundled", "cabaled")
        )
    else:
        kind = "organic"
        raw = organic_weight + 0.5  # taban: hiçbir şey tetiklenmemesi de kanıttır
        ceiling = sum(s.weight for s in signals if s.direction == "organic") + 0.5

    score = int(round(min(1.0, raw / ceiling) * 100)) if ceiling else 0
    score = max(score, 40 if kind != "organic" else 35)

    # --- Güven ----------------------------------------------------------
    computable = [s for s in signals if s.data_ok]
    signal_coverage = len(computable) / len(signals) if signals else 0
    conf = 0.55 * signal_coverage + 0.30 * coverage + 0.15 * (1.0 if market_available else 0.0)

    caveats: list[str] = []
    if token_age_hours is not None and token_age_hours < 6:
        conf *= 0.65
        caveats.append(
            "Token 6 saatten yeni — işlem geçmişi bir desen çıkarmaya yetmeyebilir."
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
