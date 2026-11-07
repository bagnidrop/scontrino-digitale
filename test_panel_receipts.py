"""Test della sezione Scontrini: elenco, filtri, paginazione, dettaglio, annullo/ripristino, CSV, isolamento."""
import csv
import io
import itertools
import random
import re
import string
from datetime import datetime, timedelta, timezone

import pytest

from testutil import appmod, make_config, csrf_of, register

app = appmod.create_app(make_config("panel_receipts.db"))
_n = itertools.count(1)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PER_PAGE = 30


def iso(days_ago=0, minutes_ago=0):
    return (NOW - timedelta(days=days_ago, minutes=minutes_ago)).isoformat()


def fresh(name="Locale"):
    n = next(_n)
    email = f"rc{n}@example.com"
    client, key = register(app, f"{name} {n}", email)
    with app.app_context():
        sid = appmod.get_db().execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
    return client, sid


def mk(sid, total=1000, number=None, payment="Carta", table="", days_ago=0, minutes_ago=0, voided=False, views=0,
       item="Piatto", lottery=""):
    token = "".join(random.choices(string.ascii_lowercase, k=14))   # solo lettere: la ricerca numerica non lo incrocia
    with app.app_context():
        db = appmod.get_db()
        if number is None:
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        rid = db.insert(
            "INSERT INTO receipts (shop_id, token, number, total_cents, vat_cents, payment_method, table_label,"
            " lottery_code, views, created_at, voided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, token, number, total, 91, payment, table, lottery, views, iso(days_ago, minutes_ago),
             iso(0) if voided else None))
        db.execute("INSERT INTO receipt_items (receipt_id, name, qty, unit_price_cents, vat_rate, line_total_cents,"
                   " vat_cents) VALUES (?,?,?,?,?,?,?)", (rid, item, 1, total, 10, total, 91))
        db.commit()
    return rid


def q1(sql, params=()):
    with app.app_context():
        return appmod.get_db().execute(sql, params).fetchone()


def get(client, path, **qs):
    r = client.get(path, query_string=qs)
    assert r.status_code == 200, (path, qs, r.status_code)
    return r.get_data(as_text=True)


def numbers(h):
    """Numeri degli scontrini mostrati nelle righe dell'elenco, nell'ordine."""
    return [int(x) for x in re.findall(r'<a class="rc-num" href="/dashboard/receipts/\d+">n\. (\d+)</a>', h)]


def card_value(h, label):
    m = re.search(r'<span class="stat-label">' + re.escape(label) + r'</span>.*?<span class="stat-value">([^<]*)</span>',
                  h, re.S)
    assert m, label
    return m.group(1).strip()


def tab_counts(h):
    return {label: int(n) for label, n in re.findall(
        r'<a class="tab[^"]*" href="[^"]+"[^>]*>\s*(Tutti|Validi|Annullati)<span class="tab-count">(\d+)</span>', h)}


def csrf(client):
    return csrf_of(client, "/dashboard/settings")


@pytest.fixture()
def shop():
    """Locale con 4 scontrini noti: n.1 Carta 10€ Sala, n.2 Contanti 20€ Terrazza, n.3 Carta 30€ Veranda ANNULLATO,
    n.4 Carta 5€ senza tavolo (piatto Tiramisù, lotteria)."""
    c, sid = fresh()
    ids = [
        mk(sid, 1000, table="Sala", days_ago=10),
        mk(sid, 2000, payment="Contanti", table="Terrazza", days_ago=3),
        mk(sid, 3000, table="Veranda", days_ago=2, voided=True),
        mk(sid, 500, table="", days_ago=0, item="Tiramisù", lottery="XYZLOTT"),
    ]
    return c, sid, ids


# ------------------------------------------------------------------ elenco e filtri
def test_default_list_shows_everything_newest_first(shop):
    c, _sid, _ids = shop
    h = get(c, "/dashboard/receipts")
    assert numbers(h) == [4, 3, 2, 1]
    assert tab_counts(h) == {"Tutti": 4, "Validi": 3, "Annullati": 1}
    assert card_value(h, "Incassato") == "35,00 €"          # 10 + 20 + 5, l'annullato (30) escluso
    assert card_value(h, "Scontrini") == "4"
    assert card_value(h, "Annullati") == "1"


