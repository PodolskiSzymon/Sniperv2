"""Testy sprawdzania sesji konta (sniper/account.py) - bez sieci i bez prawdziwych ciastek."""
import httpx
import pytest

from sniper import account
from sniper.config import BASE_HEADERS

CURL = r'''curl 'https://api.vinted.pl/svc-catalogue/items?page=1' \
  -H 'accept: application/json' \
  -H 'accept-language: pl,en;q=0.9' \
  -b 'anon_id=abc; access_token_web=TAJNE.TOKEN.XYZ; cf_clearance=zzz' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0) Edg/154.0' \
  -H 'x-csrf-token: csrf-123' \
  -H 'x-anon-id: anon-xyz' \
  -H 'sec-fetch-site: same-site' '''

BLOCK = """\
cookie
anon_id=abc; access_token_web=TAJNE.TOKEN.XYZ; cf_clearance=zzz
user-agent
Mozilla/5.0 (Windows NT 10.0) Edg/154.0
x-csrf-token
csrf-123
accept-language: pl,en;q=0.9
"""


def test_parse_curl_extracts_cookie_and_carried_headers():
    h = account.parse_curl(CURL)
    assert "access_token_web=TAJNE.TOKEN.XYZ" in h["cookie"]
    assert h["x-csrf-token"] == "csrf-123" and h["x-anon-id"] == "anon-xyz"
    assert h["user-agent"].endswith("Edg/154.0")


def test_parse_devtools_block_two_line_format():
    h = account.parse_block(BLOCK)
    assert "access_token_web=TAJNE.TOKEN.XYZ" in h["cookie"]
    assert h["x-csrf-token"] == "csrf-123" and h["accept-language"] == "pl,en;q=0.9"


def test_read_headers_keeps_only_carried(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text(CURL, encoding="utf-8")
    h = account.read_headers(f)
    assert set(h) <= set(account.CARRY) and "sec-fetch-site" not in h   # odsiane
    assert "cookie" in h


def test_build_headers_overlays_cookie_and_ua():
    headers = account.build_headers({"cookie": "x=1", "user-agent": "UA/1", "x-csrf-token": "c"})
    assert headers["cookie"] == "x=1" and headers["user-agent"] == "UA/1"
    assert headers["x-next-app"] == BASE_HEADERS["x-next-app"]      # reszta z BASE_HEADERS


def test_build_headers_requires_cookie():
    with pytest.raises(ValueError, match="cookie"):
        account.build_headers({"user-agent": "UA/1"})


@pytest.mark.parametrize("html,expected,fragment", [
    ('window.__data={"user":{"id":123456789,"login":"szymon_k"}}', True, "szymon_k"),
    ('{"current_user_id":123456789,"username":"flipper99"}', True, "flipper99"),
    ('{"is_anon_user":true,"user":null}', False, "ANONIMOWY"),
    ('<a href="/member/123456789">Profil</a><button>Wyloguj</button>', True, "wylogowania"),
    ('<html>jakis marketing bez danych</html>', None, "sprawdź zapisany HTML"),
])
def test_detect_login(html, expected, fragment):
    logged, detail = account.detect_login(html)
    assert logged is expected and fragment in detail


def test_detect_login_prefers_named_user_over_anon():
    # gdy w HTML jest i flaga anon (inny widget) i nazwa konta - traktujemy jako zalogowany
    logged, _ = account.detect_login('{"is_anon_user":true} ... "login":"szymon_k"')
    assert logged is True


def test_check_fetches_without_proxy_and_detects(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")   # musi być zignorowane (trust_env=False)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["cookie"] = request.headers.get("cookie")
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text='<script>{"user":{"id":123456789,"login":"szymon_k"}}</script>')

    real_client = httpx.Client

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(account.httpx, "Client", fake_client)

    f = tmp_path / "my_headers.txt"
    f.write_text(CURL, encoding="utf-8")
    status, logged, detail, out_path, size = account.check(f, tmp_path)

    assert status == 200 and logged is True and "szymon_k" in detail
    assert seen["url"] == account.HOME_URL
    assert "access_token_web=TAJNE.TOKEN.XYZ" in seen["cookie"] and seen["ua"].endswith("Edg/154.0")
    assert (tmp_path / "account_check.html").exists() and size > 0


def test_main_without_file_explains(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(account.ScoutConfig, "log_dir", str(tmp_path / "empty"))
    code = account.main([str(tmp_path / "nie_ma.txt")])
    assert code == 2 and "Brak pliku" in capsys.readouterr().out


@pytest.mark.parametrize("html,fragment", [
    (r'{\"login\":\"szymon_k\",\"id\":123456789}', "szymon_k"),         # ekranowane cudzysłowy (Next.js)
    ('{&quot;username&quot;:&quot;flipper99&quot;}', "flipper99"),       # HTML-owe &quot;
])
def test_detect_login_handles_escaped_json(html, fragment):
    logged, detail = account.detect_login(html)
    assert logged is True and fragment in detail


def test_find_in_saved_returns_snippets(tmp_path):
    (tmp_path / "account_check.html").write_text(
        'x' * 200 + 'blabla"login":"szymon_k","id":123' + 'y' * 200, encoding="utf-8")
    path, snippets = account.find_in_saved(tmp_path, "szymon_k", window=20)
    assert path is not None and len(snippets) == 1 and "szymon_k" in snippets[0]
    assert len(snippets[0]) < 80                        # krótki fragment, nie cały plik
    assert account.find_in_saved(tmp_path, "nie_ma_tego")[1] == []


def test_find_in_saved_missing_file(tmp_path):
    assert account.find_in_saved(tmp_path, "cokolwiek") == (None, [])
