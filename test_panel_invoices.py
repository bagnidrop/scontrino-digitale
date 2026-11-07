"""Test della sezione Fatture: elenco, filtri, cambio stato, dettaglio, CSV, isolamento tra locali."""
import csv
import html
import io
import itertools
import random
import re
import string
from datetime import datetime, timedelta, timezone

import pytest

from testutil import VALID_PIVA, appmod, make_config, csrf_of, register

app = appmod.create_app(make_config("panel_invoices.db"))
_n = itertools.count(1)
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat()


def fresh(name="Locale"):
    n = next(_n)
    email = f"inv{n}@example.com"
    client, key = register(app, f"{name} {n}", email)
    with app.app_context():
        sid = appmod.get_db().execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
    return client, sid


def mk_receipt(sid, total=1000, voided=False, days_ago=0):
    token = "".join(random.choices(string.ascii_lowercase, k=14))
    with app.app_context():
        db = appmod.get_db()
        number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        rid = db.insert(
            "INSERT INTO receipts (shop_id, token, number, total_cents, vat_cents, payment_method, table_label,"
            " lottery_code, views, created_at, voided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, token, number, total, 0, "Carta", "", "", 0, iso(days_ago), iso(0) if voided else None))
        db.execute("INSERT INTO receipt_items (receipt_id, name, qty, unit_price_cents, vat_rate, line_total_cents,"
                   " vat_cents) VALUES (?,?,?,?,?,?,?)", (rid, "Piatto", 1, total, 10, total, 0))
        db.commit()
    return rid


def mk_inv(sid, company="ACME Srl", status="nuova", total=1000, voided=False, vat=VALID_PIVA, tax="", email="amm@acme.it",
           address="Via Roma 1, Milano", sdi="ABCDEFG", pec=""):
    rid = mk_receipt(sid, total, voided)
    with app.app_context():
        db = appmod.get_db()
        iid = db.insert("INSERT INTO invoice_requests (receipt_id, company_name, vat_number, tax_code, sdi_code, pec,"
                        " address, email, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, company, vat, tax, sdi, pec, address, email, status, iso(0)))
        db.commit()
    return iid, rid


def status_of(iid):
    with app.app_context():
        return appmod.get_db().execute("SELECT status FROM invoice_requests WHERE id = ?", (iid,)).fetchone()[0]


def get(client, path, **qs):
    r = client.get(path, query_string=qs)
    assert r.status_code == 200, (path, qs, r.status_code)
    return r.get_data(as_text=True)


def companies(h):
    return re.findall(r'<td class="inv-client">\s*<a href="/dashboard/invoices/\d+"><strong>([^<]*)</strong>', h)


def card_value(h, label):
    m = re.search(r'<span class="stat-label">' + re.escape(label) + r'</span>.*?<span class="stat-value">([^<]*)</span>',
                  h, re.S)
    assert m, label
    return m.group(1).strip()


def tab_counts(h):
    return {label: int(n) for label, n in re.findall(
        r'<a class="tab[^"]*" href="[^"]+"[^>]*>\s*([^<]+?)<span class="tab-count">(\d+)</span>', h)}


def csrf(client):
    return csrf_of(client, "/dashboard/settings")


def parse_csv(resp):
    text = resp.get_data(as_text=True)
    assert text.startswith("\ufeff")
    return list(csv.reader(io.StringIO(text[1:]), delimiter=";"))


@pytest.fixture()
def shop():
    """Locale con: Alfa (nuova, 10 €), Beta (nuova, 20 €), Gamma (evasa, 30 €), Delta (nuova su scontrino ANNULLATO, 40 €)."""
    c, sid = fresh()
    ids = {
        "alfa": mk_inv(sid, "Alfa Srl", "nuova", 1000, email="info@alfa.it", vat="12345678903"),
        "beta": mk_inv(sid, "Beta Snc", "nuova", 2000, email="beta@mail.it", vat="", tax="RSSMRA80A01H501U"),
        "gamma": mk_inv(sid, "Gamma Spa", "evasa", 3000, email="gamma@mail.it"),
        "delta": mk_inv(sid, "Delta Srl", "nuova", 4000, voided=True, email="delta@mail.it"),
    }
    return c, sid, ids


# ------------------------------------------------------------------ elenco, filtri, conteggi
def test_list_all_newest_first_and_counts(shop):
    c, *_ = shop
    h = get(c, "/dashboard/invoices")
    assert companies(h) == ["Delta Srl", "Gamma Spa", "Beta Snc", "Alfa Srl"]
    assert tab_counts(h) == {"Da evadere": 2, "Evase": 1, "Tutte": 4, "Scontrino annullato": 1}
    assert card_value(h, "Da evadere") == "2"
    assert card_value(h, "Evase") == "1"
    assert card_value(h, "Importo da fatturare") == "30,00 €"      # 10 + 20: Delta (annullato) non conta


def test_voided_invoice_not_counted_as_todo(shop):
    c, *_ = shop
    h = get(c, "/dashboard/invoices", status="nuova")
    assert companies(h) == ["Beta Snc", "Alfa Srl"]
    assert "Delta" not in h
    assert "1 richiesta riguarda scontrini annullati" in h


def test_status_filters(shop):
    c, *_ = shop
    assert companies(get(c, "/dashboard/invoices", status="evasa")) == ["Gamma Spa"]
    assert companies(get(c, "/dashboard/invoices", status="void")) == ["Delta Srl"]
    assert companies(get(c, "/dashboard/invoices", status="all")) == ["Delta Srl", "Gamma Spa", "Beta Snc", "Alfa Srl"]
    void = get(c, "/dashboard/invoices", status="void")
    assert "scontrino annullato" in void and "Non evadere" in void
    assert "riguarda scontrini annullati" not in void            # già nell'elenco degli annullati


@pytest.mark.parametrize("bad", ["", "boh", "NUOVA", "done", "1' OR '1'='1"])
def test_invalid_status_means_all(shop, bad):
    c, *_ = shop
    assert len(companies(get(c, "/dashboard/invoices", status=bad))) == 4


def test_search_q(shop):
    c, *_ = shop
    assert companies(get(c, "/dashboard/invoices", q="alfa")) == ["Alfa Srl"]
    assert companies(get(c, "/dashboard/invoices", q="ALFA")) == ["Alfa Srl"]
    assert companies(get(c, "/dashboard/invoices", q="  gamma ")) == ["Gamma Spa"]
    assert companies(get(c, "/dashboard/invoices", q="mail.it")) == ["Delta Srl", "Gamma Spa", "Beta Snc"]   # email
    assert companies(get(c, "/dashboard/invoices", q="rssmra80")) == ["Beta Snc"]                             # CF, minuscolo
    assert companies(get(c, "/dashboard/invoices", q="introvabile")) == []


def test_search_by_vat_number(shop):
    c, *_ = shop
    # Alfa, Gamma e Delta hanno la P.IVA di prova, Beta solo il codice fiscale
    assert sorted(companies(get(c, "/dashboard/invoices", q=VALID_PIVA))) == ["Alfa Srl", "Delta Srl", "Gamma Spa"]


def test_search_combined_with_status_updates_counts(shop):
    c, *_ = shop
    h = get(c, "/dashboard/invoices", status="nuova", q="mail.it")
    assert companies(h) == ["Beta Snc"]
    assert tab_counts(h)["Da evadere"] == 1 and tab_counts(h)["Evase"] == 1
    assert 'name="q" value="mail.it"' in h and 'name="status" value="nuova"' in h
    assert 'href="/dashboard/invoices?status=nuova&amp;q=mail.it"' in h


def test_search_text_is_escaped(shop):
    c, *_ = shop
    h = get(c, "/dashboard/invoices", q='"><script>alert(1)</script>')
    assert "<script>alert(1)" not in h
    assert "Nessuna richiesta trovata" in h


def test_company_name_is_escaped():
    c, sid = fresh()
    mk_inv(sid, company="<b>Evil</b> & Co")
    h = get(c, "/dashboard/invoices")
    assert "<b>Evil</b>" not in h and "&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in h


def test_row_content_and_links(shop):
    c, _sid, ids = shop
    iid, rid = ids["beta"]
    h = get(c, "/dashboard/invoices")
    row = re.search(r'<tr data-href="/dashboard/invoices/%d"[^>]*>.*?</tr>' % iid, h, re.S).group(0)
    assert "Beta Snc" in row and "RSSMRA80A01H501U" in row and "ABCDEFG" in row and "beta@mail.it" in row
    assert "20,00 €" in row and "Da evadere" in row and "Segna come evasa" in row and "Copia dati" in row
    assert f'href="/dashboard/receipts/{rid}"' in row
    giid, _ = ids["gamma"]
    row = re.search(r'<tr data-href="/dashboard/invoices/%d"[^>]*>.*?</tr>' % giid, h, re.S).group(0)
    assert "Evasa" in row and "Riapri" in row and "Segna come evasa" not in row


def test_copy_block_has_billing_data(shop):
    c, _sid, ids = shop
    iid, _ = ids["alfa"]
    h = get(c, "/dashboard/invoices")
    pre = re.search(r'<pre class="inv-copy" id="cp-%d" hidden>(.*?)</pre>' % iid, h, re.S).group(1)
    assert "Ragione sociale: Alfa Srl" in pre and f"P.IVA: {VALID_PIVA}" in pre
    assert "Codice SDI: ABCDEFG" in pre and "Email: info@alfa.it" in pre and "Scontrino n. 1" in pre


def test_empty_states():
    c, sid = fresh()
    h = get(c, "/dashboard/invoices")
    assert "Nessuna richiesta di fattura per ora" in h and companies(h) == []
    assert 'class="stat-label"' not in h
    mk_inv(sid, "Solo Evasa", "evasa")
    h = get(c, "/dashboard/invoices", status="nuova")
    assert "Qui non c'è niente" in html.unescape(h) and companies(h) == []
    assert card_value(h, "Da evadere") == "0"
    h = get(c, "/dashboard/invoices", q="zzz")
    assert "Nessun risultato per «zzz»" in h


# ------------------------------------------------------------------ cambio stato
def test_toggle_status_with_next(shop):
    c, _sid, ids = shop
    iid, _ = ids["alfa"]
    t = csrf(c)
    r = c.post(f"/dashboard/invoices/{iid}/status", data={"csrf": t, "next": "/dashboard/invoices?status=nuova"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/invoices?status=nuova")
    assert status_of(iid) == "evasa"
    h = c.get("/dashboard/invoices?status=nuova").get_data(as_text=True)
    assert companies(h) == ["Beta Snc"] and "Fattura per Alfa Srl segnata come evasa." in h
    assert card_value(get(c, "/dashboard/invoices"), "Importo da fatturare") == "20,00 €"
    r = c.post(f"/dashboard/invoices/{iid}/status", data={"csrf": t, "next": "/dashboard/invoices"},
               follow_redirects=True)
    assert status_of(iid) == "nuova"
    assert "Richiesta di Alfa Srl riaperta." in r.get_data(as_text=True)
    assert card_value(r.get_data(as_text=True), "Da evadere") == "2"


def test_toggle_from_detail_page(shop):
    c, _sid, ids = shop
    iid, _ = ids["beta"]
    path = f"/dashboard/invoices/{iid}"
    h = get(c, path)
    assert "Segna come evasa" in h and "Da evadere" in h
    r = c.post(f"{path}/status", data={"csrf": csrf(c), "next": path}, follow_redirects=True)
    assert r.request.path == path
    h = r.get_data(as_text=True)
    assert "segnata come evasa" in h and "Riapri" in h and "Segna come evasa" not in h
    assert '<span class="badge badge-ok">Evasa</span>' in h


def test_toggle_without_next_goes_to_list(shop):
    c, _sid, ids = shop
    r = c.post(f"/dashboard/invoices/{ids['alfa'][0]}/status", data={"csrf": csrf(c)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/invoices")


@pytest.mark.parametrize("evil", ["https://evil.example", "//evil.example", "/dashboard/settings", "/\\evil.example",
                                  "/dashboard/invoices\\evil"])
def test_toggle_next_is_not_an_open_redirect(shop, evil):
    c, _sid, ids = shop
    r = c.post(f"/dashboard/invoices/{ids['alfa'][0]}/status", data={"csrf": csrf(c), "next": evil})
    assert r.status_code == 302
    assert "evil" not in r.headers["Location"] and "settings" not in r.headers["Location"]


def test_voided_new_invoice_cannot_be_marked_done(shop):
    c, _sid, ids = shop
    iid, _ = ids["delta"]
    r = c.post(f"/dashboard/invoices/{iid}/status", data={"csrf": csrf(c)}, follow_redirects=True)
    assert "la fattura non va evasa" in r.get_data(as_text=True)
    assert status_of(iid) == "nuova"
    detail = get(c, f"/dashboard/invoices/{iid}")
    assert "Non evadere" in detail and "Segna come evasa" not in detail


def test_voiding_receipt_removes_invoice_from_todo(shop):
    c, _sid, ids = shop
    _iid, rid = ids["alfa"]
    assert card_value(get(c, "/dashboard/invoices"), "Da evadere") == "2"
    c.post(f"/dashboard/receipts/{rid}/void", data={"csrf": csrf(c)})
    h = get(c, "/dashboard/invoices")
    assert card_value(h, "Da evadere") == "1" and card_value(h, "Importo da fatturare") == "20,00 €"
    assert 'class="nav-badge" title="Da evadere">1<' in h
    c.post(f"/dashboard/receipts/{rid}/restore", data={"csrf": csrf(c)})
    assert card_value(get(c, "/dashboard/invoices"), "Da evadere") == "2"


def test_sidebar_badge_counts_only_valid_new(shop):
    c, *_ = shop
    h = get(c, "/dashboard/receipts")
    assert 'class="nav-badge" title="Da evadere">2<' in h


def test_status_csrf_missing_is_400_and_unchanged(shop):
    c, _sid, ids = shop
    iid = ids["alfa"][0]
    assert c.post(f"/dashboard/invoices/{iid}/status", data={}).status_code == 400
    assert c.post(f"/dashboard/invoices/{iid}/status", data={"csrf": "x"}).status_code == 400
    assert status_of(iid) == "nuova"


# ------------------------------------------------------------------ dettaglio
def test_detail_content(shop):
    c, _sid, ids = shop
    iid, rid = ids["beta"]
    h = get(c, f"/dashboard/invoices/{iid}")
    assert "Beta Snc" in h and "RSSMRA80A01H501U" in h and "ABCDEFG" in h and "beta@mail.it" in h
    assert "Via Roma 1, Milano" in h and "20,00 €" in h and "n. 2 del" in h
    assert f'href="/dashboard/receipts/{rid}"' in h
    assert "è stato annullato" not in h


def test_detail_voided_warns(shop):
    c, _sid, ids = shop
    iid, rid = ids["delta"]
    h = get(c, f"/dashboard/invoices/{iid}")
    assert "Lo scontrino n. 4 è stato annullato." in h and "annullato</span>" in h


def test_detail_404(shop):
    c, _sid, ids = shop
    assert c.get("/dashboard/invoices/99999999").status_code == 404


# ------------------------------------------------------------------ CSV
def test_csv_export(shop):
    c, *_ = shop
    r = c.get("/dashboard/invoices.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "richieste-fattura.csv" in r.headers["Content-Disposition"]
    rows = parse_csv(r)
    assert rows[0] == ["Data richiesta", "Scontrino n.", "Data scontrino", "Totale €", "Ragione sociale", "P.IVA",
                       "Codice fiscale", "Codice SDI", "PEC", "Indirizzo", "Email", "Stato"]
    by_name = {row[4]: row for row in rows[1:]}
    assert sorted(by_name) == ["Alfa Srl", "Beta Snc", "Delta Srl", "Gamma Spa"]
    alfa = by_name["Alfa Srl"]
    assert alfa[1] == "1" and alfa[3] == "10,00" and alfa[5] == VALID_PIVA and alfa[7] == "ABCDEFG"
    assert alfa[9] == "Via Roma 1, Milano" and alfa[10] == "info@alfa.it" and alfa[11] == "nuova"
    assert by_name["Gamma Spa"][11] == "evasa"
    assert by_name["Delta Srl"][11] == "nuova (scontrino annullato)"
    assert by_name["Beta Snc"][6] == "RSSMRA80A01H501U"
    assert re.fullmatch(r"\d\d/\d\d/\d{4} \d\d:\d\d", alfa[0])


def test_csv_formula_injection_is_neutralised():
    c, sid = fresh()
    mk_inv(sid, company="=HYPERLINK(\"http://x\")", address="+39 Via Roma", email="@evil.it", vat="", tax="-1+1")
    mk_inv(sid, company="Normale Srl", address="Via=Roma 3")
    rows = {r[4]: r for r in parse_csv(c.get("/dashboard/invoices.csv"))[1:]}
    evil = rows["'=HYPERLINK(\"http://x\")"]
    assert evil[9] == "'+39 Via Roma" and evil[10] == "'@evil.it" and evil[6] == "'-1+1"
    for r in rows.values():
        assert not any(cell[:1] in ("=", "+", "@") for cell in r)
    assert rows["Normale Srl"][9] == "Via=Roma 3"       # solo l'inizio della cella è pericoloso


def test_csv_empty_has_only_header():
    c, _sid = fresh()
    assert len(parse_csv(c.get("/dashboard/invoices.csv"))) == 1


# ------------------------------------------------------------------ isolamento e accessi
def test_shops_are_isolated(shop):
    c, _sid, ids = shop
    other, osid = fresh()
    oiid, orid = mk_inv(osid, "Concorrente Spa", "nuova", 98765, email="spy@rival.it")
    # il primo locale non vede nulla dell'altro
    for h in (get(c, "/dashboard/invoices"), get(c, "/dashboard/invoices", q="rival"), c.get("/dashboard/invoices.csv").get_data(as_text=True)):
        assert "Concorrente" not in h and "987,65" not in h and "98765" not in h
    assert companies(get(c, "/dashboard/invoices", q="rival")) == []
    assert card_value(get(c, "/dashboard/invoices"), "Da evadere") == "2"
    # e viceversa
    h = get(other, "/dashboard/invoices")
    assert companies(h) == ["Concorrente Spa"] and "Alfa" not in h and card_value(h, "Da evadere") == "1"
    # dettaglio e cambio stato dell'altro locale: 404, nulla cambia
    assert c.get(f"/dashboard/invoices/{oiid}").status_code == 404
    assert c.post(f"/dashboard/invoices/{oiid}/status", data={"csrf": csrf(c)}).status_code == 404
    assert status_of(oiid) == "nuova"
    assert other.get(f"/dashboard/invoices/{ids['alfa'][0]}").status_code == 404
    assert other.post(f"/dashboard/invoices/{ids['alfa'][0]}/status", data={"csrf": csrf(other)}).status_code == 404
    assert status_of(ids["alfa"][0]) == "nuova"
    # la fattura altrui non incide sul badge del menu
    assert 'title="Da evadere">2<' in get(c, "/dashboard/invoices")


def test_anonymous_redirected(shop):
    _c, _sid, ids = shop
    anon = app.test_client()
    iid = ids["alfa"][0]
    for path in ("/dashboard/invoices", f"/dashboard/invoices/{iid}", "/dashboard/invoices.csv"):
        r = anon.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    r = anon.post(f"/dashboard/invoices/{iid}/status", data={"csrf": csrf_of(anon, "/login")})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert status_of(iid) == "nuova"


def test_end_to_end_request_from_customer_page():
    """Il cliente invia la richiesta dalla pagina pubblica: compare tra le fatture da evadere del locale."""
    c, key = register(app, "Trattoria E2E", "e2e@example.com")
    s = c.post("/api/v1/receipts", headers={"X-API-Key": key}, json={
        "items": [{"name": "Pizza", "qty": 2, "unit_price": "7.50", "vat_rate": 10}]}).get_json()
    cust = app.test_client()
    form = {"csrf": csrf_of(cust, f"/r/{s['token']}"), "company_name": "Cliente Spa", "vat_number": VALID_PIVA,
            "sdi_code": "ABCDEFG", "address": "Via Po 3, Torino", "email": "amm@cliente.it"}
    assert cust.post(f"/r/{s['token']}/invoice", data=form).status_code == 302
    h = get(c, "/dashboard/invoices")
    assert companies(h) == ["Cliente Spa"] and card_value(h, "Importo da fatturare") == "15,00 €"
    assert 'title="Da evadere">1<' in h
    c.post(f"/dashboard/receipts/{s['id']}/void", data={"csrf": csrf(c)})
    h = get(c, "/dashboard/invoices")
    assert card_value(h, "Da evadere") == "0" and "title=\"Da evadere\"" not in h
