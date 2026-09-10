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
    settled INTEGER NOT NULL DEFAULT 0, image TEXT,
    mcap_max REAL, gain_outcome TEXT, gain_settled INTEGER NOT NULL DEFAULT 0,
    liq_at_scan REAL, liq_min REAL, creator TEXT,
    rug_flagged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_track_scored ON track(scored_at DESC);

-- Yükseliş öğrenmesi: "organic/cabaled/inconclusive" denip sonradan sert
-- YÜKSELEN tokenlarda tekrar eden erken cüzdan / fonlayıcı / deployer.
CREATE TABLE IF NOT EXISTS gainers (
    address TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'wallet', note TEXT,
    hits INTEGER NOT NULL DEFAULT 1, first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL, via TEXT
);
CREATE TABLE IF NOT EXISTS gain_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, symbol TEXT,
    verdict_was TEXT, rise_pct REAL, mcap_at_scan REAL, mcap_peak REAL,
    scored_at INTEGER, learned_at INTEGER NOT NULL,
    markers INTEGER NOT NULL DEFAULT 0, deployer TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_gain_lessons_learned ON gain_lessons(learned_at DESC);
CREATE TABLE IF NOT EXISTS gain_hits (
    mint TEXT PRIMARY KEY, symbol TEXT, verdict TEXT, score INTEGER,
    detail TEXT, scanned_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gain_hits_at ON gain_hits(scanned_at DESC);

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

CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT);

