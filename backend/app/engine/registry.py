"""
Küratörlü adres kayıtları.

Bu dosya motorun en değerli parçası ve zamanla senin elinde büyümesi gereken
kısım. Üçüncü taraf "suçlama" listeleri değil, kendi on-chain bulgularından
türeyen listeler tut.

TIER anlamı:
    "major"    — büyük, KYC'li, köklü borsa. Buradan fonlanmak organiklik işareti.
    "regional" — bölgesel / düşük-KYC borsa. Tek başına kötü değil ama
                 tek kaynak baskınlığı varsa şüphe uyandırır.
    "low_trust"— mixer, anonim swap servisi, riskli köprü. Güçlü kırmızı bayrak.

NOT: Aşağıdaki adresler başlangıç tohumudur. Üretime almadan önce her birini
Solscan üzerinde doğrula ve kendi bulgularınla genişlet.
"""

from __future__ import annotations

# --- Borsa sıcak cüzdanları -------------------------------------------------

CEX_WALLETS: dict[str, tuple[str, str]] = {
    # adres: (borsa adı, tier)
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": ("Binance", "major"),
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": ("Binance", "major"),
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": ("Binance", "major"),
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": ("Coinbase", "major"),
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": ("Coinbase", "major"),
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": ("Kraken", "major"),
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": ("OKX", "major"),
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": ("Bybit", "regional"),
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": ("Gate.io", "regional"),
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": ("KuCoin", "regional"),
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": ("MEXC", "regional"),
}

# --- Protokol / altyapı adresleri -------------------------------------------

BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}

PROTOCOL_ACCOUNTS: dict[str, str] = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j": "Raydium AMM Authority V4",
    "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ": "Raydium",
    "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin": "Serum/OpenBook",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun Program",
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg": "Pump.fun Fee Account",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4jU": "Raydium Authority",
}

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
METAPLEX_METADATA = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# --- Kendi bulgularından büyüyecek listeler ---------------------------------

FLAGGED_WALLETS: dict[str, str] = {
    # "adres": "daha önce X tokenında bundle kümesinde görüldü (2026-08-11)"
}

KOL_WALLETS: dict[str, str] = {
    # "adres": "takma ad / bilinen influencer"
}


# --- Yardımcılar ------------------------------------------------------------


def classify_address(address: str) -> dict[str, str | None]:
    """Bir adresin bilinen kategorisini döndürür."""
    if address in BURN_ADDRESSES:
        return {"kind": "burn", "name": "Burn address", "tier": None}
    if address in CEX_WALLETS:
        name, tier = CEX_WALLETS[address]
        return {"kind": "cex", "name": name, "tier": tier}
    if address in PROTOCOL_ACCOUNTS:
        return {"kind": "protocol", "name": PROTOCOL_ACCOUNTS[address], "tier": None}
    if address in FLAGGED_WALLETS:
        return {"kind": "flagged", "name": FLAGGED_WALLETS[address], "tier": None}
    if address in KOL_WALLETS:
        return {"kind": "kol", "name": KOL_WALLETS[address], "tier": None}
    return {"kind": "unknown", "name": None, "tier": None}


def is_infrastructure(address: str) -> bool:
    """Holder yüzdesi hesaplarken dışlanması gereken adresler."""
    return (
        address in BURN_ADDRESSES
        or address in CEX_WALLETS
        or address in PROTOCOL_ACCOUNTS
        or address == SYSTEM_PROGRAM
    )
