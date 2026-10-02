"""Konfiguracja Zwiadowcy - wszystko z zmiennych środowiskowych (lub pliku sniper/.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv jest opcjonalny
    pass


def _env(name, default=""):
    return os.getenv(name, default).strip()


def _env_int(name, default):
    value = _env(name)
    return int(value) if value else default


def _env_float(name, default):
    value = _env(name)
    return float(value) if value else default


def _env_bool(name, default):
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "tak", "on")


def build_proxy_url():
    """Buduje URL proxy według oficjalnego wzorca IPRoyal: http://{proxy_auth}@{proxy}.

    Priorytet:
      1. SNIPER_PROXY_HOST (np. geo.iproyal.com:12321) + SNIPER_PROXY_AUTH (LOGIN:HASLO_country-pl)
      2. SNIPER_PROXY_URL  (gotowy http://LOGIN:HASLO_country-pl@geo.iproyal.com:12321)
    Zwraca "" gdy proxy nie jest skonfigurowane.
    """
    proxy = _env("SNIPER_PROXY_HOST")
    proxy_auth = _env("SNIPER_PROXY_AUTH")
    if proxy:
        proxy = proxy.split("://", 1)[-1]          # tolerujemy wpisanie z http://
        if not proxy_auth:
            return f"http://{proxy}"
        login, _, password = proxy_auth.partition(":")
        # quote() zmienia tylko znaki specjalne (@ : / itd.) - dla zwykłych loginów/haseł
        # wynik jest identyczny z f'http://{proxy_auth}@{proxy}'.
        return f"http://{quote(login, safe='')}:{quote(password, safe='')}@{proxy}"
    return _env("SNIPER_PROXY_URL")


class ProxyNotConfigured(RuntimeError):
    """Brak proxy w .env, a SNIPER_REQUIRE_PROXY=true (domyślnie) - nie wolno wyjść bezpośrednio."""


def require_proxy_url():
    """URL proxy albo wyjątek. Gwarantuje, że żaden ruch nie wyjdzie z pominięciem IPRoyal.

    Ustaw SNIPER_REQUIRE_PROXY=false tylko świadomie (np. testy lokalne) - wtedy brak proxy = ruch bezpośredni.
    """
    proxy_url = build_proxy_url()
    if not proxy_url and _env_bool("SNIPER_REQUIRE_PROXY", True):
        raise ProxyNotConfigured(
            "Brak proxy: ustaw SNIPER_PROXY_HOST + SNIPER_PROXY_AUTH (lub SNIPER_PROXY_URL) w sniper/.env"
        )
    return proxy_url


def requests_proxies(proxy_url=None):
    """Słownik proxies dla requests: {'http': ..., 'https': ...} (używa go sniper.diagnose)."""
    proxy_url = require_proxy_url() if proxy_url is None else proxy_url
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


BASE_URL = "https://www.vinted.pl"
# Nowy endpoint katalogu (Vinted przeniósł listę z www.vinted.pl/api/v2/catalog/items)
CATALOG_URL = "https://api.vinted.pl/svc-catalogue/items"
SIDEBAR_URL = BASE_URL + "/api/v2/items/{item_id}/details/sidebar"
SHIPPING_URL = BASE_URL + "/api/v2/items/{item_id}/shipping_details"

# ---------------------------------------------------------------------------
# 1:1 z działającego projektu (session_management.py / cookies_management.py).
# NIE zmieniać parametr po parametrze - to jest sprawdzony zestaw.
# ---------------------------------------------------------------------------

# UA identyczny z działającym zapytaniem (cURL z przeglądarki) - cf_clearance/datadome są wiązane z UA,
# więc Playwright (który zdobywa te ciastka) i httpx muszą się przedstawiać tak samo.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

# Nagłówki 1:1 z działającego zapytania do api.vinted.pl/svc-catalogue/items (cURL z przeglądarki),
# bez ciastek i tokenów (x-csrf-token / x-anon-id dochodzą z Playwrighta).
CATALOG_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'pl,en;q=0.9,en-GB;q=0.8,en-US;q=0.7',
    'locale': 'pl-PL',
    'origin': 'https://www.vinted.pl',
    'platform': 'web',
    'priority': 'u=1, i',
    'referer': 'https://www.vinted.pl/',
    'sec-ch-ua': '"Chromium";v="154", "Microsoft Edge";v="154", "Not A(Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-site',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0',
    'x-next-app': 'marketplace-web',
}

# Zapytania do www.vinted.pl/api/v2/... (sidebar, shipping_details) to dla przeglądarki ta sama domena:
# bez 'origin', z 'sec-fetch-site: same-origin'.
BASE_HEADERS = {k: v for k, v in CATALOG_HEADERS.items() if k != 'origin'}
BASE_HEADERS['sec-fetch-site'] = 'same-origin'
CATALOG_ONLY_HEADERS = {'origin': CATALOG_HEADERS['origin'], 'sec-fetch-site': CATALOG_HEADERS['sec-fetch-site']}

# session_management.py -> categories
categories = {
    'karty_pamieci': 3063,
    'elektronika': 2994,
}
CATEGORIES = categories


def get_catalog_params(category, order='newest_first', page=1, search_text='', brand_ids='', brand_collection_ids='', status_ids='', price_from='', price_to='', per_page=96):
    """Kopia session_management.get_catalog_params - parametry 1:1 z działającego zapytania (cURL z przeglądarki).

    Jedyna różnica: kategoria spoza słownika (np. "3580") jest używana wprost jako attribute_ids[catalog],
    zamiast rzucać KeyError.
    """
    catalog_ids = categories.get(category, category)
    params = {
        'page': page,
        'per_page': per_page,
        'search_text': search_text,
        'price_from': price_from,
        'price_to': price_to,
        'currency': 'PLN',
        'order': order,
        'attribute_ids[catalog]': catalog_ids,
        'attribute_ids[brand]': brand_ids,
        'attribute_ids[brand_collection]': brand_collection_ids,
        'attribute_ids[status]': status_ids,
    }
    # Vinted odrzuca puste price_from= (400 INVALID_REQUEST) - ceny wysyłamy tylko z wartością,
    # tak jak przeglądarka. Pozostałe puste pola (search_text, attribute_ids[...]) są akceptowane.
    for key in ('price_from', 'price_to'):
        if params[key] in ('', None):
            del params[key]
    return params


def make_main_loop_referer(page=1):
    """Kopia session_management.make_main_loop_referer."""
    if page == 1:
        return "https://www.vinted.pl/catalog"
    else:
        return f"https://www.vinted.pl/catalog?page={page}"


@dataclass(frozen=True)
class SmtpConfig:
    host: str = _env("SNIPER_SMTP_HOST", "smtp.poczta.onet.pl")
    port: int = _env_int("SNIPER_SMTP_PORT", 465)
    username: str = _env("SNIPER_SMTP_USER")          # np. twoj_login@onet.pl
    password: str = _env("SNIPER_SMTP_PASSWORD")      # <-- TUTAJ hasło do Onetu (przez .env!)
    sender: str = _env("SNIPER_EMAIL_FROM") or _env("SNIPER_SMTP_USER")
    recipient: str = _env("SNIPER_EMAIL_TO") or _env("SNIPER_SMTP_USER")
    timeout: float = _env_float("SNIPER_SMTP_TIMEOUT", 20.0)

    @property
    def enabled(self):
        return bool(self.username and self.password and self.recipient)


@dataclass(frozen=True)
class ScoutConfig:
    # Zbudowane z SNIPER_PROXY_HOST + SNIPER_PROXY_AUTH (albo SNIPER_PROXY_URL) - patrz build_proxy_url().
    proxy_url: str = field(default_factory=build_proxy_url)

    # Kategoria: numer catalog_id z Vinted (np. 3580 = laptopy) albo nazwa z CATEGORIES.
    # SNIPER_CATALOG ma pierwszeństwo, SNIPER_CATEGORY zostaje dla zgodności.
    category: str = _env("SNIPER_CATALOG") or _env("SNIPER_CATEGORY", "karty_pamieci")
    search_text: str = _env("SNIPER_SEARCH_TEXT")
    price_from: str = _env("SNIPER_PRICE_FROM", "100")   # minimalna cena w PLN (puste = bez filtra)
    price_to: str = _env("SNIPER_PRICE_TO")               # maksymalna cena w PLN (puste = bez filtra)
    # Ofert na jeden skan. svc-catalogue respektuje per_page (test check_per_page.py, 2026-10-02):
    # 96 ofert = ~39 KB transferu na skan, 20 ofert = ~9 KB. 20 to zapas ~1 h przy nowej ofercie co ~3 min.
    per_page: int = _env_int("SNIPER_PER_PAGE", 20)
    # Pamięć ID - musi być kilka razy większa niż strona katalogu; za mała jest podnoszona do 5 x per_page (min. 100).
    dedup_size: int = _env_int("SNIPER_DEDUP_SIZE", 100)

    # Odstęp między STARTAMI kolejnych skanów katalogu: 15 s = 4 skany na minutę (oszczędza transfer proxy)
    poll_interval: float = _env_float("SNIPER_POLL_INTERVAL", 15.0)
    poll_jitter: float = _env_float("SNIPER_POLL_JITTER", 1.0)       # losowe odchylenie +/- jitter sekund
    heartbeat_interval: float = _env_float("SNIPER_HEARTBEAT", 60.0)  # co ile sekund log "żyję" (0 = wyłączony)
    max_concurrent_details: int = _env_int("SNIPER_MAX_CONCURRENT_DETAILS", 5)
    request_timeout: float = _env_float("SNIPER_REQUEST_TIMEOUT", 10.0)
    # Pierwszy skan tylko "zapamiętuje" obecne oferty, bez alertów (żeby nie zalać skrzynki).
    skip_initial_batch: bool = _env_bool("SNIPER_SKIP_INITIAL_BATCH", True)

    browser_wait_ms: int = _env_int("SNIPER_BROWSER_WAIT_MS", 15000)
    # Odświeżanie sesji przez Playwright (przy rotacyjnym proxy każda próba = nowe IP):
    refresh_attempts: int = _env_int("SNIPER_REFRESH_ATTEMPTS", 6)          # prób w jednej serii
    refresh_retry_delay: float = _env_float("SNIPER_REFRESH_RETRY_DELAY", 5.0)  # s między próbami
    refresh_timeout: float = _env_float("SNIPER_REFRESH_TIMEOUT", 90.0)    # s limitu na jedną próbę
    refresh_backoff: float = _env_float("SNIPER_REFRESH_BACKOFF", 30.0)    # s przerwy po nieudanej serii
    # Lekka przeglądarka: bez obrazków/wideo/fontów i skryptów reklamowych (wizyta ~9 MB -> ułamek tego).
    browser_light: bool = _env_bool("SNIPER_BROWSER_LIGHT", True)
    # Sesja (ciastka + tokeny) zapisywana na dysk i używana po restarcie, jeśli młodsza niż tyle minut
    # (0 = zawsze nowa sesja przeglądarką). Gdy wygaśnie, 401/403 i tak wywoła odświeżenie.
    session_max_age_min: float = _env_float("SNIPER_SESSION_MAX_AGE", 360.0)
    # Folder na logi: sniper.log (rotacja co północ, 30 dni) + offers.jsonl (złapane oferty)
    log_dir: str = _env("SNIPER_LOG_DIR") or str(Path(__file__).with_name("logs"))

    smtp: SmtpConfig = field(default_factory=SmtpConfig)

    @property
    def catalog_id(self):
        return CATEGORIES.get(self.category, self.category)
