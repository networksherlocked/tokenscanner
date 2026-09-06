"""
Tarama cache'i + karne + itiraz kuyruğu.

İki arka uç:
  * DATABASE_URL yoksa  → SQLite (tek dosya, yerel geliştirme).
  * DATABASE_URL varsa  → Postgres (Supabase). Render'da servis yeniden
    başlasa / uykuya dalsa bile karne ve itirazlar korunur.

SQL tek yerde yazılır; Postgres için `?` → `%s` çevirisi yapılır. `ON CONFLICT`
her ikisinde de çalışır.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("solscope")

DEFAULT_TTL = 900  # 15 dakika

# AUTOINCREMENT dışında şema iki arka uçta aynı.
_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS scans (
    mint TEXT PRIMARY KEY, payload TEXT NOT NULL, verdict TEXT,
    score INTEGER, confidence INTEGER, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);

CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, verdict TEXT,
    score INTEGER, confidence INTEGER, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_mint ON scan_history(mint, created_at DESC);

CREATE TABLE IF NOT EXISTS track (
    mint TEXT PRIMARY KEY, symbol TEXT, verdict TEXT, score INTEGER,
    scored_at INTEGER NOT NULL, mcap_at_scan REAL, mcap_latest REAL,
    mcap_min REAL, latest_at INTEGER, outcome TEXT,
    settled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_track_scored ON track(scored_at DESC);

CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, verdict TEXT,
    contact TEXT, body TEXT NOT NULL, created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', note TEXT
);
CREATE INDEX IF NOT EXISTS idx_appeals_created ON appeals(created_at DESC);

CREATE TABLE IF NOT EXISTS flagged (
    address TEXT PRIMARY KEY, note TEXT, created_at INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1, via TEXT, kind TEXT NOT NULL DEFAULT 'wallet'
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, symbol TEXT,
    verdict_was TEXT, outcome TEXT, drop_pct REAL, scored_at INTEGER,
    learned_at INTEGER NOT NULL, wallets_flagged INTEGER NOT NULL DEFAULT 0,
    deployer TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_learned ON lessons(learned_at DESC);
"""

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS scans (
    mint TEXT PRIMARY KEY, payload TEXT NOT NULL, verdict TEXT,
    score INTEGER, confidence INTEGER, created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);

CREATE TABLE IF NOT EXISTS scan_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, mint TEXT NOT NULL,
    verdict TEXT, score INTEGER, confidence INTEGER, created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_mint ON scan_history(mint, created_at DESC);

