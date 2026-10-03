"""Osobny program: utrzymuje sesję TWOJEGO konta Vinted zalogowaną 24/7 (krok do auto-zakupu).

Działa z domowego IP, NIGDY przez proxy IPRoyal - sesja konta i ciastka cf_clearance/datadome są związane
z Twoim IP i przeglądarką. To osobny proces niż Zwiadowca (ten skanuje przez proxy).

Jak to działa:
  1. Wczytuje ciastka z sniper/logs/my_headers.txt (cURL skopiowany z DevTools, jak w `python -m sniper.account`)
     do TRWAŁEGO profilu Chromium (logs/account_profile) - po pierwszym razie profil pamięta sesję sam.
  2. Trzyma otwartą przeglądarkę i co kilkanaście minut wchodzi na stronę. Własny JavaScript Vinted odświeża
     wtedy access_token (żyje ~1 h) refresh-tokenem (żyje ~7 dni) - dzięki temu sesja nie wygasa.
  3. Co pętlę sprawdza przez /api/v2/banners, czy wciąż jesteś zalogowany (czyta nazwę konta).
  4. `open_item(url)` otwiera ogłoszenie na Twoim zalogowanym koncie - fundament pod (przyszły) auto-zakup.
     NA RAZIE NIC NIE KUPUJE.

Uruchomienie:
    SNIPER_ACCOUNT_ENABLED=true  (w sniper/.env)
    python -m sniper.account_session

Gdy po ~7 dniach refresh_token wygaśnie: zaloguj się w przeglądarce, skopiuj świeży cURL do my_headers.txt
i uruchom ponownie.
"""
import asyncio
import json
import logging
from pathlib import Path

from .account import DEFAULT_HEADERS_FILE, detect_banners, read_headers
from .config import AccountConfig, ScoutConfig

log = logging.getLogger("sniper.account")

HOME_URL = "https://www.vinted.pl/"
# W przeglądarce robimy fetch względny - leci jako same-origin z ciastkami konta (tak jak robi to strona).
BANNERS_PATH = "/api/v2/banners"
COOKIE_DOMAIN = ".vinted.pl"


def cookie_header_to_playwright(cookie_header, domain=COOKIE_DOMAIN):
    """'a=1; b=2' -> [{'name','value','domain','path'}] dla context.add_cookies()."""
    cookies = []
    for part in (cookie_header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return cookies


def load_account_cookies(headers_file):
    """Ciastka konta z pliku my_headers.txt w formacie Playwrighta. Rzuca, gdy brak pliku / ciastek."""
    pasted = read_headers(headers_file)                 # {'cookie': ..., 'user-agent': ..., ...}
    cookies = cookie_header_to_playwright(pasted.get("cookie", ""))
    if not cookies:
        raise ValueError(f"Brak ciastek w {headers_file} - skopiuj zapytanie jako cURL (bash) z F12.")
    return cookies, pasted.get("user-agent")


# JS wykonywany w kontekście strony: pobiera /api/v2/banners i zwraca {status, body}.
_BANNERS_FETCH = """
async (path) => {
  try {
    const r = await fetch(path, {headers: {accept: 'application/json'}, credentials: 'include'});
    return {status: r.status, body: await r.text()};
  } catch (e) { return {status: 0, body: String(e)}; }
}
"""


class VintedAccount:
    """Trwała sesja przeglądarki zalogowanej na Twoje konto (bez proxy)."""

    def __init__(self, cfg: AccountConfig, log_dir):
        self.cfg = cfg
        log_dir = Path(log_dir)
        self.headers_file = Path(cfg.headers_file) if cfg.headers_file else log_dir / DEFAULT_HEADERS_FILE
        self.profile_dir = Path(cfg.profile_dir) if cfg.profile_dir else log_dir / "account_profile"
        self._pw = None
        self.context = None
        self.page = None
        self.username = None

    async def start(self):
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        # Trwały profil => sesja przeżywa restart programu. BEZ proxy (proxy=None) - domowe IP.
        launch_kwargs = dict(headless=self.cfg.headless, proxy=None, viewport={"width": 1280, "height": 900})
        if self.cfg.chrome_path:
            launch_kwargs["executable_path"] = self.cfg.chrome_path
        self.context = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **launch_kwargs)
        self.context.set_default_navigation_timeout(self.cfg.nav_timeout * 1000)
        _, user_agent = await self._seed_cookies()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        log.info("[KONTO] Przeglądarka uruchomiona (profil: %s, headless=%s, bez proxy).",
                 self.profile_dir, self.cfg.headless)
        await self.refresh_and_check()
        return self

    async def _seed_cookies(self):
        """Wstrzykuje ciastka z my_headers.txt, jeśli plik istnieje (przy pierwszym logowaniu / po wygaśnięciu)."""
        if not self.headers_file.exists():
            log.info("[KONTO] Brak %s - polegam na zapisanym profilu przeglądarki.", self.headers_file)
            return [], None
        cookies, user_agent = load_account_cookies(self.headers_file)
        await self.context.add_cookies(cookies)
        log.info("[KONTO] Wczytałem %d ciastek z %s.", len(cookies), self.headers_file.name)
        return cookies, user_agent

    async def refresh_and_check(self):
        """Wchodzi na stronę (JS Vinted odświeża token) i sprawdza, czy jesteś zalogowany. Zwraca bool."""
        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        result = await self.page.evaluate(_BANNERS_FETCH, BANNERS_PATH)
        status, body = result.get("status"), result.get("body") or ""
        if status == 401:
            log.warning("[KONTO] 401 - sesja wygasła. Zaloguj się w przeglądarce i wklej świeży cURL do %s.",
                        self.headers_file.name)
            self.username = None
            return False
        _, name = detect_banners(body)
        if name:
            self.username = name
            log.info("[KONTO] Zalogowany jako: %s", name)
            return True
        # Brak banera polecającego nie przesądza o wylogowaniu - sprawdzamy, czy strona nie jest anonimowa.
        logged = status == 200 and '"code":0' in body
        log.info("[KONTO] Sesja %s (banner bez nazwy, status %s).",
                 "aktywna" if logged else "niepewna", status)
        return logged

    async def open_item(self, url):
        """Otwiera ogłoszenie na zalogowanym koncie. NA RAZIE tylko nawigacja - nic nie kupuje."""
        log.info("[KONTO] Otwieram ofertę (bez zakupu): %s", url)
        await self.page.goto(url, wait_until="domcontentloaded")
        return self.page.url

    async def run_forever(self):
        """Pętla podtrzymująca sesję: co keepalive_min minut wchodzi na stronę i sprawdza zalogowanie."""
        interval = max(self.cfg.keepalive_min, 1.0) * 60
        log.info("[KONTO] Podtrzymuję sesję co %.0f min. Ctrl+C kończy.", self.cfg.keepalive_min)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh_and_check()
            except Exception:
                log.exception("[KONTO] Błąd podczas podtrzymania sesji - próbuję dalej.")

    async def close(self):
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = ScoutConfig()
    if not cfg.account.enabled:
        print("Sesja konta wyłączona. Ustaw SNIPER_ACCOUNT_ENABLED=true w sniper/.env, potem:")
        print("  python -m sniper.account_session")
        return 1
    account = VintedAccount(cfg.account, cfg.log_dir)
    try:
        await account.start()
        if not account.username:
            log.warning("[KONTO] Nie potwierdziłem nazwy konta - sprawdź my_headers.txt (świeży cURL).")
        await account.run_forever()
    except KeyboardInterrupt:
        log.info("[KONTO] Zatrzymano ręcznie.")
    finally:
        await account.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
