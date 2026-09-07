"""
X (Twitter) otomatik paylaşım.

Bir tarama tamamlandığında, eşikleri geçen kararlar otomatik olarak yapılandırılmış
X hesabına atılır. Ayarlar admin panelinden gelir; anahtar yoksa ya da kapalıysa
sessizce atlanır. Tarama akışını asla bloklamaz / bozmaz.

OAuth 1.0a (user context) imzalama stdlib ile yapılır — ek bağımlılık yok.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import quote

import httpx

log = logging.getLogger("solscope")

_TWEET_URL = "https://api.twitter.com/2/tweets"
_MEDIA_URL = "https://upload.twitter.com/1.1/media/upload.json"

DEFAULTS: dict = {
    "enabled": False,
    "api_key": "",
    "api_secret": "",
    "access_token": "",
    "access_secret": "",
    "base_url": "",
    "verdicts": ["bundled", "cabaled"],
    "min_mcap": 50_000.0,
    "min_score": 70,
    "min_confidence": 60,
    "cooldown_h": 168,      # aynı token 7 gün tekrar paylaşılmaz
    "max_per_day": 20,
    "media": True,          # kartı görsel olarak yükle
    "lang": "en",
}

_CFG: dict = dict(DEFAULTS)


def configure(d: dict) -> None:
    """Çalışma anındaki yapılandırmayı günceller (None değerler yok sayılır)."""
    for k, v in (d or {}).items():
        if k in DEFAULTS and v is not None:
            _CFG[k] = v


def _mask(s: str) -> str:
    if not s:
        return ""
    return s[:3] + "…" + s[-2:] if len(s) > 6 else "•" * len(s)


def public_status() -> dict:
    """Admin panelinde göstermek için — sırlar maskeli."""
    creds_ok = all(
        _CFG[k] for k in ("api_key", "api_secret", "access_token", "access_secret")
    )
    return {
        "enabled": bool(_CFG["enabled"]),
        "creds_ok": creds_ok,
        "api_key": _mask(_CFG["api_key"]),
        "access_token": _mask(_CFG["access_token"]),
        "base_url": _CFG["base_url"],
        "verdicts": list(_CFG["verdicts"]),
        "min_mcap": _CFG["min_mcap"],
        "min_score": _CFG["min_score"],
        "min_confidence": _CFG["min_confidence"],
        "cooldown_h": _CFG["cooldown_h"],
        "max_per_day": _CFG["max_per_day"],
        "media": bool(_CFG["media"]),
        "lang": _CFG["lang"],
    }


# --- OAuth 1.0a -----------------------------------------------------------

def _pe(s) -> str:
    return quote(str(s), safe="~")


def _auth_header(method: str, url: str) -> str:
    oauth = {
        "oauth_consumer_key": _CFG["api_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": _CFG["access_token"],
        "oauth_version": "1.0",
    }
    # JSON / multipart gövdesi imzaya girmez; yalnızca oauth_* parametreleri.
    param_str = "&".join(
        f"{_pe(k)}={_pe(v)}" for k, v in sorted(oauth.items())
    )
    base = f"{method.upper()}&{_pe(url)}&{_pe(param_str)}"
    key = f"{_pe(_CFG['api_secret'])}&{_pe(_CFG['access_secret'])}"
    sig = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(
        f'{_pe(k)}="{_pe(v)}"' for k, v in sorted(oauth.items())
    )


class XError(RuntimeError):
    pass


async def _upload_media(png: bytes, timeout: float = 30.0) -> str:
    hdr = _auth_header("POST", _MEDIA_URL)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                _MEDIA_URL,
                headers={"Authorization": hdr},
                files={"media": ("card.png", png, "image/png")},
            )
    except httpx.HTTPError as exc:
        raise XError(f"media/upload bağlantı: {exc}") from exc
    if r.status_code >= 300:
        raise XError(f"media/upload {_explain(r.status_code, r.text)}")
    j = r.json()
    mid = j.get("media_id_string") or j.get("media_id") or (j.get("data") or {}).get("id")
    if not mid:
        raise XError(f"media/upload yanıtında id yok: {j}")
    return str(mid)


async def _post_tweet(text: str, media_id: str | None, timeout: float = 20.0) -> str:
    hdr = _auth_header("POST", _TWEET_URL)
    body: dict = {"text": text}
    if media_id:
        body["media"] = {"media_ids": [media_id]}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                _TWEET_URL, headers={"Authorization": hdr}, json=body
            )
    except httpx.HTTPError as exc:
        raise XError(f"X'e bağlanılamadı: {exc}") from exc
    if r.status_code >= 300:
        raise XError(_explain(r.status_code, r.text))
    return str((r.json().get("data") or {}).get("id") or "")


def _explain(status: int, text: str) -> str:
    body = (text or "")[:400]
    hint = ""
    low = body.lower()
    if status in (401, 403):
        if "not allow" in low or "read-only" in low or "oauth1 app permissions" in low:
            hint = ("  ← Access Token muhtemelen 'Read only'. X Portal → App → "
                    "User authentication settings → 'Read and write' yap, SONRA "
                    "Access Token'ı YENİDEN üret.")
        elif "not enrolled" in low or "client-not-enrolled" in low or "project" in low:
            hint = ("  ← App bir Project'e bağlı değil. X Portal'da App'i bir "
                    "Project'in altına taşı.")
        elif status == 401:
            hint = "  ← Kimlik/imza reddedildi. 4 anahtarı kontrol et (boşluk olmasın)."
    elif status == 429:
        hint = "  ← Aylık/oransal yazma limiti doldu (Free katman ~500/ay)."
    return f"X {status}: {body}{hint}"


# --- Tweet metni --------------------------------------------------------

_EMOJI = {"bundled": "🚨", "cabaled": "⚠️", "organic": "🟦", "inconclusive": "▫️"}
_HEAD = {
    "en": {"bundled": "BUNDLED", "cabaled": "CABALED",
           "organic": "ORGANIC", "inconclusive": "INCONCLUSIVE"},
    "tr": {"bundled": "BUNDLED", "cabaled": "CABALED",
           "organic": "ORGANIC", "inconclusive": "BELİRSİZ"},
}


def _tco_len(text: str) -> int:
    """URL'leri 23 karakter sayan kaba uzunluk (X kuralı)."""
    n = 0
    for tok in text.split():
        n += 23 if tok.startswith(("http://", "https://")) else len(tok)
    n += text.count(" ") + text.count("\n")
    return n