def test_status_filters(shop):
    c, *_ = shop
    valid = get(c, "/dashboard/receipts", status="valid")
    assert numbers(valid) == [4, 2, 1] and card_value(valid, "Scontrini") == "3"
    assert card_value(valid, "Incassato") == "35,00 €"
    void = get(c, "/dashboard/receipts", status="void")
    assert numbers(void) == [3] and card_value(void, "Scontrini") == "1"
    assert card_value(void, "Incassato") == "0,00 €"
    assert "Ripristina" in void and ">Annulla<" not in void.replace("> Annulla<", ">Annulla<")
    assert numbers(get(c, "/dashboard/receipts", status="all")) == [4, 3, 2, 1]


@pytest.mark.parametrize("bad", ["", "boh", "VALID", "nuova", "1; DROP TABLE receipts"])
def test_invalid_status_means_all(shop, bad):
    c, *_ = shop
    assert numbers(get(c, "/dashboard/receipts", status=bad)) == [4, 3, 2, 1]


def test_payment_filter(shop):
    c, *_ = shop
    h = get(c, "/dashboard/receipts", payment="Contanti")
    assert numbers(h) == [2]
    assert "Contanti" in h and card_value(h, "Incassato") == "20,00 €"
    carta = get(c, "/dashboard/receipts", payment="Carta")
    assert numbers(carta) == [4, 3, 1]
    assert numbers(get(c, "/dashboard/receipts", payment="Bitcoin")) == []
    # le opzioni del menu sono i metodi realmente usati
    opts = re.findall(r'<option value="([^"]*)"', carta.split('name="payment"')[1].split("</select>")[0])
    assert opts == ["", "Carta", "Contanti"]


def test_search_by_number_and_table_case_insensitive(shop):
    c, *_ = shop
    assert numbers(get(c, "/dashboard/receipts", q="2")) == [2]
    assert numbers(get(c, "/dashboard/receipts", q="#2")) == [2]
    assert numbers(get(c, "/dashboard/receipts", q="terrazza")) == [2]
    assert numbers(get(c, "/dashboard/receipts", q="TERRAZZA")) == [2]
    assert numbers(get(c, "/dashboard/receipts", q="TeRRaZ")) == [2]
    assert numbers(get(c, "/dashboard/receipts", q="sala")) == [1]
    assert numbers(get(c, "/dashboard/receipts", q="  veranda ")) == [3]
    assert numbers(get(c, "/dashboard/receipts", q="nessuno-così")) == []


def test_search_by_item_and_lottery_case_insensitive(shop):
    c, *_ = shop
    assert numbers(get(c, "/dashboard/receipts", q="TIRAMISÙ")) == [4]
    assert numbers(get(c, "/dashboard/receipts", q="xyzlot")) == [4]


def test_search_does_not_match_inside_the_link_code_unless_it_looks_like_one(shop):
    """Cercare «ab» o una cifra non deve trovare scontrini solo perché il loro codice del link lo contiene."""
    c, sid, _ = shop
    rid = mk(sid, number=99)
    token = q1("SELECT token FROM receipts WHERE id = ?", (rid,))["token"]
    assert numbers(get(c, "/dashboard/receipts", q=token[3:6])) == []        # frammento corto: nessun risultato
    assert numbers(get(c, "/dashboard/receipts", q=token)) == [99]           # codice intero incollato: lo trova


def test_search_wildcards_are_literal(shop):
    c, *_ = shop
    assert numbers(get(c, "/dashboard/receipts", q="%")) == []
    assert numbers(get(c, "/dashboard/receipts", q="_")) == []


def test_search_text_is_escaped_in_page(shop):
    c, *_ = shop
    h = get(c, "/dashboard/receipts", q="<script>alert(1)</script>")
    assert "<script>alert(1)" not in h
    assert "Nessuno scontrino con questi filtri" in h


