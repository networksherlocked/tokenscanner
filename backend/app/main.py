"""
SolScope API.

Uç noktalar:
    GET  /api/scan/{mint}        Tarama (cache'li)
    POST /api/scan/{mint}/fresh  Cache'i atlayıp yeniden tara
    GET  /api/history/{mint}     Bu tokenın geçmiş kararları
    GET  /api/recent             Son taranan tokenlar
    GET  /api/track              Karne: geçmiş kararlar + sonrasında ne oldu
    GET  /api/health             Sağlayıcı durumu
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .cache import ScanCache
from .engine import registry
from .engine import scanner
from .engine.scanner import scan_token, TokenTooSmall
from .render_card import render_badge_svg, render_png
from . import xpost
from .rpc import trades as rpc_trades
from .rpc.market import fetch_market
from .rpc.pool import RpcError, RpcPool
from .rpc.pool import mask_endpoints as pool_mask

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("solscope")

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

state: dict = {}
_inflight: dict[str, asyncio.Task] = {}
_ip_hits: dict[str, list[float]] = {}

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))

# "Yenile" butonu maliyet kontrolü: aynı token için iki canlı yeniden tarama
# arasında en az bu kadar saniye geçmeli. 0 = sınırsız. Admin panelinden
# (config: refresh_cooldown_sec) canlı ayarlanır; env yalnızca ilk varsayılan.
REFRESH_COOLDOWN_DEFAULT = int(os.getenv("REFRESH_COOLDOWN_SEC", "3600"))

# --- Karne (outcome tracking) --------------------------------------------
# Tarama sonrası tokenın market cap'i TRACK_WINDOW_SEC boyunca izlenir.
# Pencerede en düşük noktaya göre düşüş TRACK_DROP_PCT'i geçtiyse "çöktü".
TRACK_WINDOW = int(os.getenv("TRACK_WINDOW_SEC", "86400"))     # 24 saat
TRACK_DROP = float(os.getenv("TRACK_DROP_PCT", "0.35"))        # %35
TRACK_POLL = int(os.getenv("TRACK_POLL_SEC", "300"))
FLAGGED_VERDICTS = {"bundled", "cabaled"}
_last_track_refresh = 0.0

# --- Öğrenme: yanlış "organic/inconclusive" kararlardan cüzdan/deployer çıkar --
LEARN_ENABLED = os.getenv("LEARN_ENABLED", "1") != "0"
LEARN_MIN_DROP = float(os.getenv("LEARN_MIN_DROP", "0.55"))   # sadece sert çöküşler
LEARN_MAX_WALLETS = int(os.getenv("LEARN_MAX_WALLETS", "12"))

# --- Likidite çekilme (rug) tespiti: izlenen bir tokenın likiditesi sert
#     düşerse yaratıcısını kara listeye ekle. Kilit durumundan bağımsız — asıl
#     doğrulama budur (havuz zincirde izlenir). ---
RUG_ENABLED = os.getenv("RUG_ENABLED", "1") != "0"
RUG_MIN_LIQ_AT_SCAN = float(os.getenv("RUG_MIN_LIQ_AT_SCAN", "2000"))  # $ — altı gürültü
RUG_DROP_FRAC = float(os.getenv("RUG_DROP_FRAC", "0.85"))   # likidite bu oranda düştüyse
RUG_FLOOR_USD = float(os.getenv("RUG_FLOOR_USD", "800"))    # ya da mutlak bu eşiğin altı

# --- Yükseliş öğrenmesi: organic/cabaled/inconclusive denip sonradan sert
#     YÜKSELEN tokenlardan tekrar eden erken cüzdan/fonlayıcı/deployer çıkar --
GAIN_ENABLED = os.getenv("GAIN_ENABLED", "1") != "0"
GAIN_WINDOW = int(os.getenv("GAIN_WINDOW_SEC", str(7 * 86400)))   # 7 gün izle
GAIN_MIN_RISE = float(os.getenv("GAIN_MIN_RISE", "2.0"))          # ≥ +%200 (3×)
GAIN_MAX_MARKERS = int(os.getenv("GAIN_MAX_MARKERS", "14"))
GAIN_VERDICTS = {"organic", "cabaled", "inconclusive"}
# Yükseliş penceresinde (24s'den sonra) piyasa verisi bu aralıkta bir yenilenir.
GAIN_POLL_MIN = int(os.getenv("GAIN_POLL_MIN_SEC", "3600"))


async def refresh_track() -> None:
    """İzlenen tokenların market cap'ini günceller, süresi dolanları sonuçlandırır."""
    cache = state.get("cache")
    if cache is None:
        return
    now = int(time.time())
    pending = cache.track_pending(max_age=max(TRACK_WINDOW, GAIN_WINDOW) + 3600)
    for row in pending:
        try:
            await _refresh_track_row(cache, row, now)
        except Exception:  # noqa: BLE001
            # Tek bir kayıttaki hata (ör. DB/piyasa) tüm döngüyü öldürmesin.
            log.exception("Karne kaydı yenilenemedi: %s", row.get("mint"))

    # Sembolü eksik eski kayıtları (settled dahil) tazeden doldur.
    try:
        await _backfill_track_symbols(limit=25)
    except Exception:  # noqa: BLE001
        log.exception("Sembol backfill hatası")


