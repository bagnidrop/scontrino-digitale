"""Turni di cassa: apertura, chiusura, riepilogo, blocchi e isolamento."""
import itertools
import re

import pytest

import pos_service as pos
from testutil import appmod, make_config, register

app = appmod.create_app(make_config("pos_shifts.db"))
_n = itertools.count(1)


def tok(client, path="/dashboard/turni"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


@pytest.fixture
def shop():
    n = next(_n)
    email = f"shift{n}@example.com"
    client, _ = register(app, f"Turni {n}", email)
    with app.app_context():
        db = appmod.get_db()
        sid = db.execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
        pos.seed_default_menu(db, sid)
    return client, sid


def sell(sid, label="Tavolo 1", method="contanti", paid=True):
    with app.app_context():
        db = appmod.get_db()
        t = db.execute("SELECT id FROM pos_tables WHERE shop_id = ? AND label = ?", (sid, label)).fetchone()["id"]
        p = db.execute("SELECT id FROM pos_products WHERE shop_id = ? AND name = 'Margherita'", (sid,)).fetchone()["id"]
        oid = pos.open_order(db, sid, t, covers=2)
        pos.add_item(db, sid, oid, p, 2)
        if paid:
            pos.customer_reply(db, pos.request_bill(db, sid, oid), True)
            pos.add_payment(db, sid, oid, method, 1600)
            pos.close_order(db, sid, oid, appmod.insert_receipt)
        return oid


def test_open_twice_and_summary_and_close(shop):
    client, sid = shop
    assert client.post("/dashboard/turni/apri", data={"csrf": tok(client), "opening_cash": "100,00"}).status_code == 302
    client.post("/dashboard/turni/apri", data={"csrf": tok(client), "opening_cash": "5"})
    with app.app_context():
        assert appmod.get_db().execute("SELECT COUNT(*) FROM pos_shifts WHERE shop_id = ?", (sid,)).fetchone()[0] == 1
    sell(sid, "Tavolo 1", "contanti")
    sell(sid, "Tavolo 2", "carta")
    page = client.get("/dashboard/turni").get_data(as_text=True)
    assert "32,00" in page and "116,00" in page  # incassato; contanti attesi = 100 + 16
    # conto aperto: non si chiude
    oid = sell(sid, "Tavolo 3", paid=False)
    client.post("/dashboard/turni/chiudi", data={"csrf": tok(client), "closing_cash": "116"})
    with app.app_context():
        assert appmod.get_db().execute("SELECT closed_at FROM pos_shifts WHERE shop_id = ?", (sid,)).fetchone()[0] is None
        pos.cancel_order(appmod.get_db(), sid, oid)
    r = client.post("/dashboard/turni/chiudi", data={"csrf": tok(client), "closing_cash": "115,00", "note": "ok"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "mancano soldi" in body and "Contanti contati" in body
    assert "Apri il turno" in client.get("/dashboard/turni").get_data(as_text=True)  # turno chiuso: se ne può aprire un altro


def test_bad_amount_and_close_without_shift(shop):
    client, _ = shop
    r = client.post("/dashboard/turni/apri", data={"csrf": tok(client), "opening_cash": "abc"}, follow_redirects=True)
    assert "Importo non valido" in r.get_data(as_text=True)
    r = client.post("/dashboard/turni/chiudi", data={"csrf": tok(client), "closing_cash": "1"}, follow_redirects=True)
    assert "nessun turno aperto" in r.get_data(as_text=True)


def test_csrf_and_isolation(shop):
    client, sid = shop
    assert client.post("/dashboard/turni/apri", data={"opening_cash": "1"}).status_code == 400
    client.post("/dashboard/turni/apri", data={"csrf": tok(client), "opening_cash": "50"})
    with app.app_context():
        shift_id = appmod.get_db().execute("SELECT id FROM pos_shifts WHERE shop_id = ?", (sid,)).fetchone()["id"]
    other, _ = register(app, "Altro", f"other{next(_n)}@example.com")
    page = other.get(f"/dashboard/turni?id={shift_id}").get_data(as_text=True)
    assert "Turno del" not in page and "Apri il turno" in page
