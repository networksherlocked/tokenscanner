# SolScope

> Arayüz **UAVSX** adıyla yayında; iki dilli (EN varsayılan / TR), tarama konsolu
> ve "Hakkında" sayfası içerir. Render servis adı ve URL'i `solscope` olarak kaldı.

Solana tokenlarının arz dağıtımını inceleyen on-chain adli analiz motoru.
Bir mint adresi alır, 14 bağımsız sinyal çalıştırır ve **Bundled / Cabaled /
Organic / Inconclusive** kararını skor + güven değeriyle döndürür.

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
- Lansman verisi çekilemezse (çok yüksek hacimli / eski Raydium tokeni) bundle
  sinyalleri mevcut holder'lara düşer ve sonuçta bu açıkça belirtilir.

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
~30 sn), disk kalıcı değil (önbellek `/tmp`'de, yeniden dağıtımda sıfırlanır).

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
  engine/signals.py   13 bağımsız sinyal + kalibrasyon tablosu
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

1. **Fonlama grafiğinde derinlik.** Şu an her cüzdanın 1 adım gerisine
   bakıyoruz. 2–3 hop geriye gidip A→B→C dallanma desenlerini yakalamak
   bundle tespitini belirgin biçimde güçlendirir.
2. ~~**Deployer geçmişi.**~~ v4'te eklendi (Helius DAS). Bir sonraki adım: o
   geçmiş tokenların kaçının rug olduğunu (fiyat −%99, likidite çekilmiş)
   kontrol etmek.
3. **Yüksek hacimli eski tokenlar için lansman verisi.** Bonding curve /
   pair imza taraması bütçesi dolduğunda Bitquery / Birdeye (`sort_type=asc`)
   gibi bir kaynaktan ilk trade'leri çekmek.
3. **`registry.FLAGGED_WALLETS`'ı büyüt.** Motorun en değerli parçası bu.
   Her Bundled kararında kümedeki cüzdanları otomatik kaydet — sistem
   kullandıkça keskinleşir.
4. **LP kilit durumu.** Raydium/Pump.fun LP tokenının burn veya lock edilip
   edilmediği; şu an yalnızca likidite/mcap oranına bakıyoruz.
5. **İtiraz akışı.** Bir karara itiraz formu + manuel inceleme kuyruğu.
   Hukuki olarak da, kalibrasyon açısından da gerekli.

## Hukuki not

Yayına almadan önce kendi sözlerinle yazılmış bir **Yöntem**, **Şartlar** ve
**İtiraz** sayfası şart. Bir tokena "Bundled" demek itibar zedeleyici bir iddia
olarak okunabilir; çıktının bir *algoritmik görüş* olduğunu, olgusal tespit
olmadığını her yerde açıkça belirt. Referans aldığın sitelerin metinlerini
kopyalama — o metinler telifli.
