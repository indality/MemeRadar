# Memecoin Trend Radar

Datu vākšanas un analīzes rīks jaunu/mazas kapitalizācijas Solana memecoin izsekošanai.
**Tikai monitorings — bots neveic nekādas transakcijas un nepiekļūst tavai maciņa atslēgai.**

## Kas iekšā

- `fetch_trends.py` — Python skripts, kas ik pa laikam nolasa pump.fun un DexScreener publiskos API, aprēķina heiristisku "momentum score" (apjoms/likviditāte × jaunums) un saglabā `data/latest.json` + CSV žurnālu.
- `dashboard.html` — statisks HTML dashboard, kas rāda `data/latest.json` kā šķirojamu tabulu, auto-atsvaidzina ik pēc 30s.
- `.github/workflows/fetch.yml` — GitHub Actions konfigurācija, kas palaiž fetcher ik pēc 10 minūtēm bez maksas, pat kad tavs dators ir izslēgts.

## 1. Testēšana lokāli

```bash
pip install requests rich --break-system-packages
python fetch_trends.py            # viena palaišana
python fetch_trends.py --loop 60  # atkārto ik pēc 60s
```

Tas izveidos `data/latest.json` un `data/log_YYYYMMDD.csv`.

Lai apskatītu dashboard lokāli (nevar atvērt kā `file://`, jāservē caur HTTP):

```bash
python -m http.server 8000
# tad pārlūkā atver: http://localhost:8000/dashboard.html
```

## 2. 24/7 automatizācija (bez maksas, bez servera)

1. Izveido jaunu GitHub repo un iepiešļauj šos failus.
2. GitHub Actions workflow (`.github/workflows/fetch.yml`) jau ir konfigurēts palaisties ik pēc 10 minūtēm — tas automātiski atjaunos `data/latest.json` repo iekšā.
3. Iespējo GitHub Pages repo iestatījumos (Settings → Pages → Deploy from branch → main).
4. Tavs dashboard būs pieejams `https://<lietotajvards>.github.io/<repo>/dashboard.html` — no jebkuras ierīces, bez sava servera.

> Piezīme: Repo jābūt public vai jābūt GitHub Pro/Team plānam privātam repo ar Pages.

## Riska dati (RugCheck)

Lai netērētu RugCheck bezmaksas API limitu, riska pārbaude (LP lock statuss, mint/freeze
authority, top 10 holder %, kopējais risk score) tiek darīta **tikai top N kandidātiem**
pēc momentum score katrā ciklā (noklusējums: `RUGCHECK_TOP_N = 20`, ~1.1s starp pieprasījumiem).

Dashboard krāsu kodējums riska kolonnā:
- 🟢 zaļš — score < 20 (RugCheck skala, zemāks = drošāks)
- 🟡 dzeltens — score 20–49
- 🔴 sarkans — score ≥ 50, vai redzams ⚠mint / ⚠freeze karogs (authority nav atteikts)

Tokeniem ārpus top N pie risk kolonnas rādīsies "nav pārbaudīts" — tas nenozīmē, ka tie ir
droši, tikai to, ka tie vēl nav sasnieguši pārbaudes slieksni. Nākamajā ciklā, ja to score
pieaugs, tie tiks pārbaudīti.

## 3. Filtru pielāgošana

`fetch_trends.py` sākumā:

```python
MIN_LIQUIDITY_USD = 5_000
MIN_VOLUME_24H_USD = 10_000
MAX_AGE_HOURS = 48
```

Palielini `MIN_LIQUIDITY_USD`, ja gribi izslēgt visriskantākos, tikko izveidotos tokenus.
Samazini `MAX_AGE_HOURS`, ja interesē tikai pēdējo dažu stundu launch'i.

## Ierobežojumi un riski

- **pump.fun API nav oficiāli dokumentēts.** Endpointi var mainīties bez brīdinājuma — ja skripts pārstāj strādāt, pārbaudi pump.fun pārlūka Network cilni, lai atrastu aktuālo API adresi.
- **Score formula ir heiristika, nevis prognoze.** Augsts score nozīmē "aktīva tirdzniecība attiecībā pret likviditāti pēdējā laikā", nevis "šis pieaugs".
- Dati var nokavēties par 10–30 sekundēm līdz vairākām minūtēm atkarībā no atsvaidzes intervāla — sub-minūtes botu sacensībās to neatsver.
- ~98% pump.fun tokenu izgāžas vai ir rug pull. Šis rīks palīdz *redzēt*, kas notiek tirgū — tas nenovērš risku.

## Iespējamie nākamie soļi

- Pievienot Telegram/Discord webhook paziņojumus, kad tokens pārsniedz score slieksni.
- Pievienot holder koncentrācijas datus (top 10 wallet %) kā papildu filtru.
- Uzglabāt vēsturiskos datus, lai redzētu score izmaiņas laikā (grafiks nevis momentuzņēmums).
