"""Auto-zakup w Zwiadowcy: okazja z oceny AI -> buyer kupuje -> mail z wynikiem do weryfikacji.

Przepływ (python -m sniper z SNIPER_BUY_ENABLED=true):
  Zwiadowca wykrywa ofertę -> evaluator ocenia AI -> jeśli is_deal i score >= SNIPER_BUY_MIN_SCORE,
  evaluator NIE wysyła zwykłego maila, tylko przekazuje ofertę tutaj -> kolejka (jeden zakup naraz)
  -> attempt_purchase() (Kup teraz -> twarde limity -> Zapłać) -> OD RAZU mail z wynikiem:
  „KUPIONE - sprawdź i ewentualnie anuluj”, „NIEPOTWIERDZONE” albo „NIE KUPIONO (powód)”.

Przeglądarka konta (account_session.VintedAccount, domowe IP, bez proxy) startuje razem ze Zwiadowcą
i jest podtrzymywana w tle - wtedy NIE uruchamiaj osobno `python -m sniper.account_session`
(ten sam profil Chromium może mieć otwarty tylko jeden proces).
"""
import asyncio
import logging

from .buyer import PurchaseLedger, attempt_purchase, summarize

log = logging.getLogger("sniper.autobuy")

# Twardy limit czasu jednej próby zakupu (otwarcie oferty + Kup teraz + Zapłać).
PURCHASE_TIMEOUT_S = 240


