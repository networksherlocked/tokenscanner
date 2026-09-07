# SolScope

> Arayüz **america.sx** adıyla yayında (özgün "resmi belge" teması — koyu lacivert /
> eski-altın / parşömen, kartal arması, Oswald başlık fontu). İki dilli
> (EN varsayılan / TR), tarama konsolu ve "Hakkında" sayfası içerir. Render
> servis adı ve URL'i `solscope` olarak kaldı; `localStorage` dil anahtarı
> `americasx_lang` (eski `uavsx_lang` geriye dönük okunuyor).

Solana tokenlarının arz dağıtımını inceleyen on-chain adli analiz motoru.
Bir mint adresi alır, **16 bağımsız sinyal** çalıştırır ve **Bundled / Cabaled /
Organic / Inconclusive** kararını skor + güven değeriyle döndürür.

**Paylaşım & şeffaflık (v5):**
- `GET /card/{mint}.png` — X/OG için 1200×630 kart (Pillow). `GET /t/{mint}` bu
  karta OG etiketleriyle işaret eder ve arayüzü o mint'le otomatik başlatır.
- `GET /badge/{mint}.svg` — projeler sitelerine gömebilir (`<img src=…>`).
- `POST /api/appeal` — karara itiraz (SQLite `appeals`, elle inceleme kuyruğu).
- Sonuç panelinde "Bu kararı paylaş": link kopyala · X'te paylaş · rozet göm · itiraz.
- "Yöntem" sayfası: 16 sinyalin tam listesi + lansman analizi açıklaması.

