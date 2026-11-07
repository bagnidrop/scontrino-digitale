import re

from testutil import VALID_PIVA, appmod, csrf_of, make_config, register

app = appmod.create_app(make_config("settings.db"))
c1, key1 = register(app, "Trattoria Uno", "uno@example.com")
c2, key2 = register(app, "Bar Due", "due@example.com")


def post(client, path, **data):
    return client.post(path, data=dict(csrf=csrf_of(client, "/dashboard/settings"), **data))


def shop(email):
    with app.app_context():
        return appmod.get_db().execute("SELECT * FROM shops WHERE email = ?", (email,)).fetchone()


def test_tabs_respond():
    for t in ("locale", "messaggio", "fatture", "account", "boh"):
        r = c1.get(f"/dashboard/settings?tab={t}")
        assert r.status_code == 200
    html = c1.get("/dashboard/settings?tab=messaggio").get_data(as_text=True)
    assert 'id="se-preview"' in html and "Trattoria Uno" in html


def test_login_required():
    anon = app.test_client()
    assert anon.get("/dashboard/settings").status_code == 302
    assert anon.post("/dashboard/settings/password").status_code in (302, 400)


def test_locale_validations_and_save():
    r = post(c1, "/dashboard/settings?tab=locale", name="", vat_number="", review_url="")
    assert r.status_code == 400 and "nome del locale è obbligatorio" in r.get_data(as_text=True)
    r = post(c1, "/dashboard/settings?tab=locale", name="X", review_url="http://x.it")
    assert "deve iniziare con https://" in r.get_data(as_text=True)
    r = post(c1, "/dashboard/settings?tab=locale", name="X", vat_number="12345678900")
    assert "partita IVA non è valida" in r.get_data(as_text=True)
    assert shop("uno@example.com")["name"] == "Trattoria Uno"
    r = post(c1, "/dashboard/settings?tab=locale", name="Trattoria Nuova", address="Via Roma 1",
             vat_number="IT " + VALID_PIVA, review_url="https://g.page/r/abc", footer_note="Ciao")
    assert r.status_code == 302
    s = shop("uno@example.com")
    assert s["name"] == "Trattoria Nuova" and s["vat_number"] == VALID_PIVA and s["address"] == "Via Roma 1"


def test_message_template_adds_link_and_preview():
    r = post(c1, "/dashboard/settings?tab=messaggio", message_template="Ciao da {negozio}!")
    assert r.status_code == 302
    assert shop("uno@example.com")["message_template"] == "Ciao da {negozio}! {link}"
    html = c1.get("/dashboard/settings?tab=messaggio").get_data(as_text=True)
    assert "Ciao da Trattoria Nuova!" in html and "/r/esempio1234" in html
    r = post(c1, "/dashboard/settings?tab=messaggio", message_template="  ")
    assert r.status_code == 400


def test_invoice_window():
    assert post(c1, "/dashboard/settings?tab=fatture", invoice_window_days="31").status_code == 400
    assert post(c1, "/dashboard/settings?tab=fatture", invoice_window_days="abc").status_code == 400
    assert post(c1, "/dashboard/settings?tab=fatture", invoice_window_days="7").status_code == 302
    assert shop("uno@example.com")["invoice_window_days"] == 7
    assert post(c1, "/dashboard/settings?tab=fatture", invoice_window_days="0").status_code == 302
    assert shop("uno@example.com")["invoice_window_days"] == 0


def test_password_change():
    p = "/dashboard/settings/password"
    cases = [
        (dict(current_password="sbagliata", new_password="nuovapass1", confirm_password="nuovapass1"), "attuale non è corretta"),
        (dict(current_password="password123", new_password="corta", confirm_password="corta"), "almeno 8 caratteri"),
        (dict(current_password="password123", new_password="nuovapass1", confirm_password="diversa123"), "non coincide"),
    ]
    for data, msg in cases:
        r = post(c1, p, **data)
        assert r.status_code == 400 and msg in r.get_data(as_text=True)
    r = post(c1, p, current_password="password123", new_password="nuovapass1", confirm_password="nuovapass1")
    assert r.status_code == 302
    fresh = app.test_client()
    bad = fresh.post("/login", data={"csrf": csrf_of(fresh), "email": "uno@example.com", "password": "password123"})
    assert bad.status_code == 200
    ok = fresh.post("/login", data={"csrf": csrf_of(fresh), "email": "uno@example.com", "password": "nuovapass1"})
    assert ok.status_code == 302


def test_regenerate_key_shown_once():
    r = c1.post("/dashboard/settings/regenerate-key", data={"csrf": csrf_of(c1, "/dashboard/settings")}, follow_redirects=True)
    html = r.get_data(as_text=True)
    new = re.search(r'id="apikey">([^<]+)<', html).group(1)
    assert new != key1
    assert 'id="apikey"' not in c1.get("/dashboard/settings?tab=account").get_data(as_text=True)
    assert 'data-confirm=' in c1.get("/dashboard/settings?tab=account").get_data(as_text=True)


def test_isolation_between_shops():
    before = shop("due@example.com")
    post(c1, "/dashboard/settings?tab=locale", name="Hack", vat_number="", shop_id=before["id"], id=before["id"])
    post(c1, "/dashboard/settings/password", current_password="nuovapass1", new_password="altrapass99", confirm_password="altrapass99")
    after = shop("due@example.com")
    assert after["name"] == "Bar Due" and after["password_hash"] == before["password_hash"]
    assert "Bar Due" in c2.get("/dashboard/settings").get_data(as_text=True)
    assert "uno@example.com" not in c2.get("/dashboard/settings?tab=account").get_data(as_text=True)


def test_csrf_required():
    for path in ("/dashboard/settings", "/dashboard/settings/password", "/dashboard/settings/regenerate-key"):
        assert c2.post(path, data={"name": "x"}).status_code == 400
        assert c2.post(path, data={"csrf": "sbagliato"}).status_code == 400