class AutoBuyer:
    def __init__(self, cfg, notifier, account=None, ledger=None):
        """cfg = ScoutConfig; account = VintedAccount (albo atrapa w testach)."""
        self.cfg = cfg.buyer
        self.notifier = notifier
        if account is None:
            from .account_session import VintedAccount
            account = VintedAccount(cfg.account, cfg.log_dir)
        self.account = account
        self.ledger = ledger if ledger is not None else PurchaseLedger(cfg.log_dir)
        self.keepalive_min = cfg.account.keepalive_min
        self.ready = False
        self._queue = asyncio.Queue()
        self._queued = set()
        self._lock = asyncio.Lock()          # przeglądarka: zakup i podtrzymanie sesji nigdy naraz
        self._tasks = []
        self.stats = {"submitted": 0, "bought": 0, "unconfirmed": 0, "skipped": 0, "error": 0}

    # ------------------------------------------------------------------ start / stop
    async def start(self):
        """Uruchamia przeglądarkę konta. Zwraca True, gdy zalogowano (inaczej auto-zakup wyłączony)."""
        try:
            await self.account.start()
        except Exception as exc:
            log.error("[AUTO-BUY] Nie uruchomiłem przeglądarki konta: %s - auto-zakup WYŁĄCZONY, "
                      "okazje idą zwykłym mailem.", exc)
            return False
        if not getattr(self.account, "username", None):
            log.error("[AUTO-BUY] Nie potwierdziłem zalogowania na konto - auto-zakup WYŁĄCZONY "
                      "(wklej świeży cURL do my_headers.txt, ewentualnie --reset w sniper.account_session).")
            return False
        self.ready = True
        self._tasks = [asyncio.create_task(self._worker(), name="autobuy-worker"),
                       asyncio.create_task(self._keepalive(), name="autobuy-keepalive")]
        log.info("[AUTO-BUY] Auto-zakup WŁĄCZONY na koncie %s: ocena >= %g, suma <= %.0f zł, max %d/dobę%s. "
                 "Dziś już: %d.", self.account.username, self.cfg.min_score, self.cfg.max_total_pln,
                 self.cfg.max_per_day, ", tylko PL" if self.cfg.pl_only else "", self.ledger.count_today())
        return True

    async def shutdown(self, timeout=PURCHASE_TIMEOUT_S):
        """Dokończ zakup w toku (nie przerywamy płatności w połowie), resztę kolejki porzuć."""
        if self._lock.locked():
            log.info("[AUTO-BUY] Czekam na dokończenie zakupu w toku...")
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout)
                self._lock.release()
            except asyncio.TimeoutError:
                pass
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.account.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ wejście z evaluatora
    def wants(self, record):
        """Czy ta ocena to okazja do auto-zakupu?"""
        if not self.ready or record.get("status") != "oceniona":
            return False
        ev = record.get("evaluation") or {}
        score = ev.get("score")
        return bool(ev.get("is_deal")) and score is not None and score >= self.cfg.min_score

    def submit(self, offer, record):
        """Nieblokujące: oferta trafia do kolejki zakupów (jeden zakup naraz)."""
        if offer.id in self._queued:
            return
        self._queued.add(offer.id)
        self.stats["submitted"] += 1
        log.warning("[AUTO-BUY] OKAZJA %s (ocena %g) -> kolejka zakupu: %s",
                    offer.id, record["evaluation"]["score"], offer.url)
        self._queue.put_nowait((offer, record))

    def report(self):
        s = self.stats
        return (f"AUTO-BUY: okazje {s['submitted']}, kupione {s['bought']}, niepotwierdzone {s['unconfirmed']}, "
                f"pominięte {s['skipped']}, błędy {s['error']} | dziś {self.ledger.count_today()}/{self.cfg.max_per_day}")

    # ------------------------------------------------------------------ praca w tle
    def _precheck(self, offer):
        """Szybkie odrzucenie BEZ otwierania przeglądarki (pełne limity i tak sprawdza decide_purchase)."""
        if self.ledger.already_bought(offer.id):
            return "ta oferta jest już w rejestrze bought.jsonl"
        done = self.ledger.count_today()
        if done >= self.cfg.max_per_day:
            return f"limit {self.cfg.max_per_day} zakupów na dobę osiągnięty ({done})"
        if offer.total_price is not None and offer.total_price > self.cfg.max_total_pln:
            return (f"cena z wysyłką {offer.total_price:.2f} zł > limit {self.cfg.max_total_pln:.0f} zł "
                    "(SNIPER_BUY_MAX_TOTAL)")
        return None

    async def _worker(self):
        while True:
            offer, record = await self._queue.get()
            try:
                result = await self.buy(offer, record)
                self._mail(offer, record, result)
            except Exception:
                log.exception("[AUTO-BUY] Nieoczekiwany błąd przy %s", offer.id)
            finally:
                self._queued.discard(offer.id)
                self._queue.task_done()

    async def buy(self, offer, record):
        reason = self._precheck(offer)
        if reason:
            log.info("[AUTO-BUY] Nie kupuję %s: %s", offer.id, reason)
            self.stats["skipped"] += 1
            return {"status": "skipped", "reason": reason, "parsed": None}

        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    attempt_purchase(self.account, offer.url, record, self.cfg, self.ledger), PURCHASE_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Mogło dojść do płatności - blokujemy ponowny zakup tej oferty.
                reason = f"zakup przekroczył {PURCHASE_TIMEOUT_S} s - stan nieznany, sprawdź w przeglądarce"
                self.ledger.record({"item_id": str(offer.id), "item_title": offer.title,
                                    "total": offer.total_price, "currency": offer.currency},
                                   "pay_unconfirmed", reason)
                result = {"status": "pay_unconfirmed", "reason": reason, "parsed": None}

        status = result["status"]
        key = {"bought": "bought", "pay_unconfirmed": "unconfirmed", "skipped": "skipped"}.get(status, "error")
        self.stats[key] += 1
        log.warning("[AUTO-BUY] Wynik %s: %s - %s", offer.id, status, result["reason"])
        return result

    def _mail(self, offer, record, result):
        if self.notifier is None:
            return
        purchase = dict(result)
        purchase["summary"] = summarize(result["parsed"]) if result.get("parsed") else None
        self.notifier.notify(offer, ai=record, purchase=purchase)

    async def _keepalive(self):
        interval = max(self.keepalive_min, 1.0) * 60
        while True:
            await asyncio.sleep(interval)
            async with self._lock:
                try:
                    if not await self.account.refresh_and_check():
                        log.error("[AUTO-BUY] Sesja konta wygasła - zakupy będą się nie udawać. "
                                  "Wklej świeży cURL do my_headers.txt i zrestartuj Zwiadowcę.")
                except Exception:
                    log.exception("[AUTO-BUY] Błąd podtrzymania sesji konta - próbuję dalej.")