**Motor doğruluğu (v6):**
- Yeni `lp_lock` sinyali: likidite yakılmış/kilitli mi yoksa geliştirici
  çekebilir mi? (yol haritası #4 — bkz. aşağı)
- `funding_tree` artık **3-hop** (`FUNDING_TREE_HOPS`): A→B→C dallanma
  desenleri — farklı direkt fonlayıcılar 3 hop geriden tek kaynağa çıkıyorsa
  yakalanır. `convergence` alanı en derin ortak atayı verir.
- Eski/yüksek hacimli tokenlar için indeksleyici lansman verisi iskelesi:
  `rpc/trades.py` (Birdeye adaptörü, `BIRDEYE_API_KEY` opsiyonel).
- pump.fun API düzeltmesi: v3 API artık harici tokenları da indeksliyor
  (`protocol: "non_launchpad"`); bunlar artık pump.fun lansmanı sayılmıyor,
  lansman analizi yanlış çıpaya gitmiyordu.

**Motor doğruluğu (v5):**
- `deployer_history` artık deployer'ın önceki tokenlarının kaç tanesinin
  öldüğünü/rug olduğunu da kontrol ediyor (DexScreener, ~12 örnek). Seri rug
  profili → güçlü bundled sinyali.
- Yeni `funding_tree` sinyali: farklı direkt fonlayıcılar tek bir üst
  kaynağa çıkıyorsa koordinasyon var demektir. (v6'da 3-hop oldu.)

**İki katmanlı analiz** (v4):
- **Lansman** — pump.fun `bonding_curve` (yoksa DEX pair) çıpasından imzalar en
  eskiye kadar sayılır, ilk ~40 işlem parse edilerek **lansmandaki ilk alıcılar**
  çıkarılır. Bundle sinyalleri (yaş kümesi, ortak fonlayıcı, eşzamanlı giriş,
  eşit bakiye, ücret parmak izi, taze cüzdan) BUNLARIN üzerinde çalışır — çünkü
  `getTokenLargestAccounts` eski bir tokende paket cüzdanlarını değil, ikincil
  piyasadan alan balinaları gösterir.
- **Mevcut yapı** — top 20 holder'dan yoğunlaşma, tek cüzdan baskınlığı, likidite.
- **Deployer** — pump.fun `creator` + Helius DAS `getAssetsByCreator` ile seri
  lansman tespiti.
- Lansman verisi çekilemezse (çok yüksek hacimli / eski Raydium tokeni) önce
  bir indeksleyiciden (Birdeye — `EARLY_TRADES_PROVIDER` / `BIRDEYE_API_KEY`)
  ilk trade'ler denenir; o da yoksa bundle sinyalleri mevcut holder'lara düşer
  ve sonuçta bu açıkça belirtilir.

- **$10k eşiği:** market cap'i `MIN_MARKET_CAP_USD` (varsayılan 10.000$) altındaki
  tokenlar hiç taranmaz — zincir sorgusu bile yapılmadan 422 döner.
- **Karne (`/api/track`):** her taramadan sonra tokenın market cap'i
  `TRACK_WINDOW_SEC` (30 dk) boyunca izlenir; en düşük noktaya göre düşüş
  `TRACK_DROP_PCT`'i (%35) geçtiyse "çöktü" sayılır. Karar + sonuç ana sayfanın
  altında listelenir (isabet / kaçırdı / korudu / henüz düşüş yok).

---

## Dizin yapısı

```
backend/
  app/
    main.py            FastAPI — API + statik arayüzü birlikte sunar
    cache.py           SQLite tarama önbelleği
    rpc/               pool.py · solana.py · market.py
    engine/            registry.py · signals.py · classifier.py · scanner.py
  tests/test_engine.py
  requirements.txt
  .env.example
frontend/index.html    Build gerektirmeyen tek dosyalık arayüz
render.yaml            Render.com blueprint (tek servis, ücretsiz katman)
```

## Yerel kurulum

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env      # RPC anahtarların varsa doldur
cd backend
set -a; source .env; set +a
uvicorn app.main:app --reload --port 8000
# tarayıcıda http://localhost:8000  — arayüz de API de aynı adreste
```

Arayüzü ayrı bir sunucudan açacaksan, `index.html` içindeki ana script'ten önce
ayrı bir `<script>` etiketiyle `window.SOLSCOPE_API` değerini ver.

Ana sayfa açılışta `/api/recent`'ten son 10 taramayı listeler; tarama sırasında
aşamaları gösteren bir ilerleme penceresi çıkar.

## Yayına alma (Render.com)

1. Bu klasörü bir GitHub reposuna push et.
2. Render panelinde **New > Blueprint** → repoyu seç. `render.yaml` otomatik okunur;
   tek bir web servisi (`solscope`, ücretsiz katman, Frankfurt) oluşur.
3. Build biter bitmez `https://solscope-XXXX.onrender.com` adresinde canlıdır.
4. RPC anahtarların olduğunda: servis **Environment** sekmesi → `RPC_ENDPOINTS`
   değişkenine `<url>|<rps>` çiftlerini virgülle ayırarak gir, servisi yeniden dağıt.

Ücretsiz katman notları: 15 dk hareketsizlikte servis uykuya dalar (ilk istek
~30 sn). Çözüm: [.github/workflows/keepalive.yml](.github/workflows/keepalive.yml)
her 10 dk'da `/api/health`'e vurur (repo GitHub'da olmalı, Actions açık olmalı).
Daha güvenilir: UptimeRobot (ücretsiz, 5 dk kontrol).

### Kalıcı depo (Supabase)

`DATABASE_URL` boşsa SQLite `/tmp`'de tutulur — servis yeniden başladığında
karne ve itirazlar sıfırlanır. Kalıcı olması için ücretsiz Supabase Postgres:

1. supabase.com → yeni proje.
2. **Project Settings → Database → Connection string → URI** (Transaction pooler,
   port 6543). Sonuna `?sslmode=require` ekle.
3. Render'da servis **Environment** → `DATABASE_URL` = bu dize.
4. Yeniden dağıt. Log'da `depo: Postgres` yazmalı. Şema otomatik oluşur.

SQLite ve Postgres aynı kodu paylaşır (`cache.py`); yerelde `DATABASE_URL`
vermezsen SQLite ile çalışır.

### Admin paneli — `/admin`

`ADMIN_TOKEN` env verilirse `<servis>/admin` adresinde bir panel açılır
(verilmezse 404). Token'ı `X-Admin-Token` başlığıyla gönderir. 5 sekme:

- **Panel** — istatistikler, RPC kota göstergesi, sağlayıcı durumu (izleme).
- **Ayarlar** — panelden canlı düzenlenen her şey tek yerde:
  - **RPC uç noktaları** (`RPC_ENDPOINTS`) — kaydedince havuz **anında yeniden
    yapılandırılır**, yeniden dağıtım yok.
  - **Birdeye API anahtarı** — eski token lansman verisi için.
  - **Önbellek süresi** (saat).
  - **Admin şifresi** değiştir.
  - Yalnızca-env değerler (bilgi amaçlı liste).
  Hepsi DB'ye yazılır ve ilgili env değişkenini ezer.
- **Denetim** — alt sekmeler: İtirazlar / İşaretli cüzdanlar. İşaretli cüzdanlar
  motorun `flagged_wallets` / `deployer_history` sinyalinde **anında** kullanılır.
- **AI izleme** — tüm izlenen tokenlar (karne); kayıt sil.
- **AI tespit** — öğrenme döngüsünün çıkardığı dersler; işaretlemeyi "geri al".
- **Araçlar** — bir mint'in önbelleğini sil ya da zincirden zorla yeniden tara.

Tüm ayarlar tek endpoint'ten: `GET/POST /api/admin/settings`.

### X (Twitter) otomatik paylaşım

`app/xpost.py` — eşikleri geçen tarama sonuçlarını otomatik tweet'ler (kart
görseli + `/t/{mint}` linki). Tarama akışını bloklamaz, hata taramayı etkilemez.

- **Kimlik**: X Developer App (Read and write) → 4 OAuth 1.0a anahtarı. Env
  (`X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET`) ya da
  admin panel → Ayarlar → "X otomatik paylaşım" (DB, canlı).
- **Varsayılan KAPALI.** Admin panelden aç/kapa.
- **Eşikler** (panelden): hangi kararlar (Bundled/Cabaled), min market cap
  ($50k), min skor (70), min güven (60), token başına bekleme (7 gün),
  günlük üst sınır (20), tweet dili (EN/TR), kart görseli aç/kapa.
- OAuth 1.0a imzalama stdlib ile (ek bağımlılık yok). Medya: v1.1
  `media/upload`; başarısız olursa metin+link ile devam.
- `POST /api/admin/x/test` — eşik atlayarak kimlikleri dener.
  `GET /api/admin/x/posts` — denetim kaydı. Tablo: `x_posts`.
- **Hukuki**: her tweet "algoritmik sinyal, dolandırıcılık hükmü değil" ibaresi
  taşır ve yöntem sayfasına link verir. Yüksek eşik + kill-switch.

Token üret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

### Öğrenme döngüsü

Bir token **organic/inconclusive** dendi ve 24 saat izlemede **sert çöktü** ise
(`LEARN_MIN_DROP`, varsayılan %55) motor kendi kayıtlı taramasına geri döner:

1. Lansman alıcıları + fonlayıcıları + 2-hop fonlama ağacı + deployer'ı inceler.
2. **Koordinasyon izi varsa** (ortak fonlayıcı ≥2 cüzdan, taze cüzdan kümesi,
   tek üst kaynak, eşik-altı bot imzaları) → o cüzdanları ve deployer'ı
   `flagged` tablosuna ekler (`via = mint`, `hits` = kaç ayrı çöküşte görüldü).
3. **İz yoksa** → hiçbir şey işaretlemez, "muhtemelen piyasa çöküşü" diye
   ders kaydeder (masum cüzdanları kirletmemek için).

Sonraki taramalarda bu cüzdanlar `sig_flagged_wallets`'i, bu deployer
`sig_deployer_history`'yi anında tetikler — token buna göre yeniden sınıflanır.
Yanlış bir öğrenme olursa admin → **Öğrenilenler → geri al**.

### Motoru RPC'siz test etme

```bash
cd backend && python -m tests.test_engine
```

Dört sentetik senaryoyu (bundle / organik / cabal / eski token + balina)
çalıştırır. Sinyal eşiklerini kalibre ederken bunu kullan — her denemede kredi
harcamana gerek yok.

---

## Ücretsiz katmanda hayatta kalma

Bir tarama yaklaşık **125 RPC çağrısı** tutar. Üç kritik kural:

1. **`getProgramAccounts` kullanma.** Tüm holder listesini çekmek Helius'ta
   çağrı başına 10 kredi ve sınırsız tarama demek. `getTokenLargestAccounts`
   ile top 20'de kal — dağıtım manipülasyonu zaten orada görünür.
2. **Havuzla.** `RPC_ENDPOINTS` birden çok sağlayıcı alır; `pool.py` round-robin
   çevirir, 429 veya kredi bitişinde o sağlayıcıyı cooldown'a alır.
   Helius (1M kredi/ay, 10 rps) + Alchemy (30M CU/ay) + QuickNode (10M kredi/ay)
   birlikte ayda 30.000+ tarama eder.
3. **Cache'le.** `CACHE_TTL` varsayılan 15 dakika. Popüler bir token günde
   yüzlerce kez sorgulanır; hepsini zincire götürürsen fatura 50 katına çıkar.
   Ayrıca aynı token için eşzamanlı istekler tek taramada birleştirilir.

---

## Mimari

```
backend/app/
  rpc/pool.py       Çok sağlayıcılı RPC havuzu — token bucket, cooldown, failover
  rpc/solana.py     Zincir sorguları: holder, cüzdan yaşı, fonlama kaynağı, ücret
  rpc/market.py     DexScreener — fiyat, likidite, çift oluşum zamanı
  engine/registry.py  Küratörlü adres listeleri (CEX, LP, burn, işaretli cüzdan)
  engine/signals.py   16 bağımsız sinyal + kalibrasyon tablosu
  rpc/liquidity.py    LP kilit durumu (Raydium API burnPercent + LP mint analizi)
  rpc/trades.py       Eski token lansman verisi — indeksleyici adaptörleri (Birdeye)
  engine/classifier.py  Yakınsama kuralı → karar, skor, güven
  engine/scanner.py   Orkestrasyon
  cache.py          SQLite tarama önbelleği + karar geçmişi
  main.py           FastAPI
```

### Karar kuralı

Tek sinyal asla karar vermez. `classifier.py`:

- **Bundled** — ya (a) ≥ 3 *sert* sinyal + bundled ağırlık ≥ 1.8 (klasik taze
  lansman), ya da (b) ≥ 2 sert sinyal + `combo` ≥ 2.0 (eski/konsolide token;
  `combo = bundled_ağırlık + 0.6·cabaled_ağırlık`). Sert sinyaller: yaş kümesi,
  ortak fonlayıcı, eşzamanlı giriş, eşit bakiyeler, ücret parmak izi, işaretli
  cüzdan, **tek cüzdan baskınlığı**.
- **Cabaled** — `combo` ≥ 0.9, bundled eşiği tutmamış.
- **Inconclusive** — coverage < 0.4 ya da 3+ sert sinyal veri yokluğundan kör.
- **Organic** — hiçbiri.

İki ayrı sayı döner: **skor** (kategoriye uyum gücü, fiyat tahmini değil) ve
**güven** (elimizde ne kadar veri vardı).

> **Eski tokenlar:** `getTokenLargestAccounts` bir tokenın *şu anki* en büyük
> cüzdanlarını verir, lansmandaki paket cüzdanlarını değil. Bir haftadan eski
> tokenlarda orijinal paket çoktan dağılmış olabilir; tarama mevcut holder
> yapısını yansıtır ve sonuçta bu uyarı gösterilir. Derin tespit için yol
> haritasındaki *deployer geçmişi* ve *çok-hop fonlama grafiği* gerekli.

---

## Yol haritası

Sıradaki en yüksek getirili işler:

1. ~~**Fonlama grafiğinde derinlik.**~~ v6'da eklendi: `build_funding_tree`
   `FUNDING_TREE_HOPS` (varsayılan 3) hop geriye izliyor, `sig_funding_tree`
   `convergence` (herhangi bir hop'ta ortak ata) üzerinden tetikleniyor.
   Sıradaki: hop sayısını maliyet/isabet ile kalibre etmek, mevcut holder
   fallback'inde de zincir izlemek.
2. ~~**Deployer geçmişi.**~~ v4'te eklendi (Helius DAS). Bir sonraki adım: o
   geçmiş tokenların kaçının rug olduğunu (fiyat −%99, likidite çekilmiş)
   kontrol etmek.
3. ~~**Yüksek hacimli eski tokenlar için lansman verisi.**~~ İskele v6'da
   eklendi: `rpc/trades.py` sağlayıcı-agnostik `fetch_early_trades()` +
   Birdeye adaptörü (`sort_type=asc`). Anahtar env (`BIRDEYE_API_KEY`) ya da
   **admin panel → Entegrasyonlar** (DB, canlı); boşsa sessizce atlanır.
   **Yapılacak:** anahtar geldiğinde Birdeye şemasını doğrula
   (`_parse_birdeye_item` birden çok alan adı deniyor), Bitquery adaptörü ekle.
4. ~~**LP kilit durumu.**~~ v6'da eklendi (`rpc/liquidity.py` + `sig_lp_lock`):
   pump.fun bonding curve / PumpSwap → protokol kilidi; Raydium → API'nin
   `burnPercent`'i, düşükse LP mint'in en büyük sahibinin authority'si bir
   program PDA'sı mı (kilitli) yoksa düz cüzdan mı (rug riski). Concentrated
   liquidity (Orca/CLMM) henüz kapsam dışı — pozisyon NFT'leri farklı ele alınmalı.
5. **`registry.FLAGGED_WALLETS`'ı büyüt.** Motorun en değerli parçası bu.
   Öğrenme döngüsü + admin paneli bunu DB'de yapıyor; sıradaki adım kürasyon
   ve dışa/içe aktarma.
6. **İtiraz akışı.** v5'te eklendi (`POST /api/appeal` + admin kuyruğu).

## Hukuki not

Yayına almadan önce kendi sözlerinle yazılmış bir **Yöntem**, **Şartlar** ve
**İtiraz** sayfası şart. Bir tokena "Bundled" demek itibar zedeleyici bir iddia
olarak okunabilir; çıktının bir *algoritmik görüş* olduğunu, olgusal tespit
olmadığını her yerde açıkça belirt. Referans aldığın sitelerin metinlerini
kopyalama — o metinler telifli.
