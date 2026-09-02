"""
Çok sağlayıcılı Solana RPC havuzu.

Ücretsiz katmanlarda hayatta kalmanın tek yolu: birden fazla sağlayıcıyı
havuzla, her birine kendi token-bucket rate limiter'ını tak, kredisi/kotası
biteni geçici olarak devre dışı bırak.

Sağlayıcılar RPC_ENDPOINTS ortam değişkeninden okunur:
    RPC_ENDPOINTS="https://mainnet.helius-rpc.com/?api-key=XXX|10,https://solana-mainnet.g.alchemy.com/v2/YYY|25"
Format: <url>|<saniyedeki_istek_limiti>  (virgülle ayrılmış)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

PUBLIC_FALLBACK = "https://api.mainnet-beta.solana.com|2"


class RpcError(RuntimeError):
    """RPC katmanından dönen hata (tüm sağlayıcılar tükendiğinde de atılır)."""


@dataclass
class _Bucket:
    """Basit token-bucket. Saniyedeki istek sayısını sınırlar."""

    rate: float
    capacity: float
    tokens: float = field(init=False)
    updated: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.rate
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


@dataclass
class Provider:
    url: str
    rps: float
    bucket: _Bucket = field(init=False)
    cooldown_until: float = 0.0
    calls: int = 0
    failures: int = 0

    def __post_init__(self) -> None:
        self.bucket = _Bucket(rate=self.rps, capacity=max(1.0, self.rps))

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def penalise(self, seconds: float = 30.0) -> None:
        self.cooldown_until = time.monotonic() + seconds
        self.failures += 1

    @property
    def label(self) -> str:
        host = self.url.split("//")[-1].split("/")[0]
        return host


class RpcPool:
    """Round-robin RPC havuzu. Her çağrı sırayla farklı bir sağlayıcıya gider."""

    def __init__(self, endpoints: str | None = None, timeout: float = 30.0) -> None:
        raw = endpoints or os.getenv("RPC_ENDPOINTS") or PUBLIC_FALLBACK
        self.providers: list[Provider] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            url, _, rps = chunk.partition("|")
            self.providers.append(Provider(url=url.strip(), rps=float(rps or 5)))
        if not self.providers:
            raise RpcError("Hiç RPC sağlayıcı tanımlanmadı (RPC_ENDPOINTS boş).")
        self._cycle = itertools.cycle(range(len(self.providers)))
        self._client = httpx.AsyncClient(
            timeout=timeout, limits=httpx.Limits(max_connections=32)
        )
        self._id = itertools.count(1)
        # Helius DAS (getAsset, getAssetsByCreator) yalnızca Helius uçlarında var.
        self._das = [p for p in self.providers if "helius" in p.url.lower()]

    @property
    def has_das(self) -> bool:
        return bool(self._das)

    async def das(self, method: str, params: Any) -> Any:
        """DAS çağrısı — yalnızca Helius sağlayıcısına gider (named params)."""
        if not self._das:
            raise RpcError("DAS için Helius uç noktası yok.")
        provider = min(self._das, key=lambda p: p.cooldown_until)
        await provider.bucket.acquire()
        provider.calls += 1
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._id),
            "method": method,
            "params": params,
        }
        try:
            resp = await self._client.post(provider.url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            provider.penalise(20.0)
            raise RpcError(f"DAS {method}: {exc}") from exc
        if "error" in body:
            raise RpcError(f"DAS {method}: {body['error']}")
        return body.get("result")

    async def aclose(self) -> None:
        await self._client.aclose()

    def _pick(self) -> Provider:
        for _ in range(len(self.providers)):
            p = self.providers[next(self._cycle)]
            if p.available:
                return p
        # Hepsi cooldown'da: en erken açılanı bekle.
        return min(self.providers, key=lambda p: p.cooldown_until)

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Tek bir JSON-RPC çağrısı. Hata durumunda diğer sağlayıcılara düşer."""
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._id),
            "method": method,
            "params": params or [],
        }
        last_error: Exception | None = None

        for attempt in range(len(self.providers) * 2):
            provider = self._pick()
            wait = provider.cooldown_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
            await provider.bucket.acquire()
            provider.calls += 1
            try:
                resp = await self._client.post(provider.url, json=payload)
                if resp.status_code == 429:
                    provider.penalise(15.0)
                    last_error = RpcError(f"{provider.label}: 429 rate limit")
                    continue
                if resp.status_code in (401, 402, 403):
                    # Kredi bitti veya anahtar geçersiz — uzun cooldown.
                    provider.penalise(900.0)
                    last_error = RpcError(f"{provider.label}: {resp.status_code}")
                    continue
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                provider.penalise(20.0)
                last_error = exc
                continue

            if "error" in body:
                err = body["error"]
                code = err.get("code")
                # -32005 = node behind / rate limited, -32603 = internal
                if code in (-32005, -32603, 429):
                    provider.penalise(15.0)
                    last_error = RpcError(f"{provider.label}: {err}")
                    continue
                raise RpcError(f"{method} başarısız: {err}")
            return body.get("result")

        raise RpcError(f"{method}: tüm sağlayıcılar tükendi ({last_error})")

    async def batch(
        self, calls: list[tuple[str, list[Any]]], concurrency: int = 8
    ) -> list[Any]:
        """Birden çok çağrıyı sınırlı eşzamanlılıkla çalıştırır.

        Hata veren çağrılar için None döner — tek bir cüzdanın verisi
        alınamadı diye tüm tarama çökmemeli.
        """
        sem = asyncio.Semaphore(concurrency)

        async def one(method: str, params: list[Any]) -> Any:
            async with sem:
                try:
                    return await self.call(method, params)
                except Exception as exc:  # noqa: BLE001
                    log.warning("RPC çağrısı düştü: %s %s", method, exc)
                    return None

        return await asyncio.gather(*(one(m, p) for m, p in calls))

    def stats(self) -> dict[str, dict[str, int | bool]]:
        return {
            p.label: {
                "calls": p.calls,
                "failures": p.failures,
                "available": p.available,
            }
            for p in self.providers
        }
