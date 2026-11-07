"""Test delle sezioni Clienti (contatti con consenso marketing) e Messaggi (registro invii)."""
import csv
import html as htmllib
import io
import itertools
import random
import re
import string
from datetime import datetime, timedelta, timezone

import pytest

from testutil import appmod, make_config, csrf_of, register

app = appmod.create_app(make_config("panel_clients_messages.db"))
_n = itertools.count(1)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CT_PER_PAGE = 25
MSG_PER_PAGE = 25


def iso(days_ago=0, minutes_ago=0):
    return (NOW - timedelta(days=days_ago, minutes=minutes_ago)).isoformat()


def fresh(name="Locale"):
    n = next(_n)
    email = f"cm{n}@example.com"
    client, key = register(app, f"{name} {n}", email)
    with app.app_context():
        sid = appmod.get_db().execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
    return client, sid


def mk_receipt(sid, voided=False):
    token = "".join(random.choices(string.ascii_lowercase, k=14))
    with app.app_context():
        db = appmod.get_db()
        number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        rid = db.insert(
            "INSERT INTO receipts (shop_id, token, number, total_cents, vat_cents, payment_method, table_label,"
            " lottery_code, views, created_at, voided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, token, number, 1000, 0, "Carta", "", "", 0, iso(0), iso(0) if voided else None))
        db.commit()
    return rid, number


def mk_contact(sid, phone, days_ago=0, minutes_ago=0):
    with app.app_context():
        db = appmod.get_db()
        cid = db.insert("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?)",
                        (sid, phone, iso(days_ago, minutes_ago)))
        db.commit()
    return cid


def mk_msg(rid, status="sent", channel="whatsapp", days_ago=0, minutes_ago=0, phone="+39•••••567", error=None):
    with app.app_context():
        db = appmod.get_db()
        mid = db.insert("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at)"
                        " VALUES (?,?,?,?,?,?)", (rid, channel, phone, status, error, iso(days_ago, minutes_ago)))
        db.commit()
    return mid


def q1(sql, params=()):
    with app.app_context():
        return appmod.get_db().execute(sql, params).fetchone()


def get(client, path, **qs):
    r = client.get(path, query_string=qs)
    assert r.status_code == 200, (path, qs, r.status_code)
    return r.get_data(as_text=True)


def csrf(client):
    return csrf_of(client, "/dashboard/settings")


def card_value(h, label):
    m = re.search(r'<span class="stat-label">' + re.escape(label) + r'</span>.*?<span class="stat-value">([^<]*)</span>',
                  h, re.S)
    assert m, label
    return m.group(1).strip()


def parse_csv(resp):
    text = resp.get_data(as_text=True)
    assert text.startswith("\ufeff")
    return list(csv.reader(io.StringIO(text[1:]), delimiter=";"))


# ================================================================== CLIENTI
def phones(h):
    return re.findall(r'<a class="ct-phone mono" href="tel:([^"]+)"', h)


@pytest.fixture()
def clients():
    c, sid = fresh()
    ids = [mk_contact(sid, "+393331111111", days_ago=60),
           mk_contact(sid, "+393332222222", days_ago=10),
           mk_contact(sid, "+393473333333", days_ago=1),
           mk_contact(sid, "+393204444444", days_ago=0)]
    return c, sid, ids


def test_contacts_list_newest_first_with_consent_date(clients):
    c, *_ = clients
    h = get(c, "/dashboard/contacts")
    assert phones(h) == ["+393204444444", "+393473333333", "+393332222222", "+393331111111"]
    assert appmod.fmt_dt(iso(60)) in h and appmod.fmt_dt(iso(1)) in h       # data del consenso
    assert "4 clienti" in h
    assert card_value(h, "Clienti con consenso") == "4"
    assert card_value(h, "Nuovi, ultimi 30 giorni") == "3"


def test_contacts_cards_links_and_sent_count(clients):
    c, sid, _ = clients
    rid, _n = mk_receipt(sid)
    mk_msg(rid, "sent")
    mk_msg(rid, "sent", "sms")
    mk_msg(rid, "failed")
    h = get(c, "/dashboard/contacts")
    assert card_value(h, "Scontrini inviati") == "2"
    assert 'href="/dashboard/contacts?new=30"' in h
    assert 'href="/dashboard/messages?status=sent&amp;period=30d"' in h
    assert 'href="/dashboard/contacts.csv"' in h


def test_contacts_recent_filter(clients):
    c, *_ = clients
    h = get(c, "/dashboard/contacts", new="30")
    assert phones(h) == ["+393204444444", "+393473333333", "+393332222222"]
    assert "Solo i nuovi degli ultimi 30 giorni" in h
    assert card_value(h, "Clienti con consenso") == "4"          # le card mostrano sempre i totali


def test_contacts_search(clients):
    c, *_ = clients
    assert phones(get(c, "/dashboard/contacts", q="2222")) == ["+393332222222"]
    assert phones(get(c, "/dashboard/contacts", q="+39 347")) == ["+393473333333"]       # spazi e + ignorati
    assert phones(get(c, "/dashboard/contacts", q="320")) == ["+393204444444"]
    assert phones(get(c, "/dashboard/contacts", q="99999")) == []
    h = get(c, "/dashboard/contacts", q="2222")
    assert "1 cliente per «2222»" in h and 'name="q" value="2222"' in h


def test_contacts_search_digits_are_substring(clients):
    c, *_ = clients
    assert sorted(phones(get(c, "/dashboard/contacts", q="333"))) == sorted(
        ["+393331111111", "+393332222222", "+393473333333"])      # "333" compare anche in 3473333333


def test_contacts_search_without_digits_finds_nothing(clients):
    c, *_ = clients
    h = get(c, "/dashboard/contacts", q="mario")
    assert phones(h) == [] and "Nessun cliente trovato" in h


def test_contacts_search_with_recent_filter(clients):
    c, *_ = clients
    assert phones(get(c, "/dashboard/contacts", q="1111", new="30")) == []
    assert phones(get(c, "/dashboard/contacts", q="2222", new="30")) == ["+393332222222"]


def test_contacts_search_text_escaped(clients):
    c, *_ = clients
    h = get(c, "/dashboard/contacts", q="<script>alert(1)</script>")
    assert "<script>alert(1)" not in h


@pytest.mark.parametrize("page", ["999", "0", "-2", "abc", "", "99999999999999999999"])
def test_contacts_page_out_of_range(clients, page):
    c, *_ = clients
    r = c.get("/dashboard/contacts", query_string={"page": page})
    assert r.status_code == 200 and len(phones(r.get_data(as_text=True))) == 4


def test_contacts_pagination():
    c, sid = fresh()
    for i in range(60):
        mk_contact(sid, f"+39333{i:07d}", minutes_ago=100 - i)      # +39333 0000059 è il più recente
    p1 = get(c, "/dashboard/contacts")
    assert len(phones(p1)) == CT_PER_PAGE and phones(p1)[0] == "+393330000059" and "Pagina 1 di 3" in p1
    p2 = get(c, "/dashboard/contacts", page=2)
    assert len(phones(p2)) == CT_PER_PAGE and phones(p2)[0] == "+393330000034" and "Pagina 2 di 3" in p2
    p3 = get(c, "/dashboard/contacts", page=3)
    assert phones(p3) == [f"+39333{i:07d}" for i in range(9, -1, -1)]
    assert "Successiva" not in p3 and "Precedente" in p3
    assert phones(get(c, "/dashboard/contacts", page=50)) == phones(p3)
    assert "60 clienti" in p1


def test_contacts_pager_keeps_search():
    c, sid = fresh()
    for i in range(30):
        mk_contact(sid, f"+39333{i:07d}")
    h = get(c, "/dashboard/contacts", q="333")
    nxt = re.search(r'href="([^"]+)">Successiva', h).group(1).replace("&amp;", "&")
    assert "q=333" in nxt and "page=2" in nxt


def test_contacts_empty_state():
    c, _sid = fresh()
    h = get(c, "/dashboard/contacts")
    assert "Ancora nessun cliente nell'elenco" in htmllib.unescape(h)
    assert phones(h) == [] and "Esporta CSV" not in h
    assert card_value(h, "Clienti con consenso") == "0"
    assert 'href="/dashboard/integration"' in h


def test_contacts_isolated_between_shops(clients):
    c, sid, ids = clients
    other, osid = fresh()
    oid = mk_contact(osid, "+393999999999")
    assert "999999999" not in get(c, "/dashboard/contacts")
    assert "999999999" not in c.get("/dashboard/contacts.csv").get_data(as_text=True)
    assert phones(get(c, "/dashboard/contacts", q="9999")) == []
    assert phones(get(other, "/dashboard/contacts")) == ["+393999999999"]
    assert card_value(get(other, "/dashboard/contacts"), "Clienti con consenso") == "1"
    # eliminazione cross-locale: 404 e il numero resta
    assert c.post(f"/dashboard/contacts/{oid}/delete", data={"csrf": csrf(c)}).status_code == 404
    assert q1("SELECT COUNT(*) FROM contacts WHERE id = ?", (oid,))[0] == 1
    assert other.post(f"/dashboard/contacts/{ids[0]}/delete", data={"csrf": csrf(other)}).status_code == 404
    assert q1("SELECT COUNT(*) FROM contacts WHERE id = ?", (ids[0],))[0] == 1


def test_contact_delete(clients):
    c, sid, ids = clients
    r = c.post(f"/dashboard/contacts/{ids[1]}/delete", data={"csrf": csrf(c), "next": "/dashboard/contacts?q=333"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/contacts?q=333")
    assert q1("SELECT COUNT(*) FROM contacts WHERE id = ?", (ids[1],))[0] == 0
    assert q1("SELECT COUNT(*) FROM contacts WHERE shop_id = ?", (sid,))[0] == 3
    h = c.get("/dashboard/contacts").get_data(as_text=True)
    assert "+393332222222" not in h and "Numero eliminato" in h
    assert card_value(h, "Clienti con consenso") == "3"


def test_contact_delete_form_in_list(clients):
    c, _sid, ids = clients
    h = get(c, "/dashboard/contacts")
    assert f'action="/dashboard/contacts/{ids[0]}/delete"' in h and 'name="csrf"' in h
    assert "data-confirm=" in h


def test_contact_delete_without_next_goes_to_list(clients):
    c, _sid, ids = clients
    r = c.post(f"/dashboard/contacts/{ids[0]}/delete", data={"csrf": csrf(c)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/contacts")


@pytest.mark.parametrize("evil", ["https://evil.example", "//evil.example", "/dashboard/settings", "/\\evil.example"])
def test_contact_delete_next_not_open_redirect(clients, evil):
    c, _sid, ids = clients
    r = c.post(f"/dashboard/contacts/{ids[0]}/delete", data={"csrf": csrf(c), "next": evil})
    assert r.status_code == 302 and "evil" not in r.headers["Location"] and "settings" not in r.headers["Location"]


def test_contact_delete_csrf_and_login(clients):
    c, _sid, ids = clients
    assert c.post(f"/dashboard/contacts/{ids[0]}/delete", data={}).status_code == 400
    assert c.post(f"/dashboard/contacts/{ids[0]}/delete", data={"csrf": "x"}).status_code == 400
    anon = app.test_client()
    r = anon.post(f"/dashboard/contacts/{ids[0]}/delete", data={"csrf": csrf_of(anon, "/login")})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.post("/dashboard/contacts/99999999/delete", data={"csrf": csrf(c)}).status_code == 404
    assert q1("SELECT COUNT(*) FROM contacts WHERE id = ?", (ids[0],))[0] == 1


def test_contacts_anonymous_redirected(clients):
    anon = app.test_client()
    for p in ("/dashboard/contacts", "/dashboard/contacts.csv"):
        r = anon.get(p)
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_contacts_csv_structure_and_filters(clients):
    c, *_ = clients
    r = c.get("/dashboard/contacts.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "clienti-consenso-marketing.csv" in r.headers["Content-Disposition"]
    rows = parse_csv(r)
    assert rows[0] == ["Numero", "Consenso marketing del"]
    assert len(rows) == 5
    assert re.fullmatch(r"\d\d/\d\d/\d{4} \d\d:\d\d", rows[1][1])
    assert rows[1][1] == appmod.fmt_dt(iso(0))
    assert len(parse_csv(c.get("/dashboard/contacts.csv?new=30"))) == 4
    assert len(parse_csv(c.get("/dashboard/contacts.csv?q=2222"))) == 2
    assert len(parse_csv(c.get("/dashboard/contacts.csv?q=zzz"))) == 1


def test_contacts_csv_phone_numbers_are_exported_unaltered(clients):
    c, *_ = clients
    numbers = [r[0] for r in parse_csv(c.get("/dashboard/contacts.csv"))[1:]]
    assert numbers == ["+393204444444", "+393473333333", "+393332222222", "+393331111111"]


def test_contacts_csv_formula_injection_neutralised():
    c, sid = fresh()
    mk_contact(sid, "=cmd|' /C calc'!A0")
    mk_contact(sid, "@SUM(1)")
    mk_contact(sid, "-2+3")
    cells = [r[0] for r in parse_csv(c.get("/dashboard/contacts.csv"))[1:]]
    assert sorted(cells) == sorted(["'=cmd|' /C calc'!A0", "'@SUM(1)", "'-2+3"])


# ================================================================== MESSAGGI
def msg_rows(h):
    """Righe del registro: (canale, numero mostrato, esito, numero scontrino)."""
    out = []
    for tr in re.findall(r'<tr data-href="/dashboard/receipts/\d+"[^>]*>(.*?)</tr>', h, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        chan = "WhatsApp" if "WhatsApp" in tds[1] else ("SMS" if "SMS" in tds[1] else "?")
        outcome = "sent" if "Inviato" in tds[3] else ("failed" if "Non partito" in tds[3] else "?")
        out.append((chan, re.sub(r"<[^>]+>", "", tds[2]).strip(), outcome, int(re.search(r"n\. (\d+)", tds[4]).group(1))))
    return out


@pytest.fixture()
def msgs():
    """Messaggi del locale (numero scontrino tra parentesi):
    m1 whatsapp sent oggi (n.1) | m2 sms sent 3 giorni fa (n.2) | m3 whatsapp failed 10 giorni fa (n.3, annullato)
    m4 sms failed 60 giorni fa (n.4)."""
    c, sid = fresh()
    r1, _ = mk_receipt(sid)
    r2, _ = mk_receipt(sid)
    r3, _ = mk_receipt(sid, voided=True)
    r4, _ = mk_receipt(sid)
    mk_msg(r1, "sent", "whatsapp", 0, phone="+39•••••567")
    mk_msg(r2, "sent", "sms", 3, phone="+39•••••111")
    mk_msg(r3, "failed", "whatsapp", 10, phone="+39•••••222", error="Numero non valido (21211)")
    mk_msg(r4, "failed", "sms", 60, phone="+39•••••333", error="Errore di rete: timed out")
    return c, sid


def test_messages_default_period_30_days(msgs):
    c, _ = msgs
    h = get(c, "/dashboard/messages")
    assert msg_rows(h) == [("WhatsApp", "+39•••••567", "sent", 1), ("SMS", "+39•••••111", "sent", 2),
                           ("WhatsApp", "+39•••••222", "failed", 3)]
    assert card_value(h, "Inviati") == "2" and card_value(h, "Non partiti") == "1"
    assert card_value(h, "WhatsApp") == "2" and card_value(h, "SMS") == "1"
    assert re.search(r'<a class="tab is-active" href="/dashboard/messages\?period=30d"', h)


def test_messages_periods(msgs):
    c, _ = msgs
    assert [r[3] for r in msg_rows(get(c, "/dashboard/messages", period="today"))] == [1]
    assert [r[3] for r in msg_rows(get(c, "/dashboard/messages", period="7d"))] == [1, 2]
    assert [r[3] for r in msg_rows(get(c, "/dashboard/messages", period="30d"))] == [1, 2, 3]
    assert [r[3] for r in msg_rows(get(c, "/dashboard/messages", period="90d"))] == [1, 2, 3, 4]
    h = get(c, "/dashboard/messages", period="all")
    assert [r[3] for r in msg_rows(h)] == [1, 2, 3, 4]
    assert card_value(h, "Inviati") == "2" and card_value(h, "Non partiti") == "2"


def test_messages_status_filter(msgs):
    c, _ = msgs
    sent = get(c, "/dashboard/messages", status="sent", period="all")
    assert [r[3] for r in msg_rows(sent)] == [1, 2]
    failed = get(c, "/dashboard/messages", status="failed", period="all")
    assert [r[3] for r in msg_rows(failed)] == [3, 4]
    assert card_value(failed, "Inviati") == "2"            # le card non seguono il filtro esito


def test_messages_channel_filter(msgs):
    c, _ = msgs
    wa = get(c, "/dashboard/messages", channel="whatsapp", period="all")
    assert [(r[0], r[3]) for r in msg_rows(wa)] == [("WhatsApp", 1), ("WhatsApp", 3)]
    sms = get(c, "/dashboard/messages", channel="sms", period="all")
    assert [(r[0], r[3]) for r in msg_rows(sms)] == [("SMS", 2), ("SMS", 4)]
    assert '<option value="sms" selected>' in sms


def test_messages_filters_combined(msgs):
    c, _ = msgs
    h = get(c, "/dashboard/messages", status="failed", channel="sms", period="all")
    assert [r[3] for r in msg_rows(h)] == [4]
    h = get(c, "/dashboard/messages", status="sent", channel="whatsapp", period="7d")
    assert [r[3] for r in msg_rows(h)] == [1]
    h = get(c, "/dashboard/messages", status="failed", channel="sms", period="7d")
    assert msg_rows(h) == [] and "Nessun messaggio con questi filtri" in h


def test_messages_filter_links_keep_state(msgs):
    c, _ = msgs
    h = get(c, "/dashboard/messages", status="failed", channel="sms", period="7d")
    assert 'href="/dashboard/messages?period=7d&amp;status=sent&amp;channel=sms"' in h
    assert 'href="/dashboard/messages?period=30d&amp;status=failed&amp;channel=sms"' in h
    assert 'href="/dashboard/messages?period=7d&amp;status=failed&amp;channel=whatsapp"' in h
    assert 'name="period" value="7d"' in h and 'name="status" value="failed"' in h


@pytest.mark.parametrize("qs", [
    {"status": "boh"}, {"channel": "telegram"}, {"period": "yesterday"}, {"status": "", "channel": "", "period": ""},
    {"page": "abc"}, {"page": "-1"}, {"page": "0"}, {"page": "9999"}, {"status": "sent' OR 1=1--"},
    {"channel": "<script>"}, {"period": "a" * 300}, {"status": ["sent", "failed"]},
])
def test_messages_invalid_values_never_500(msgs, qs):
    c, _ = msgs
    r = c.get("/dashboard/messages", query_string=qs)
    assert r.status_code == 200
    assert "<script>" not in r.get_data(as_text=True)


def test_messages_invalid_values_fall_back_to_defaults(msgs):
    c, _ = msgs
    h = get(c, "/dashboard/messages", status="boh", channel="telegram", period="yesterday")
    assert [r[3] for r in msg_rows(h)] == [1, 2, 3]            # tutti, ultimi 30 giorni
    assert re.search(r'<a class="tab is-active" href="/dashboard/messages\?period=30d', h)


def test_messages_error_is_explained_in_plain_italian(msgs):
    c, _ = msgs
    h = get(c, "/dashboard/messages", period="all")
    assert "Il numero non sembra valido" in h                       # 21211
    assert "Problema di connessione" in h                           # timed out
    assert "Dettaglio: Numero non valido (21211)" in h
    ok = get(c, "/dashboard/messages", status="sent")
    assert "Il numero non sembra valido" not in ok and "Dettaglio" not in ok


def test_messages_voided_receipt_badge_and_link(msgs):
    c, sid = msgs
    h = get(c, "/dashboard/messages", status="failed")
    assert "n. 3" in h and "annullato" in h
    rid = q1("SELECT id FROM receipts WHERE shop_id = ? AND number = 3", (sid,))[0]
    assert f'data-href="/dashboard/receipts/{rid}"' in h


def test_messages_phone_always_masked_in_list():
    """Invio vero dal pannello: nel registro compare solo il numero mascherato, mai quello intero."""
    c, sid = fresh()
    rid, _ = mk_receipt(sid)
    for phone, ch in (("333 1234567", "whatsapp"), ("+39 347 7654321", "sms"), ("3205550123", "sms")):
        r = c.post(f"/dashboard/receipts/{rid}/send", data={"csrf": csrf(c), "phone": phone, "channel": ch})
        assert r.status_code == 302
    h = get(c, "/dashboard/messages", period="all")
    rows = msg_rows(h)
    assert len(rows) == 3
    for digits in ("3331234567", "3477654321", "3205550123", "1234567", "7654321", "5550123"):
        assert digits not in h
    for _ch, shown, _st, _n in rows:
        assert "•" in shown and not re.fullmatch(r"\+?\d[\d ]{8,}", shown)
    with app.app_context():
        stored = [r[0] for r in appmod.get_db().execute(
            "SELECT phone_masked FROM messages WHERE receipt_id = ?", (rid,)).fetchall()]
    assert len(stored) == 3
    for st in stored:
        assert "•" in st and not re.search(r"\d{7}", st)        # nel database mai il numero intero


def test_messages_masked_numbers_never_expand_for_odd_values():
    c, sid = fresh()
    rid, _ = mk_receipt(sid)
    mk_msg(rid, phone="+39•••••567")
    h = get(c, "/dashboard/messages")
    assert "+39•••••567" in h


def test_messages_pagination():
    c, sid = fresh()
    rid, _ = mk_receipt(sid)
    for i in range(60):
        mk_msg(rid, phone=f"+39•••{i:04d}", minutes_ago=100 - i)       # …0059 è il più recente
    p1 = msg_rows(get(c, "/dashboard/messages"))
    assert len(p1) == MSG_PER_PAGE and p1[0][1] == "+39•••0059"
    h2 = get(c, "/dashboard/messages", page=2)
    assert len(msg_rows(h2)) == MSG_PER_PAGE and msg_rows(h2)[0][1] == "+39•••0034" and "Pagina 2 di 3" in h2
    h3 = get(c, "/dashboard/messages", page=3)
    assert len(msg_rows(h3)) == 10 and msg_rows(h3)[-1][1] == "+39•••0000"
    assert msg_rows(get(c, "/dashboard/messages", page=77)) == msg_rows(h3)
    nxt = re.search(r'href="([^"]+)">Successiva', get(c, "/dashboard/messages", status="sent", channel="whatsapp")).group(1)
    assert "status=sent" in nxt and "channel=whatsapp" in nxt and "page=2" in nxt


def test_messages_empty_state():
    c, sid = fresh()
    h = get(c, "/dashboard/messages")
    assert "Nessun messaggio in questo periodo" in h and msg_rows(h) == []
    assert 'href="/dashboard/messages?period=all"' in h
    assert card_value(h, "Inviati") == "0" and card_value(h, "Non partiti") == "0"
    h = get(c, "/dashboard/messages", period="all")
    assert "Scopri come inviare" in h and 'href="/dashboard/integration"' in h
    rid, _ = mk_receipt(sid)
    mk_msg(rid, "sent", "sms", 90)
    h = get(c, "/dashboard/messages")
    assert "Nessun messaggio in questo periodo" in h and card_value(h, "Inviati") == "0"
    assert [r[3] for r in msg_rows(get(c, "/dashboard/messages", period="all"))] == [1]


def test_messages_isolated_between_shops(msgs):
    c, _ = msgs
    other, osid = fresh()
    rid, _ = mk_receipt(osid)
    mk_msg(rid, "failed", "sms", 0, phone="+39•••ALTRUI")
    assert "ALTRUI" not in get(c, "/dashboard/messages", period="all")
    assert card_value(get(c, "/dashboard/messages"), "Non partiti") == "1"
    h = get(other, "/dashboard/messages")
    assert [r[1] for r in msg_rows(h)] == ["+39•••ALTRUI"] and card_value(h, "Inviati") == "0"


def test_messages_anonymous_redirected():
    r = app.test_client().get("/dashboard/messages")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
