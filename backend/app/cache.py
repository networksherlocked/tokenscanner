"""
Tarama cache'i.

Ücretsiz katmanda hayatta kalmanın ikinci kuralı: aynı tokenı iki kez tarama.
Popüler bir token günde yüzlerce kez sorgulanır; hepsini zincire gitmeden
karşılamak kredi faturasını 50 kat düşürür.

SQLite ile başlıyoruz — tek dosya, sıfır kurulum. Trafik büyüyünce aynı
arayüzü Postgres/Redis'e taşımak kolay.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DEFAULT_TTL = 900  # 15 dakika

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    mint        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    verdict     TEXT,
    score       INTEGER,
    confidence  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);

CREATE TABLE IF NOT EXISTS scan_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mint        TEXT NOT NULL,
    verdict     TEXT,
    score       INTEGER,
    confidence  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_mint ON scan_history(mint, created_at DESC);
"""


class ScanCache:
    def __init__(self, path: str = "scans.db", ttl: int = DEFAULT_TTL) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get(self, mint: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload, created_at FROM scans WHERE mint = ?", (mint,)
        ).fetchone()
        if not row:
            return None
        if time.time() - row["created_at"] > self.ttl:
            return None
        payload = json.loads(row["payload"])
        payload["cached"] = True
        payload["cache_age_s"] = int(time.time() - row["created_at"])
        return payload

    def put(self, mint: str, payload: dict) -> None:
        now = int(time.time())
        verdict = payload.get("verdict", {})
        blob = json.dumps(payload, ensure_ascii=False)
        with self.conn:
            self.conn.execute(
                "INSERT INTO scans (mint, payload, verdict, score, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mint) DO UPDATE SET payload=excluded.payload, "
                "verdict=excluded.verdict, score=excluded.score, "
                "confidence=excluded.confidence, created_at=excluded.created_at",
                (
                    mint,
                    blob,
                    verdict.get("kind"),
                    verdict.get("score"),
                    verdict.get("confidence"),
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO scan_history (mint, verdict, score, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    mint,
                    verdict.get("kind"),
                    verdict.get("score"),
                    verdict.get("confidence"),
                    now,
                ),
            )

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT mint, payload, verdict, score, confidence, created_at "
            "FROM scans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            raw = d.pop("payload", None)
            token = {}
            try:
                token = (json.loads(raw) or {}).get("token") or {}
            except (TypeError, ValueError):
                pass
            d["symbol"] = token.get("symbol")
            d["name"] = token.get("name")
            out.append(d)
        return out

    def history(self, mint: str, limit: int = 20) -> list[dict]:
        """Aynı tokenın geçmiş kararları — verdict'in zamanla değiştiğini gösterir."""
        rows = self.conn.execute(
            "SELECT verdict, score, confidence, created_at FROM scan_history "
            "WHERE mint = ? ORDER BY created_at DESC LIMIT ?",
            (mint, limit),
        ).fetchall()
        return [dict(r) for r in rows]