async def _refresh_track_row(cache, row: dict, now: int) -> None:
    mint = row["mint"]
    mcap_min = row.get("mcap_min")
    mcap_max = row.get("mcap_max")
    age = now - row["scored_at"]
    crash_done = bool(row.get("settled"))
    # 24s çöküş penceresi bittiyse yükseliş takibi seyrek yenilenir.
    if crash_done and row.get("latest_at") and now - row["latest_at"] < GAIN_POLL_MIN:
        return
    sym = None
    img = None
    liq = None
    try:
        snap = await fetch_market(mint)
        mcap = snap.market_cap
        sym = snap.symbol
        img = snap.image_url
        liq = snap.liquidity_usd
    except Exception:  # noqa: BLE001
        mcap = None
    liq_min = row.get("liq_min")
    if liq is not None:
        liq_min = liq if liq_min is None else min(liq_min, liq)
    if mcap:
        mcap_min = mcap if mcap_min is None else min(mcap_min, mcap)
        mcap_max = mcap if mcap_max is None else max(mcap_max, mcap)
        cache.track_update(
            mint, mcap, mcap_min, now,
            mcap_max=mcap_max, symbol=sym, image=img, liq_min=liq_min,
        )

    # --- Likidite çekilme (rug) tespiti — kilit durumundan bağımsız ------
    if (
        RUG_ENABLED
        and not row.get("rug_flagged")
        and liq is not None
        and (row.get("liq_at_scan") or 0) >= RUG_MIN_LIQ_AT_SCAN
    ):
        liq0 = float(row["liq_at_scan"])
        if liq <= RUG_FLOOR_USD or liq <= liq0 * (1.0 - RUG_DROP_FRAC):
            try:
                learn_from_rug(cache, {**row, "symbol": sym or row.get("symbol")},
                               liq0, liq)
            except Exception:  # noqa: BLE001
                log.exception("Rug dersi çıkarılamadı: %s", mint)

    base = row.get("mcap_at_scan")

    if not crash_done and age >= TRACK_WINDOW:
        drop = (
            max(0.0, (base - mcap_min) / base)
            if base and mcap_min is not None
            else 0.0
        )
        flagged = (row.get("verdict") or "") in FLAGGED_VERDICTS
        crashed = drop >= TRACK_DROP
        if flagged and crashed:
            outcome = "hit"
        elif flagged and not crashed:
            outcome = "no_dump"
        elif not flagged and crashed:
            outcome = "miss"
        else:
            outcome = "clear"
        cache.track_settle(mint, outcome)

        if outcome == "miss" and LEARN_ENABLED and drop >= LEARN_MIN_DROP:
            try:
                learn_from_miss(cache, row, drop)
            except Exception:  # noqa: BLE001
                log.exception("Ders çıkarılamadı: %s", mint)

    # --- Yükseliş penceresi (organic/cabaled/inconclusive, daha uzun) -----
    if not row.get("gain_settled") and age >= GAIN_WINDOW:
        verdict_now = row.get("verdict") or ""
        rise = (
            max(0.0, (mcap_max - base) / base)
            if base and mcap_max is not None
            else 0.0
        )
        if verdict_now not in GAIN_VERDICTS:
            cache.track_settle_gain(mint, "n/a")
        elif rise >= GAIN_MIN_RISE:
            cache.track_settle_gain(mint, "runup")
            if GAIN_ENABLED:
                try:
                    learn_from_gain(cache, {**row, "mcap_max": mcap_max}, rise)
                except Exception:  # noqa: BLE001
                    log.exception("Yükseliş dersi çıkarılamadı: %s", mint)
        else:
            cache.track_settle_gain(mint, "flat")


def _short_addr(a: str | None) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 12 else (a or "?")


def _cluster_evidence(scan: dict) -> tuple[list[str], str]:
    """Kayıtlı taramada (YENİ RPC çağrısı yapmadan) organize dağıtım izi ara.

    Bulursa şüpheli cüzdanlar + insan-okur açıklama listesi döndürür. Bulamazsa
    boş — genel piyasa çöküşünü koordineli rug sanıp masum cüzdanları
    işaretlememek için.
    """
    launch = scan.get("launch") or {}
    buyers = launch.get("buyers") or []
    signals = {s.get("key"): s for s in scan.get("signals", [])}

    reasons: list[str] = []
    suspects: set[str] = set()

    # 1) ortak fonlayıcı — birden çok lansman alıcısının ilk SOL'u aynı cüzdandan
    funders: dict[str, list[str]] = {}
    for b in buyers:
        f = b.get("funder")
        if f:
            funders.setdefault(f, []).append(b.get("owner"))
    for f, owners in funders.items():
        if len(owners) >= 2:
            reasons.append(
                f"{len(owners)} lansman alıcısının ilk SOL'unu aynı cüzdan "
                f"({_short_addr(f)}) göndermiş — cüzdanları bu adres finanse etmiş"
            )
            suspects.update(o for o in owners if o)
            suspects.add(f)

    # 2) çok-hop fonlama ağacı — araya cüzdan koyarak gizlenmiş ortak kaynak
    ft = launch.get("funding_tree") or {}
    conv = ft.get("convergence") or {}
    if conv.get("buyers", 0) >= 2 and conv.get("ancestor"):
        reasons.append(
            f"{conv['buyers']} lansman alıcısının parası {conv.get('max_hop', 2)} "
            f"adım geriden tek adrese ({_short_addr(conv['ancestor'])}) çıkıyor — "
            f"araya cüzdan koyarak gizlenmiş ortak kaynak"
        )
        suspects.add(conv["ancestor"])
    for gf, ffs in (ft.get("grandfunders") or {}).items():
        if len(ffs) >= 2:
            reasons.append(
                f"{len(ffs)} ayrı fonlayıcı tek üst kaynağa "
                f"({_short_addr(gf)}) bağlanıyor"
            )
            suspects.add(gf)
            suspects.update(ffs)

    # 3) taze cüzdan kümesi — lansmanda geçmişsiz cüzdanlarla giriş
    fresh = [b.get("owner") for b in buyers if 0 < (b.get("tx_count") or 0) <= 10]
    if len(fresh) >= 3:
        reasons.append(
            f"{len(fresh)} lansman alıcısı sıfır geçmişli (o gün açılmış) cüzdan"
        )
        suspects.update(o for o in fresh if o)

    # 4) motorun eşik altında kalan sert küme sinyalleri
    _labels = {
        "common_funder": "ortak fonlayıcı",
        "same_slot_entry": "aynı slotta giriş",
        "fee_fingerprint": "aynı öncelik ücreti",
        "identical_balances": "birebir eşit bakiye",
    }
    for key, lbl in _labels.items():
        sg = signals.get(key) or {}
        n = (sg.get("evidence") or {}).get("cluster_size", 0)
        if n >= 2:
            reasons.append(f"eşik altında kalan ama görünür {n} cüzdanlık '{lbl}' kümesi")

    # 5) mevcut yapıda tek elde toplanmış arz — çöküş anında satan taraf
    ev = (signals.get("supply_whale") or {}).get("evidence") or {}
    top_owner = ev.get("top_owner")
    top_share = ev.get("top_share") or 0
    if top_owner and top_share >= 15 and ev.get("top_tag") in (None, "", "unknown"):
        reasons.append(
            f"tek cüzdan ({_short_addr(top_owner)}) çöküşten önce dolaşan arzın "
            f"%{top_share:.0f}'ini biriktirmişti — büyük olasılıkla satışı yapan taraf"
        )
        suspects.add(top_owner)

    return sorted(x for x in suspects if x), "; ".join(reasons)


