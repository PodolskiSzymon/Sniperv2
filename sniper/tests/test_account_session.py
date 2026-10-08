"""Testy sesji konta (sniper/account_session.py) - części bez przeglądarki (konwersja ciastek, konfiguracja)."""
import pytest

from sniper import account_session as acc
from sniper.config import AccountConfig, ScoutConfig


def test_cookie_header_to_playwright():
    cookies = acc.cookie_header_to_playwright("a=1; access_token_web=TOK.EN; b=2 ")
    assert cookies == [
        {"name": "a", "value": "1", "domain": ".vinted.pl", "path": "/"},
        {"name": "access_token_web", "value": "TOK.EN", "domain": ".vinted.pl", "path": "/"},
        {"name": "b", "value": "2", "domain": ".vinted.pl", "path": "/"},
    ]
    assert acc.cookie_header_to_playwright("") == []
    assert acc.cookie_header_to_playwright("smieci_bez_rowna") == []


def test_load_account_cookies(tmp_path):
    f = tmp_path / "my_headers.txt"
    f.write_text("curl 'https://www.vinted.pl/' -b 'anon_id=abc; access_token_web=T.O.K' "
                 "-H 'user-agent: Edg/154'", encoding="utf-8")
    cookies, ua = acc.load_account_cookies(f)
    names = {c["name"] for c in cookies}
    assert names == {"anon_id", "access_token_web"} and ua == "Edg/154"


def test_load_account_cookies_requires_cookie(tmp_path):
    f = tmp_path / "my_headers.txt"
    f.write_text("curl 'https://www.vinted.pl/' -H 'user-agent: Edg/154'", encoding="utf-8")
    with pytest.raises(ValueError, match="Brak ciastek"):
        acc.load_account_cookies(f)


def test_account_paths_default_to_log_dir(tmp_path):
    cfg = AccountConfig()
    account = acc.VintedAccount(cfg, tmp_path)
    assert account.headers_file == tmp_path / "my_headers.txt"
    assert account.profile_dir == tmp_path / "account_profile"


def test_account_paths_from_config(tmp_path):
    cfg = AccountConfig(headers_file=str(tmp_path / "h.txt"), profile_dir=str(tmp_path / "prof"))
    account = acc.VintedAccount(cfg, tmp_path / "logs")
    assert account.headers_file == tmp_path / "h.txt" and account.profile_dir == tmp_path / "prof"


def test_account_disabled_by_default():
    assert ScoutConfig().account.enabled is False


class FakePage:
    def __init__(self, banners_result):
        self._banners = banners_result
        self.goto_urls = []

    async def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        return None

    async def evaluate(self, js, arg=None):
        return self._banners

    @property
    def url(self):
        return self.goto_urls[-1] if self.goto_urls else ""


def _account_with_page(tmp_path, banners_result):
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = FakePage(banners_result)
    return account


def test_refresh_and_check_reads_username(tmp_path):
    import asyncio
    body = '{"banners":{"x":{"extra":{"invite_url":"https://www.vinted.pl/invite/koala_test/tok"}}},"code":0}'
    account = _account_with_page(tmp_path, {"status": 200, "body": body})
    assert asyncio.run(account.refresh_and_check()) is True
    assert account.username == "koala_test" and acc.HOME_URL in account.page.goto_urls


def test_refresh_and_check_detects_expired(tmp_path):
    import asyncio
    account = _account_with_page(tmp_path, {"status": 401, "body": '{"code":100}'})
    assert asyncio.run(account.refresh_and_check()) is False and account.username is None


def test_refresh_and_check_logged_in_without_banner(tmp_path):
    import asyncio
    account = _account_with_page(tmp_path, {"status": 200, "body": '{"banners":{},"code":0}'})
    assert asyncio.run(account.refresh_and_check()) is True      # code:0 = sesja aktywna, choć bez nazwy


def test_open_item_navigates_without_buying(tmp_path):
    import asyncio
    account = _account_with_page(tmp_path, {"status": 200, "body": "{}"})
    url = "https://www.vinted.pl/items/123-laptop"
    assert asyncio.run(account.open_item(url)) == url and account.page.goto_urls == [url]


def test_is_session_refresh_detects_loop():
    assert acc.VintedAccount.is_session_refresh("https://www.vinted.pl/session-refresh?ref_url=%2F") is True
    assert acc.VintedAccount.is_session_refresh("https://www.vinted.pl/") is False
    assert acc.VintedAccount.is_session_refresh("") is False


def test_reset_profile_removes_dir(tmp_path):
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.profile_dir.mkdir(parents=True)
    (account.profile_dir / "Cookies").write_text("stare", encoding="utf-8")
    account.reset_profile()
    assert not account.profile_dir.exists()


def test_stuck_on_session_refresh_when_not_refresh(tmp_path):
    import asyncio
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = FakePage({"status": 200, "body": "{}"})
    account.page.goto_urls.append("https://www.vinted.pl/")      # nie jest to session-refresh
    assert asyncio.run(account._stuck_on_session_refresh()) is False


