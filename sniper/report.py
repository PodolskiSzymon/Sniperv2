"""Podgląd ocen AI w przeglądarce: sniper/logs/oceny.html (odświeżany po każdej ocenie).

Trafiają tu oferty z oceną POWYŻEJ SNIPER_AI_REPORT_ABOVE (domyślnie 5) - żeby było widać, jak AI ocenia
i na ile wycenia laptopa (wartość rynkowa, maksymalna cena zakupu, potencjalny zysk). Plik jest statyczny
(otwierasz go w Edge), sam przeładowuje się co minutę; filtrowanie i sortowanie działa w przeglądarce.

Odbudowa z całej historii (logs/evaluations.jsonl):
    python -m sniper.report
"""
import json
import logging
import os
from datetime import datetime
from html import escape
from pathlib import Path

log = logging.getLogger("sniper.report")

REPORT_FILE = "oceny.html"
MAX_ROWS = 500          # najnowsze N ofert w pliku - żeby strona była lekka


def qualifies(record, above):
    """Czy rekord oceny trafia do raportu: oceniony i score > above."""
    if (record or {}).get("status") != "oceniona":
        return False
    score = ((record.get("evaluation") or {}).get("score"))
    return isinstance(score, (int, float)) and score > above


def load_records(jsonl_path, above, limit=MAX_ROWS):
    rows = []
    path = Path(jsonl_path)
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if qualifies(record, above):
                rows.append(record)
    return rows[-limit:]


# ---------------------------------------------------------------------------- formatowanie
def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pln(value):
    value = _num(value)
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ") + " zł"


def _safe_url(url):
    url = str(url or "")
    return url if url.startswith(("https://", "http://")) else ""


def _when(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def _score_class(score):
    if score >= 8:
        return "s-high"
    if score >= 7:
        return "s-mid"
    return "s-low"


def _card(record):
    e = lambda v: escape(str(v))  # noqa: E731
    ev = record.get("evaluation") or {}
    offer = record.get("offer") or {}
    score = _num(ev.get("score")) or 0
    deal = bool(ev.get("is_deal"))
    total = _num(offer.get("total_price"))
    if total is None:
        total = _num(offer.get("price"))
    market = _num(ev.get("market_value_pln"))
    max_buy = _num(ev.get("max_buy_price_pln"))
    profit = _num(ev.get("potential_profit_pln"))
    url = _safe_url(offer.get("url"))
    photos = offer.get("photo_urls") or []
    photo = _safe_url(photos[0]) if photos else ""
    seller = offer.get("seller") or {}
    flags = [f for f in (ev.get("red_flags") or []) if f]
    under = total is not None and max_buy is not None and total <= max_buy
    title = offer.get("title") or "?"
    gpu = ev.get("gpu_model") or "karta nieznana"
    search = " ".join(str(x) for x in (title, gpu, ev.get("laptop_model") or "", seller.get("name") or "")).lower()

    img = (f'<a class="ph" href="{e(url)}" target="_blank" rel="noopener"><img loading="lazy" src="{e(photo)}" '
           f'alt=""></a>' if photo else '<div class="ph none">brak zdjęcia</div>')
    flag_html = "".join(f'<span class="flag">{e(f)}</span>' for f in flags) or '<span class="ok">brak czerwonych flag</span>'
    auto = '<span class="tag buy">auto-zakup</span>' if record.get("auto_buy") else ""
    notes = (f'<p class="notes"><b>Zdjęcia (AI widzi {e(ev.get("photos_seen", "?"))} z '
             f'{e(record.get("photos_sent", "?"))}):</b> {e(ev["photo_notes"])}</p>') if ev.get("photo_notes") else ""
    return f"""<article class="card{' deal' if deal else ''}" data-score="{score}" data-deal="{int(deal)}"
  data-profit="{profit if profit is not None else -1e9}" data-time="{e(record.get('evaluated_at') or '')}"
  data-text="{e(search)}">
  {img}
  <div class="body">
    <div class="top">
      <span class="score {_score_class(score)}">{score:g}<small>/10</small></span>
      <span class="verdict {'yes' if deal else 'no'}">{'OKAZJA' if deal else 'nie kupować'}</span>{auto}
      <span class="when">{e(_when(record.get('evaluated_at')))}</span>
    </div>
    <h2><a href="{e(url)}" target="_blank" rel="noopener">{e(title)}</a></h2>
    <p class="spec">{e(gpu)}{(' · ' + e(ev['laptop_model'])) if ev.get('laptop_model') else ''}</p>
    <dl class="money">
      <div><dt>Cena łączna</dt><dd class="{'under' if under else 'over' if max_buy is not None and total is not None else ''}">{_pln(total)}</dd></div>
      <div><dt>Maks. cena zakupu</dt><dd>{_pln(max_buy)}</dd></div>
      <div><dt>Wartość rynkowa (AI)</dt><dd>{_pln(market)}</dd></div>
      <div><dt>Potencjalny zysk</dt><dd class="{'pos' if (profit or 0) > 0 else 'neg' if profit is not None else ''}">{_pln(profit)}</dd></div>
    </dl>
    <p class="why">{e(ev.get('reasoning') or '')}</p>
    <div class="flags">{flag_html}</div>
    {notes}
    <p class="meta">Sprzedawca: {e(seller.get('name') or '?')} ({e(seller.get('country') or seller.get('country_code') or '?')})
      · model {e(record.get('model') or '?')} · ID {e(offer.get('id') or '?')}</p>
  </div>
</article>"""


_CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2329;--muted:#66707a;--line:#e3e6ea;--accent:#007782;
--good:#0a7d32;--good-bg:#e5f5ea;--bad:#b42318;--bad-bg:#fdecea;--mid:#9a6700;--mid-bg:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1d2226;--ink:#e8ebee;--muted:#9aa4ad;--line:#2d3339;
--accent:#3fb7c1;--good:#4cc77a;--good-bg:#173a24;--bad:#ff8a80;--bad-bg:#3d1c1a;--mid:#e8b84a;--mid-bg:#3a2f12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px}
h1{margin:0 0 4px;font-size:18px}.sum{color:var(--muted);font-size:13px}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;align-items:center}
.bar button,.bar select,.bar input{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
.bar button.on{background:var(--accent);border-color:var(--accent);color:#fff}.bar input[type=search]{min-width:200px;flex:1}
.bar label{color:var(--muted);font-size:13px;display:flex;gap:4px;align-items:center}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:12px}
.card{display:grid;grid-template-columns:180px 1fr;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.card.deal{border-color:var(--good);box-shadow:0 0 0 1px var(--good)}
.ph img{width:180px;height:180px;object-fit:cover;border-radius:8px;display:block}
.ph.none{width:180px;height:180px;border-radius:8px;background:var(--bg);display:grid;place-items:center;color:var(--muted)}
.top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.score{font-weight:700;font-size:20px;padding:2px 10px;border-radius:8px}.score small{font-size:12px;font-weight:400}
.s-high{background:var(--good-bg);color:var(--good)}.s-mid{background:var(--mid-bg);color:var(--mid)}.s-low{background:var(--bg);color:var(--muted)}
.verdict{font-weight:600;font-size:12px;padding:3px 8px;border-radius:999px}.verdict.yes{background:var(--good);color:#fff}
.verdict.no{background:var(--bg);color:var(--muted)}.tag.buy{font-size:12px;padding:3px 8px;border-radius:999px;background:var(--accent);color:#fff}
.when{margin-left:auto;color:var(--muted);font-size:12px}
h2{font-size:16px;margin:6px 0 2px}h2 a{color:var(--ink);text-decoration:none}h2 a:hover{text-decoration:underline}
.spec{margin:0 0 8px;color:var(--muted)}
.money{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:0 0 8px}
.money div{background:var(--bg);border-radius:8px;padding:6px 8px}.money dt{font-size:11px;color:var(--muted)}
.money dd{margin:0;font-weight:700;font-size:15px}.under,.pos{color:var(--good)}.over,.neg{color:var(--bad)}
.why{margin:0 0 8px}.flags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.flag{background:var(--bad-bg);color:var(--bad);border-radius:6px;padding:2px 8px;font-size:12px}.ok{color:var(--good);font-size:12px}
.notes,.meta{margin:4px 0 0;color:var(--muted);font-size:12px}.empty{text-align:center;color:var(--muted);padding:40px}
@media (max-width:640px){.card{grid-template-columns:1fr}.ph img,.ph.none{width:100%;height:220px}.money{grid-template-columns:repeat(2,1fr)}}
"""

_JS = """
(function(){
  var KEY='sniper-oceny', st={f:'all',s:'time',q:'',auto:true};
  try{Object.assign(st,JSON.parse(sessionStorage.getItem(KEY)||'{}'))}catch(e){}
  var main=document.querySelector('main'),cards=[].slice.call(main.querySelectorAll('.card'));
  var q=document.getElementById('q'),sort=document.getElementById('sort'),auto=document.getElementById('auto');
  function save(){try{sessionStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
  function apply(){
    var shown=0;
    cards.sort(function(a,b){
      if(st.s==='score')return b.dataset.score-a.dataset.score||(b.dataset.time>a.dataset.time?1:-1);
      if(st.s==='profit')return b.dataset.profit-a.dataset.profit;
      return b.dataset.time>a.dataset.time?1:-1;
    }).forEach(function(c){
      var ok=(st.f==='all')||(st.f==='deal'&&c.dataset.deal==='1')||(st.f==='8'&&+c.dataset.score>=8);
      if(ok&&st.q)ok=c.dataset.text.indexOf(st.q.toLowerCase())>=0;
      c.style.display=ok?'':'none';if(ok)shown++;main.appendChild(c);
    });
    document.getElementById('shown').textContent=shown;
    [].forEach.call(document.querySelectorAll('[data-f]'),function(b){b.classList.toggle('on',b.dataset.f===st.f)});
    q.value=st.q;sort.value=st.s;auto.checked=st.auto;save();
  }
  [].forEach.call(document.querySelectorAll('[data-f]'),function(b){b.onclick=function(){st.f=b.dataset.f;apply()}});
  q.oninput=function(){st.q=q.value;apply()};sort.onchange=function(){st.s=sort.value;apply()};
  auto.onchange=function(){st.auto=auto.checked;save()};
  setInterval(function(){if(st.auto&&!document.hidden)location.reload()},60000);
  apply();
})();
"""


def render(records, above):
    deals = sum(1 for r in records if (r.get("evaluation") or {}).get("is_deal"))
    scores = [_num((r.get("evaluation") or {}).get("score")) or 0 for r in records]
    avg = (sum(scores) / len(scores)) if scores else 0
    cards = "\n".join(_card(r) for r in reversed(records)) or (
        f'<p class="empty">Na razie brak ofert z oceną powyżej {above:g}. Strona odświeży się sama.</p>')
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oceny AI – Sniper</title><style>{_CSS}</style></head>
<body><header>
<h1>Oceny AI – oferty z oceną powyżej {above:g}/10</h1>
<div class="sum">{len(records)} ofert · {deals} okazji (AI: kupować) · średnia ocena {avg:.1f} ·
  pokazane: <b id="shown">{len(records)}</b> · aktualizacja {now}</div>
<div class="bar">
  <button data-f="all">Wszystkie</button><button data-f="deal">Tylko okazje</button><button data-f="8">Ocena 8+</button>
  <select id="sort"><option value="time">Najnowsze</option><option value="score">Najwyższa ocena</option>
  <option value="profit">Największy zysk</option></select>
  <input id="q" type="search" placeholder="Szukaj: tytuł, karta, model, sprzedawca">
  <label><input id="auto" type="checkbox" checked> odświeżaj co minutę</label>
</div></header>
<main>
{cards}
</main><script>{_JS}</script></body></html>"""


class ReportWriter:
    """Trzyma ostatnie oferty z oceną > above i przepisuje oceny.html po każdej nowej."""

    def __init__(self, log_dir, above=5.0, limit=MAX_ROWS):
        self.log_dir = Path(log_dir)
        self.path = self.log_dir / REPORT_FILE
        self.above = above
        self.limit = limit
        self.records = load_records(self.log_dir / "evaluations.jsonl", above, limit)
        self.write()

    def add(self, record):
        if not qualifies(record, self.above):
            return False
        self.records.append(record)
        del self.records[:-self.limit]
        self.write()
        return True

    def write(self):
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".html.tmp")
            tmp.write_text(render(self.records, self.above), encoding="utf-8")
            os.replace(tmp, self.path)        # podmiana na raz - przeglądarka nie złapie pół pliku
        except OSError as exc:
            log.warning("[RAPORT] Nie zapisałem %s: %s", self.path, exc)


def main():
    from .config import ScoutConfig
    cfg = ScoutConfig()
    writer = ReportWriter(cfg.log_dir, cfg.ai.report_above)
    print(f"Zapisano {writer.path.resolve()} ({len(writer.records)} ofert z oceną powyżej "
          f"{cfg.ai.report_above:g}). Otwórz go w przeglądarce.")


if __name__ == "__main__":
    main()