def learn_from_miss(cache: ScanCache, row: dict, drop: float) -> None:
    mint = row["mint"]
    sym = row.get("symbol") or mint[:6]
    verdict_was = row.get("verdict") or "?"
    pct = f"%{drop * 100:.0f}"
    win_h = max(1, round(TRACK_WINDOW / 3600))

    scan = cache.scan_payload(mint)
    if not scan:
        cache.add_lesson(
            mint=mint, symbol=row.get("symbol"), verdict_was=verdict_was,
            outcome="miss", drop_pct=round(drop, 3), scored_at=row.get("scored_at"),
            learned_at=int(time.time()), wallets_flagged=0, deployer=None,
            detail=(
                f"{sym}: ilk taramada '{verdict_was}' kararı verildi, sonraki "
                f"{win_h} saatte piyasa değeri {pct} düştü. O taramanın ham "
                f"verisi artık saklı olmadığı için geriye dönük cüzdan analizi "
                f"yapılamadı — kimse kara listeye eklenmedi."
            ),
        )
        return

    suspects, why = _cluster_evidence(scan)
    deployer = ((scan.get("launch") or {}).get("deployer") or {}).get("address")

    note = (
        f"{sym}: '{verdict_was}' dendi, {win_h} saatte {pct} çöktü — "
        f"otomatik ders (mint {mint[:6]}…)"
    )
    flagged_wallets: list[str] = []
    for addr in suspects[:LEARN_MAX_WALLETS]:
        if addr and not registry.is_infrastructure(addr):
            cache.flagged_add(addr, note, via=mint, kind="wallet", bump=True)
            flagged_wallets.append(addr)
    dep_flagged = bool(deployer) and not registry.is_infrastructure(deployer)
    if dep_flagged:
        cache.flagged_add(
            deployer, note + " · deployer", via=mint, kind="deployer", bump=True
        )
    if flagged_wallets or dep_flagged:
        _reload_flagged()
    flagged_n = len(flagged_wallets) + (1 if dep_flagged else 0)

    head = (
        f"{sym}: ilk taramada '{verdict_was}' kararı verildi; sonraki {win_h} "
        f"saatte piyasa değeri {pct} düştü (sert çöküş)."
    )
    if why:
        detail = (
            f"{head} Kayıtlı tarama verisi geriye dönük incelendi ve şu "
            f"organize dağıtım izleri bulundu: {why}."
        )
        if flagged_n:
            who = []
            if flagged_wallets:
                who.append(f"{len(flagged_wallets)} cüzdan")
            if dep_flagged:
                who.append("deployer")
            detail += (
                f" {' + '.join(who)} kara listeye eklendi; bundan sonra bu "
                f"adreslerin geçtiği her taramada 'Daha önce işaretlenmiş "
                f"cüzdanlar' sinyali tetiklenip karar sertleşecek."
            )
    else:
        detail = (
            f"{head} Kayıtlı tarama verisinde organize dağıtım izi (ortak "
            f"fonlayıcı, gizli fonlama ağacı, taze cüzdan kümesi, tek elde "
            f"toplanmış arz) bulunamadı — bu büyük olasılıkla genel "
            f"piyasa/likidite çöküşü, koordineli bir rug değil. Masum "
            f"cüzdanları cezalandırmamak için hiçbir adres işaretlenmedi."
        )

    cache.add_lesson(
        mint=mint, symbol=row.get("symbol"), verdict_was=verdict_was,
        outcome="miss", drop_pct=round(drop, 3), scored_at=row.get("scored_at"),
        learned_at=int(time.time()), wallets_flagged=flagged_n, deployer=deployer,
        detail=detail,
    )
    log.info(
        "DERS: %s (%s, -%s) · %s işaretlendi · %s",
        mint, verdict_was, pct, flagged_n, (why or "iz yok")[:120],
    )


def learn_from_rug(cache: ScanCache, row: dict, liq0: float, liq_now: float) -> None:
    """İzlenen bir tokenın likiditesi sert düştü → likidite çekilmiş.

    Kilit durumu ne dersse desin, ZİNCİRDE gözlenen budur. Tokenın yaratıcısını
    (varsa) kara listeye ekler; sonraki taramalarda o adresin bastığı her token
    'deployer geçmişi' / 'işaretli cüzdan' sinyalini tetikler.
    """
    mint = row["mint"]
    sym = row.get("symbol") or mint[:6]
    verdict_was = row.get("verdict") or "?"
    drop = 1.0 - (liq_now / liq0) if liq0 else 1.0
    pct = f"%{drop * 100:.0f}"
    age_h = max(1, round((int(time.time()) - row["scored_at"]) / 3600))

    creator = row.get("creator")
    scan = cache.scan_payload(mint)
    if not creator and scan:
        liq_blk = scan.get("liquidity") or {}
        creator = (
            liq_blk.get("pool_creator")
            or ((scan.get("launch") or {}).get("deployer") or {}).get("address")
        )

    note = (
        f"{sym}: taramadan {age_h} saat sonra likidite {pct} çekildi "
        f"(${liq0:,.0f} → ${liq_now:,.0f}) — rug. mint {mint[:6]}…"
    )
    flagged_creator = False
    if creator and not registry.is_infrastructure(creator):
        cache.flagged_add(creator, note + " · yaratıcı", via=mint, kind="deployer", bump=True)
        flagged_creator = True
        _reload_flagged()

    cache.track_mark_rug(mint, "rug")

    if flagged_creator:
        detail = (
            f"{sym}: ilk taramada '{verdict_was}' kararı verildi. Taramadan {age_h} "
            f"saat sonra havuz likiditesi ${liq0:,.0f}'dan ${liq_now:,.0f}'a düştü "
            f"({pct} çekilme) — token yaratıcısı likiditeyi çekti (rug). Yaratıcı "
            f"({_short_addr(creator)}) kalıcı kara listeye eklendi; bundan sonra "
            f"bu adresin bastığı her token 'Deployer geçmişi' ve 'Daha önce "
            f"işaretlenmiş cüzdanlar' sinyallerini anında tetikleyecek."
        )
    else:
        detail = (
            f"{sym}: taramadan {age_h} saat sonra havuz likiditesi {pct} çekildi "
            f"(${liq0:,.0f} → ${liq_now:,.0f}) — rug. Ancak havuzu açan cüzdan "
            f"çözülemedi (ör. desteklenmeyen AMM), bu yüzden kimse işaretlenemedi."
        )

    cache.add_lesson(
        mint=mint, symbol=row.get("symbol"), verdict_was=verdict_was,
        outcome="rug", drop_pct=round(drop, 3), scored_at=row.get("scored_at"),
        learned_at=int(time.time()),
        wallets_flagged=1 if flagged_creator else 0,
        deployer=creator, detail=detail,
    )
    log.info(
        "RUG: %s (%s) · likidite %s çekildi · yaratıcı %s",
        mint, verdict_was, pct, creator if flagged_creator else "çözülemedi",
    )


def _momentum_candidates(scan: dict) -> tuple[list[tuple[str, str]], str | None]:
    """Kayıtlı taramadan yükseliş kaydına eklenecek (adres, kind) adayları +
    deployer adresi. Altyapı/borsa adresleri elenir."""
    launch = scan.get("launch") or {}
    buyers = launch.get("buyers") or []
    deployer = (launch.get("deployer") or {}).get("address")

    owners = [b.get("owner") for b in buyers if b.get("owner")]
    funders: dict[str, int] = {}
    for b in buyers:
        f = b.get("funder")
        if f:
            funders[f] = funders.get(f, 0) + 1

    out: list[tuple[str, str]] = []
    for o in owners:
        if not registry.is_infrastructure(o):
            out.append((o, "wallet"))
    # Fonlayıcı yalnızca ≥2 alıcıyı beslediyse anlamlı (tek besleme = borsa/rastgele)
    for f, n in funders.items():
        if n >= 2 and not registry.is_infrastructure(f):
            out.append((f, "funder"))
    if deployer and not registry.is_infrastructure(deployer):
        out.append((deployer, "deployer"))
    return out, deployer


