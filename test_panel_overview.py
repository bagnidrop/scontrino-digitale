"""Test della Panoramica (/dashboard) e controlli trasversali del pannello (accesso, menu, CSP)."""
import html as htmllib
import itertools
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from testutil import appmod, make_config, register

app = appmod.create_app(make_config("panel_overview.db"))
_n = itertools.count(1)
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def fresh(name="Locale"):
    """Nuovo locale (con client già loggato) -> (client, shop_id)."""
    n = next(_n)
    email = f"ov{n}@example.com"
    client, _key = register(app, f"{name} {n}", email)
    with app.app_context():
        sid = appmod.get_db().execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
    return client, sid


def mk_receipt(sid, total=500, number=None, payment="Carta", table="", days_ago=0, hours_ago=0, voided=False,
               views=0, item="Piatto"):
    with app.app_context():
        db = appmod.get_db()
        if number is None:
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        rid = db.insert(
            "INSERT INTO receipts (shop_id, token, number, total_cents, vat_cents, payment_method, table_label,"
            " lottery_code, views, created_at, voided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, secrets.token_urlsafe(10), number, total, 0, payment, table, "", views,
             iso(days_ago, hours_ago), iso(0) if voided else None))
        db.execute("INSERT INTO receipt_items (receipt_id, name, qty, unit_price_cents, vat_rate, line_total_cents,"
                   " vat_cents) VALUES (?,?,?,?,?,?,?)", (rid, item, 1, total, 10, total, 0))
        db.commit()
    return rid


