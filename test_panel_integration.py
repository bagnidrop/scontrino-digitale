import re

from testutil import appmod, make_config, register, seed_receipts, csrf_of


def _app(name):
    return appmod.create_app(make_config(name))


def test_new_shop_waiting_and_requires_login():
    app = _app("in1.db")
    anon = app.test_client()
    assert anon.get("/dashboard/integration").status_code == 302
    c, key = register(app, "Trattoria Uno", "uno@test.it")
    r = c.get("/dashboard/integration")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "In attesa" in html and "Nessuno" in html
    assert "/api/v1/receipts" in html and "LA_TUA_API_KEY" in html
    assert 'data-copy="#in-ex-create-curl"' in html
    assert key not in html  # la chiave intera non compare mai
    assert key[:8] in html  # solo il prefisso
    assert 'href="/dashboard/settings"' in html and 'href="/dashboard/messages"' in html
    assert 'action="/dashboard/test-receipt"' in html


def test_connected_after_real_receipt_and_test_receipt_alone_is_not():
    app = _app("in2.db")
    c, key = register(app, "Trattoria Due", "due@test.it")
    c.post("/dashboard/test-receipt", data={"csrf": csrf_of(c, "/dashboard/integration")})
    html = c.get("/dashboard/integration").get_data(as_text=True)
    assert "In attesa" in html and "Lo scontrino di prova funziona" in html
    seed_receipts(c, key, [dict(price="12.00", table="T1", payment="Contanti")])
    html = c.get("/dashboard/integration").get_data(as_text=True)
    assert "Collegata" in html and "In attesa" not in html


def test_data_isolated_between_shops():
    app = _app("in3.db")
    c1, k1 = register(app, "Uno", "a@test.it")
    c2, _ = register(app, "Due", "b@test.it")
    seed_receipts(c1, k1, [dict(price="5.00"), dict(price="6.00")])
    h2 = c2.get("/dashboard/integration").get_data(as_text=True)
    assert "In attesa" in h2 and "Collegata" not in h2
    h1 = c1.get("/dashboard/integration").get_data(as_text=True)
    assert "Collegata" in h1


def test_voided_receipts_do_not_count():
    app = _app("in4.db")
    c, key = register(app, "Tre", "c@test.it")
    p = seed_receipts(c, key, [dict(price="5.00")])[0]
    c.post(f"/api/v1/receipts/{p['token']}/void", headers={"X-API-Key": key})
    assert "In attesa" in c.get("/dashboard/integration").get_data(as_text=True)
