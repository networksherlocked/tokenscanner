"""
Lansman analizi — asıl bundle tespiti burada.

`getTokenLargestAccounts` bir tokenın ŞU ANKİ en büyük cüzdanlarını verir;
77 günlük bir tokende bunlar lansmanı snipe'layanlar değil, ikincil piyasadan
alan balinalar/market maker'lardır. Gerçek paket, tokenın İLK işlemlerinde
görünür.

Strateji: bonding curve (pump.fun) ya da pool adresini çıpa alıp imzaları en
eskiye kadar sayfalarız (bütçeli), en eski ~40 işlemi parse edip ilk alıcıları
çıkarırız. Bütçe dolar da başlangıcı göremezsek `available=False` — o zaman
motor "yalnızca mevcut yapı" moduna düşer ve bunu sonuçta belirtir.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import solana
from .pool import RpcPool
from ..engine import registry

log = logging.getLogger(__name__)

LAUNCH_MAX_PAGES = 22       # ~22k imza — hızlı taşınan lansmanları kapsar
LAUNCH_MAX_TX = 40          # parse edilecek en eski işlem sayısı
LAUNCH_MAX_BUYERS = 26
LAUNCH_ENRICH_MAX = 20      # kaç alıcının yaşı/fonlayıcısı çıkarılsın


@dataclass
class LaunchBuyer:
    owner: str
    amount_raw: int = 0
    first_slot: int | None = None
    first_block_time: int | None = None
    entry_fee: int | None = None
    first_signature: str | None = None
    # sahip cüzdan zenginleştirmesi (solana.enrich ile doldurulur)
    owner_created_at: int | None = None
    owner_tx_count: int = 0
    funder: str | None = None
    share: float = 0.0          # lansman alımı içindeki payı (%)
    token_account: str = ""     # signals uyumu için
    tag: str = "unknown"


@dataclass
class DeployerInfo:
    address: str | None = None
    prior_tokens: int = 0
    checked_tokens: int = 0      # kaç önceki tokenın piyasası kontrol edildi
    dead_tokens: int = 0         # likidite ~0 / çift yok = ölmüş/rug
    checked: bool = False

    @property
    def dead_rate(self) -> float:
        return self.dead_tokens / self.checked_tokens if self.checked_tokens else 0.0


@dataclass
class LaunchSnapshot:
    available: bool = False
    source: str = ""            # "bonding_curve" | "pair"
    buyers: list[LaunchBuyer] = field(default_factory=list)
    launch_slot: int | None = None
    reached_start: bool = False
    note: str = ""             # "budget" = bütçe doldu, başlangıç görülemedi
    # 2-hop fonlama ağacı: grandfunder -> beslediği direkt fonlayıcılar
    funding_tree: dict = field(default_factory=dict)


async def _pages_to_oldest(
    pool: RpcPool, address: str, max_pages: int
) -> tuple[list[list[dict]], bool]:
    pages: list[list[dict]] = []
    before: str | None = None
    for _ in range(max_pages):
        params: list = [address, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        try:
            page = await pool.call("getSignaturesForAddress", params)
        except Exception as exc:  # noqa: BLE001
            log.debug("lansman imza sayfası düştü %s: %s", address, exc)
            break
        if not page:
            return pages, True
        pages.append(page)
        if len(page) < 1000:
            return pages, True
        before = page[-1]["signature"]
    return pages, False


def _token_gains(tx: dict, mint: str) -> list[tuple[str, int]]:
    """İşlemde bu mint'ten bakiyesi ARTAN (owner, delta) çiftleri."""
    meta = tx.get("meta") or {}
    pre = {
        b["accountIndex"]: b
        for b in (meta.get("preTokenBalances") or [])
        if b.get("mint") == mint
    }
    post = {
        b["accountIndex"]: b
        for b in (meta.get("postTokenBalances") or [])
        if b.get("mint") == mint
    }
    out: list[tuple[str, int]] = []
    for idx in set(pre) | set(post):
        rec = post.get(idx) or pre.get(idx)
        owner = rec.get("owner")
        if not owner:
            continue
        pa = int((pre.get(idx) or {}).get("uiTokenAmount", {}).get("amount", 0) or 0)
        qa = int((post.get(idx) or {}).get("uiTokenAmount", {}).get("amount", 0) or 0)
        out.append((owner, qa - pa))
    return out


