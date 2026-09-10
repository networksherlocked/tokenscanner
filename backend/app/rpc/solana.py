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
    is_contract: bool = False  # sahip hesabı System Program dışı (PDA/havuz/vesting)


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


async def classify_owner_accounts(pool: RpcPool, holders: list[HolderRecord]) -> None:
    """Sahip hesapların tipi: normal cüzdan (System Program) mı, yoksa bir
    program/PDA (AMM havuz kasası, vesting kontratı, çok-imza hazine) mi?

    Amaç: bir LP havuzu ya da vesting kilidi, "tek cüzdan baskınlığı" veya
    "top-10 yoğunlaşması" sinyallerini SAHTE tetiklemesin. Tek getMultipleAccounts.
    """
    owners = list({h.owner for h in holders if h.owner})
    if not owners:
        return
    prog_of: dict[str, str | None] = {}
    for i in range(0, len(owners), 100):
        chunk = owners[i : i + 100]
        try:
            res = await pool.call(
                "getMultipleAccounts", [chunk, {"encoding": "base64"}]
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("sahip hesap tipi çözülemedi: %s", exc)
            return
        for addr, acc in zip(chunk, (res or {}).get("value", [])):
            prog_of[addr] = (acc or {}).get("owner") if acc else None
    from ..engine import registry

    for h in holders:
        prog = prog_of.get(h.owner)
        # prog None: hesap yok / çözülemedi → cüzdan varsay (yanlış dışlama yapma).
        h.is_contract = bool(prog and prog != registry.SYSTEM_PROGRAM)


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


async def _resolve_wallet_meta(
    pool: RpcPool, records: list, cache=None, max_pages: int = 4, concurrency: int = 6
) -> None:
    """`records` (HolderRecord veya LaunchBuyer) için owner_created_at /
    owner_tx_count / funder doldurur. Değişmez veriyi `cache` (wallet_meta)
    üzerinden okur/yazar — tekrar taramada RPC harcanmaz, bu da aktif/köklü
    cüzdanlı organik lansmanları çözebilmemizi sağlar.
    """
    addrs = [getattr(r, "owner", None) for r in records if getattr(r, "owner", None)]
    hit: dict[str, dict] = {}
    if cache is not None and addrs:
        try:
            hit = cache.wallet_meta_get_many(addrs)
        except Exception as exc:  # noqa: BLE001
            log.debug("wallet_meta okuma düştü: %s", exc)

    sem = asyncio.Semaphore(concurrency)
    fresh: dict[str, dict] = {}

    async def one(r) -> None:
        owner = getattr(r, "owner", None)
        if not owner:
            return
        c = hit.get(owner)
        # reached=True → veri kesin, RPC atla. reached=False ama tx_count yüksek
        # → hâlâ aktif, tekrar denemenin faydası yok.
        if c and (c["reached"] or c["tx_count"] >= max_pages * 1000):
            r.owner_tx_count = c["tx_count"]
            r.owner_created_at = c["created_at"]
            r.funder = c["funder"]
            return
        async with sem:
            oldest, count, reached = await _oldest_signature(
                pool, owner, max_pages=max_pages
            )
        r.owner_tx_count = count
        rec = {"address": owner, "tx_count": count, "reached": reached,
               "created_at": None, "funder": None}
        if oldest and reached:
            r.owner_created_at = oldest.get("blockTime")
            rec["created_at"] = r.owner_created_at
            sig = oldest.get("signature")
            if sig:
                r.funder = await _find_funder(pool, sig, owner)
                rec["funder"] = r.funder
        fresh[owner] = rec

    await asyncio.gather(*(one(r) for r in records))

    if cache is not None and fresh:
        try:
            cache.wallet_meta_put_many(list(fresh.values()))
        except Exception as exc:  # noqa: BLE001
            log.debug("wallet_meta yazma düştü: %s", exc)


async def enrich_owners(
    pool: RpcPool, holders: list[HolderRecord], concurrency: int = 6, cache=None
) -> None:
    """Sahip cüzdanların yaşını ve ilk fonlayıcısını bulur."""
    await _resolve_wallet_meta(pool, holders, cache=cache, concurrency=concurrency)


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
            src = info.get("source")
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


async def resolve_mint_creator(pool: RpcPool, mint: str) -> str | None:
    """Tokeni basan cüzdanı platformdan bağımsız çözer: mint hesabının en eski
    (genesis) işleminin ücret ödeyeni = yaratıcı.

    pump.fun / Meteora / Raydium-native / Moonshot fark etmez. ~2 RPC çağrısı.
    """
    oldest, _, reached = await _oldest_signature(pool, mint, max_pages=2)
    if not oldest or not oldest.get("signature"):
        return None
    tx = await pool.call(
        "getTransaction",
        [
            oldest["signature"],
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    )
    if not tx:
        return None
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    if not keys:
        return None
    # accountKeys[0] = ücret ödeyen/imzalayan = pratikte yaratıcı.
    k0 = keys[0]
    if isinstance(k0, dict):
        return k0.get("pubkey")
    return k0 if isinstance(k0, str) else None


async def resolve_token_meta(pool: RpcPool, mint: str) -> dict:
    """Token adı / sembolü / logosu — Helius DAS `getAsset` ile (zincir-üstü
    Metaplex metadata). DexScreener Render IP'sinden bloklu ve GeckoTerminal
    ücretsiz kısıtı sık dolduğu için piyasa API'leri düşse bile ad/sembol
    gösterebilelim diye. DAS uç noktası yoksa boş döner (tek RPC çağrısı)."""
    if not getattr(pool, "has_das", False):
        return {}
    try:
        res = await pool.das("getAsset", {"id": mint})
    except Exception as exc:  # noqa: BLE001
        log.info("getAsset düştü %s: %s", mint, exc)
        return {}
    content = (res or {}).get("content") or {}
    meta = content.get("metadata") or {}
    out: dict = {}
    name = (meta.get("name") or "").strip()
    sym = (meta.get("symbol") or "").strip()
    if name:
        out["name"] = name
    if sym:
        out["symbol"] = sym
    img = (content.get("links") or {}).get("image")
    if isinstance(img, str) and img.startswith("http"):
        out["image"] = img
    return out


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
    pool: RpcPool, mint: str, deep: bool = True, cache=None
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
    await classify_owner_accounts(pool, holders)

    if deep:
        await asyncio.gather(
            enrich_token_accounts(pool, holders),
            enrich_owners(pool, holders, cache=cache),
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
