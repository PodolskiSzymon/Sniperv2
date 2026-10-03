"""Testy połączenia Zwiadowca -> ocena AI -> auto-zakup -> mail (sniper/autobuy.py), na atrapach."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

from sniper import autobuy
from sniper.autobuy import AutoBuyer
from sniper.buyer import PurchaseLedger
from sniper.config import BuyerConfig
from sniper.evaluator import OfferEvaluator
from sniper.notifier import build_message, sample_offer
from sniper.tests.test_buyer import CHECKOUT


def make_offer(**kw):
    seller = replace(sample_offer().seller, country="Polska", country_code="PL")
    base = dict(id=10225109576, seller=seller, total_price=23.9)
    base.update(kw)
    return replace(sample_offer(), **base)


def record_for(offer, score=9, is_deal=True, status="oceniona"):
    return {"status": status, "evaluation": {"score": score, "is_deal": is_deal},
            "offer": {"seller": {"country_code": offer.seller.country_code}}}


class FakeAccount:
    def __init__(self, logged_in=True, pay_delay=0.0):
        self.username = None
        self._logged_in = logged_in
        self.pay_delay = pay_delay
        self.page = "checkout-page"
        self.calls = []

    async def start(self):
        self.calls.append("start")
        self.username = "szymooon_koala" if self._logged_in else None

    async def open(self, url):
        self.calls.append("open")

    async def buy_now_and_get_checkout(self):
        self.calls.append("buy_now")
        return CHECKOUT

    async def focus(self):
        pass

    async def finalize_purchase(self, page):
        self.calls.append("pay")
        await asyncio.sleep(self.pay_delay)
        return "zapytanie POST .../checkout/payment"

    async def refresh_and_check(self):
        return True

    async def close(self):
        self.calls.append("close")


class FakeNotifier:
    def __init__(self):
        self.mails = []

    def notify(self, offer, ai=None, purchase=None):
        self.mails.append((offer, ai, purchase))


def make_buyer(tmp_path, account=None, **buy_kw):
    buy = dict(enabled=True, max_total_pln=2500.0, max_per_day=2, pl_only=True, min_score=8.0)
    buy.update(buy_kw)
    cfg = SimpleNamespace(buyer=BuyerConfig(**buy), account=SimpleNamespace(keepalive_min=20.0), log_dir=tmp_path)
    notifier = FakeNotifier()
    return AutoBuyer(cfg, notifier, account=account or FakeAccount(), ledger=PurchaseLedger(tmp_path)), notifier


async def run_one(buyer, offer, record):
    assert await buyer.start()
    buyer.submit(offer, record)
    await buyer._queue.join()
    await buyer.shutdown()


# ----------------------------------------------------------------------------- co jest okazją
def test_wants_only_deals_above_buy_threshold(tmp_path):
    buyer, _ = make_buyer(tmp_path)
    offer = make_offer()
    assert buyer.wants(record_for(offer)) is False                  # przeglądarka jeszcze nie gotowa
    buyer.ready = True
    assert buyer.wants(record_for(offer)) is True
    assert buyer.wants(record_for(offer, score=7)) is False          # poniżej SNIPER_BUY_MIN_SCORE
    assert buyer.wants(record_for(offer, is_deal=False)) is False     # AI: nie okazja
    assert buyer.wants(record_for(offer, status="nieoceniona")) is False


def test_start_fails_without_login(tmp_path):
    buyer, _ = make_buyer(tmp_path, account=FakeAccount(logged_in=False))
    assert asyncio.run(buyer.start()) is False and buyer.ready is False


# ----------------------------------------------------------------------------- pełny przepływ
def test_deal_is_bought_and_mailed(tmp_path):
    account = FakeAccount()
    buyer, notifier = make_buyer(tmp_path, account=account)
    offer = make_offer()
    asyncio.run(run_one(buyer, offer, record_for(offer)))
    assert account.calls[:4] == ["start", "open", "buy_now", "pay"] and account.calls[-1] == "close"
    assert len(notifier.mails) == 1
    _, ai, purchase = notifier.mails[0]
    assert purchase["status"] == "bought" and ai["evaluation"]["score"] == 9
    assert buyer.ledger.already_bought("10225109576") and buyer.stats["bought"] == 1


def test_daily_limit_skips_without_browser_but_still_mails(tmp_path):
    account = FakeAccount()
    buyer, notifier = make_buyer(tmp_path, account=account, max_per_day=1)
    buyer.ledger.record({"item_id": "inna", "item_title": "x", "total": 1.0}, "bought")
    offer = make_offer()
    asyncio.run(run_one(buyer, offer, record_for(offer)))
    assert "open" not in account.calls                               # przeglądarka nieruszona
    purchase = notifier.mails[0][2]
    assert purchase["status"] == "skipped" and "na dobę" in purchase["reason"]


def test_price_over_limit_skips_before_browser(tmp_path):
    account = FakeAccount()
    buyer, notifier = make_buyer(tmp_path, account=account, max_total_pln=10.0)
    offer = make_offer()
    asyncio.run(run_one(buyer, offer, record_for(offer)))
    assert "open" not in account.calls and notifier.mails[0][2]["status"] == "skipped"


def test_timeout_marks_unconfirmed_and_blocks_rebuy(tmp_path, monkeypatch):
    monkeypatch.setattr(autobuy, "PURCHASE_TIMEOUT_S", 0.2)
    buyer, notifier = make_buyer(tmp_path, account=FakeAccount(pay_delay=5))
    offer = make_offer()
    asyncio.run(run_one(buyer, offer, record_for(offer)))
    assert notifier.mails[0][2]["status"] == "pay_unconfirmed"
    assert buyer.ledger.already_bought(offer.id)                       # nie kupimy drugi raz


# ----------------------------------------------------------------------------- evaluator -> buyer
def test_evaluator_hands_deal_to_buyer_instead_of_mail(tmp_path):
    ev = object.__new__(OfferEvaluator)
    ev.cfg = SimpleNamespace(notify_all=False, min_score=6.0)
    ev.notifier = FakeNotifier()
    ev.log_dir = None
    ev.window, ev.total = {"notified": 0}, {"notified": 0}
    ev.save = lambda record: None
    submitted = []
    ev.buyer = SimpleNamespace(wants=lambda r: r["evaluation"]["is_deal"],
                               submit=lambda offer, record: submitted.append(offer.id))
    offer = make_offer()
    ev._finish(offer, record_for(offer))
    assert submitted == [offer.id] and ev.notifier.mails == []           # mail wyśle buyer z wynikiem
    ev._finish(offer, record_for(offer, score=7, is_deal=False))         # zwykła oferta: stary mail
    assert submitted == [offer.id] and len(ev.notifier.mails) == 1


# ----------------------------------------------------------------------------- mail
def test_purchase_mail_subject_and_body():
    offer = make_offer()
    from sniper.buyer import parse_checkout
    bought = {"status": "bought", "reason": "OK", "parsed": parse_checkout(CHECKOUT), "summary": "taśma | suma 23.9"}
    msg = build_message(offer, "a@b", "c@d", ai=None, purchase=bought)
    assert "KUPIONE 23.90 zł - sprawdź / anuluj" in msg["Subject"]
    text = msg.get_body(("plain",)).get_content()
    assert "AUTO-ZAKUP" in text and "https://www.vinted.pl/inbox" in text and "anuluj" in text
    skipped = {"status": "skipped", "reason": "limit 2 zakupów na dobę", "parsed": None}
    msg = build_message(offer, "a@b", "c@d", purchase=skipped)
    assert "NIE KUPIONO" in msg["Subject"] and "limit 2 zakupów" in msg.get_body(("plain",)).get_content()


def _bare_evaluator(mail_only_purchases, buyer):
    ev = object.__new__(OfferEvaluator)
    ev.cfg = SimpleNamespace(notify_all=False, min_score=6.0, mail_only_purchases=mail_only_purchases)
    ev.notifier = FakeNotifier()
    ev.window, ev.total = {"notified": 0}, {"notified": 0}
    ev.save = lambda record: None
    ev.buyer = buyer
    return ev


def test_mail_only_purchases_silences_ordinary_offers(tmp_path):
    submitted = []
    ev_of = lambda r: r.get("evaluation") or {}  # noqa: E731
    buyer = SimpleNamespace(wants=lambda r: bool(ev_of(r).get("is_deal")) and ev_of(r).get("score", 0) >= 8,
                            submit=lambda offer, record: submitted.append(offer.id))
    ev = _bare_evaluator(True, buyer)
    offer = make_offer()
    ev._finish(offer, record_for(offer, score=7, is_deal=False))      # zwykła oferta >= SNIPER_AI_MIN_SCORE
    ev._finish(offer, {"status": "nieoceniona", "evaluation": None})    # błąd AI
    assert ev.notifier.mails == []
    ev._finish(offer, record_for(offer, score=9))                      # okazja -> buyer (on wyśle mail)
    assert submitted == [offer.id]


def test_mail_only_purchases_ignored_without_buyer(tmp_path):
    ev = _bare_evaluator(True, None)                                    # auto-zakup nie działa
    offer = make_offer()
    ev._finish(offer, record_for(offer, score=7, is_deal=False))
    assert len(ev.notifier.mails) == 1                                  # nie gubimy okazji