def test_search_combined_with_status_and_chip(shop):
    c, *_ = shop
    h = get(c, "/dashboard/receipts", q="veranda", status="valid")
    assert numbers(h) == []
    h = get(c, "/dashboard/receipts", q="veranda", status="void")
    assert numbers(h) == [3]
    assert "Cerca: veranda" in h and "Solo annullati" in h


def test_period_filter(shop):
    c, *_ = shop
    assert numbers(get(c, "/dashboard/receipts", period="today")) == [4]
    assert numbers(get(c, "/dashboard/receipts", period="7d")) == [4, 3, 2]
    assert numbers(get(c, "/dashboard/receipts", period="30d")) == [4, 3, 2, 1]
    assert numbers(get(c, "/dashboard/receipts", period="90d")) == [4, 3, 2, 1]
    assert numbers(get(c, "/dashboard/receipts", period="all")) == [4, 3, 2, 1]
    h = get(c, "/dashboard/receipts", period="7d")
    assert "Ultimi 7 giorni" in h and card_value(h, "Incassato") == "25,00 €"      # 5 + 20, l'annullato (30) escluso


@pytest.mark.parametrize("bad", ["", "boh", "yesterday", "7", "<b>"])
def test_invalid_period_means_all_here(shop, bad):
    c, *_ = shop
    assert numbers(get(c, "/dashboard/receipts", period=bad)) == [4, 3, 2, 1]


def test_day_filter(shop):
    c, _sid, ids = shop
    with app.app_context():
        created = appmod.get_db().execute("SELECT created_at FROM receipts WHERE id = ?", (ids[1],)).fetchone()[0]
    day = appmod.fmt_dt(created, "%Y-%m-%d")
    h = get(c, "/dashboard/receipts", day=day)
    assert numbers(h) == [2]
    assert f'name="day" value="{day}"' in h
    # il giorno vince sul periodo
    assert numbers(get(c, "/dashboard/receipts", day=day, period="today")) == [2]
    assert numbers(get(c, "/dashboard/receipts", day="2001-01-01")) == []
    for bad in ("boh", "2026-13-45", "31/10/2026", "2026-02-30"):
        assert numbers(get(c, "/dashboard/receipts", day=bad)) == [4, 3, 2, 1], bad


def test_chips_and_clear_link(shop):
    c, *_ = shop
    h = get(c, "/dashboard/receipts", status="valid", payment="Carta", period="7d")
    for chip in ("Solo validi", "Carta", "Ultimi 7 giorni"):
        assert chip in h
    assert 'href="/dashboard/receipts">Azzera tutto' in h
    assert "Azzera tutto" not in get(c, "/dashboard/receipts")


def test_tab_and_card_links_keep_filters(shop):
    c, *_ = shop
    h = get(c, "/dashboard/receipts", payment="Carta")
    assert 'href="/dashboard/receipts?payment=Carta&amp;status=valid"' in h
    assert 'href="/dashboard/receipts?payment=Carta&amp;status=void"' in h
    assert 'href="/dashboard/receipts.csv?payment=Carta"' in h


def test_filters_dont_leak_other_shop(shop):
    c, *_ = shop
    other, osid = fresh()
    mk(osid, 99900, table="Sala", payment="Carta")
    h = get(c, "/dashboard/receipts", q="sala")
    assert numbers(h) == [1] and "999,00" not in h
    assert "999,00" not in get(c, "/dashboard/receipts")


def test_row_content(shop):
    c, _sid, ids = shop
    h = get(c, "/dashboard/receipts")
    assert f'data-href="/dashboard/receipts/{ids[1]}"' in h
    assert "20,00 €" in h and "Terrazza" in h and "Senza tavolo" in h
    row3 = re.search(r'<tr data-href="/dashboard/receipts/%d" class="is-void">.*?</tr>' % ids[2], h, re.S).group(0)
    assert "annullato" in row3 and "Ripristina" in row3 and "Annulla</button>" not in row3
    row1 = re.search(r'<tr data-href="/dashboard/receipts/%d"[^>]*>.*?</tr>' % ids[0], h, re.S).group(0)
    assert "Annulla</button>" in row1 and "Ripristina" not in row1


