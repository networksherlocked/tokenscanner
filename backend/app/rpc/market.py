"""
Piyasa verisi — DexScreener (anahtarsız, ücretsiz, ~300 istek/dk), düşerse
GeckoTerminal'e (anahtarsız, herkese açık) yedeklenir.

Tek bağımlı olma: bir sağlayıcı düşerse tarama tamamen ölmemeli, sadece
ilgili sinyaller "veri yok" durumuna geçmeli.

NOT: DexScreener'ın kendisi çalışıyor (yerelden test edilip doğrulandı) ama
Render'ın barındırma IP aralığından — tarayıcı gibi görünen başlıklarla
denense bile — sık sık hızlı ve tutarlı biçimde reddediliyor; bu, IP
itibarına dayalı bir engelleme gibi görünüyor (User-Agent'a değil). Tek
sağlayıcıya bağlı kalmamak için farklı bir barındırma/Cloudflare
itibarına sahip GeckoTerminal'e yedeklendi.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
GECKOTERMINAL_TOKEN = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}"

# --- Süreç-içi önbellek + eşzamanlı istek birleştirme + GeckoTerminal kısıtı ---
#
# DexScreener Render IP'sinden neredeyse her zaman reddediliyor; her çağrı
# GeckoTerminal'e düşüyor. GeckoTerminal'in ücretsiz API'si çok sıkı kısıtlı
# (~5 art arda istekte 429). Bir tarama + karne yenilemesi + deployer geçmişi
# kontrolü aynı mint'i defalarca çekince limit hemen doluyor ve "piyasa verisi
# yok" çıkıyordu. Çözüm: kısa ömürlü önbellek + aynı mint için tek uçuş +
# GeckoTerminal çağrıları arasında minimum aralık.
_CACHE_OK_TTL = 120.0       # başarılı sonuç bu kadar saniye taze sayılır
_CACHE_FAIL_TTL = 40.0      # başarısız sonuç da kısa süre önbelleklenir (hammer'ı önler)
_GT_MIN_INTERVAL = 2.1      # GeckoTerminal çağrıları arası en az bu kadar sn (~28/dk, ücretsiz limit 30/dk)

_market_cache: dict[str, tuple[float, "MarketSnapshot"]] = {}
_inflight: dict[str, asyncio.Task] = {}
_gt_lock = asyncio.Lock()
_gt_last = 0.0

# Varsayılan httpx User-Agent'ı ("python-httpx/x.y") Cloudflare arkasındaki
# API'lerde bot imzası olarak damgalanıp paylaşılan barındırma IP'lerinden
# (Render gibi) sessizce reddedilebiliyor — tarayıcı gibi görünen başlıklar
# bunu önler. DexScreener anahtarsız/herkese açık; bu sahtecilik değil.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@dataclass
class MarketSnapshot:
    available: bool = False
    name: str | None = None
    symbol: str | None = None
    price_usd: float | None = None
    market_cap: float | None = None
    liquidity_usd: float | None = None
    volume_24h: float | None = None
    pair_created_at: int | None = None  # unix ts (saniye)
    dex: str | None = None
    pair_address: str | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    socials: list[dict] = field(default_factory=list)  # [{"type": "twitter"|"telegram"|"website"|…, "url": …}]
    image_url: str | None = None        # token logosu (DexScreener CDN)

    @property
    def liquidity_ratio(self) -> float | None:
        """Likidite / piyasa değeri. Düşükse çıkış zor demektir."""
        if not self.market_cap or not self.liquidity_usd:
            return None
        return self.liquidity_usd / self.market_cap


async def fetch_market(mint: str, timeout: float = 12.0) -> MarketSnapshot:
    now = time.monotonic()
    hit = _market_cache.get(mint)
    if hit and now < hit[0]:
        return hit[1]
    # Aynı mint için zaten bir istek uçuyorsa ona bağlan (birden çok tarama /
    # backfill / deployer kontrolü aynı anda aynı mint'i çekmesin).
    task = _inflight.get(mint)
    if task is None:
        task = asyncio.ensure_future(_fetch_market_uncached(mint, timeout))
        _inflight[mint] = task
        try:
            snap = await task
        finally:
            _inflight.pop(mint, None)
        ttl = _CACHE_OK_TTL if snap.available else _CACHE_FAIL_TTL
        _market_cache[mint] = (time.monotonic() + ttl, snap)
        if len(_market_cache) > 800:
            cut = time.monotonic()
            for k in [k for k, (exp, _) in _market_cache.items() if exp < cut]:
                _market_cache.pop(k, None)
        return snap
    return await asyncio.shield(task)


async def _fetch_market_uncached(mint: str, timeout: float) -> MarketSnapshot:
    snap = await _fetch_dexscreener(mint, min(timeout, 8.0))
    if snap.available:
        return snap
    # DexScreener boş/düştü — GeckoTerminal'e yedeklen. Farklı host, farklı
    # Cloudflare itibarı; DexScreener'ı engelleyen IP kısıtı burada geçerli
    # olmayabilir. Tek deneme yeterli — asıl yedeklilik artık iki farklı
    # sağlayıcı arasında, aynı sağlayıcıyı tekrar tekrar denemekte değil.
    gt = await _fetch_geckoterminal(mint, min(timeout, 8.0))
    return gt if gt.available else snap


async def _fetch_dexscreener(mint: str, timeout: float) -> MarketSnapshot:
    snap = MarketSnapshot()
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS) as client:
            resp = await client.get(DEXSCREENER.format(mint=mint))
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("DexScreener piyasa verisi alınamadı %s: %s", mint, exc)
        return snap

    pairs = data.get("pairs") or []
    if not pairs:
        return snap

    # En derin likiditeye sahip çifti birincil kabul et.
    pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    base = pair.get("baseToken") or {}
    txns = (pair.get("txns") or {}).get("h24") or {}

    snap.available = True
    snap.name = base.get("name")
    snap.symbol = base.get("symbol")
    snap.price_usd = _f(pair.get("priceUsd"))
    snap.market_cap = _f(pair.get("marketCap") or pair.get("fdv"))
    snap.liquidity_usd = _f((pair.get("liquidity") or {}).get("usd"))
    snap.volume_24h = _f((pair.get("volume") or {}).get("h24"))
    snap.dex = pair.get("dexId")
    snap.pair_address = pair.get("pairAddress")
    snap.buys_24h = txns.get("buys")
    snap.sells_24h = txns.get("sells")

    created = pair.get("pairCreatedAt")
    if created:
        snap.pair_created_at = int(created) // 1000  # ms -> s

    info = pair.get("info") or {}
    img = info.get("imageUrl") or info.get("openGraph")
    if isinstance(img, str) and img.startswith("http"):
        snap.image_url = img
    socials: list[dict] = []
    for s in info.get("socials") or []:
        url = s.get("url")
        if url:
            socials.append({"type": (s.get("type") or "social").lower(), "url": url})
    for w in info.get("websites") or []:
        url = w.get("url")
        if url:
            socials.append({"type": "website", "url": url})
    snap.socials = socials

    return snap


async def _fetch_geckoterminal(mint: str, timeout: float) -> MarketSnapshot:
    """DexScreener yedeği. Tek çağrıda ad/sembol/fiyat/mcap/likidite/hacim +
    havuz adresi (top_pools[0]) verir — sosyal linkler yok (DexScreener'da
    varsa zaten dolduruldu, burada eklemeye çalışmıyoruz).

    GeckoTerminal ücretsiz API'si art arda ~5 istekte 429 veriyor — çağrılar
    arasında en az `_GT_MIN_INTERVAL` sn bekleyerek global bir kuyruk kuruyoruz.
    """
    global _gt_last
    snap = MarketSnapshot()
    async with _gt_lock:
        wait = _GT_MIN_INTERVAL - (time.monotonic() - _gt_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS) as client:
                resp = await client.get(GECKOTERMINAL_TOKEN.format(mint=mint))
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("GeckoTerminal yedek piyasa verisi de alınamadı %s: %s", mint, exc)
            return snap
        finally:
            _gt_last = time.monotonic()

    attrs = ((data or {}).get("data") or {}).get("attributes") or {}
    if not attrs or not attrs.get("symbol"):
        return snap

    snap.available = True
    snap.name = attrs.get("name")
    snap.symbol = attrs.get("symbol")
    snap.price_usd = _f(attrs.get("price_usd"))
    snap.market_cap = _f(attrs.get("market_cap_usd")) or _f(attrs.get("fdv_usd"))
    snap.liquidity_usd = _f(attrs.get("total_reserve_in_usd"))
    snap.volume_24h = _f((attrs.get("volume_usd") or {}).get("h24"))
    img = attrs.get("image_url")
    if isinstance(img, str) and img.startswith("http"):
        snap.image_url = img

    pools = (((data or {}).get("data") or {}).get("relationships") or {}).get("top_pools") or {}
    ids = [p.get("id") for p in (pools.get("data") or []) if p.get("id")]
    if ids:
        # id biçimi "solana_<havuz adresi>"
        snap.pair_address = ids[0].split("_", 1)[-1]

    return snap


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