def learn_from_gain(cache: ScanCache, row: dict, rise: float) -> None:
    """organic/cabaled/inconclusive denip sonradan sert yükselen bir token —
    erken cüzdanlarını/fonlayıcılarını/deployer'ını yükseliş kaydına ekle."""
    mint = row["mint"]
    sym = row.get("symbol") or mint[:6]
    verdict_was = row.get("verdict") or "?"
    pct = f"%{rise * 100:.0f}"
    mult = f"{rise + 1:.1f}×"
    win_d = max(1, round(GAIN_WINDOW / 86400))
    base = row.get("mcap_at_scan")
    peak = row.get("mcap_max")

    scan = cache.scan_payload(mint)
    if not scan:
        cache.add_gain_lesson(
            mint=mint, symbol=row.get("symbol"), verdict_was=verdict_was,
            rise_pct=round(rise, 3), mcap_at_scan=base, mcap_peak=peak,
            scored_at=row.get("scored_at"), learned_at=int(time.time()),
            markers=0, deployer=None,
            detail=(
                f"{sym}: ilk taramada '{verdict_was}' dendi, sonraki {win_d} günde "
                f"piyasa değeri {pct} arttı ({mult}). O taramanın ham verisi artık "
                f"saklı değil — geriye dönük cüzdan analizi yapılamadı."
            ),
        )
        return

    cands, deployer = _momentum_candidates(scan)
    note = f"{sym}: '{verdict_was}' → {win_d}g içinde {mult} yükseldi (mint {mint[:6]}…)"
    recorded = 0
    for addr, kind in cands[:GAIN_MAX_MARKERS]:
        cache.gainer_add(addr, kind=kind, note=note, via=mint, bump=True)
        recorded += 1
    if recorded:
        _reload_gainers()

    n_wallet = sum(1 for _, k in cands if k == "wallet")
    n_funder = sum(1 for _, k in cands if k == "funder")
    has_dep = any(k == "deployer" for _, k in cands)
    who = []
    if n_wallet:
        who.append(f"{n_wallet} erken alıcı cüzdanı")
    if n_funder:
        who.append(f"{n_funder} ortak fonlayıcı")
    if has_dep:
        who.append("deployer")
    who_txt = ", ".join(who) if who else "kayıtlı cüzdan yok"

    detail = (
        f"{sym}: ilk taramada '{verdict_was}' kararı verildi; sonraki {win_d} günde "
        f"piyasa değeri {pct} arttı ({mult}, {_short_addr(mint)}). "
        f"O taramanın erken katılımcıları yükseliş kaydına eklendi: {who_txt}. "
        f"Bir adres ≥2 ayrı yükselişte görülünce 'doğrulanmış' sayılır; bundan "
        f"sonra bu adreslerin geçtiği taramalarda sonuç ekranında 'Yükseliş "
        f"sinyali' notu çıkar. Not: bu bir korelasyondur, nedensellik ya da "
        f"fiyat tahmini değildir."
    )
    cache.add_gain_lesson(
        mint=mint, symbol=row.get("symbol"), verdict_was=verdict_was,
        rise_pct=round(rise, 3), mcap_at_scan=base, mcap_peak=peak,
        scored_at=row.get("scored_at"), learned_at=int(time.time()),
        markers=recorded, deployer=deployer, detail=detail,
    )
    log.info(
        "YÜKSELİŞ DERSİ: %s (%s, +%s) · %s adres kaydedildi",
        mint, verdict_was, pct, recorded,
    )


