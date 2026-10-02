# Vinted Sniper – kontekst projektu (dla nowej rozmowy z Claude)

Stan na 2026-10-02. MVP działa na komputerze użytkownika (Windows, Python 3.12, folder `F:\WEBSCRAPER`):
wykrywa nowo dodane oferty w kategorii Vinted, sprawdza je i wysyła alert e-mail. Następny etap: ocena
ofert przez model AI na podstawie wytycznych użytkownika.

Rozmawiamy po polsku. Claude **nie ma dostępu do vinted.pl ze swojego środowiska** (sandbox blokuje
domenę) – wszystko, co dotyka prawdziwego Vinted, uruchamia użytkownik u siebie i wkleja log
(albo pliki z `sniper/logs/`). Zmiany testujemy lokalnie atrapami (httpx MockTransport, fałszywe proxy,
Chromium pod Xvfb) i dopiero wtedy wypychamy.

## Uruchomienie

```bash
pip install -r sniper/requirements.txt
playwright install chromium
cp sniper/.env.example sniper/.env   # proxy IPRoyal, SMTP Onet, filtry
python -m sniper                     # Zwiadowca
python -m sniper.notifier            # mail testowy
python -m sniper.diagnose            # to samo zapytanie przez przeglądarkę / httpx / requests
python -m pytest sniper/tests        # testy (bez sieci)
```

## Architektura (`sniper/`)

| Plik | Rola |
|---|---|
| `config.py` | Wszystko z `sniper/.env`; `build_proxy_url()` (wzorzec IPRoyal `http://{auth}@{host}`), `get_catalog_params()`, nagłówki `CATALOG_HEADERS` / `BASE_HEADERS`, `ScoutConfig`, `SmtpConfig`. |
| `session.py` | `VintedSession`: `httpx.AsyncClient` za proxy; `get_json()` przy 401/403 wstrzymuje ruch i odświeża sesję Playwrightem (headless, przez `ProxyRelay`); próby z limitem czasu; lekki tryb przeglądarki; zapis/odczyt sesji z `logs/session.json`. |
| `proxy_relay.py` | Lokalny przekaźnik proxy dla Chromium (Chromium nie wysyła loginu/hasła proxy przy HTTPS → `ERR_PROXY_AUTH_UNSUPPORTED`). Dokleja `Proxy-Authorization`, liczy bajty, rozpoznaje 407. |
| `scout.py` | Pętla: skan katalogu → nowe ID → równolegle sidebar + shipping → odrzuć sprzedane/zarezerwowane/spoza ceny → `emit()` (log, `offers.jsonl`, kolejka `scout.offers` dla AI, mail). Heartbeat co 60 s. |
| `extractor.py` | Czyste parsowanie JSON → `Offer` (tytuł, cena, opis, `photo_urls` = `full_size_url`, sprzedawca, wysyłka, suma). |
| `notifier.py` | Mail tekst + HTML przez `aiosmtplib` (Onet `smtp.poczta.onet.pl:465`, SSL), wysyłany w tle. |
| `dedup.py` | `RecentIds`: `deque(maxlen)` + `set`. |
| `traffic.py` | Licznik transferu przez proxy (katalog / detale / przeglądarka) → heartbeat + `logs/traffic.csv`. |
| `diagnose.py` | Narzędzie diagnostyczne. |
| `tests/` | 24 testy (pytest), `fixtures.json` = prawdziwe odpowiedzi API. |

## Ustalenia o API Vinted (zweryfikowane na żywo przez użytkownika)

* **Katalog**: `GET https://api.vinted.pl/svc-catalogue/items` (stary `www.vinted.pl/api/v2/catalog/items` = 404).
  Parametry jak w przeglądarce: `page, per_page, search_text, price_from, currency=PLN, order=newest_first,
  attribute_ids[catalog], attribute_ids[brand], attribute_ids[brand_collection], attribute_ids[status]`
  (+ `price_to`, jeśli ustawione – niezweryfikowane). **Puste `price_from=` → 400 INVALID_REQUEST** – wysyłamy
  ceny tylko z wartością. `per_page` jest respektowane (96 → ~39 KB, 20 → ~9 KB, 4 → ~3 KB odpowiedzi).
* **Nagłówki katalogu** (z cURL przeglądarki): Edge 154 UA + `sec-ch-ua`, `origin: https://www.vinted.pl`,
  `sec-fetch-site: same-site`, `priority: u=1, i`, `referer: https://www.vinted.pl/`, `platform: web`,
  `x-next-app: marketplace-web`, plus `x-csrf-token` i `x-anon-id` z Playwrighta.