def test_clear_profile_locks_removes_only_locks(tmp_path):
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.profile_dir.mkdir(parents=True)
    for name in ("SingletonLock", "lockfile", "Cookies"):
        (account.profile_dir / name).write_text("x", encoding="utf-8")
    account.clear_profile_locks()
    assert not (account.profile_dir / "SingletonLock").exists()
    assert not (account.profile_dir / "lockfile").exists()
    assert (account.profile_dir / "Cookies").exists()      # ciastka (logowanie) zostają


def test_is_checkout_url():
    assert acc.VintedAccount._is_checkout_url("https://www.vinted.pl/api/v2/purchases/abc/checkout") is True
    assert acc.VintedAccount._is_checkout_url("https://www.vinted.pl/api/v2/purchases/abc/checkout?x=1") is True
    assert acc.VintedAccount._is_checkout_url("https://www.vinted.pl/api/v2/items/123") is False
    assert acc.VintedAccount._is_checkout_url("") is False


def test_seed_only_new_or_changed_headers(tmp_path):
    """Stary my_headers.txt nie może nadpisywać odświeżonych tokenów profilu przy każdym starcie."""
    import asyncio
    import os

    class FakeContext:
        def __init__(self):
            self.added = []

        async def add_cookies(self, cookies):
            self.added.append(cookies)

    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.profile_dir.mkdir(parents=True)
    account.context = FakeContext()
    account.headers_file.write_text("curl 'https://www.vinted.pl/' -b 'access_token_web=STARY; a=1'",
                                    encoding="utf-8")
    assert account.needs_seed() is True
    asyncio.run(account._seed_cookies())
    assert len(account.context.added) == 1                              # pierwszy raz: wgrane
    assert account.needs_seed() is False
    asyncio.run(account._seed_cookies())
    assert len(account.context.added) == 1                              # restart: NIE nadpisuje profilu
    stat = account.headers_file.stat()
    os.utime(account.headers_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))   # świeży cURL
    assert account.needs_seed() is True
    account.reset_profile()
    account.profile_dir.mkdir(parents=True)
    assert account.needs_seed() is True                                 # po --reset wgrywa od nowa


def test_start_reseeds_when_profile_not_logged_in(tmp_path, monkeypatch):
    """Profil bez ważnej sesji + pominięte ciastka -> start wgrywa je jeszcze raz i sprawdza ponownie."""
    import asyncio
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.profile_dir.mkdir(parents=True)
    account.headers_file.write_text("curl 'https://www.vinted.pl/' -b 'a=1'", encoding="utf-8")
    (account.profile_dir / account.SEED_MARKER).write_text(account._headers_stamp(), encoding="utf-8")
    seeds, checks = [], iter([False, True])

    class FakeCtx:
        pages = ["strona"]

        def set_default_navigation_timeout(self, ms):
            pass

        async def add_cookies(self, cookies):
            seeds.append(cookies)

    class FakeChromium:
        async def launch_persistent_context(self, path, **kw):
            return FakeCtx()

    class FakePW:
        chromium = FakeChromium()

        async def start(self):
            return self

    import playwright.async_api as pwa
    monkeypatch.setattr(pwa, "async_playwright", lambda: FakePW())

    async def fake_check():
        return next(checks)
    monkeypatch.setattr(account, "refresh_and_check", fake_check)
    asyncio.run(account.start())
    assert len(seeds) == 1 and account.logged_in is True     # pominięte przy starcie, wgrane po porażce


class _Loc:
    def __init__(self, visible):
        self._visible = visible

    async def count(self):
        return 1 if self._visible is not None else 0

    def nth(self, i):
        return self

    async def is_visible(self):
        return bool(self._visible)


class GuestPage(FakePage):
    """Strona jak dla gościa: banners 200/code:0 bez nazwy, ale w nagłówku „Zaloguj się”."""
    def __init__(self, login_visible):
        super().__init__({"status": 200, "body": '{"banners":{},"code":0}'})
        self._login_visible = login_visible

    async def wait_for_load_state(self, state, timeout=None):
        return None

    def get_by_text(self, pattern):
        return _Loc(self._login_visible)


class _Ctx:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, url=None):
        return self._cookies


def test_banners_ok_but_login_button_means_logged_out(tmp_path):
    import asyncio
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = GuestPage(login_visible=True)
    assert asyncio.run(account.refresh_and_check()) is False        # fałszywe „Sesja aktywna” z logu użytkownika


def test_banners_ok_without_account_token_means_logged_out(tmp_path):
    import asyncio
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = GuestPage(login_visible=None)
    account.context = _Ctx([{"name": "anon_id", "value": "x"}])
    account.headers_file = tmp_path / "brak.txt"                      # needs_seed() = False
    assert asyncio.run(account.refresh_and_check()) is False


def test_banners_ok_with_token_and_no_login_button_is_logged_in(tmp_path):
    import asyncio
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = GuestPage(login_visible=False)
    account.context = _Ctx([{"name": "access_token_web", "value": "tok"}])
    account.headers_file = tmp_path / "brak.txt"
    assert asyncio.run(account.refresh_and_check()) is True
