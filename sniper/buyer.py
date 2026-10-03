"""Auto-zakup okazji z konta - rdzeń decyzji i bezpieczniki (krok 3).

Przepływ docelowy:
  ocena AI >= próg  ->  bot otwiera ofertę  ->  'Kup teraz'  ->  ekran checkout
  ->  parse_checkout()  ->  decide_purchase() sprawdza TWARDE limity
  ->  realny zakup: wywołanie auto-kliknięcia 'Zapłać' -> człowiek rozwiązuje captchę.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import BuyerConfig

log = logging.getLogger("sniper.buyer")


def _amount(node):
    if not isinstance(node, dict):
        return None, None
    try:
        value = float(node["amount"])
    except (KeyError, TypeError, ValueError):
        value = None
    return value, node.get("currency_code")


def parse_checkout(payload):
    checkout = (payload or {}).get("checkout") or {}
    comp = checkout.get("components") or {}
    pay = comp.get("pay_button_v2") or {}
    total, currency = _amount((pay.get("total") or {}).get("price"))

    summary = comp.get("order_summary_v2") or {}
    items = summary.get("order_items") or []
    first = items[0] if items else {}
    item_price, item_currency = _amount(first.get("price"))

    card = ((comp.get("payment_method") or {}).get("selected_payment_method") or {}).get("credit_card") or {}
    address = (comp.get("shipping_address") or {}).get("address") or {}

    return {
        "purchase_id": checkout.get("id"),
        "checksum": checkout.get("checksum"),
        "item_id": str(first.get("id")) if first.get("id") is not None else None,
        "item_title": first.get("title"),
        "item_count": len(items),
        "item_price": item_price,
        "total": total,
        "currency": currency or item_currency,
        "payments_available": bool(pay.get("payments_available")),
        "pay_button_title": pay.get("button_title"),
        "card_last4": card.get("last4"),
        "buyer_country": address.get("country_code"),
    }


class PurchaseLedger:
    def __init__(self, log_dir):
        self.path = Path(log_dir) / "bought.jsonl" if log_dir else None
        self._rows = self._load()

    def _load(self):
        rows = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return rows

    CONSUMED = ("ready", "bought")

    def already_bought(self, item_id):
        return any(r.get("status") in self.CONSUMED and str(r.get("item_id")) == str(item_id) for r in self._rows)

    def count_on(self, day):
        return sum(1 for r in self._rows if r.get("status") in self.CONSUMED and r.get("local_date") == day.isoformat())

    def count_today(self):
        return self.count_on(datetime.now().astimezone().date())

    def record(self, parsed, status, reason=""):
        now = datetime.now(timezone.utc)
        row = {
            "ts": now.isoformat(),
            "local_date": now.astimezone().date().isoformat(),
            "status": status,
            "reason": reason,
            "item_id": parsed.get("item_id"),
            "item_title": parsed.get("item_title"),
            "total": parsed.get("total"),
            "currency": parsed.get("currency"),
            "purchase_id": parsed.get("purchase_id"),
        }
        self._rows.append(row)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.warning("[BUY] Nie zapisałem rejestru zakupów: %s", exc)
        return row


def decide_purchase(parsed, cfg: BuyerConfig, ledger, offer=None):
    if not parsed.get("item_id"):
        return False, "brak przedmiotu w checkout (pusty koszyk?)"
    if parsed.get("item_count", 0) != 1:
        return False, f"koszyk ma {parsed.get('item_count')} przedmiotów - kupujemy tylko pojedyncze"
    if not parsed.get("payments_available"):
        return False, "płatność niedostępna (payments_available=false)"
    total = parsed.get("total")
    if total is None:
        return False, "nie odczytałem sumy do zapłaty"
    if parsed.get("currency") not in (None, "PLN"):
        return False, f"waluta {parsed.get('currency')} != PLN"
    if total > cfg.max_total_pln:
        return False, f"suma {total:.2f} > limit {cfg.max_total_pln:.0f} zł (SNIPER_BUY_MAX_TOTAL)"
    if ledger.already_bought(parsed["item_id"]):
        return False, "ta oferta już kupiona (rejestr bought.jsonl)"
    done = ledger.count_today()
    if done >= cfg.max_per_day:
        return False, f"limit {cfg.max_per_day} zakupów na dobę osiągnięty ({done})"

    if offer is not None:
        ev = offer.get("evaluation") or {}
        score = ev.get("score")
        if score is not None and score < cfg.min_score:
            return False, f"ocena AI {score:g} < {cfg.min_score:g} (SNIPER_BUY_MIN_SCORE)"
        if cfg.pl_only:
            seller = (offer.get("offer") or {}).get("seller") or {}
            country = (seller.get("country_code") or seller.get("country") or "").upper()
            if country not in ("PL", "POLSKA"):
                return False, f"sprzedawca spoza PL ({country or '?'}) a SNIPER_BUY_PL_ONLY=true"

    return True, f"OK: {parsed['item_title']} za {total:.2f} {parsed.get('currency') or 'PLN'}"


def summarize(parsed):
    return (f"{parsed.get('item_title') or '?'} | suma {parsed.get('total')} {parsed.get('currency') or ''} | "
            f"karta ...{parsed.get('card_last4') or '????'} | {parsed.get('buyer_country') or '?'} | "
            f"id {parsed.get('item_id')}")


async def attempt_purchase(nav, url, offer, cfg: BuyerConfig, ledger):
    """Zarządza pełnym procesem: wejście -> weryfikacja limitów -> klik 'Zapłać'."""
    await nav.open(url)
    try:
        payload = await nav.buy_now_and_get_checkout()
    except Exception as exc:
        log.warning("[BUY] Nie wszedłem do checkoutu dla %s: %r", url, exc)
        return {"status": "error", "reason": str(exc), "parsed": None}

    parsed = parse_checkout(payload)
    ok, reason = decide_purchase(parsed, cfg, ledger, offer)
    if not ok:
        ledger.record(parsed, "skipped", reason)
        log.info("[BUY] Nie przygotowuję zakupu %s: %s", parsed.get("item_id"), reason)
        return {"status": "skipped", "reason": reason, "parsed": parsed}

    try:
        await nav.focus()
    except Exception:
        pass
    
    log.warning("[BUY] LIMITI ZAAKCEPTOWANE: %s | %s. Odpalam auto-zakup...", summarize(parsed), reason)

    # WŁAŚCIWY MOMENT NA KLIKNIĘCIE ZAPŁAĆ
    try:
        await nav.finalize_purchase(nav.page)
        ledger.record(parsed, "bought", reason)
        log.warning("[BUY] AUTO-ZAKUP WYKONANY! Przycisk 'Zapłać' kliknięty. Przejdź do okna i przesuń suwak (jeśli jest).")
        return {"status": "bought", "reason": reason, "parsed": parsed}
    except Exception as exc:
        log.error("[BUY] Błąd podczas klikania 'Zapłać': %s", exc)
        ledger.record(parsed, "error", str(exc))
        return {"status": "error", "reason": str(exc), "parsed": parsed}


async def _cli(argv=None):
    import argparse
    import asyncio
    import logging

    from .account_session import VintedAccount
    from .config import ScoutConfig

    parser = argparse.ArgumentParser(description="Test: auto-zakup na wklejonym linku.")
    parser.add_argument("url", help="link do oferty na Vinted")
    parser.add_argument("--max", type=float, help="nadpisz limit sumy (PLN) na ten test")
    parser.add_argument("--ignore-limits", action="store_true", help="pomiń limity (tylko do testu)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = ScoutConfig()
    buy_cfg = cfg.buyer
    if args.max is not None:
        from dataclasses import replace
        buy_cfg = replace(buy_cfg, max_total_pln=args.max)
    if args.ignore_limits:
        from dataclasses import replace
        buy_cfg = replace(buy_cfg, max_total_pln=10**9, max_per_day=10**9, pl_only=False, min_score=0.0)

    ledger = PurchaseLedger(cfg.log_dir)
    account = VintedAccount(cfg.account, cfg.log_dir)
    try:
        await account.start()
        if not account.username:
            print("Nie potwierdziłem zalogowania - sprawdź my_headers.txt (świeży cURL) i spróbuj --reset.")
            return 1
        print(f"Zalogowany jako {account.username}. Przygotowuję auto-zakup: {args.url}")
        result = await attempt_purchase(account, args.url, None, buy_cfg, ledger)
        print(f"\nWynik: {result['status']} - {result['reason']}")
        if result["status"] == "bought":
            print("Skrypt kliknął 'ZAPŁAĆ'. Sprawdź otwarte okno przeglądarki, by rozwiązać captchę!")
            print("Potem Enter tutaj, żeby zamknąć program.")
            await asyncio.get_event_loop().run_in_executor(None, input)
    finally:
        await account.close()
    return 0


if __name__ == "__main__":
    import asyncio as _asyncio
    import sys as _sys
    raise SystemExit(_asyncio.run(_cli(_sys.argv[1:])))