* **Szczegóły oferty** (nadal stare API, same-origin): `GET www.vinted.pl/api/v2/items/{id}/details/sidebar`
  (plugin `item_status.item_closing_action`: `"sold"` = sprzedana, `null` = aktywna; `description`,
  `user_info_header` ze sprzedawcą) i `/api/v2/items/{id}/shipping_details`.
* **ID ofert**: nadawane przy tworzeniu, nie publikacji – w „najnowszych” pojawiają się oferty z niższym ID
  (szkice, podbicia). Dlatego deduplikacja jest bez progu „niższe ID = stare”.
* Ciastka `cf_clearance` / `datadome` są wiązane z UA → Playwright i httpx mają ten sam UA.

## Proxy i transfer

* Proxy IPRoyal (rotacyjne, każde połączenie = nowe IP) **tylko dla kodu w `sniper/`**. Stare skrypty
  użytkownika (poza tym repo) działają z domowego IP – nie dodawać im proxy.
* Bez proxy Zwiadowca nie startuje (`SNIPER_REQUIRE_PROXY=true`).
* 407 od IPRoyal = złe hasło albo brak transferu na koncie (Chromium pokazuje wtedy mylące
  `ERR_PROXY_AUTH_UNSUPPORTED`). `WinError 10054` = węzeł proxy zerwał połączenie (szum, ponawiamy).
* Pomiary (4 oferty/skan co 5 s): skan ≈ 5,9 KB (z czego ~2,9 KB to wysyłane nagłówki z ciastkami),
  ≈ 4,4 MB/h; szczegóły jednej oferty ≈ 15 KB; **pełna wizyta przeglądarki ≈ 9,3 MB** – dlatego tryb
  lekki (`SNIPER_BROWSER_LIGHT`) i ponowne użycie sesji po restarcie (`SNIPER_SESSION_MAX_AGE`).
  Wyższe `SNIPER_PRICE_FROM` = mniej ofert = mniej zapytań o szczegóły.

## Najważniejsze zmienne `.env`

`SNIPER_PROXY_HOST`, `SNIPER_PROXY_AUTH`, `SNIPER_CATALOG` (np. 3580 = laptopy), `SNIPER_PRICE_FROM`,
`SNIPER_PRICE_TO`, `SNIPER_PER_PAGE`, `SNIPER_POLL_INTERVAL`, `SNIPER_SMTP_USER`, `SNIPER_SMTP_PASSWORD`,
`SNIPER_EMAIL_TO`, `SNIPER_REFRESH_*`, `SNIPER_BROWSER_LIGHT`, `SNIPER_SESSION_MAX_AGE`, `SNIPER_HEARTBEAT`.
Pełna lista: `sniper/.env.example`. `.env` i `sniper/logs/` (logi, `session.json` z tokenami) są w `.gitignore`.

## Następny krok: ocena AI

* Wejście: słownik z `Offer.to_dict()` (już trafia do `scout.offers` i `logs/offers.jsonl`) + wytyczne
  użytkownika. Zdjęcia przekazywać jako URL-e (`photo_urls`) – model pobiera je sam, więc obrazy nie idą
  przez proxy ani łącze użytkownika.
* Wywołanie API modelu i tak idzie bezpośrednio z komputera (nie przez proxy) – nie kosztuje transferu IPRoyal.
* Pomysł użytkownika: pobierać szczegóły oferty z domowego IP zamiast przez proxy. Uwaga: ciastka
  anty-botowe zdobyte przez proxy mogą nie działać z innego IP → potrzebna osobna „domowa” sesja
  (osobna wizyta Playwrighta bez proxy). Szczegóły to ~15 KB/ofertę, więc oszczędność jest mała w porównaniu
  ze skanowaniem i wizytami przeglądarki.
* Inne możliwe oszczędności: wysyłać do `api.vinted.pl` tylko niezbędne ciastka (połowa każdego skanu
  to upload nagłówków), rzadsze skanowanie, wyższy `SNIPER_PRICE_FROM`.

## Preferencje użytkownika

* Odpowiedzi i komentarze w kodzie po polsku; konkretnie, z wynikami testów.
* Zmiany parametrów zapytań do Vinted tylko na podstawie realnego ruchu przeglądarki (cURL z F12) albo
  testu u użytkownika – bez zgadywania endpointów.
* Nie commitować sekretów (hasła, ciastka, tokeny, `.env`).