def test_empty_states():
    c, sid = fresh()
    h = get(c, "/dashboard/receipts")
    assert "Ancora nessuno scontrino" in h and "/dashboard/integration" in h and numbers(h) == []
    assert card_value(h, "Scontrini") == "0" and card_value(h, "Incassato") == "0,00 €"
    mk(sid, 500)
    h = get(c, "/dashboard/receipts", q="zzz")
    assert "Nessuno scontrino con questi filtri" in h and "Ancora nessuno scontrino" not in h
    assert 'href="/dashboard/receipts">Azzera i filtri' in h


# ------------------------------------------------------------------ paginazione
def test_pagination():
    c, sid = fresh()
    for i in range(65):
        mk(sid, 100, minutes_ago=100 - i)           # n. 65 è il più recente
    p1 = get(c, "/dashboard/receipts")
    assert numbers(p1) == list(range(65, 35, -1)) and len(numbers(p1)) == PER_PAGE
    assert "Pagina 1 di 3" in p1 and "Successiva" in p1 and "Precedente" not in p1
    p2 = get(c, "/dashboard/receipts", page=2)
    assert numbers(p2) == list(range(35, 5, -1)) and "Pagina 2 di 3" in p2 and "Precedente" in p2
    p3 = get(c, "/dashboard/receipts", page=3)
    assert numbers(p3) == [5, 4, 3, 2, 1] and "Pagina 3 di 3" in p3 and "Successiva" not in p3
    assert card_value(p3, "Scontrini") == "65"


def test_pagination_exact_page_size_and_single_page():
    c, sid = fresh()
    for i in range(PER_PAGE):
        mk(sid, 100)
    h = get(c, "/dashboard/receipts")
    assert len(numbers(h)) == PER_PAGE and "Pagina 1 di" not in h
    mk(sid, 100)
    assert "Pagina 1 di 2" in get(c, "/dashboard/receipts")


@pytest.mark.parametrize("page", ["999", "0", "-4", "abc", "", "2.5", "99999999999999999999"])
def test_page_out_of_range_no_error(page):
    c, sid = fresh()
    for i in range(35):
        mk(sid, 100)
    r = c.get("/dashboard/receipts", query_string={"page": page})
    assert r.status_code == 200
    n = numbers(r.get_data(as_text=True))
    assert 1 <= len(n) <= PER_PAGE


def test_page_beyond_last_shows_last_page():
    c, sid = fresh()
    for i in range(35):
        mk(sid, 100)
    h = get(c, "/dashboard/receipts", page=50)
    assert "Pagina 2 di 2" in h and len(numbers(h)) == 5


def test_pager_keeps_filters():
    c, sid = fresh()
    for i in range(40):
        mk(sid, 100, payment="Carta")
    mk(sid, 100, payment="Contanti")
    h = get(c, "/dashboard/receipts", payment="Carta", status="valid")
    assert "Pagina 1 di 2" in h
    nxt = re.search(r'href="([^"]+)">Successiva', h).group(1).replace("&amp;", "&")
    assert "payment=Carta" in nxt and "status=valid" in nxt and "page=2" in nxt
    h2 = get(c, nxt.split("?")[0], **dict(x.split("=") for x in nxt.split("?")[1].split("&")))
    assert len(numbers(h2)) == 10


# ------------------------------------------------------------------ dettaglio
def test_detail_valid(shop):
    c, _sid, ids = shop
    h = get(c, f"/dashboard/receipts/{ids[1]}")
    assert "Scontrino n. 2" in h and "20,00 €" in h and "Contanti" in h
    assert "Piatto" in h and "IVA 10%" in h
    assert '<span class="badge badge-ok">valido</span>' in h
    assert "Annulla scontrino" in h and "Ripristina" not in h and "Scontrino annullato il" not in h
    assert f'action="/dashboard/receipts/{ids[1]}/void"' in h
    assert f'action="/dashboard/receipts/{ids[1]}/send"' in h          # modulo di invio presente
    assert f'/dashboard/receipts/{ids[0]}"' in h and f'/dashboard/receipts/{ids[2]}"' in h   # precedente e successivo
    assert re.search(r'href="http://[^"]+/r/[a-z]{14}"', h)             # link pubblico
    assert "Scontrino creato" in h