CREATE TABLE IF NOT EXISTS track (
    mint TEXT PRIMARY KEY, symbol TEXT, verdict TEXT, score INTEGER,
    scored_at BIGINT NOT NULL, mcap_at_scan DOUBLE PRECISION,
    mcap_latest DOUBLE PRECISION, mcap_min DOUBLE PRECISION,
    latest_at BIGINT, outcome TEXT, settled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_track_scored ON track(scored_at DESC);

CREATE TABLE IF NOT EXISTS appeals (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, mint TEXT NOT NULL,
    verdict TEXT, contact TEXT, body TEXT NOT NULL, created_at BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', note TEXT
);
CREATE INDEX IF NOT EXISTS idx_appeals_created ON appeals(created_at DESC);

CREATE TABLE IF NOT EXISTS flagged (
    address TEXT PRIMARY KEY, note TEXT, created_at BIGINT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1, via TEXT, kind TEXT NOT NULL DEFAULT 'wallet'
);

CREATE TABLE IF NOT EXISTS lessons (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, mint TEXT NOT NULL,
    symbol TEXT, verdict_was TEXT, outcome TEXT, drop_pct DOUBLE PRECISION,
    scored_at BIGINT, learned_at BIGINT NOT NULL,
    wallets_flagged INTEGER NOT NULL DEFAULT 0, deployer TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_learned ON lessons(learned_at DESC);
"""


class ScanCache:
    def __init__(
        self, path: str = "scans.db", ttl: int = DEFAULT_TTL, dsn: str | None = None
    ) -> None:
        self.ttl = ttl
        self.pg = False
        if dsn:
            try:
                self._init_pg(dsn)
                self.pg = True
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "DATABASE_URL verildi ama Postgres'e bağlanılamadı (%s) — "
                    "SQLite'a düşülüyor. Bağlantı dizesini kontrol et "
                    "(Transaction pooler / port 6543 / ?sslmode=require).",
                    exc,
                )
        if not self.pg:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(_SCHEMA_SQLITE)
            self.conn.commit()
        self._migrate()

    def _init_pg(self, dsn: str) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=5,
            open=False,
            timeout=15,
            max_idle=300,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,  # transaction pooler uyumu
                "autocommit": True,
            },
        )
        self.pool.open(wait=True, timeout=15)
        with self.pool.connection() as c:
            c.execute(_SCHEMA_PG)

    def _migrate(self) -> None:
        # Eski kurulumlar için eklenen kolonlar (IF NOT EXISTS her yerde yok).
        for stmt in (
            "ALTER TABLE appeals ADD COLUMN note TEXT",
            "ALTER TABLE flagged ADD COLUMN hits INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE flagged ADD COLUMN via TEXT",
            "ALTER TABLE flagged ADD COLUMN kind TEXT NOT NULL DEFAULT 'wallet'",
        ):
            try:
                self._write(stmt)
            except Exception:  # noqa: BLE001  (kolon zaten var)
                pass

    def close(self) -> None:
        if self.pg:
            self.pool.close()
        else:
            self.conn.close()

    # ---- düşük seviye yardımcılar ---------------------------------------

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        if self.pg:
            sql = sql.replace("?", "%s")
            with self.pool.connection() as c, c.cursor() as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        r = self._rows(sql, params)
        return r[0] if r else None

    def _write(self, sql: str, params: tuple = ()) -> None:
        if self.pg:
            with self.pool.connection() as c, c.cursor() as cur:
                cur.execute(sql.replace("?", "%s"), params)
        else:
            with self.conn:
                self.conn.execute(sql, params)

    # ---- tarama önbelleği ---------------------------------------------

    def get(self, mint: str) -> dict | None:
        row = self._one(
            "SELECT payload, created_at FROM scans WHERE mint = ?", (mint,)
        )
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
        v = payload.get("verdict", {})
        blob = json.dumps(payload, ensure_ascii=False)
        self._write(
            "INSERT INTO scans (mint, payload, verdict, score, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(mint) DO UPDATE SET payload=excluded.payload, "
            "verdict=excluded.verdict, score=excluded.score, "
            "confidence=excluded.confidence, created_at=excluded.created_at",
            (mint, blob, v.get("kind"), v.get("score"), v.get("confidence"), now),
        )
        self._write(
            "INSERT INTO scan_history (mint, verdict, score, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mint, v.get("kind"), v.get("score"), v.get("confidence"), now),
        )

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self._rows(
            "SELECT mint, payload, verdict, score, confidence, created_at "
            "FROM scans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        out: list[dict] = []
        for d in rows:
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

    # ---- karne / outcome takibi -------------------------------------------

    def track_start(
        self, mint: str, symbol: str | None, verdict: str | None,
        score: int | None, mcap: float | None,
    ) -> None:
        now = int(time.time())
        self._write(
            "INSERT INTO track "
            "(mint, symbol, verdict, score, scored_at, mcap_at_scan, "
            " mcap_latest, mcap_min, latest_at, outcome, settled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0) "
            "ON CONFLICT(mint) DO UPDATE SET symbol=excluded.symbol, "
            "verdict=excluded.verdict, score=excluded.score, "
            "scored_at=excluded.scored_at, mcap_at_scan=excluded.mcap_at_scan, "
            "mcap_latest=excluded.mcap_latest, mcap_min=excluded.mcap_min, "
            "latest_at=excluded.latest_at, outcome=NULL, settled=0",
            (mint, symbol, verdict, score, now, mcap, mcap, mcap, now),
        )

    def track_pending(self, max_age: int) -> list[dict]:
        cutoff = int(time.time()) - max_age
        return self._rows(
            "SELECT * FROM track WHERE settled = 0 AND scored_at >= ? "
            "ORDER BY scored_at ASC",
            (cutoff,),
        )

    def track_update(
        self, mint: str, mcap_latest: float, mcap_min: float, at: int
    ) -> None:
        self._write(
            "UPDATE track SET mcap_latest = ?, mcap_min = ?, latest_at = ? "
            "WHERE mint = ?",
            (mcap_latest, mcap_min, at, mint),
        )

    def track_settle(self, mint: str, outcome: str) -> None:
        self._write(
            "UPDATE track SET outcome = ?, settled = 1 WHERE mint = ?",
            (outcome, mint),
        )

    def track_list(self, limit: int = 20) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM track ORDER BY scored_at DESC LIMIT ?", (limit,)
        )
        now = int(time.time())
        for d in rows:
            base = d.get("mcap_at_scan")
            latest = d.get("mcap_latest")
            low = d.get("mcap_min")
            d["change_pct"] = (
                (latest - base) / base if base and latest is not None else None
            )
            d["drop_pct"] = (
                max(0.0, (base - low) / base) if base and low is not None else None
            )
            d["age_sec"] = now - d["scored_at"]
        return rows

    # ---- itiraz akışı ---------------------------------------------------

    def add_appeal(
        self, mint: str, verdict: str | None, contact: str | None, body: str
    ) -> int:
        params = (mint, verdict, (contact or "")[:200], body[:4000], int(time.time()))
        if self.pg:
            with self.pool.connection() as c, c.cursor() as cur:
                cur.execute(
                    "INSERT INTO appeals (mint, verdict, contact, body, created_at) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    params,
                )
                row = cur.fetchone()
                return int(row["id"]) if row else 0
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO appeals (mint, verdict, contact, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                params,
            )
            return int(cur.lastrowid)

    def appeals_today(self, contact: str | None, ip_key: str) -> int:
        since = int(time.time()) - 86_400
        row = self._one(
            "SELECT COUNT(*) AS c FROM appeals WHERE created_at >= ? AND "
            "(contact = ? OR contact = ?)",
            (since, contact or "\x00", ip_key),
        )
        return int(row["c"]) if row else 0

    # ---- admin ---------------------------------------------------------

    def appeals_list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            return self._rows(
                "SELECT * FROM appeals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        return self._rows(
            "SELECT * FROM appeals ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def appeal_set_status(self, appeal_id: int, status: str, note: str | None) -> None:
        self._write(
            "UPDATE appeals SET status = ?, note = ? WHERE id = ?",
            (status, (note or "")[:2000] or None, appeal_id),
        )

    def cache_delete(self, mint: str) -> None:
        self._write("DELETE FROM scans WHERE mint = ?", (mint,))

    def track_delete(self, mint: str) -> None:
        self._write("DELETE FROM track WHERE mint = ?", (mint,))

    def flagged_list(self) -> list[dict]:
        return self._rows(
            "SELECT address, note, created_at, hits, via, kind FROM flagged "
            "ORDER BY hits DESC, created_at DESC"
        )

    def flagged_add(
        self, address: str, note: str | None,
        via: str = "manual", kind: str = "wallet", bump: bool = False,
    ) -> None:
        """bump=True ise mevcut kayıtta hits +1 ve not güncellenir."""
        set_clause = (
            "note=excluded.note, hits=flagged.hits+1, via=excluded.via"
            if bump else "note=COALESCE(flagged.note, excluded.note)"
        )
        self._write(
            "INSERT INTO flagged (address, note, created_at, hits, via, kind) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            f"ON CONFLICT(address) DO UPDATE SET {set_clause}",
            (address, (note or "")[:500] or None, int(time.time()), via, kind),
        )

    def flagged_remove(self, address: str) -> None:
        self._write("DELETE FROM flagged WHERE address = ?", (address,))

    def flagged_is(self, address: str) -> bool:
        return self._one(
            "SELECT 1 AS x FROM flagged WHERE address = ?", (address,)
        ) is not None

    # ---- öğrenme / dersler --------------------------------------------

    def scan_payload(self, mint: str) -> dict | None:
        """TTL'e bakmadan ham kayıtlı tarama (öğrenme için)."""
        row = self._one("SELECT payload FROM scans WHERE mint = ?", (mint,))
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, ValueError):
            return None

    def add_lesson(self, **kw) -> int:
        cols = (
            "mint", "symbol", "verdict_was", "outcome", "drop_pct",
            "scored_at", "learned_at", "wallets_flagged", "deployer", "detail",
        )
        vals = tuple(kw.get(c) for c in cols[:-1]) + ((kw.get("detail") or "")[:2000],)
        ph = ", ".join("?" for _ in cols)
        if self.pg:
            with self.pool.connection() as c, c.cursor() as cur:
                cur.execute(
                    f"INSERT INTO lessons ({', '.join(cols)}) VALUES ({ph}) RETURNING id"
                    .replace("?", "%s"),
                    vals,
                )
                r = cur.fetchone()
                return int(r["id"]) if r else 0
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO lessons ({', '.join(cols)}) VALUES ({ph})", vals
            )
            return int(cur.lastrowid)

    def lessons_list(self, limit: int = 100) -> list[dict]:
        return self._rows(
            "SELECT * FROM lessons ORDER BY learned_at DESC LIMIT ?", (limit,)
        )

    def lesson_undo(self, mint: str) -> int:
        """Bir dersin işaretlediği (otomatik) cüzdanları geri al."""
        rows = self._rows(
            "SELECT address FROM flagged WHERE via = ?", (mint,)
        )
        self._write("DELETE FROM flagged WHERE via = ?", (mint,))
        self._write("DELETE FROM lessons WHERE mint = ?", (mint,))
        return len(rows)

    def stats(self) -> dict:
        def n(sql, params=()):
            r = self._one(sql, params)
            return int(next(iter(r.values()))) if r else 0

        settled = self._rows(
            "SELECT outcome FROM track WHERE settled = 1 AND outcome IS NOT NULL"
        )
        hits = sum(1 for r in settled if r["outcome"] in ("hit", "clear"))
        return {
            "scans_cached": n("SELECT COUNT(*) FROM scans"),
            "scans_total": n("SELECT COUNT(*) FROM scan_history"),
            "track_open": n("SELECT COUNT(*) FROM track WHERE settled = 0"),
            "track_settled": len(settled),
            "track_correct": hits,
            "track_miss": sum(1 for r in settled if r["outcome"] == "miss"),
            "appeals_open": n("SELECT COUNT(*) FROM appeals WHERE status = 'open'"),
            "appeals_total": n("SELECT COUNT(*) FROM appeals"),
            "flagged": n("SELECT COUNT(*) FROM flagged"),
            "lessons": n("SELECT COUNT(*) FROM lessons"),
            "backend": "postgres" if self.pg else "sqlite",
        }

    def history(self, mint: str, limit: int = 20) -> list[dict]:
        return self._rows(
            "SELECT verdict, score, confidence, created_at FROM scan_history "
            "WHERE mint = ? ORDER BY created_at DESC LIMIT ?",
            (mint, limit),
        )