def build_tweet(result: dict, base_url: str = "", lang: str = "en") -> str:
    v = result.get("verdict") or {}
    t = result.get("token") or {}
    kind = v.get("kind", "")
    sym = (t.get("symbol") or (result.get("mint") or "")[:6]).lstrip("$")
    sigs = result.get("signals") or []
    fired = [s for s in sigs if s.get("fired")]
    hard = [s.get("label") or s.get("key") for s in fired
            if s.get("direction") == "bundled"][:3]
    emoji = _EMOJI.get(kind, "•")
    head = _HEAD.get(lang, _HEAD["en"]).get(kind, kind.upper())
    mint = result.get("mint") or ""
    link = f"{base_url.rstrip('/')}/t/{mint}" if base_url and mint else ""

    if lang == "tr":
        l2 = f"{len(fired)}/{len(sigs)} zincir sinyali tetiklendi"
        l3 = (f"Skor {v.get('score')} · güven {v.get('confidence')} — "
              "algoritmik bir sinyal, dolandırıcılık hükmü değil.")
    else:
        l2 = f"{len(fired)}/{len(sigs)} on-chain signals fired"
        l3 = (f"Score {v.get('score')} · confidence {v.get('confidence')} — "
              "an algorithmic signal, not a fraud verdict.")

    detail = f" ({', '.join(x for x in hard if x)})" if hard else ""
    text = f"{emoji} {head} — ${sym}\n\n{l2}{detail}\n{l3}"
    if link:
        text += f"\n\n{link}"
    # 280 sınırı — sinyal parantezini gerekirse at
    if _tco_len(text) > 278 and detail:
        text = text.replace(detail, "", 1)
    return text