-- Değişmez lansman verisi: bir tokenın ilk alıcıları ve pump.fun meta'sı
-- asla değişmez. İlk başarılı taramada saklanır, sonraki taramalarda canlı
-- zincir/pump çağrısı düşerse buradan geri yüklenir (karar kararlı kalsın).
CREATE TABLE IF NOT EXISTS launch_cache (
    mint TEXT PRIMARY KEY, pump TEXT, launch TEXT, saved_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS x_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, tweet_id TEXT,
    verdict TEXT, ok INTEGER NOT NULL DEFAULT 1, detail TEXT,
    posted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xposts_posted ON x_posts(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_xposts_mint ON x_posts(mint, posted_at DESC);

-- Cüzdan yaşı / ilk fonlayıcısı DEĞİŞMEZ. Bir kez çözünce sakla; sonraki
-- taramalarda RPC harcama. Bu, taze cüzdanlı paketler kadar aktif/köklü
-- cüzdanlı organik lansmanları da çözebilmemizi sağlar (asıl darboğaz buydu).
CREATE TABLE IF NOT EXISTS wallet_meta (
    address TEXT PRIMARY KEY, created_at INTEGER, funder TEXT,
    tx_count INTEGER NOT NULL DEFAULT 0, reached INTEGER NOT NULL DEFAULT 0,
    resolved_at INTEGER NOT NULL
);
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
    latest_at BIGINT, outcome TEXT, settled INTEGER NOT NULL DEFAULT 0,
    image TEXT, mcap_max DOUBLE PRECISION, gain_outcome TEXT,
    gain_settled INTEGER NOT NULL DEFAULT 0,
    liq_at_scan DOUBLE PRECISION, liq_min DOUBLE PRECISION, creator TEXT,
    rug_flagged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_track_scored ON track(scored_at DESC);

CREATE TABLE IF NOT EXISTS gainers (
    address TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'wallet', note TEXT,
    hits INTEGER NOT NULL DEFAULT 1, first_seen BIGINT NOT NULL,
    last_seen BIGINT NOT NULL, via TEXT
);
CREATE TABLE IF NOT EXISTS gain_lessons (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, mint TEXT NOT NULL,
    symbol TEXT, verdict_was TEXT, rise_pct DOUBLE PRECISION,
    mcap_at_scan DOUBLE PRECISION, mcap_peak DOUBLE PRECISION,
    scored_at BIGINT, learned_at BIGINT NOT NULL,
    markers INTEGER NOT NULL DEFAULT 0, deployer TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_gain_lessons_learned ON gain_lessons(learned_at DESC);
CREATE TABLE IF NOT EXISTS gain_hits (
    mint TEXT PRIMARY KEY, symbol TEXT, verdict TEXT, score INTEGER,
    detail TEXT, scanned_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gain_hits_at ON gain_hits(scanned_at DESC);

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

CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS launch_cache (
    mint TEXT PRIMARY KEY, pump TEXT, launch TEXT, saved_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS x_posts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, mint TEXT NOT NULL,
    tweet_id TEXT, verdict TEXT, ok INTEGER NOT NULL DEFAULT 1, detail TEXT,
    posted_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xposts_posted ON x_posts(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_xposts_mint ON x_posts(mint, posted_at DESC);

CREATE TABLE IF NOT EXISTS wallet_meta (
    address TEXT PRIMARY KEY, created_at BIGINT, funder TEXT,
    tx_count INTEGER NOT NULL DEFAULT 0, reached INTEGER NOT NULL DEFAULT 0,
    resolved_at BIGINT NOT NULL
);
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
            "ALTER TABLE track ADD COLUMN image TEXT",
            "ALTER TABLE track ADD COLUMN mcap_max REAL",
            "ALTER TABLE track ADD COLUMN gain_outcome TEXT",
            "ALTER TABLE track ADD COLUMN gain_settled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE track ADD COLUMN liq_at_scan REAL",
            "ALTER TABLE track ADD COLUMN liq_min REAL",
            "ALTER TABLE track ADD COLUMN creator TEXT",
            "ALTER TABLE track ADD COLUMN rug_flagged INTEGER NOT NULL DEFAULT 0",
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
            d["market_cap"] = token.get("market_cap")
            d["image"] = token.get("image")
            out.append(d)
        return out

    # ---- karne / outcome takibi -------------------------------------------

    def track_start(
        self, mint: str, symbol: str | None, verdict: str | None,
        score: int | None, mcap: float | None, image: str | None = None,
        liquidity: float | None = None, creator: str | None = None,
    ) -> None:
        now = int(time.time())
        self._write(
            "INSERT INTO track "
            "(mint, symbol, verdict, score, scored_at, mcap_at_scan, "
            " mcap_latest, mcap_min, mcap_max, latest_at, outcome, settled, "
            " gain_outcome, gain_settled, image, liq_at_scan, liq_min, "
            " creator, rug_flagged) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, 0, ?, ?, ?, ?, 0) "
            "ON CONFLICT(mint) DO UPDATE SET symbol=excluded.symbol, "
            "verdict=excluded.verdict, score=excluded.score, "
            "scored_at=excluded.scored_at, mcap_at_scan=excluded.mcap_at_scan, "
            "mcap_latest=excluded.mcap_latest, mcap_min=excluded.mcap_min, "
            "mcap_max=excluded.mcap_max, latest_at=excluded.latest_at, "
            "outcome=NULL, settled=0, gain_outcome=NULL, gain_settled=0, "
            "image=COALESCE(excluded.image, track.image), "
            "liq_at_scan=excluded.liq_at_scan, liq_min=excluded.liq_min, "
            "creator=COALESCE(excluded.creator, track.creator), rug_flagged=0",
            (mint, symbol, verdict, score, now, mcap, mcap, mcap, mcap, now,
             image, liquidity, liquidity, creator),
        )

    def track_pending(self, max_age: int) -> list[dict]:
        """Karne (24s çöküş) VEYA yükseliş penceresi hâlâ açık olan kayıtlar."""
        cutoff = int(time.time()) - max_age
        return self._rows(
            "SELECT * FROM track WHERE (settled = 0 OR gain_settled = 0) "
            "AND scored_at >= ? ORDER BY scored_at ASC",
            (cutoff,),
        )

    def track_update(
        self,
        mint: str,
        mcap_latest: float,
        mcap_min: float,
        at: int,
        mcap_max: float | None = None,
        symbol: str | None = None,
        image: str | None = None,
        liq_min: float | None = None,
    ) -> None:
        # symbol/image yalnızca boşsa doldurulur (eski kayıtlarda sık sık NULL).
        # liq_min çağıran tarafından hesaplanır (Python min); None ise dokunma.
        # mcap_at_scan da aynı şekilde: taramada piyasa verisi çekilemediyse
        # (ör. DexScreener o an düşmüştü) NULL kalmış olabilir — "önceki değer"
        # sonsuza dek "—" göstermesin ve çöküş yüzdesi (base=None→%0 varsayımı)
        # yanlış "temiz" sonuca düşmesin diye ilk başarılı okumayla dolduruyoruz.
        # NOT: "CASE WHEN ? IS NULL" gibi çıplak parametreler Postgres'te tip
        # çıkarımı hatası verir (IndeterminateDatatype) — COALESCE kullan.
        self._write(
            "UPDATE track SET mcap_latest = ?, mcap_min = ?, latest_at = ?, "
            "mcap_max = ?, "
            "mcap_at_scan = COALESCE(mcap_at_scan, ?), "
            "liq_min = COALESCE(?, liq_min), "
            "symbol = COALESCE(NULLIF(symbol, ''), ?), "
            "image = COALESCE(NULLIF(image, ''), ?) WHERE mint = ?",
            (mcap_latest, mcap_min, at, mcap_max, mcap_latest, liq_min, symbol, image, mint),
        )

    def track_mark_rug(self, mint: str, outcome: str = "rug") -> None:
        """Likidite çekildi — kaydı sonuçlandır + tekrar işaretlenmesin."""
        self._write(
            "UPDATE track SET rug_flagged = 1, outcome = ?, settled = 1, "
            "gain_outcome = COALESCE(gain_outcome, 'n/a'), gain_settled = 1 "
            "WHERE mint = ?",
            (outcome, mint),
        )

    def track_settle(self, mint: str, outcome: str) -> None:
        self._write(
            "UPDATE track SET outcome = ?, settled = 1 WHERE mint = ?",
            (outcome, mint),
        )

    def track_settle_gain(self, mint: str, outcome: str) -> None:
        self._write(
            "UPDATE track SET gain_outcome = ?, gain_settled = 1 WHERE mint = ?",
            (outcome, mint),
        )

    def track_missing_symbol(self, limit: int = 25) -> list[str]:
        rows = self._rows(
            "SELECT mint FROM track WHERE symbol IS NULL OR symbol = '' "
            "ORDER BY scored_at DESC LIMIT ?",
            (limit,),
        )
        return [r["mint"] for r in rows]

    def track_missing_image(self, limit: int = 25) -> list[str]:
        rows = self._rows(
            "SELECT mint FROM track WHERE image IS NULL OR image = '' "
            "ORDER BY scored_at DESC LIMIT ?",
            (limit,),
        )
        return [r["mint"] for r in rows]

    def track_set_symbol(self, mint: str, symbol: str) -> None:
        self._write(
            "UPDATE track SET symbol = ? "
            "WHERE mint = ? AND (symbol IS NULL OR symbol = '')",
            (symbol, mint),
        )

    def track_set_image(self, mint: str, image: str) -> None:
        self._write(
            "UPDATE track SET image = ? "
            "WHERE mint = ? AND (image IS NULL OR image = '')",
            (image, mint),
        )

    def _track_enrich(self, rows: list[dict]) -> list[dict]:
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
            high = d.get("mcap_max")
            d["rise_pct"] = (
                max(0.0, (high - base) / base) if base and high is not None else None
            )
            d["age_sec"] = now - d["scored_at"]
            # Sembolü/logosu olmayan (eski) kayıtlar için kayıtlı taramadan doldur.
            if not d.get("symbol") or not d.get("image"):
                pl = self.scan_payload(d["mint"])
                tok = (pl or {}).get("token") or {}
                if tok:
                    d["symbol"] = d.get("symbol") or tok.get("symbol")
                    d["image"] = d.get("image") or tok.get("image")
        return rows

    def track_list(self, limit: int = 20) -> list[dict]:
        return self._track_enrich(self._rows(
            "SELECT * FROM track ORDER BY scored_at DESC LIMIT ?", (limit,)
        ))

    def organic_list(self, limit: int = 30) -> list[dict]:
        """Organic kararı verilmiş ve sonradan çökmemiş tokenlar.

        Ayrı ve daha uzun bir liste: koordineli dağıtım izi bulunmayan
        lansmanlar AI-tespit akışından daha yavaş düşsün diye tutulur.
        """
        return self._track_enrich(self._rows(
            "SELECT * FROM track WHERE verdict = 'organic' "
            "AND (outcome IS NULL OR outcome <> 'miss') "
            "ORDER BY scored_at DESC LIMIT ?", (limit,)
        ))

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

    # ---- config (k/v) -------------------------------------------------

    def config_get(self, key: str) -> str | None:
        row = self._one("SELECT v FROM config WHERE k = ?", (key,))
        return row["v"] if row else None

    def config_set(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO config (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )

    # ---- X (Twitter) otomatik paylaşım kaydı --------------------------

    def x_post_record(
        self, mint: str, tweet_id: str | None, verdict: str | None,
        ok: bool, detail: str | None,
    ) -> None:
        self._write(
            "INSERT INTO x_posts (mint, tweet_id, verdict, ok, detail, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mint, tweet_id, verdict, 1 if ok else 0,
             (detail or "")[:300] or None, int(time.time())),
        )

    def x_posted_since(self, mint: str, since_ts: float) -> bool:
        """Bu mint için `since_ts`'den bu yana BAŞARILI bir paylaşım var mı?"""
        row = self._one(
            "SELECT 1 AS x FROM x_posts WHERE mint = ? AND ok = 1 AND posted_at >= ? "
            "LIMIT 1",
            (mint, int(since_ts)),
        )
        return row is not None

    def x_posts_today(self) -> int:
        cutoff = int(time.time()) - 86400
        row = self._one(
            "SELECT COUNT(*) AS n FROM x_posts WHERE ok = 1 AND posted_at >= ?",
            (cutoff,),
        )
        return int(row["n"]) if row else 0

    def x_posts_recent(self, limit: int = 25) -> list[dict]:
        return self._rows(
            "SELECT mint, tweet_id, verdict, ok, detail, posted_at "
            "FROM x_posts ORDER BY posted_at DESC LIMIT ?",
            (limit,),
        )

    # ---- değişmez lansman önbelleği ----------------------------------

    def launch_cache_get(self, mint: str) -> dict | None:
        row = self._one(
            "SELECT pump, launch FROM launch_cache WHERE mint = ?", (mint,)
        )
        if not row:
            return None
        def _load(v):
            try:
                return json.loads(v) if v else None
            except (TypeError, ValueError):
                return None
        return {"pump": _load(row.get("pump")), "launch": _load(row.get("launch"))}

    def launch_cache_put(
        self, mint: str, pump: dict | None = None, launch: dict | None = None
    ) -> None:
        """pump/launch'tan yalnızca verilen alanı yazar; None geçilen alan
        varolan (iyi) kaydı ezmez."""
        cur = self.launch_cache_get(mint) or {}
        p = pump if pump is not None else cur.get("pump")
        l = launch if launch is not None else cur.get("launch")
        self._write(
            "INSERT INTO launch_cache (mint, pump, launch, saved_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(mint) DO UPDATE SET "
            "pump = excluded.pump, launch = excluded.launch, "
            "saved_at = excluded.saved_at",
            (
                mint,
                json.dumps(p, ensure_ascii=False) if p else None,
                json.dumps(l, ensure_ascii=False) if l else None,
                int(time.time()),
            ),
        )

    # ---- cüzdan meta önbelleği (yaş + ilk fonlayıcı, değişmez) --------

    def wallet_meta_get_many(self, addresses: list[str]) -> dict[str, dict]:
        if not addresses:
            return {}
        out: dict[str, dict] = {}
        uniq = list(dict.fromkeys(addresses))
        for i in range(0, len(uniq), 400):
            chunk = uniq[i : i + 400]
            ph = ",".join("?" for _ in chunk)
            rows = self._rows(
                f"SELECT address, created_at, funder, tx_count, reached "
                f"FROM wallet_meta WHERE address IN ({ph})",
                tuple(chunk),
            )
            for r in rows:
                out[r["address"]] = {
                    "created_at": r["created_at"],
                    "funder": r["funder"],
                    "tx_count": r["tx_count"] or 0,
                    "reached": bool(r["reached"]),
                }
        return out

    def wallet_meta_put_many(self, rows: list[dict]) -> None:
        """rows: [{address, created_at, funder, tx_count, reached}]. reached=True
        kaydı asla ezilmez (o veri kesin ve değişmez)."""
        now = int(time.time())
        for r in rows:
            addr = r.get("address")
            if not addr:
                continue
            self._write(
                "INSERT INTO wallet_meta "
                "(address, created_at, funder, tx_count, reached, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(address) DO UPDATE SET "
                "created_at = COALESCE(wallet_meta.created_at, excluded.created_at), "
                "funder = COALESCE(wallet_meta.funder, excluded.funder), "
                "tx_count = CASE WHEN excluded.tx_count > wallet_meta.tx_count "
                "                THEN excluded.tx_count ELSE wallet_meta.tx_count END, "
                "reached = CASE WHEN excluded.reached > wallet_meta.reached "
                "               THEN excluded.reached ELSE wallet_meta.reached END, "
                "resolved_at = excluded.resolved_at",
                (
                    addr,
                    r.get("created_at"),
                    r.get("funder"),
                    int(r.get("tx_count") or 0),
                    1 if r.get("reached") else 0,
                    now,
                ),
            )

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

    # ---- yükseliş öğrenmesi ------------------------------------------

    def gainer_add(
        self, address: str, kind: str = "wallet", note: str | None = None,
        via: str = "auto", bump: bool = True,
    ) -> None:
        now = int(time.time())
        set_clause = (
            "hits = gainers.hits + 1, last_seen = excluded.last_seen, "
            "note = excluded.note, via = excluded.via"
            if bump else "note = COALESCE(gainers.note, excluded.note)"
        )
        self._write(
            "INSERT INTO gainers (address, kind, note, hits, first_seen, "
            "last_seen, via) VALUES (?, ?, ?, 1, ?, ?, ?) "
            f"ON CONFLICT(address) DO UPDATE SET {set_clause}",
            (address, kind, (note or "")[:500] or None, now, now, via),
        )

    def gainers_list(self) -> list[dict]:
        return self._rows(
            "SELECT address, kind, note, hits, first_seen, last_seen, via "
            "FROM gainers ORDER BY hits DESC, last_seen DESC"
        )

    def gainer_remove(self, address: str) -> None:
        self._write("DELETE FROM gainers WHERE address = ?", (address,))

    def add_gain_lesson(self, **kw) -> int:
        cols = (
            "mint", "symbol", "verdict_was", "rise_pct", "mcap_at_scan",
            "mcap_peak", "scored_at", "learned_at", "markers", "deployer", "detail",
        )
        vals = tuple(kw.get(c) for c in cols[:-1]) + ((kw.get("detail") or "")[:2000],)
        ph = ", ".join("?" for _ in cols)
        if self.pg:
            with self.pool.connection() as c, c.cursor() as cur:
                cur.execute(
                    f"INSERT INTO gain_lessons ({', '.join(cols)}) VALUES ({ph}) "
                    "RETURNING id".replace("?", "%s"),
                    vals,
                )
                r = cur.fetchone()
                return int(r["id"]) if r else 0
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO gain_lessons ({', '.join(cols)}) VALUES ({ph})", vals
            )
            return int(cur.lastrowid)

    def gain_lessons_list(self, limit: int = 200) -> list[dict]:
        return self._rows(
            "SELECT * FROM gain_lessons ORDER BY learned_at DESC LIMIT ?", (limit,)
        )

    def gain_lesson_undo(self, mint: str) -> int:
        rows = self._rows("SELECT address FROM gainers WHERE via = ?", (mint,))
        self._write("DELETE FROM gainers WHERE via = ?", (mint,))
        self._write("DELETE FROM gain_lessons WHERE mint = ?", (mint,))
        return len(rows)

    def add_gain_hit(
        self, mint: str, symbol: str | None, verdict: str | None,
        score: int, detail: str | None,
    ) -> None:
        self._write(
            "INSERT INTO gain_hits (mint, symbol, verdict, score, detail, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(mint) DO UPDATE SET "
            "symbol=excluded.symbol, verdict=excluded.verdict, score=excluded.score, "
            "detail=excluded.detail, scanned_at=excluded.scanned_at",
            (mint, symbol, verdict, int(score), (detail or "")[:1000] or None,
             int(time.time())),
        )

    def gain_hits_list(self, limit: int = 100) -> list[dict]:
        return self._rows(
            "SELECT * FROM gain_hits ORDER BY scanned_at DESC LIMIT ?", (limit,)
        )

    # ---- tam veritabanı yedeği -------------------------------------------

    _BACKUP_TABLES = [
        "config", "flagged", "gainers", "lessons", "gain_lessons",
        "appeals", "track", "launch_cache", "wallet_meta",
        "gain_hits", "x_posts", "scan_history", "scans",
    ]
    # id'si otomatik üretilen (identity/autoincrement) tablolar — geri yüklerken
    # id sütunu atılır, veritabanı yeniden üretir.
    _AUTO_ID_TABLES = {"lessons", "gain_lessons", "appeals", "x_posts", "scan_history"}

    def _table_columns(self, table: str) -> set[str]:
        try:
            if self.pg:
                rows = self._rows(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ?",
                    (table,),
                )
                return {r["column_name"] for r in rows}
            rows = self._rows(f"PRAGMA table_info({table})")
            return {r["name"] for r in rows}
        except Exception:  # noqa: BLE001
            return set()

    def export_all(self, include_scans: bool = True) -> dict:
        """Tüm tabloları JSON'a çevrilebilir bir dict'e döker."""
        tables: dict[str, list[dict]] = {}
        for t in self._BACKUP_TABLES:
            try:
                if t == "scans" and not include_scans:
                    tables[t] = self._rows(
                        "SELECT mint, verdict, score, confidence, created_at FROM scans"
                    )
                else:
                    tables[t] = self._rows(f"SELECT * FROM {t}")
            except Exception:  # noqa: BLE001  (tablo yoksa / eski şema)
                tables[t] = []
        return {
            "format": "solscope-backup",
            "version": 1,
            "generated_at": int(time.time()),
            "backend": "postgres" if self.pg else "sqlite",
            "scans_full": include_scans,
            "counts": {t: len(rows) for t, rows in tables.items()},
            "tables": tables,
        }

    def import_all(self, data: dict, only: list[str] | None = None) -> dict[str, int]:
        """Yedeği geri yükler. Her tablo için: mevcut satırları SİL, yedektekileri
        ekle. Şema drift'ine dayanıklı (bilinmeyen sütunlar atılır). Tablo bazında
        çalışır — biri düşerse diğerleri devam eder."""
        tabs = data.get("tables") or {}
        done: dict[str, int] = {}
        for t in self._BACKUP_TABLES:
            if t not in tabs:
                continue
            if only and t not in only:
                continue
            cols = self._table_columns(t)
            if not cols:
                continue
            rows = tabs[t] or []
            drop_id = t in self._AUTO_ID_TABLES
            try:
                self._write(f"DELETE FROM {t}")
                inserted = 0
                for raw in rows:
                    r = {
                        k: v for k, v in raw.items()
                        if k in cols and not (drop_id and k == "id")
                    }
                    if not r:
                        continue
                    ks = list(r)
                    ph = ", ".join("?" for _ in ks)
                    self._write(
                        f"INSERT INTO {t} ({', '.join(ks)}) VALUES ({ph})",
                        tuple(r[k] for k in ks),
                    )
                    inserted += 1
                done[t] = inserted
            except Exception as exc:  # noqa: BLE001
                log.error("Yedek geri yükleme düştü (%s): %s", t, exc)
                done[t] = -1
        return done

    def stats(self) -> dict:
        def n(sql, params=()):
            r = self._one(sql, params)
            return int(next(iter(r.values()))) if r else 0

        settled = self._rows(
            "SELECT outcome FROM track WHERE settled = 1 AND outcome IS NOT NULL"
        )
        hits = sum(1 for r in settled if r["outcome"] in ("hit", "clear"))

        # Organik tespit başarısı: "organic" denip 24s izlemede korunan / toplam.
        org = self._rows(
            "SELECT outcome FROM track WHERE settled = 1 AND verdict = 'organic' "
            "AND outcome IS NOT NULL ORDER BY latest_at ASC"
        )
        org_seq = [1 if r["outcome"] == "clear" else 0 for r in org]
        org_ok = sum(org_seq)

        vb_rows = self._rows(
            "SELECT verdict, COUNT(*) AS c FROM scan_history "
            "WHERE verdict IS NOT NULL GROUP BY verdict"
        )
        verdict_breakdown = {"bundled": 0, "cabaled": 0, "organic": 0, "inconclusive": 0}
        for r in vb_rows:
            if r["verdict"] in verdict_breakdown:
                verdict_breakdown[r["verdict"]] = int(r["c"])

        day_ago = int(time.time()) - 86400

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
            "gainers": n("SELECT COUNT(*) FROM gainers WHERE hits >= 2"),
            "gain_lessons": n("SELECT COUNT(*) FROM gain_lessons"),
            "backend": "postgres" if self.pg else "sqlite",
            "scans_24h": n("SELECT COUNT(*) FROM scan_history WHERE created_at >= ?", (day_ago,)),
            "rugs_caught": n("SELECT COUNT(*) FROM track WHERE rug_flagged = 1"),
            "flagged_deployers": n("SELECT COUNT(*) FROM flagged WHERE kind = 'deployer'"),
            "verdict_breakdown": verdict_breakdown,
            "organic_perf": {
                "settled": len(org_seq),
                "correct": org_ok,
                "rate": round(org_ok / len(org_seq), 4) if org_seq else None,
                "recent": org_seq[-48:],
            },
        }

    def recent_scans(self, limit: int = 8) -> list[dict]:
        return self._rows(
            "SELECT mint, verdict, score, confidence, created_at FROM scan_history "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def history(self, mint: str, limit: int = 20) -> list[dict]:
        return self._rows(
            "SELECT verdict, score, confidence, created_at FROM scan_history "
            "WHERE mint = ? ORDER BY created_at DESC LIMIT ?",
            (mint, limit),
        )