async def collect_launch_snapshot(
    pool: RpcPool, mint: str, anchor: str, source: str
) -> LaunchSnapshot:
    snap = LaunchSnapshot(source=source)
    pages, reached = await _pages_to_oldest(pool, anchor, LAUNCH_MAX_PAGES)
    snap.reached_start = reached
    if not pages:
        return snap
    if not reached:
        # Bütçe doldu ama sayfa hâlâ doluydu — gerçek başlangıcı görmedik.
        snap.note = "budget"
        return snap

    # En eski LAUNCH_MAX_TX imza (eskiden yeniye).
    oldest: list[dict] = []
    for page in reversed(pages):
        for sig in reversed(page):
            oldest.append(sig)
            if len(oldest) >= LAUNCH_MAX_TX:
                break
        if len(oldest) >= LAUNCH_MAX_TX:
            break

    txs = await pool.batch(
        [
            (
                "getTransaction",
                [
                    s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            )
            for s in oldest
        ],
        concurrency=8,
    )

    ignore = {anchor, mint, registry.SYSTEM_PROGRAM}
    buyers: dict[str, LaunchBuyer] = {}
    for sig, tx in zip(oldest, txs):
        if not tx:
            continue
        for owner, delta in _token_gains(tx, mint):
            if delta <= 0 or owner in buyers or owner in ignore:
                continue
            if registry.is_infrastructure(owner):
                continue
            buyers[owner] = LaunchBuyer(
                owner=owner,
                amount_raw=delta,
                first_slot=sig.get("slot"),
                first_block_time=sig.get("blockTime"),
                entry_fee=(tx.get("meta") or {}).get("fee"),
                first_signature=sig.get("signature"),
            )
            if len(buyers) >= LAUNCH_MAX_BUYERS:
                break
        if len(buyers) >= LAUNCH_MAX_BUYERS:
            break

    if len(buyers) < 4:
        return snap

    snap.buyers = list(buyers.values())
    snap.launch_slot = min(
        (b.first_slot for b in snap.buyers if b.first_slot), default=None
    )
    total = sum(b.amount_raw for b in snap.buyers) or 1
    for b in snap.buyers:
        b.share = b.amount_raw / total * 100
    snap.available = True
    return snap


async def enrich_launch_buyers(
    pool: RpcPool, buyers: list[LaunchBuyer], concurrency: int = 6
) -> None:
    """Her lansman alıcısının cüzdan yaşı ve ilk fonlayıcısı."""
    sem = asyncio.Semaphore(concurrency)

    async def one(b: LaunchBuyer) -> None:
        async with sem:
            oldest, count, reached = await solana._oldest_signature(
                pool, b.owner, max_pages=2
            )
            b.owner_tx_count = count
            if not oldest or not reached:
                return
            b.owner_created_at = oldest.get("blockTime")
            sig = oldest.get("signature")
            if sig:
                b.funder = await solana._find_funder(pool, sig, b.owner)

    # En büyük alıcılardan başla; bütçe LAUNCH_ENRICH_MAX ile sınırlı.
    ordered = sorted(buyers, key=lambda x: x.amount_raw, reverse=True)
    await asyncio.gather(*(one(b) for b in ordered[:LAUNCH_ENRICH_MAX]))


async def build_funding_tree(
    pool: RpcPool, buyers: list[LaunchBuyer], max_funders: int = 12
) -> dict:
    """2. hop: her direkt fonlayıcıyı KİM fonladı? Farklı direkt fonlayıcılar
    tek bir 'grandfunder'a çıkıyorsa, cüzdanlar farklı görünse bile koordinasyon
    vardır."""
    funders: dict[str, list[str]] = {}
    for b in buyers:
        if b.funder and not registry.is_infrastructure(b.funder):
            funders.setdefault(b.funder, []).append(b.owner)
    picks = list(funders)[:max_funders]
    if not picks:
        return {}

    sem = asyncio.Semaphore(6)
    grand: dict[str, str] = {}

    async def one(f: str) -> None:
        async with sem:
            oldest, _, reached = await solana._oldest_signature(pool, f, max_pages=2)
            if oldest and reached:
                sig = oldest.get("signature")
                if sig:
                    gf = await solana._find_funder(pool, sig, f)
                    if gf and not registry.is_infrastructure(gf):
                        grand[f] = gf

    await asyncio.gather(*(one(f) for f in picks))

    tree: dict[str, list[str]] = {}
    for f, gf in grand.items():
        tree.setdefault(gf, []).append(f)
    return {
        "grandfunders": {gf: fs for gf, fs in tree.items() if len(fs) >= 1},
        "funder_buyers": funders,
    }


async def analyze_deployer(
    pool: RpcPool, creator: str, mint: str
) -> DeployerInfo:
    """Deployer'ın geçmiş token sayısı (Helius DAS varsa)."""
    info = DeployerInfo(address=creator)
    if not pool.has_das:
        return info
    try:
        res = await pool.das(
            "getAssetsByCreator",
            {
                "creatorAddress": creator,
                "onlyVerified": False,
                "page": 1,
                "limit": 1000,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.info("deployer DAS sorgusu düştü %s: %s", creator, exc)
        return info
    items = (res or {}).get("items") or []
    ids = {it.get("id") for it in items if it.get("id")}
    ids.discard(mint)
    info.prior_tokens = len(ids)
    info.checked = True

    # Geçmiş tokenların kaçı ölmüş/rug? DexScreener (anahtarsız) ile bak.
    from .market import fetch_market

    sample = list(ids)[:12]
    if sample:
        sem = asyncio.Semaphore(6)

        async def check(tid: str) -> bool:
            async with sem:
                try:
                    m = await fetch_market(tid, timeout=8.0)
                except Exception:  # noqa: BLE001
                    return False
                # çift yok ya da likidite $1k altı = pratikte ölü
                return not m.available or (m.liquidity_usd or 0) < 1000

        results = await asyncio.gather(*(check(t) for t in sample))
        info.checked_tokens = len(results)
        info.dead_tokens = sum(1 for r in results if r)
    return info