def test_detail_voided(shop):
    c, _sid, ids = shop
    h = get(c, f"/dashboard/receipts/{ids[2]}")
    assert "Scontrino n. 3" in h and "30,00 €" in h
    assert "Scontrino annullato il" in h and '<span class="badge badge-danger">annullato</span>' in h
    assert "Ripristina" in h and "Annulla scontrino" not in h
    assert f'action="/dashboard/receipts/{ids[2]}/restore"' in h
    assert f'action="/dashboard/receipts/{ids[2]}/send"' not in h
    assert "ripristinalo per poterlo inviare" in h


def test_detail_timeline_invoice_and_messages(shop):
    c, _sid, ids = shop
    with app.app_context():
        db = appmod.get_db()
        db.execute("INSERT INTO invoice_requests (receipt_id, company_name, vat_number, sdi_code, address, email,"
                   " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (ids[0], "Pizzeria Srl", "12345678903", "ABCDEFG", "Via 1", "a@b.it", "nuova", iso(0)))
        db.execute("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at)"
                   " VALUES (?,?,?,?,?,?)", (ids[0], "sms", "+39 333 ••• 4567", "failed", "numero non valido", iso(0)))
        db.commit()
    h = get(c, f"/dashboard/receipts/{ids[0]}")
    assert "Richiesta di fattura" in h and "Pizzeria Srl" in h
    assert "Invio via SMS non riuscito" in h and "+39 333 ••• 4567" in h
    assert "Ancora 2 invii" in h


def test_detail_404_for_unknown_and_other_shop(shop):
    c, _sid, ids = shop
    assert c.get("/dashboard/receipts/99999999").status_code == 404
    other, osid = fresh()
    foreign = mk(osid, 12345)
    assert c.get(f"/dashboard/receipts/{foreign}").status_code == 404
    assert other.get(f"/dashboard/receipts/{ids[0]}").status_code == 404
    assert "123,45" not in c.get(f"/dashboard/receipts/{foreign}").get_data(as_text=True)


# ------------------------------------------------------------------ annulla / ripristina
def test_void_and_restore_from_list_with_next(shop):
    c, _sid, ids = shop
    t = csrf(c)
    nxt = "/dashboard/receipts?status=valid"
    r = c.post(f"/dashboard/receipts/{ids[0]}/void", data={"csrf": t, "next": nxt})
    assert r.status_code == 302 and r.headers["Location"].endswith(nxt)
    assert q1("SELECT voided_at FROM receipts WHERE id = ?", (ids[0],))[0] is not None
    h = c.get(nxt).get_data(as_text=True)
    assert numbers(h) == [4, 2]                                     # n.1 non è più tra i validi
    r = c.post(f"/dashboard/receipts/{ids[0]}/void", data={"csrf": t, "next": "/dashboard/receipts"},
               follow_redirects=True)
    h = r.get_data(as_text=True)
    assert "Scontrino n. 1 annullato." in h and "Ripristina" in h
    assert card_value(h, "Incassato") == "25,00 €"
    r = c.post(f"/dashboard/receipts/{ids[0]}/restore", data={"csrf": t, "next": "/dashboard/receipts"},
               follow_redirects=True)
    h = r.get_data(as_text=True)
    assert "Scontrino n. 1 ripristinato." in h
    assert q1("SELECT voided_at FROM receipts WHERE id = ?", (ids[0],))[0] is None
    assert card_value(h, "Incassato") == "35,00 €"


def test_void_and_restore_from_detail(shop):
    c, _sid, ids = shop
    t = csrf(c)
    back = f"/dashboard/receipts/{ids[1]}"
    r = c.post(f"{back}/void", data={"csrf": t, "next": back}, follow_redirects=True)
    h = r.get_data(as_text=True)
    assert r.request.path == back
    assert "Scontrino n. 2 annullato." in h and "Scontrino annullato il" in h and "Ripristina" in h
    r = c.post(f"{back}/restore", data={"csrf": t, "next": back}, follow_redirects=True)
    h = r.get_data(as_text=True)
    assert r.request.path == back
    assert "Scontrino n. 2 ripristinato." in h and "Annulla scontrino" in h and "Scontrino annullato il" not in h


def test_void_without_next_goes_back_to_page(shop):
    c, _sid, ids = shop
    t = csrf(c)
    r = c.post(f"/dashboard/receipts/{ids[0]}/void", data={"csrf": t, "page": "2"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/receipts?page=2")
    r = c.post(f"/dashboard/receipts/{ids[0]}/restore", data={"csrf": t})
    assert r.headers["Location"].endswith("/dashboard/receipts?page=1")


@pytest.mark.parametrize("evil", ["https://evil.example/x", "//evil.example", "/dashboard/settings",
                                  "/\\evil.example", "/dashboard/receipts\\@evil.example"])
def test_void_next_is_not_an_open_redirect(shop, evil):
    c, _sid, ids = shop
    r = c.post(f"/dashboard/receipts/{ids[0]}/void", data={"csrf": csrf(c), "next": evil})
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "evil" not in loc and "/dashboard/settings" not in loc


def test_void_affects_overview_income(shop):
    c, _sid, ids = shop
    c.post(f"/dashboard/receipts/{ids[3]}/void", data={"csrf": csrf(c)})
    h = get(c, "/dashboard", period="30d")
    assert card_value(h, "Incasso") == "30,00 €"        # 10 + 20


def test_void_restore_other_shop_404_and_untouched(shop):
    c, _sid, ids = shop
    other, osid = fresh()
    foreign = mk(osid, 777)
    t = csrf(c)
    assert c.post(f"/dashboard/receipts/{foreign}/void", data={"csrf": t}).status_code == 404
    assert q1("SELECT voided_at FROM receipts WHERE id = ?", (foreign,))[0] is None
    assert other.post(f"/dashboard/receipts/{ids[2]}/restore", data={"csrf": csrf(other)}).status_code == 404
    assert q1("SELECT voided_at FROM receipts WHERE id = ?", (ids[2],))[0] is not None
    assert c.post("/dashboard/receipts/99999999/void", data={"csrf": t}).status_code == 404


def test_missing_or_wrong_csrf_is_400(shop):
    c, _sid, ids = shop
    for action in ("void", "restore", "send"):
        assert c.post(f"/dashboard/receipts/{ids[0]}/{action}", data={}).status_code == 400
        assert c.post(f"/dashboard/receipts/{ids[0]}/{action}", data={"csrf": "sbagliato"}).status_code == 400
    assert q1("SELECT voided_at FROM receipts WHERE id = ?", (ids[0],))[0] is None


def test_anonymous_redirected_to_login(shop):
    _c, _sid, ids = shop
    anon = app.test_client()
    for path in ("/dashboard/receipts", f"/dashboard/receipts/{ids[0]}", "/dashboard/receipts.csv"):
        r = anon.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    t = csrf_of(anon, "/login")
    for action in ("void", "restore", "send"):
        r = anon.post(f"/dashboard/receipts/{ids[0]}/{action}", data={"csrf": t})
        assert r.status_code == 302 and "/login" in r.headers["Location"], action
    assert q1("SELECT voided_at FROM receipts WHERE id = ?", (ids[0],))[0] is None
    assert anon.post(f"/dashboard/receipts/{ids[0]}/void", data={}).status_code == 400


# ------------------------------------------------------------------ invio dal dettaglio
def test_send_masks_number_and_logs(shop):
    c, _sid, ids = shop
    r = c.post(f"/dashboard/receipts/{ids[0]}/send", data={"csrf": csrf(c), "phone": "333 1234567",
                                                          "channel": "sms"}, follow_redirects=True)
    h = r.get_data(as_text=True)
    assert "Scontrino inviato a" in h and "+39333" not in h
    assert "1234567" not in h.replace('placeholder="333 1234567"', "")       # il numero intero non compare mai
    assert q1("SELECT COUNT(*) FROM messages WHERE receipt_id = ? AND status = 'sent'", (ids[0],))[0] == 1


def test_send_without_consent_saves_no_contact(shop):
    c, sid, ids = shop
    c.post(f"/dashboard/receipts/{ids[0]}/send", data={"csrf": csrf(c), "phone": "3331234567"})
    assert q1("SELECT COUNT(*) FROM contacts WHERE shop_id = ?", (sid,))[0] == 0
    c.post(f"/dashboard/receipts/{ids[1]}/send", data={"csrf": csrf(c), "phone": "3331234567",
                                                      "marketing_consent": "1"})
    assert q1("SELECT COUNT(*) FROM contacts WHERE shop_id = ?", (sid,))[0] == 1


def test_send_rejects_bad_phone_voided_and_limit(shop):
    c, _sid, ids = shop
    t = csrf(c)
    h = c.post(f"/dashboard/receipts/{ids[0]}/send", data={"csrf": t, "phone": "abc"},
               follow_redirects=True).get_data(as_text=True)
    assert "non sembra valido" in h
    h = c.post(f"/dashboard/receipts/{ids[2]}/send", data={"csrf": t, "phone": "3331234567"},
               follow_redirects=True).get_data(as_text=True)
    assert "Scontrino annullato: non si può inviare" in h
    for i in range(3):
        c.post(f"/dashboard/receipts/{ids[1]}/send", data={"csrf": t, "phone": f"33312345{60 + i}"})
    h = c.post(f"/dashboard/receipts/{ids[1]}/send", data={"csrf": t, "phone": "3331234599"},
               follow_redirects=True).get_data(as_text=True)
    assert "massimo" in h
    assert q1("SELECT COUNT(*) FROM messages WHERE receipt_id = ?", (ids[1],))[0] == 3
    assert q1("SELECT COUNT(*) FROM messages WHERE receipt_id = ?", (ids[2],))[0] == 0


# ------------------------------------------------------------------ CSV
def parse_csv(resp):
    text = resp.get_data(as_text=True)
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:]), delimiter=";"))