def mk_invoice(rid, status="nuova", company="ACME Srl"):
    with app.app_context():
        db = appmod.get_db()
        db.execute("INSERT INTO invoice_requests (receipt_id, company_name, vat_number, sdi_code, address, email,"
                   " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (rid, company, "12345678903", "ABCDEFG", "Via Roma 1", "a@b.it", status, iso(0)))
        db.commit()


def mk_message(rid, status="sent", channel="whatsapp", days_ago=0, error=None):
    with app.app_context():
        db = appmod.get_db()
        db.execute("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at)"
                   " VALUES (?,?,?,?,?,?)", (rid, channel, "+39 333 ••• 4567", status, error, iso(days_ago)))
        db.commit()


def get(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


def card_value(page, label):
    m = re.search(r'<span class="stat-label">' + re.escape(label) + r'</span>.*?<span class="stat-value">([^<]*)</span>',
                  page, re.S)
    assert m, f"card «{label}» assente"
    return m.group(1).strip()


def card_href(page, label):
    m = re.search(r'<a class="card is-link stat-card[^"]*" href="([^"]+)">\s*<span class="stat-top">\s*'
                  r'<span class="stat-label">' + re.escape(label) + "</span>", page)
    assert m, f"card «{label}» senza link"
    u = urlparse(htmllib.unescape(m.group(1)))
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


# ------------------------------------------------------------------ Panoramica
def test_voided_receipt_excluded_from_income():
    c, sid = fresh()
    mk_receipt(sid, 500)
    mk_receipt(sid, 1000, voided=True)
    h = get(c, "/dashboard")
    assert card_value(h, "Incasso") == "5,00 €"
    assert "15,00 €" not in h
    assert card_value(h, "Scontrini") == "1"
    assert card_value(h, "Scontrino medio") == "5,00 €"


def test_counts_and_average():
    c, sid = fresh()
    mk_receipt(sid, 1000)
    mk_receipt(sid, 2000)
    mk_receipt(sid, 3000)
    h = get(c, "/dashboard")
    assert card_value(h, "Incasso") == "60,00 €"
    assert card_value(h, "Scontrini") == "3"
    assert card_value(h, "Scontrino medio") == "20,00 €"


def test_new_shop_empty_state():
    c, _sid = fresh()
    h = get(c, "/dashboard")
    assert "Benvenuto" in h and "Prova uno scontrino" in h and "Collega la cassa" in h
    assert "0 su 4 completati" in h
    assert 'class="stat-label"' not in h            # niente KPI
    assert 'action="/dashboard/test-receipt"' in h
    assert 'href="/dashboard/integration"' in h and 'href="/dashboard/settings"' in h
    assert 'aria-label="Periodo"' not in h           # niente schede periodo


def test_new_shop_checklist_progress():
    c, sid = fresh()
    with app.app_context():
        db = appmod.get_db()
        db.execute("UPDATE shops SET review_url = ? WHERE id = ?", ("https://g.page/r/abc", sid))
        db.commit()
    assert "1 su 4 completati" in get(c, "/dashboard")


def test_test_receipt_leaves_empty_state():
    c, _sid = fresh()
    h = c.get("/dashboard").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', h).group(1)
    r = c.post("/dashboard/test-receipt", data={"csrf": csrf})
    assert r.status_code == 302 and "/r/" in r.headers["Location"]
    h = get(c, "/dashboard")
    assert "Benvenuto" not in h and card_value(h, "Scontrini") == "1"
    assert card_value(h, "Incasso") == "46,00 €"      # 2x14 + 3 + 2x5,50 + 2x2


def test_periods():
    c, sid = fresh()
    mk_receipt(sid, 500)                    # oggi
    mk_receipt(sid, 1000, days_ago=3)       # 7d
    mk_receipt(sid, 2000, days_ago=20)      # 30d
    mk_receipt(sid, 4000, days_ago=60)      # 90d
    expected = {"today": ("5,00 €", "1"), "7d": ("15,00 €", "2"), "30d": ("35,00 €", "3"), "90d": ("75,00 €", "4")}
    for period, (tot, cnt) in expected.items():
        h = get(c, f"/dashboard?period={period}")
        assert card_value(h, "Incasso") == tot, period
        assert card_value(h, "Scontrini") == cnt, period
        assert re.search(r'<a class="tab is-active" href="/dashboard\?period=' + period + r'" aria-current="page"', h)


@pytest.mark.parametrize("bad", ["", "boh", "all", "7D", "1y", "-1", "%00", "30d;x", "a" * 500, "today&period=7d"])
def test_unknown_period_falls_back_to_today(bad):
    c, sid = fresh()
    mk_receipt(sid, 500)
    mk_receipt(sid, 1000, days_ago=3)
    r = c.get("/dashboard", query_string={"period": bad})
    assert r.status_code == 200
    h = r.get_data(as_text=True)
    assert card_value(h, "Incasso") == "5,00 €"
    assert re.search(r'<a class="tab is-active" href="/dashboard\?period=today"', h)


def test_card_links_per_period():
    c, sid = fresh()
    mk_receipt(sid, 500)
    stats_for = {"today": "7d", "7d": "7d", "30d": "30d", "90d": "90d"}
    for period, sp in stats_for.items():
        h = get(c, f"/dashboard?period={period}")
        for label in ("Incasso", "Scontrini", "Aperti dal cliente"):
            assert card_href(h, label) == ("/dashboard/receipts", {"period": period, "status": "valid"}), (period, label)
        assert card_href(h, "Scontrino medio") == ("/dashboard/stats", {"period": sp})
        assert card_href(h, "Messaggi inviati") == ("/dashboard/messages", {"period": period, "status": "sent"})
        assert card_href(h, "Fatture da evadere") == ("/dashboard/invoices", {"status": "all"})


def test_period_tabs_links():
    c, sid = fresh()
    mk_receipt(sid, 500)
    h = get(c, "/dashboard")
    for p in ("today", "7d", "30d", "90d"):
        assert f'href="/dashboard?period={p}"' in h


def test_pending_invoices_card_and_todo():
    c, sid = fresh()
    r1 = mk_receipt(sid, 500)
    r2 = mk_receipt(sid, 700)
    r3 = mk_receipt(sid, 900, voided=True)
    r4 = mk_receipt(sid, 300)
    mk_invoice(r1)
    mk_invoice(r2)
    mk_invoice(r3)                       # scontrino annullato: non conta
    mk_invoice(r4, status="evasa")       # già evasa: non conta
    h = get(c, "/dashboard")
    assert card_value(h, "Fatture da evadere") == "2"
    assert card_href(h, "Fatture da evadere") == ("/dashboard/invoices", {"status": "nuova"})
    assert "2 fatture da evadere" in h
    assert re.search(r'href="/dashboard/invoices\?status=nuova"', h)
    assert "Tutto in ordine" not in h


def test_only_voided_invoice_not_pending():
    c, sid = fresh()
    mk_invoice(mk_receipt(sid, 900, voided=True, days_ago=3))  # annullato, ma non di oggi
    h = get(c, "/dashboard")
    assert card_value(h, "Fatture da evadere") == "0"
    assert not re.search(r"\d+ fattur[ae] da evadere", h) and "Tutto in ordine" in h


def test_todo_failed_messages_and_voided_today():
    c, sid = fresh()
    r1 = mk_receipt(sid, 500)
    mk_receipt(sid, 800, voided=True)
    mk_message(r1, "failed", error="Numero non valido")
    mk_message(r1, "failed", error="timeout")
    h = get(c, "/dashboard")
    assert "2 messaggi non consegnati" in h
    assert 'href="/dashboard/messages?status=failed&amp;period=30d"' in h
    assert "1 scontrino annullato oggi" in h
    assert 'href="/dashboard/receipts?period=today&amp;status=void"' in h
    assert "Tutto in ordine" not in h


def test_messages_sent_counted_only_in_period():
    c, sid = fresh()
    r1 = mk_receipt(sid, 500)
    mk_message(r1, "sent")
    mk_message(r1, "sent", days_ago=3)
    mk_message(r1, "failed")            # non è un invio riuscito
    assert card_value(get(c, "/dashboard?period=today"), "Messaggi inviati") == "1"
    assert card_value(get(c, "/dashboard?period=7d"), "Messaggi inviati") == "2"


def test_opened_percentage_ignores_voided():
    c, sid = fresh()
    mk_receipt(sid, 500, views=2)
    mk_receipt(sid, 500)
    mk_receipt(sid, 500, views=5, voided=True)
    h = get(c, "/dashboard")
    assert card_value(h, "Aperti dal cliente") == "50%"
    assert "1 su 2 scontrini" in h


def test_top_dishes_exclude_voided_and_isolated():
    c, sid = fresh()
    other, osid = fresh()
    mk_receipt(sid, 1500, item="Lasagna")
    mk_receipt(sid, 9900, item="PiattoAnnullato", voided=True)
    mk_receipt(osid, 7700, item="PiattoAltrui")
    h = get(c, "/dashboard")
    assert "Lasagna" in h
    assert "PiattoAnnullato" not in h and "PiattoAltrui" not in h
    assert "77,00 €" not in h


def test_isolation_between_shops():
    a, asid = fresh()
    b, bsid = fresh()
    mk_receipt(asid, 500)
    mk_receipt(bsid, 99900)
    assert card_value(get(a, "/dashboard"), "Incasso") == "5,00 €"
    assert card_value(get(b, "/dashboard"), "Incasso") == "999,00 €"


def test_chart_bars_link_to_day_and_recent_list():
    c, sid = fresh()
    rid = mk_receipt(sid, 500, table="Tavolo 9")
    h = get(c, "/dashboard")
    days = re.findall(r'href="/dashboard/receipts\?day=(\d{4}-\d{2}-\d{2})"', h)
    assert len(set(days)) == 7
    assert f'href="/dashboard/receipts/{rid}"' in h and "Tavolo 9" in h
    h30 = get(c, "/dashboard?period=30d")
    assert len(set(re.findall(r'href="/dashboard/receipts\?day=(\d{4}-\d{2}-\d{2})"', h30))) == 30


def test_recent_list_shows_only_six_and_voided_badge():
    c, sid = fresh()
    for i in range(1, 9):
        mk_receipt(sid, 100 * i, voided=(i == 8))
    h = get(c, "/dashboard")
    recent = h.split('id="ov-recent-h"')[1]
    assert len(re.findall(r'<td><a href="/dashboard/receipts/\d+"><strong>N\. \d+', recent)) == 6
    assert "N. 8" in recent and "N. 3" in recent and "N. 2</strong>" not in recent
    assert "annullato" in recent


# ------------------------------------------------------------------ Trasversali
@pytest.fixture(scope="module")
def world():
    c, sid = fresh("Mondo")
    r1 = mk_receipt(sid, 500, table="T1")
    r2 = mk_receipt(sid, 700, voided=True)
    mk_invoice(r1)
    mk_message(r1)
    with app.app_context():
        db = appmod.get_db()
        db.execute("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?)", (sid, "+393331234567", iso(0)))
        inv = db.execute("SELECT id FROM invoice_requests WHERE receipt_id = ?", (r1,)).fetchone()["id"]
        db.commit()
    return c, {"r1": r1, "r2": r2, "inv": inv}


PAGES = [
    ("/dashboard", "Panoramica"), ("/dashboard/receipts", "Scontrini"), ("/dashboard/receipts/{r1}", "Scontrini"),
    ("/dashboard/receipts/{r2}", "Scontrini"), ("/dashboard/invoices", "Fatture"),
    ("/dashboard/invoices/{inv}", "Fatture"), ("/dashboard/contacts", "Clienti"),
    ("/dashboard/messages", "Messaggi"), ("/dashboard/stats", "Statistiche"),
    ("/dashboard/integration", "Integrazione"), ("/dashboard/settings", "Impostazioni"),
]
NAV_LABELS = ["Panoramica", "Scontrini", "Fatture", "Clienti", "Messaggi", "Statistiche", "Integrazione",
              "Impostazioni"]


@pytest.mark.parametrize("path,active", PAGES)
def test_page_ok_logged_in_and_redirect_anonymous(world, path, active):
    client, ids = world
    path = path.format(**ids)
    assert client.get(path).status_code == 200
    anon = app.test_client()
    r = anon.get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


@pytest.mark.parametrize("path,active", PAGES)
def test_sidebar_and_csp_rules(world, path, active):
    client, ids = world
    h = get(client, path.format(**ids))
    nav = re.search(r'<nav class="nav">(.*?)</nav>', h, re.S).group(1)
    for label in NAV_LABELS:
        assert f"<span>{label}</span>" in nav, label
    current = re.findall(r'<a class="nav-link is-active" href="[^"]+" aria-current="page">\s*<svg.*?</svg>\s*<span>([^<]+)</span>',
                         nav, re.S)
    assert current == [active]
    assert nav.count('aria-current="page"') == 1
    assert 'style="' not in h and "style='" not in h and "<style" not in h.lower()
    assert not re.search(r"<script(?![^>]*\ssrc=)", h, re.I)
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", h, re.I)      # niente gestori onclick= ecc.
    assert not re.search(r'(?:\ssrc="https?:|<link[^>]+href="https?:)', h, re.I)   # nessuna risorsa esterna


def test_csv_exports_require_login():
    anon = app.test_client()
    for p in ("/dashboard/receipts.csv", "/dashboard/invoices.csv", "/dashboard/contacts.csv"):
        r = anon.get(p)
        assert r.status_code == 302 and "/login" in r.headers["Location"], p


def test_csp_header_present(world):
    client, _ = world
    csp = client.get("/dashboard").headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp
