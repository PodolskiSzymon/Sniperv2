"""Testy podglądu ocen AI (sniper/report.py -> logs/oceny.html)."""
import json

from sniper.report import REPORT_FILE, ReportWriter, load_records, qualifies, render


def rec(score, deal=False, title="Lenovo Legion 5 RTX 4060", total=1800.0, url="https://www.vinted.pl/items/1",
        status="oceniona", **ev):
    evaluation = {"score": score, "is_deal": deal, "gpu_model": "RTX 4060", "laptop_model": "Legion 5",
                  "market_value_pln": 3100, "max_buy_price_pln": 1950, "potential_profit_pln": 1150,
                  "photos_seen": 3, "photo_notes": "Matryca cała.", "reasoning": "Cena poniżej progu.",
                  "red_flags": []}
    evaluation.update(ev)
    return {"status": status, "evaluated_at": "2026-10-09T10:00:00+00:00", "model": "gemini-3.8-flash",
            "photos_sent": 3, "evaluation": evaluation if status == "oceniona" else None,
            "offer": {"id": 1, "title": title, "url": url, "total_price": total, "price": total - 15,
                      "photo_urls": ["https://images1.vinted.net/a.webp"],
                      "seller": {"name": "jan", "country": "Polska"}}}


def test_only_scores_above_threshold():
    assert qualifies(rec(6), 5) and not qualifies(rec(5), 5)          # „powyżej 5” = 6+
    assert not qualifies(rec(9, status="nieoceniona"), 5)
    assert not qualifies(rec(9, status="odfiltrowana"), 5)


def test_card_shows_valuation_and_verdict():
    html = render([rec(9, deal=True)], 5)
    assert "9<small>/10</small>" in html and "OKAZJA" in html
    for text in ("1 800 zł", "1 950 zł", "3 100 zł", "1 150 zł"):   # cena, maks., wartość, zysk
        assert text in html
    assert "Cena poniżej progu." in html and 'class="under"' in html


def test_seller_text_is_escaped_and_bad_urls_dropped():
    html = render([rec(7, title='<script>alert(1)</script>', url="javascript:alert(1)",
                       red_flags=['<img src=x onerror=alert(1)>'])], 5)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "javascript:alert" not in html
    assert "<img src=x onerror" not in html


def test_empty_report_has_message():
    assert "Na razie brak ofert z oceną powyżej 5" in render([], 5)


def test_writer_loads_history_and_appends(tmp_path):
    lines = [rec(4), rec(6, title="A"), rec(9, deal=True, title="B"), rec(8, status="nieoceniona")]
    (tmp_path / "evaluations.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\nzepsuta linia\n",
                                                encoding="utf-8")
    assert [r["offer"]["title"] for r in load_records(tmp_path / "evaluations.jsonl", 5)] == ["A", "B"]
    writer = ReportWriter(tmp_path, 5, limit=2)
    page = (tmp_path / REPORT_FILE).read_text(encoding="utf-8")
    assert ">A</a>" in page and ">B</a>" in page
    assert writer.add(rec(3, title="C")) is False                     # poniżej progu - pomijane
    assert writer.add(rec(7, title="D")) is True
    page = (tmp_path / REPORT_FILE).read_text(encoding="utf-8")
    assert ">D</a>" in page and ">A</a>" not in page                 # limit 2 najnowszych