# --- Karar --------------------------------------------------------------

def should_autopost(result: dict, cache) -> tuple[bool, str]:
    if not _CFG["enabled"]:
        return False, "kapalı"
    if not all(_CFG[k] for k in
               ("api_key", "api_secret", "access_token", "access_secret")):
        return False, "kimlik bilgileri eksik"
    v = result.get("verdict") or {}
    kind = v.get("kind")
    if kind not in _CFG["verdicts"]:
        return False, f"karar '{kind}' paylaşım listesinde değil"
    if (v.get("score") or 0) < _CFG["min_score"]:
        return False, "skor eşiğin altında"
    if (v.get("confidence") or 0) < _CFG["min_confidence"]:
        return False, "güven eşiğin altında"
    mc = (result.get("token") or {}).get("market_cap") or 0
    if mc < _CFG["min_mcap"]:
        return False, "market cap eşiğin altında"
    mint = result.get("mint") or ""
    if cache.x_posted_since(mint, time.time() - _CFG["cooldown_h"] * 3600):
        return False, "token için bekleme süresi dolmadı"
    if cache.x_posts_today() >= _CFG["max_per_day"]:
        return False, "günlük paylaşım sınırına ulaşıldı"
    return True, "ok"


async def maybe_autopost(result: dict, cache) -> None:
    """Tarama akışından `asyncio.create_task` ile çağrılır. Hata fırlatmaz."""
    mint = result.get("mint") or "?"
    try:
        ok, reason = should_autopost(result, cache)
    except Exception:  # noqa: BLE001
        log.exception("X autopost eşik kontrolü patladı: %s", mint)
        return
    if not ok:
        log.info("X autopost atlandı (%s): %s", mint, reason)
        return

    kind = (result.get("verdict") or {}).get("kind")
    try:
        media_id = None
        if _CFG["media"]:
            try:
                from .render_card import render_png
                media_id = await _upload_media(render_png(result, mint))
            except Exception as exc:  # noqa: BLE001
                log.warning("X görsel yüklenemedi (%s), metin+link ile devam: %s",
                            mint, exc)
        text = build_tweet(result, _CFG["base_url"], _CFG["lang"])
        tid = await _post_tweet(text, media_id)
        cache.x_post_record(mint, tid, kind, True, text)
        log.info("X'te paylaşıldı: %s → tweet %s", mint, tid)
    except Exception as exc:  # noqa: BLE001
        log.exception("X autopost başarısız: %s", mint)
        cache.x_post_record(mint, None, kind, False, str(exc))


async def send_test_tweet(cache, sample: dict | None = None) -> dict:
    """Admin panelinden manuel test. Eşik kontrolü YOK; sadece kimlik denemesi."""
    if not all(_CFG[k] for k in
               ("api_key", "api_secret", "access_token", "access_secret")):
        raise XError("Önce 4 API kimlik bilgisini kaydet.")
    if sample:
        text = build_tweet(sample, _CFG["base_url"], _CFG["lang"])
        media_id = None
        if _CFG["media"]:
            try:
                from .render_card import render_png
                media_id = await _upload_media(
                    render_png(sample, sample.get("mint", ""))
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("test görseli yüklenemedi: %s", exc)
        tid = await _post_tweet(text, media_id)
    else:
        ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        text = (f"america.sx — otomatik paylaşım testi ({ts}). "
                "Bu tweet admin panelinden gönderildi.")
        tid = await _post_tweet(text, None)
    cache.x_post_record(sample.get("mint") if sample else "test", tid,
                        (sample or {}).get("verdict", {}).get("kind"), True, text)
    return {"tweet_id": tid, "text": text}