def test_csv_export(shop):
    c, *_ = shop
    r = c.get("/dashboard/receipts.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "scontrini.csv" in r.headers["Content-Disposition"]
    rows = parse_csv(r)
    assert rows[0][:8] == ["Numero", "Data", "Ora", "Tavolo", "Pagamento", "Totale €", "IVA €", "Stato"]
    body = {row[0]: row for row in rows[1:]}
    assert sorted(body) == ["1", "2", "3", "4"]
    assert body["2"][3:8] == ["Terrazza", "Contanti", "20,00", "0,91", "valido"]
    assert body["3"][7] == "annullato" and body["4"][11] == "XYZLOTT"


def test_csv_respects_filters_and_shop(shop):
    c, *_ = shop
    other, osid = fresh()
    mk(osid, 5555, payment="Contanti")
    rows = parse_csv(c.get("/dashboard/receipts.csv?status=valid&payment=Contanti"))
    assert [r[0] for r in rows[1:]] == ["2"]
    rows = parse_csv(c.get("/dashboard/receipts.csv?status=void"))
    assert [r[0] for r in rows[1:]] == ["3"]
    assert "55,55" not in c.get("/dashboard/receipts.csv").get_data(as_text=True)


def test_csv_formula_injection_neutralised():
    c, sid = fresh()
    mk(sid, 100, table="=HYPERLINK(1)", payment="+cmd")
    rows = parse_csv(c.get("/dashboard/receipts.csv"))
    assert rows[1][3] == "'=HYPERLINK(1)" and rows[1][4] == "'+cmd"