async def _track_loop() -> None:
    while True:
        try:
            await refresh_track()
        except Exception:  # noqa: BLE001
            log.exception("Karne yenileme hatası")
        await asyncio.sleep(TRACK_POLL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pool"] = RpcPool()
    dsn = os.getenv("DATABASE_URL") or None
    state["cache"] = ScanCache(
        path=os.getenv("CACHE_PATH", "data/scans.db"),
        ttl=int(os.getenv("CACHE_TTL", "900")),
        dsn=dsn,
    )
    log.info(
        "Havuz hazır: %s sağlayıcı · depo: %s",
        len(state["pool"].providers),
        "Postgres" if dsn else "SQLite",
    )
    try:
        rows = {r["address"]: (r["note"] or "flagged") for r in state["cache"].flagged_list()}
        registry.set_runtime_flagged(rows)
        if rows:
            log.info("İşaretli cüzdan yüklendi: %s", len(rows))
    except Exception:  # noqa: BLE001
        log.exception("İşaretli cüzdan listesi yüklenemedi")
    try:
        _reload_gainers()
        n = len(registry.RUNTIME_GAINERS)
        if n:
            log.info("Yükseliş sinyali cüzdanı yüklendi: %s", n)
    except Exception:  # noqa: BLE001
        log.exception("Yükseliş sinyali listesi yüklenemedi")
    try:
        bkey = state["cache"].config_get("birdeye_api_key")
        rpc_trades.set_runtime_config(birdeye_api_key=bkey)
        if bkey:
            log.info("Birdeye anahtarı DB'den yüklendi.")
    except Exception:  # noqa: BLE001
        log.exception("Entegrasyon anahtarları yüklenemedi")
    try:
        rpc_db = state["cache"].config_get("rpc_endpoints")
        if rpc_db:
            state["pool"].reconfigure(rpc_db)
            log.info("RPC uç noktaları DB'den yüklendi.")
    except Exception:  # noqa: BLE001
        log.exception("RPC uç noktaları DB'den yüklenemedi (env'e düşülüyor)")
    try:
        _reconfigure_x()
        if xpost.public_status()["enabled"]:
            log.info("X otomatik paylaşım AÇIK.")
    except Exception:  # noqa: BLE001
        log.exception("X paylaşım yapılandırması yüklenemedi")
    try:
        ttl_db = state["cache"].config_get("cache_ttl_sec")
        if ttl_db and int(ttl_db) > 0:
            state["cache"].ttl = int(ttl_db)
            log.info("Önbellek süresi DB'den: %s sn", ttl_db)
    except Exception:  # noqa: BLE001
        log.exception("Önbellek süresi yüklenemedi")
    try:
        mmc_db = state["cache"].config_get("min_market_cap_usd")
        if mmc_db is not None:
            scanner.set_min_market_cap(mmc_db)
            log.info("Min. tarama eşiği DB'den: $%s", scanner.get_min_market_cap())
    except Exception:  # noqa: BLE001
        log.exception("Min. tarama eşiği yüklenemedi")
    track_task = asyncio.create_task(_track_loop())
    yield
    track_task.cancel()
    await state["pool"].aclose()
    state["cache"].close()


app = FastAPI(title="america.sx", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < 60]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(429, "Dakikadaki tarama limitini aştın. Biraz bekle.")
    hits.append(now)
    _ip_hits[ip] = hits


def _refresh_cooldown_sec() -> int:
    try:
        v = state["cache"].config_get("refresh_cooldown_sec")
        if v is not None:
            return max(0, int(v))
    except Exception:  # noqa: BLE001
        pass
    return max(0, REFRESH_COOLDOWN_DEFAULT)


def _check_refresh_cooldown(mint: str) -> None:
    """Yakın zamanda taranmış bir tokenı tekrar canlı taramayı engeller."""
    cd = _refresh_cooldown_sec()
    if cd <= 0:
        return
    try:
        prev = state["cache"].scan_payload(mint)
    except Exception:  # noqa: BLE001
        prev = None
    if not prev:
        return
    last = float(prev.get("scanned_at") or 0)
    wait = cd - (time.time() - last)
    if wait > 0:
        mins = max(1, round(wait / 60))
        raise HTTPException(
            429,
            f"Bu token yakın zamanda tarandı. Sonuç ekranındaki karar güncel — "
            f"tekrar canlı taramak için ~{mins} dk sonra dene.",
        )


def _validate(mint: str) -> str:
    mint = mint.strip()
    if not BASE58.match(mint):
        raise HTTPException(400, "Geçersiz Solana adresi.")
    return mint


async def _run_scan(mint: str) -> dict:
    """Aynı token için eşzamanlı istekleri tek taramada birleştirir."""
    if mint in _inflight:
        return await _inflight[mint]

    async def work() -> dict:
        try:
            result = await scan_token(state["pool"], mint, state["cache"])
            state["cache"].put(mint, result)
            token = result.get("token") or {}
            verdict = result.get("verdict") or {}
            liq = result.get("liquidity") or {}
            creator = (
                liq.get("pool_creator")
                or ((result.get("launch") or {}).get("deployer") or {}).get("address")
            )
            # Tüm kararlar izlenir (organik dahil — likidite çekilme takibi için).
            state["cache"].track_start(
                mint,
                token.get("symbol"),
                verdict.get("kind"),
                verdict.get("score"),
                token.get("market_cap"),
                image=token.get("image"),
                liquidity=token.get("liquidity_usd"),
                creator=creator,
            )
            # X otomatik paylaşım — bloklamaz, hata taramayı etkilemez.
            asyncio.create_task(xpost.maybe_autopost(result, state["cache"]))
            return result
        finally:
            _inflight.pop(mint, None)

    task = asyncio.create_task(work())
    _inflight[mint] = task
    return await task


@app.get("/api/scan/{mint}")
async def scan(mint: str, request: Request):
    mint = _validate(mint)
    cached = state["cache"].get(mint)
    if cached:
        return cached
    _check_rate(request)
    try:
        return await _run_scan(mint)
    except TokenTooSmall as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RpcError as exc:
        raise HTTPException(
            503, f"Zincir verisi şu an alınamıyor: {exc}"
        ) from exc


@app.post("/api/scan/{mint}/fresh")
async def rescan(mint: str, request: Request):
    mint = _validate(mint)
    _check_rate(request)
    _check_refresh_cooldown(mint)
    try:
        return await _run_scan(mint)
    except TokenTooSmall as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RpcError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/history/{mint}")
async def history(mint: str):
    return {"mint": _validate(mint), "history": state["cache"].history(_validate(mint))}


@app.get("/api/recent")
async def recent(limit: int = 20):
    return {"scans": state["cache"].recent(min(limit, 50))}


@app.get("/api/organic")
async def organic(limit: int = 30):
    """Organic kararlı, çökmemiş tokenlar — karneden ayrı ve daha uzun liste."""
    return {
        "records": state["cache"].organic_list(min(limit, 60)),
        "window_sec": TRACK_WINDOW,
    }


async def _backfill_track_symbols(limit: int = 25, budget: float = 6.0) -> None:
    """Sembolü / logosu eksik karne kayıtlarını DexScreener'dan doldurur.

    Eski kayıtlarda `symbol`/`image` çoğu zaman NULL; kayıtlı tarama payload'ı
    da silinmiş olabilir. Burada tazeden çekip kalıcı yazıyoruz. Eşzamanlı ve
    süre sınırlı — /api/track yanıtını fazla bekletmesin.
    """
    cache = state.get("cache")
    if cache is None:
        return
    mints = list(dict.fromkeys(
        cache.track_missing_symbol(limit) + cache.track_missing_image(limit)
    ))
    if not mints:
        return

    async def one(mint: str) -> None:
        try:
            snap = await fetch_market(mint)
        except Exception:  # noqa: BLE001
            return
        if snap.symbol:
            cache.track_set_symbol(mint, snap.symbol)
        if snap.image_url:
            cache.track_set_image(mint, snap.image_url)

    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(m) for m in mints)), timeout=budget
        )
    except asyncio.TimeoutError:
        pass


@app.get("/api/track")
async def track(limit: int = 20):
    # Instance yeni uyandıysa arka plan döngüsü henüz dönmemiş olabilir —
    # sayfa açılışında bir kez tetikle (throttle'lı, bloklamadan).
    global _last_track_refresh
    now = time.time()
    if now - _last_track_refresh > 45:
        _last_track_refresh = now
        asyncio.create_task(refresh_track())
    await _backfill_track_symbols(limit=min(limit, 50))
    return {
        "records": state["cache"].track_list(min(limit, 50)),
        "window_sec": TRACK_WINDOW,
        "drop_pct": TRACK_DROP,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "store": "postgres" if state["cache"].pg else "sqlite",
        "admin": bool(ADMIN_TOKEN),
        "providers": state["pool"].stats(),
    }


# --- Paylaşım: kart, rozet, OG sayfası -----------------------------------

_VLABEL = {
    "bundled": "Bundled", "cabaled": "Cabaled",
    "organic": "Organic", "inconclusive": "Inconclusive",
}


def _cached_scan(mint: str) -> dict | None:
    try:
        mint = _validate(mint)
    except HTTPException:
        return None
    return state["cache"].get(mint)


@app.get("/card/{mint}.png")
async def card_png(mint: str):
    scan = _cached_scan(mint)
    png = render_png(scan, mint)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/badge/{mint}.svg")
