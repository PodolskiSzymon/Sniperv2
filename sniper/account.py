"""Krok 1 do auto-zakupu: sprawdź, czy nagłówki skopiowane z przeglądarki dają dostęp do TWOJEGO konta Vinted.

Pobiera stronę www.vinted.pl z Twojego domowego IP (BEZ proxy IPRoyal) z ciastkami i nagłówkami, które
wkleisz z DevTools, i szuka w HTML oznak zalogowania (Twój login / id konta). Nic nie kupuje, nic nie zmienia.

Dlaczego bez proxy: sesja konta oraz ciastka cf_clearance / datadome są związane z IP i przeglądarką, z których
je skopiowałeś. Wejście na konto z rotacyjnych IP IPRoyal wyglądałoby dla Vinted jak przejęcie konta.

Jak użyć (z katalogu repozytorium):
  1. W przeglądarce zalogowany na Vinted: F12 -> Sieć -> kliknij dowolne zapytanie do vinted.pl ->
     PPM -> Kopiuj -> "Kopiuj jako cURL (bash)".
  2. Wklej do pliku  sniper/logs/my_headers.txt   (folder logs/ jest w .gitignore - NIE commituj go).
  3. python -m sniper.account
  4. Po teście wyloguj się w przeglądarce (unieważnia sesję) i usuń my_headers.txt.

UWAGA: my_headers.txt zawiera access_token_web = pełny dostęp do konta z kartą. Trzymaj go tylko lokalnie.
"""
import argparse
import re
import shlex
import sys
from pathlib import Path

import httpx

from .config import BASE_HEADERS, ScoutConfig

HOME_URL = "https://www.vinted.pl/"
DEFAULT_HEADERS_FILE = "my_headers.txt"
# Nagłówki, które przenosimy z wklejonego cURL-a (reszta z BASE_HEADERS). cookie niesie sesję konta.
CARRY = ("cookie", "user-agent", "x-csrf-token", "x-anon-id", "accept-language")


def parse_curl(text):
    """Nagłówki z 'Kopiuj jako cURL (bash)'. Zwraca {nazwa_małymi: wartość}."""
    text = text.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")  # łamania linii bash/cmd/PowerShell
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    headers = {}
    for flag, value in zip(tokens, tokens[1:]):
        if flag in ("-H", "--header") and ":" in value:
            name, _, val = value.partition(":")
            headers[name.strip().lower()] = val.strip()
        elif flag in ("-b", "--cookie"):
            headers["cookie"] = value.strip()
    return headers


def parse_block(text):
    """Nagłówki z bloku DevTools: albo 'nazwa: wartość', albo nazwa i wartość w kolejnych liniach."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    headers = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        head = stripped.split(":", 1)[0]
        if ":" in stripped and not stripped.startswith(":") and " " not in head:
            name, _, value = stripped.partition(":")
            if value.strip():
                headers.setdefault(name.strip().lower(), value.strip())
                continue
        if re.fullmatch(r"[a-z0-9-]+", stripped) and i + 1 < len(lines):
            headers.setdefault(stripped, lines[i + 1].strip())
    return headers


def read_headers(path):
    text = Path(path).read_text(encoding="utf-8")
    headers = parse_curl(text) if re.search(r"(^|\s)curl", text) else parse_block(text)
    return {name: value for name, value in headers.items() if name in CARRY}


def build_headers(pasted):
    if "cookie" not in pasted:
        raise ValueError("W pliku nie znalazłem nagłówka 'cookie' - skopiuj zapytanie jako cURL (bash) jeszcze raz.")
    headers = dict(BASE_HEADERS)
    headers.update(pasted)                 # user-agent/tokeny ze wklejki muszą pasować do ciastek
    return headers


# Oznaki zalogowania w HTML Vinted (JSON z danymi użytkownika wstrzyknięty w stronę).
_LOGIN_FIELD = re.compile(r'"(?:login|username)"\s*:\s*"([^"]{2,40})"')
_USER_ID = re.compile(r'"(?:current_)?user_id"\s*:\s*(\d{3,})')
_ANON = re.compile(r'"(?:is_anon(?:_user)?|anon)"\s*:\s*true')


def detect_login(html):
    """Zwraca (zalogowany: bool|None, opis). None = niejednoznaczne (zajrzyj do zapisanego HTML)."""
    if _ANON.search(html) and not _LOGIN_FIELD.search(html):
        return False, "strona zwróciła stan ANONIMOWY (niezalogowany)"
    login = _LOGIN_FIELD.search(html)
    user_id = _USER_ID.search(html)
    if login:
        who = login.group(1) + (f" (id {user_id.group(1)})" if user_id else "")
        return True, f"zalogowany jako: {who}"
    if "/member/" in html and ("Wyloguj" in html or "logout" in html.lower()):
        return True, "znaleziono oznaki zalogowania (link wylogowania), ale bez nazwy konta"
    return None, "nie rozpoznałem jednoznacznie - sprawdź zapisany HTML (szukaj swojej nazwy / 'Wyloguj')"


def check(headers_file, out_dir):
    pasted = read_headers(headers_file)
    headers = build_headers(pasted)
    # trust_env=False: ignorujemy ewentualne HTTP(S)_PROXY ze środowiska - to ma iść z domowego IP.
    with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True, trust_env=False) as client:
        response = client.get(HOME_URL)
    status, html = response.status_code, response.text

    out_path = Path(out_dir) / "account_check.html"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
    except OSError:
        out_path = None

    logged, detail = detect_login(html)
    return status, logged, detail, out_path, len(html)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sprawdza, czy wklejone nagłówki logują Cię na Vinted (bez proxy).")
    parser.add_argument("headers_file", nargs="?", help="plik z nagłówkami (domyślnie sniper/logs/my_headers.txt)")
    args = parser.parse_args(argv)

    log_dir = Path(ScoutConfig().log_dir)
    headers_file = Path(args.headers_file) if args.headers_file else log_dir / DEFAULT_HEADERS_FILE
    if not headers_file.exists():
        print(f"Brak pliku z nagłówkami: {headers_file}")
        print("Skopiuj z DevTools (F12 -> Sieć -> zapytanie do vinted.pl -> PPM -> Kopiuj jako cURL (bash))")
        print(f"i wklej do: {headers_file}")
        return 2

    print(f"Czytam nagłówki z {headers_file}")
    print("Pobieram www.vinted.pl z Twojego IP (BEZ proxy)...")
    try:
        status, logged, detail, out_path, size = check(headers_file, log_dir)
    except (ValueError, httpx.HTTPError) as exc:
        print(f"BŁĄD: {exc}")
        return 1

    mark = {True: "TAK", False: "NIE", None: "?"}[logged]
    print(f"\nHTTP {status} | {size // 1024} KB | zalogowany: {mark}")
    print(f"  {detail}")
    if out_path:
        print(f"  HTML zapisany: {out_path}")
    print("\nPo teście: wyloguj się w przeglądarce (unieważnia sesję) i usuń plik z nagłówkami.")
    return 0 if logged else 1


if __name__ == "__main__":
    sys.exit(main())
