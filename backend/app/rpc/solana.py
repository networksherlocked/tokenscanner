"""
Solana üzerinden ham veri toplama.

Buradaki her fonksiyon kredi maliyetine göre tasarlandı. `getProgramAccounts`
bilerek kullanılmıyor: Helius'ta 10 kredi ve sınırsız tarama demek. Bunun
yerine `getTokenLargestAccounts` (top 20) + hedefli sorgular yapıyoruz.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .pool import RpcPool

log = logging.getLogger(__name__)

LAMPORTS = 1_000_000_000


@dataclass
class HolderRecord:
    """Tek bir top-holder hakkında topladığımız her şey."""

    token_account: str
    owner: str | None = None
    amount_raw: int = 0
    ui_amount: float = 0.0
    share: float = 0.0  # dolaşımdaki arzın yüzdesi

    # Token hesabının ilk hareketi = bu cüzdanın tokena ilk girişi
    first_slot: int | None = None
    first_block_time: int | None = None
    first_signature: str | None = None
    entry_fee: int | None = None  # lamports, priority fee parmak izi için
    token_tx_count: int = 0

    # Sahip cüzdanın kendi geçmişi
    owner_created_at: int | None = None  # unix ts
    owner_tx_count: int = 0
    funder: str | None = None  # ilk SOL'u kimden aldı

    tag: str = "unknown"  # registry sınıflandırması


@dataclass
class MintInfo:
    mint: str
    decimals: int = 0
    supply_raw: int = 0
    mint_authority: str | None = None
    freeze_authority: str | None = None
    program: str | None = None

    @property
    def supply(self) -> float:
        return self.supply_raw / (10**self.decimals) if self.decimals else self.supply_raw


@dataclass
class ChainSnapshot:
    mint_info: MintInfo
    holders: list[HolderRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    coverage: float = 1.0  # veri tamlığı 0..1


async def get_mint_info(pool: RpcPool, mint: str) -> MintInfo:
    res = await pool.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not res or not res.get("value"):
        raise ValueError(f"Mint bulunamadı: {mint}")
    value = res["value"]
    parsed = value.get("data", {}).get("parsed", {})
    info = parsed.get("info", {})
    return MintInfo(
        mint=mint,
        decimals=int(info.get("decimals", 0)),
        supply_raw=int(info.get("supply", 0)),
        mint_authority=info.get("mintAuthority"),
        freeze_authority=info.get("freezeAuthority"),
        program=value.get("owner"),
    )


async def get_top_holders(pool: RpcPool, mint: str, decimals: int) -> list[HolderRecord]:
    """En büyük 20 token hesabı. Tek çağrı, ucuz."""
    res = await pool.call("getTokenLargestAccounts", [mint])
    out: list[HolderRecord] = []
    for row in (res or {}).get("value", []):
        raw = int(row.get("amount", 0))
        if raw <= 0:
            continue
        out.append(
            HolderRecord(
                token_account=row["address"],
                amount_raw=raw,
                ui_amount=raw / (10**decimals) if decimals else raw,
            )
        )
    return out


async def resolve_owners(pool: RpcPool, holders: list[HolderRecord]) -> None:
    """Token hesaplarının sahiplerini tek getMultipleAccounts çağrısıyla çözer."""
    if not holders:
        return
    for i in range(0, len(holders), 100):
        chunk = holders[i : i + 100]
        res = await pool.call(
            "getMultipleAccounts",
            [[h.token_account for h in chunk], {"encoding": "jsonParsed"}],
        )
        for holder, acc in zip(chunk, (res or {}).get("value", [])):
            if not acc:
                continue
            info = acc.get("data", {}).get("parsed", {}).get("info", {})
            holder.owner = info.get("owner")


async def _oldest_signature(
    pool: RpcPool, address: str, max_pages: int = 3
) -> tuple[dict | None, int, bool]:
    """Bir adresin en eski imzasını, yaklaşık işlem sayısını ve gerçek başlangıca
    ulaşıp ulaşmadığımızı döndürür.

    Sayfa başına 1000 imza. max_pages ile maliyeti sınırlıyoruz. Bütçe dolduğu
    hâlde sayfa hâlâ doluysa `reached_start=False` — yani elimizdeki "en eski"
    imza aslında cüzdanın ilk işlemi DEĞİL, sadece görebildiğimiz kadar geri.
    Bu durumda çağıran taraf yaş/fonlayıcı çıkarımı yapmamalı (yoksa aktif bir
    cüzdan "yeni doğmuş" gibi görünür ve motoru yanıltır).
    """
    before: str | None = None
    oldest: dict | None = None
    total = 0
    reached_start = False
    for _ in range(max_pages):
        params: list = [address, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        try:
            page = await pool.call("getSignaturesForAddress", params)
        except Exception as exc:  # noqa: BLE001
            log.debug("imza sayfası alınamadı %s: %s", address, exc)
            break
        if not page:
            reached_start = True
            break
        total += len(page)
        oldest = page[-1]
        if len(page) < 1000:
            reached_start = True
            break
        before = page[-1]["signature"]
    return oldest, total, reached_start


async def enrich_token_accounts(
    pool: RpcPool, holders: list[HolderRecord], concurrency: int = 6
) -> None:
    """Her token hesabının ilk hareketini (= tokena giriş anı) bulur."""
    sem = asyncio.Semaphore(concurrency)

    async def one(h: HolderRecord) -> None:
        async with sem:
            oldest, count, _ = await _oldest_signature(
                pool, h.token_account, max_pages=2
            )
            h.token_tx_count = count
            if oldest:
                h.first_slot = oldest.get("slot")
                h.first_block_time = oldest.get("blockTime")
                h.first_signature = oldest.get("signature")

    await asyncio.gather(*(one(h) for h in holders))


async def enrich_owners(
    pool: RpcPool, holders: list[HolderRecord], concurrency: int = 6
) -> None:
    """Sahip cüzdanların yaşını ve ilk fonlayıcısını bulur."""
    sem = asyncio.Semaphore(concurrency)

    async def one(h: HolderRecord) -> None:
        if not h.owner:
            return
        async with sem:
            oldest, count, reached_start = await _oldest_signature(
                pool, h.owner, max_pages=3
            )
            h.owner_tx_count = count
            if not oldest:
                return
            if not reached_start:
                # Bütçe içinde cüzdanın başına ulaşamadık — bu "en eski" imza
                # yanıltıcı derecede yeni. Yaş ve fonlayıcı çıkarımını atla;
                # owner_tx_count (>= 3000) tek başına "taze değil" bilgisini verir.
                return
            h.owner_created_at = oldest.get("blockTime")
            sig = oldest.get("signature")
            if sig:
                h.funder = await _find_funder(pool, sig, h.owner)

    await asyncio.gather(*(one(h) for h in holders))


async def _find_funder(pool: RpcPool, signature: str, owner: str) -> str | None:
    """Cüzdanın ilk işleminde ona SOL gönderen adresi bulur."""
    tx = await pool.call(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx:
        return None
    message = tx.get("transaction", {}).get("message", {})
    for ix in message.get("instructions", []):
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") in ("transfer", "createAccount", "transferChecked"):
            info = parsed.get("info", {})
            dest = info.get("destination") or info.get("newAccount")
            src = info.get("source") or info.get("lamports") and info.get("source")
            if dest == owner and src and src != owner:
                return src

    # Parse edilmiş talimat bulunamadıysa bakiye değişiminden çıkar:
    # bakiyesi artan hesap owner ise, en çok azalan hesap fonlayıcıdır.
    meta = tx.get("meta") or {}
    keys = [
        k["pubkey"] if isinstance(k, dict) else k
        for k in message.get("accountKeys", [])
    ]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if len(keys) != len(pre) or len(pre) != len(post):
        return None
    deltas = {k: post[i] - pre[i] for i, k in enumerate(keys)}
    if deltas.get(owner, 0) <= 0:
        return None
    candidates = [(v, k) for k, v in deltas.items() if v < 0 and k != owner]
    if not candidates:
        return None
    return min(candidates)[1]


async def fetch_entry_fees(
    pool: RpcPool, holders: list[HolderRecord], concurrency: int = 6
) -> None:
    """İlk alım işlemlerinin ücretlerini çeker — priority fee parmak izi için."""
    sem = asyncio.Semaphore(concurrency)

    async def one(h: HolderRecord) -> None:
        if not h.first_signature:
            return
        async with sem:
            tx = await pool.call(
                "getTransaction",
                [
                    h.first_signature,
                    {"encoding": "json", "maxSupportedTransactionVersion": 0},
                ],
            )
            if tx and tx.get("meta"):
                h.entry_fee = tx["meta"].get("fee")

    await asyncio.gather(*(one(h) for h in holders))


async def collect_chain_snapshot(
    pool: RpcPool, mint: str, deep: bool = True
) -> ChainSnapshot:
    """Zincir anlık görüntüsü.

    deep=True  : eski davranış — her holder için yaş + fonlayıcı + ücret (~125 çağrı).
    deep=False : yalnızca sahip + bakiye (~3 çağrı). Bundle sinyalleri lansman
                 alıcılarından geldiğinde mevcut holder'lar sadece yoğunlaşma /
                 balina / likidite için lazım.
    """
    mint_info = await get_mint_info(pool, mint)
    holders = await get_top_holders(pool, mint, mint_info.decimals)

    snapshot = ChainSnapshot(mint_info=mint_info, holders=holders)
    if not holders:
        snapshot.errors.append("Hiç holder bulunamadı.")
        snapshot.coverage = 0.0
        return snapshot

    await resolve_owners(pool, holders)

    if deep:
        await asyncio.gather(
            enrich_token_accounts(pool, holders),
            enrich_owners(pool, holders),
        )
        await fetch_entry_fees(pool, holders)

    total = mint_info.supply_raw or sum(h.amount_raw for h in holders)
    for h in holders:
        h.share = (h.amount_raw / total * 100) if total else 0.0

    if deep:
        fields_ok = sum(
            bool(h.owner) + bool(h.owner_created_at) + bool(h.funder)
            for h in holders
        )
        snapshot.coverage = round(fields_ok / (len(holders) * 3), 3)
    else:
        resolved = sum(1 for h in holders if h.owner)
        snapshot.coverage = round(resolved / len(holders), 3)
    return snapshot