async def badge_svg(mint: str):
    scan = _cached_scan(mint)
    return Response(
        content=render_badge_svg(scan, mint),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/t/{mint}", response_class=HTMLResponse)
async def share_page(mint: str, request: Request):
    """Arayüzün aynısı ama <head>'e o tokenın OG etiketleri enjekte edilmiş."""
    if not _FRONTEND_DIR.is_dir():
        raise HTTPException(404, "frontend yok")
    doc = (_FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    scan = _cached_scan(mint)
    base = str(request.base_url).rstrip("/")

    # mint _validate'ten geçti — yalnızca base58, HTML/JS'e güvenli.
    mint = _validate(mint)
    if scan:
        tok = scan.get("token") or {}
        v = scan.get("verdict") or {}
        label = _VLABEL.get(v.get("kind"), "Scanned")
        sym = tok.get("symbol") or mint[:6]
        title = f"{label} — {sym} · america.sx"
        desc = (
            f"{label} · score {v.get('score')} · confidence {v.get('confidence')}. "
            f"{v.get('summary', '')}"
        )[:200]
    else:
        title = "america.sx — Solana launch forensics"
        desc = "Paste a Solana mint. Seventeen independent on-chain signals decide."

    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
        )

    og = (
        f'<meta property="og:type" content="website">'
        f'<meta property="og:title" content="{esc(title)}">'
        f'<meta property="og:description" content="{esc(desc)}">'
        f'<meta property="og:image" content="{base}/card/{mint}.png">'
        f'<meta property="og:url" content="{base}/t/{mint}">'
        f'<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{esc(title)}">'
        f'<meta name="twitter:description" content="{esc(desc)}">'
        f'<meta name="twitter:image" content="{base}/card/{mint}.png">'
        f'<script>window.__PREFILL_MINT__="{mint}";</script>'
    )
    doc = doc.replace("</head>", og + "</head>", 1)
    return HTMLResponse(doc)


@app.post("/api/appeal")
async def appeal(request: Request, payload: dict = Body(...)):
    mint = _validate(str(payload.get("mint", "")))
    body = str(payload.get("body", "")).strip()
    contact = str(payload.get("contact", "")).strip() or None
    if len(body) < 20:
        raise HTTPException(422, "Lütfen itirazını biraz daha açık yaz (en az 20 karakter).")
    if len(body) > 4000:
        body = body[:4000]
    ip = request.client.host if request.client else "unknown"
    cache = state["cache"]
    if cache.appeals_today(contact, ip) >= 5:
        raise HTTPException(429, "Bugünlük itiraz limitine ulaştın. Yarın tekrar dene.")
    cached = cache.get(mint)
    verdict = (cached or {}).get("verdict", {}).get("kind")
    # IP'yi contact yoksa istismar anahtarı olarak sakla (e-posta değil)
    aid = cache.add_appeal(mint, verdict, contact or ip, body)
    log.info("İtiraz #%s — %s (%s)", aid, mint, verdict)
    return {"ok": True, "id": aid}


# --- Admin paneli --------------------------------------------------------

import hashlib

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or ""
RPC_QUOTA = int(os.getenv("RPC_MONTHLY_QUOTA", "0"))  # 0 = bilinmiyor


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _admin_enabled() -> bool:
    if ADMIN_TOKEN:
        return True
    try:
        return bool(state["cache"].config_get("admin_token_sha256"))
    except Exception:  # noqa: BLE001
        return False


def _admin_ok(given: str) -> bool:
    if not given:
        return False
    db_hash = None
    try:
        db_hash = state["cache"].config_get("admin_token_sha256")
    except Exception:  # noqa: BLE001
        pass
    if db_hash:
        return secrets.compare_digest(_sha(given), db_hash)
    return bool(ADMIN_TOKEN) and secrets.compare_digest(given, ADMIN_TOKEN)


def _admin(request: Request) -> None:
    if not _admin_enabled():
        raise HTTPException(404, "Admin paneli kapalı (ADMIN_TOKEN tanımsız).")
    given = request.headers.get("X-Admin-Token") or ""
    if not given:
        auth = request.headers.get("Authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not _admin_ok(given):
        raise HTTPException(401, "Yetkisiz.")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    f = _FRONTEND_DIR / "admin.html"
    if not f.is_file():
        raise HTTPException(404, "admin.html yok")
    return HTMLResponse(f.read_text(encoding="utf-8"))


@app.get("/api/admin/overview", dependencies=[Depends(_admin)])
async def admin_overview():
    return {
        "stats": state["cache"].stats(),
        "providers": state["pool"].stats(),
        "config": {
            "track_window_sec": TRACK_WINDOW,
            "track_drop_pct": TRACK_DROP,
            "min_market_cap": scanner.get_min_market_cap(),
            "rate_limit_per_min": RATE_LIMIT,
            "rpc_quota": RPC_QUOTA,
            "cache_ttl_hours": round(state["cache"].ttl / 3600, 2),
        },
    }


@app.post("/api/admin/password", dependencies=[Depends(_admin)])
async def admin_password(payload: dict = Body(...)):
    new = str(payload.get("new", "")).strip()
    if len(new) < 8:
        raise HTTPException(422, "Yeni şifre en az 8 karakter olmalı.")
    state["cache"].config_set("admin_token_sha256", _sha(new))
    log.info("Admin şifresi değiştirildi.")
    return {"ok": True}


@app.get("/api/admin/appeals", dependencies=[Depends(_admin)])
async def admin_appeals(status: str | None = None):
    return {"appeals": state["cache"].appeals_list(status)}


@app.post("/api/admin/appeals/{appeal_id}", dependencies=[Depends(_admin)])
async def admin_appeal_update(appeal_id: int, payload: dict = Body(...)):
    status = str(payload.get("status", "")).strip()
    if status not in ("open", "resolved", "dismissed"):
        raise HTTPException(422, "status: open | resolved | dismissed")
    state["cache"].appeal_set_status(appeal_id, status, payload.get("note"))
    return {"ok": True}


@app.get("/api/admin/track", dependencies=[Depends(_admin)])
async def admin_track():
    return {"records": state["cache"].track_list(200), "window_sec": TRACK_WINDOW}


@app.delete("/api/admin/track/{mint}", dependencies=[Depends(_admin)])
async def admin_track_delete(mint: str):
    state["cache"].track_delete(_validate(mint))
    return {"ok": True}


@app.delete("/api/admin/cache/{mint}", dependencies=[Depends(_admin)])
async def admin_cache_delete(mint: str):
    state["cache"].cache_delete(_validate(mint))
    return {"ok": True}


@app.post("/api/admin/rescan/{mint}", dependencies=[Depends(_admin)])
async def admin_rescan(mint: str):
    mint = _validate(mint)
    state["cache"].cache_delete(mint)
    try:
        return await _run_scan(mint)
    except TokenTooSmall as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, RpcError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/admin/flagged", dependencies=[Depends(_admin)])
async def admin_flagged():
    return {"flagged": state["cache"].flagged_list()}


@app.post("/api/admin/flagged", dependencies=[Depends(_admin)])
async def admin_flagged_add(payload: dict = Body(...)):
    addr = str(payload.get("address", "")).strip()
    if not BASE58.match(addr):
        raise HTTPException(422, "Geçersiz Solana adresi.")
    state["cache"].flagged_add(addr, payload.get("note"))
    _reload_flagged()
    return {"ok": True}


@app.delete("/api/admin/flagged/{address}", dependencies=[Depends(_admin)])
async def admin_flagged_remove(address: str):
    state["cache"].flagged_remove(address.strip())
    _reload_flagged()
    return {"ok": True}


@app.get("/api/admin/lessons", dependencies=[Depends(_admin)])
async def admin_lessons():
    return {
        "lessons": state["cache"].lessons_list(),
        "window_h": round(TRACK_WINDOW / 3600, 1),
        "min_drop_pct": LEARN_MIN_DROP,
        "learn_enabled": LEARN_ENABLED,
    }


@app.post("/api/admin/lessons/{mint}/undo", dependencies=[Depends(_admin)])
async def admin_lesson_undo(mint: str):
    removed = state["cache"].lesson_undo(_validate(mint))
    _reload_flagged()
    return {"ok": True, "removed": removed}


@app.get("/api/admin/momentum", dependencies=[Depends(_admin)])
async def admin_momentum():
    """Yükseliş öğrenmesi: dersler + izlenen cüzdanlar + sinyal yakalanan taramalar."""
    c = state["cache"]
    return {
        "lessons": c.gain_lessons_list(200),
        "gainers": c.gainers_list(),
        "hits": c.gain_hits_list(100),
        "window_d": round(GAIN_WINDOW / 86400, 1),
        "min_rise_pct": GAIN_MIN_RISE,
        "enabled": GAIN_ENABLED,
    }


@app.post("/api/admin/momentum/{mint}/undo", dependencies=[Depends(_admin)])
async def admin_momentum_undo(mint: str):
    removed = state["cache"].gain_lesson_undo(_validate(mint))
    _reload_gainers()
    return {"ok": True, "removed": removed}


@app.delete("/api/admin/gainers/{address}", dependencies=[Depends(_admin)])
async def admin_gainer_remove(address: str):
    state["cache"].gainer_remove(address)
    _reload_gainers()
    return {"ok": True}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "…" + key[-4:]


@app.get("/api/admin/settings", dependencies=[Depends(_admin)])
async def admin_settings():
    """Panelden ayarlanabilen tüm yapılandırma tek yerde."""
    cache = state["cache"]
    pool = state["pool"]
    db_rpc = cache.config_get("rpc_endpoints") or ""
    db_bkey = cache.config_get("birdeye_api_key") or ""
    env_bkey = os.getenv("BIRDEYE_API_KEY", "")
    return {
        # --- düzenlenebilir ---
        "cache_ttl_hours": round(cache.ttl / 3600, 3),
        "cache_ttl_source": "db" if cache.config_get("cache_ttl_sec") else "env",
        "refresh_cooldown_min": round(_refresh_cooldown_sec() / 60, 2),
        "refresh_cooldown_source": (
            "db" if cache.config_get("refresh_cooldown_sec") is not None else "env"
        ),
        "min_market_cap": scanner.get_min_market_cap(),
        "min_market_cap_source": (
            "db" if cache.config_get("min_market_cap_usd") is not None else "env"
        ),
        "rpc_endpoints_masked": pool_mask(pool.raw),
        "rpc_endpoints_source": "db" if db_rpc else "env",
        "rpc_provider_count": len(pool.providers),
        "rpc_has_das": pool.has_das,
        "birdeye": {
            "provider": rpc_trades.provider_name(),
            "configured": rpc_trades.available(),
            "source": "db" if db_bkey else ("env" if env_bkey else "none"),
            "masked": _mask_key(db_bkey or env_bkey),
        },
        "x_autopost": xpost.public_status(),
        # --- yalnızca env (bilgi amaçlı) ---
        "env_only": {
            "rate_limit_per_min": RATE_LIMIT,
            "track_window_sec": TRACK_WINDOW,
            "track_drop_pct": TRACK_DROP,
            "rpc_quota": RPC_QUOTA,
            "database": "postgres" if cache.pg else "sqlite",
        },
    }


@app.post("/api/admin/settings", dependencies=[Depends(_admin)])
async def admin_settings_set(payload: dict = Body(...)):
    """Panelden gelen ayarları uygular (verilen alanlar). Hepsi anında geçerli,
    DB'ye yazılır; ilgili env değişkeni yalnızca başlangıç varsayılanı olur."""
    cache = state["cache"]
    changed: list[str] = []

    if "cache_ttl_hours" in payload:
        try:
            hours = float(payload["cache_ttl_hours"])
        except (TypeError, ValueError):
            raise HTTPException(422, "cache_ttl_hours bir sayı olmalı.") from None
        if not 0 < hours <= 168:
            raise HTTPException(422, "Önbellek süresi 0–168 saat arasında olmalı.")
        sec = int(round(hours * 3600))
        cache.config_set("cache_ttl_sec", str(sec))
        cache.ttl = sec
        changed.append("cache_ttl")

    if "refresh_cooldown_min" in payload:
        try:
            mins = float(payload["refresh_cooldown_min"])
        except (TypeError, ValueError):
            raise HTTPException(422, "refresh_cooldown_min bir sayı olmalı.") from None
        if not 0 <= mins <= 1440:
            raise HTTPException(422, "Yenileme aralığı 0–1440 dakika arasında olmalı.")
        cache.config_set("refresh_cooldown_sec", str(int(round(mins * 60))))
        changed.append("refresh_cooldown")

    if "min_market_cap" in payload:
        try:
            mmc = float(payload["min_market_cap"])
        except (TypeError, ValueError):
            raise HTTPException(422, "min_market_cap bir sayı olmalı.") from None
        if not 0 <= mmc <= 100_000_000:
            raise HTTPException(422, "Eşik 0–100.000.000 USD arasında olmalı.")
        cache.config_set("min_market_cap_usd", str(mmc))
        scanner.set_min_market_cap(mmc)
        changed.append("min_market_cap")

    if "rpc_endpoints" in payload:
        raw = str(payload.get("rpc_endpoints") or "").strip()
        if not raw:
            raise HTTPException(422, "RPC uç noktası boş olamaz.")
        try:
            state["pool"].reconfigure(raw)
        except RpcError as exc:
            raise HTTPException(422, f"Geçersiz RPC yapılandırması: {exc}") from exc
        cache.config_set("rpc_endpoints", raw)
        changed.append("rpc_endpoints")

    if "birdeye_api_key" in payload:
        key = str(payload.get("birdeye_api_key") or "").strip()
        cache.config_set("birdeye_api_key", key)
        rpc_trades.set_runtime_config(birdeye_api_key=key or None)
        changed.append("birdeye")

    if "public_base_url" in payload:
        url = str(payload.get("public_base_url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(422, "public_base_url http(s):// ile başlamalı.")
        cache.config_set("public_base_url", url)
        changed.append("public_base_url")

    # --- X otomatik paylaşım ---
    if "x_autopost_enabled" in payload:
        cache.config_set(
            "x_autopost_enabled", "1" if payload.get("x_autopost_enabled") else "0"
        )
        changed.append("x_enabled")

    for fld, key in (
        ("x_api_key", "x_api_key"), ("x_api_secret", "x_api_secret"),
        ("x_access_token", "x_access_token"), ("x_access_secret", "x_access_secret"),
    ):
        if fld in payload:
            cache.config_set(key, str(payload.get(fld) or "").strip())
            changed.append(fld)

    if "x_autopost_config" in payload and isinstance(
        payload["x_autopost_config"], dict
    ):
        c = payload["x_autopost_config"]
        allowed = {
            "verdicts", "min_mcap", "min_score", "min_confidence",
            "cooldown_h", "max_per_day", "media", "lang",
        }
        clean = {k: v for k, v in c.items() if k in allowed}
        if "verdicts" in clean:
            clean["verdicts"] = [
                x for x in clean["verdicts"]
                if x in ("bundled", "cabaled", "organic", "inconclusive")
            ] or ["bundled"]
        if "lang" in clean and clean["lang"] not in ("en", "tr"):
            clean["lang"] = "en"
        cache.config_set("x_autopost_config", json.dumps(clean))
        changed.append("x_config")

    if not changed:
        raise HTTPException(422, "Değiştirilecek bir alan gönderilmedi.")

    if any(x.startswith("x_") for x in changed) or "public_base_url" in changed:
        _reconfigure_x()

    log.info("Admin ayar değişikliği: %s", ", ".join(changed))
    return {"ok": True, "changed": changed}


@app.post("/api/admin/x/test", dependencies=[Depends(_admin)])
async def admin_x_test(payload: dict = Body(default={})):
    """Manuel test tweet'i — eşik kontrolü yok, sadece kimlik denemesi.

    payload.mint verilirse o tokenın kayıtlı taramasıyla gerçek biçimde atar.
    """
    cache = state["cache"]
    sample = None
    mint = str((payload or {}).get("mint") or "").strip()
    if mint:
        try:
            mint = _validate(mint)
        except HTTPException:
            raise HTTPException(422, "Geçersiz mint.") from None
        sample = cache.scan_payload(mint)
        if not sample:
            raise HTTPException(404, "Bu mint için kayıtlı tarama yok — önce tara.")
    try:
        res = await xpost.send_test_tweet(cache, sample)
    except xpost.XError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"X hatası: {exc}") from exc
    return {"ok": True, **res}


@app.get("/api/admin/x/posts", dependencies=[Depends(_admin)])
async def admin_x_posts():
    return {"posts": state["cache"].x_posts_recent(30)}


@app.get("/api/admin/backup", dependencies=[Depends(_admin)])
async def admin_backup(full: int = 1):
    """Tüm veritabanının JSON yedeği (indirilebilir dosya)."""
    data = state["cache"].export_all(include_scans=bool(full))
    ts = time.strftime("%Y%m%d-%H%M", time.gmtime())
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="solscope-yedek-{ts}.json"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/admin/restore", dependencies=[Depends(_admin)])
async def admin_restore(payload: dict = Body(...)):
    """Yedekten geri yükler — İLGİLİ TABLOLARIN MEVCUT VERİSİNİ SİLER."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict) or data.get("format") != "solscope-backup":
        raise HTTPException(422, "Geçerli bir SolScope yedeği değil.")
    only = payload.get("only")
    only = only if isinstance(only, list) and only else None
    done = state["cache"].import_all(data, only=only)
    # Canlı listeleri / ayarları tazele.
    try:
        _reload_flagged()
        _reload_gainers()
        _reconfigure_x()
        c = state["cache"]
        scanner.set_min_market_cap(c.config_get("min_market_cap_usd"))
        ttl = c.config_get("cache_ttl_sec")
        if ttl and int(ttl) > 0:
            c.ttl = int(ttl)
        rpc_raw = c.config_get("rpc_endpoints")
        if rpc_raw:
            state["pool"].reconfigure(rpc_raw)
        rpc_trades.set_runtime_config(birdeye_api_key=c.config_get("birdeye_api_key"))
    except Exception:  # noqa: BLE001
        log.exception("Geri yükleme sonrası tazeleme kısmen düştü")
    log.info("Yedek geri yüklendi: %s", done)
    return {"ok": True, "restored": done}


def _reload_flagged() -> None:
    rows = {
        r["address"]: (r["note"] or "flagged")
        for r in state["cache"].flagged_list()
    }
    registry.set_runtime_flagged(rows)


def _reload_gainers() -> None:
    rows = {
        r["address"]: {
            "kind": r.get("kind") or "wallet",
            "hits": int(r.get("hits") or 1),
            "note": r.get("note"),
        }
        for r in state["cache"].gainers_list()
    }
    registry.set_runtime_gainers(rows)


def _reconfigure_x() -> None:
    """DB config + env → xpost.configure(). Her admin değişikliğinden sonra çağır."""
    c = state["cache"]
    raw = c.config_get("x_autopost_config")
    extra: dict = {}
    if raw:
        try:
            extra = json.loads(raw) or {}
        except (TypeError, ValueError):
            extra = {}
    xpost.configure({
        "enabled": c.config_get("x_autopost_enabled") == "1",
        "api_key": c.config_get("x_api_key") or os.getenv("X_API_KEY", ""),
        "api_secret": c.config_get("x_api_secret") or os.getenv("X_API_SECRET", ""),
        "access_token": c.config_get("x_access_token")
        or os.getenv("X_ACCESS_TOKEN", ""),
        "access_secret": c.config_get("x_access_secret")
        or os.getenv("X_ACCESS_SECRET", ""),
        "base_url": c.config_get("public_base_url")
        or os.getenv("PUBLIC_BASE_URL", ""),
        **extra,
    })


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Beklenmeyen hata: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Tarama tamamlanamadı. Tekrar dene."},
    )


# --- Statik arayüz ---------------------------------------------------------
# API rotalarından SONRA bağlanmalı: "/" catch-all olduğu için önce eklenirse
# /api/* isteklerini gölgeler. FRONTEND_DIR yoksa (yalnız-API dağıtımı) atlanır.
_FRONTEND_DIR = Path(
    os.getenv("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend")
)
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    log.info("Arayüz sunuluyor: %s", _FRONTEND_DIR)
else:
    log.warning("Arayüz klasörü bulunamadı (%s) — yalnızca API aktif.", _FRONTEND_DIR)
