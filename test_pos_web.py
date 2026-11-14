"""Cassa via web: terminale (API JSON), preconto pubblico, coda fiscale, menu, isolamento tra aziende."""
import itertools
import re

import pytest

from testutil import appmod, make_config, register

app = appmod.create_app(make_config("pos_web.db"))
_n = itertools.count(1)


def csrf(client):
    return re.search(r'name="csrf" content="([^"]+)"', client.get("/cassa").get_data(as_text=True)).group(1)


@pytest.fixture
def shop():
    n = next(_n)
    client, _ = register(app, f"Trattoria {n}", f"web{n}@example.com")
    tok = csrf(client)
    h = {"X-CSRF": tok}
    r = client.post("/dashboard/menu/seed", data={"csrf": tok})
    assert r.status_code == 302
    return client, h


def call(client, h, method, path, body=None):
    r = client.open("/cassa/api" + path, method=method, json=body, headers=h)
    return r.status_code, r.get_json()


def first(client, h):
    _, floor = call(client, h, "GET", "/floor")
    t = floor["tables"][0]["id"]
    _, menu = call(client, h, "GET", "/menu")
    return t, menu["products"]


def test_page_requires_login_and_csrf():
    c = app.test_client()
    assert c.get("/cassa").status_code == 302
    assert c.get("/cassa/api/floor").status_code == 401
    client, _ = register(app, "Csrf", "csrfpos@example.com")
    assert client.post("/cassa/api/orders", json={"table_id": 1}).status_code == 400  # niente header CSRF


def test_full_flow_with_customer_page(shop):
    client, h = shop
    t, prods = first(client, h)
    code, o = call(client, h, "POST", "/orders", {"table_id": t, "covers": 2})
    assert code == 200 and o["status"] == "open"
    oid = o["id"]
    call(client, h, "POST", f"/orders/{oid}/items", {"product_id": prods[0]["id"], "qty": 2})
    assert call(client, h, "POST", f"/orders/{oid}/pay", {"method": "carta", "amount_cents": 100})[0] == 409
    code, o = call(client, h, "POST", f"/orders/{oid}/bill")
    assert o["status"] == "preconto" and o["bill"]["url"].endswith(o["bill"]["url"].rsplit("/", 1)[1])
    token = o["bill"]["url"].rsplit("/", 1)[1]
    # il cliente apre la pagina pubblica (senza login) e segnala un errore
    anon = app.test_client()
    page = anon.get(f"/p/{token}").get_data(as_text=True)
    assert "NON fiscale" in page and "È tutto corretto" in page
    assert anon.get(f"/p/{token}/qr.svg").mimetype == "image/svg+xml"
    ctok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    anon.post(f"/p/{token}/reply", data={"csrf": ctok, "answer": "error", "note": "una pizza di troppo"})
    _, o = call(client, h, "GET", f"/orders/{oid}")
    assert o["customer_status"] == "disputed" and "pizza" in o["customer_note"]
    # staff corregge, riemette, il cliente conferma
    call(client, h, "POST", f"/orders/{oid}/reopen")
    call(client, h, "PATCH", f"/orders/{oid}/items/{o['items'][0]['id']}", {"qty": 1})
    call(client, h, "POST", f"/orders/{oid}/bill")
    page = anon.get(f"/p/{token}").get_data(as_text=True)
    ctok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert anon.post(f"/p/{token}/reply", data={"csrf": ctok, "answer": "ok"}).status_code == 302
    # senza CSRF il cliente non può rispondere
    assert anon.post(f"/p/{token}/reply", data={"answer": "ok"}).status_code == 400
    code, o = call(client, h, "POST", f"/orders/{oid}/pay", {"method": "contanti", "amount_cents": prods[0]["price"], "tendered_cents": 5000})
    assert code == 200 and o["change_cents"] == 5000 - prods[0]["price"]
    code, o = call(client, h, "POST", f"/orders/{oid}/close")
    assert code == 200 and o["status"] == "paid" and o["receipt"]["url"] and o["fiscal_status"] == "da_emettere"
    assert "Conto pagato" in anon.get(f"/p/{token}").get_data(as_text=True)
    # coda fiscale
    page = client.get("/dashboard/fiscal").get_data(as_text=True)
    assert "Da fiscalizzare" in page and "non fiscale" in page
    ftok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post(f"/dashboard/fiscal/{oid}", data={"csrf": ftok, "number": "0001-0042"})
    _, o = call(client, h, "GET", f"/orders/{oid}")
    assert o["fiscal_status"] == "emesso"
    assert "0001-0042" in client.get("/dashboard/fiscal").get_data(as_text=True)


def test_errors_are_json_and_isolated(shop):
    client, h = shop
    t, prods = first(client, h)
    _, o = call(client, h, "POST", "/orders", {"table_id": t})
    code, body = call(client, h, "POST", f"/orders/{o['id']}/bill")
    assert code == 400 and "vuoto" in body["error"]
    other, h2 = shop2 = register(app, "Altra", "altra@example.com")[0], {"X-CSRF": ""}
    h2["X-CSRF"] = csrf(other)
    assert call(other, h2, "GET", f"/orders/{o['id']}")[0] == 404
    assert call(other, h2, "POST", "/orders", {"table_id": t})[0] == 404  # tavolo di un'altra azienda


def test_unknown_bill_token_404():
    assert app.test_client().get("/p/nonesiste").status_code == 404
    assert app.test_client().get("/p/nonesiste/qr.svg").status_code == 404


def test_menu_page_add_product(shop):
    client, _ = shop
    page = client.get("/dashboard/menu").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    cid = re.search(r'<option value="(\d+)"', page).group(1)
    client.post("/dashboard/menu/product", data={"csrf": tok, "name": "Gelato", "price": "3,50", "category_id": cid, "vat_rate": "10"})
    assert "Gelato" in client.get("/dashboard/menu").get_data(as_text=True)
    _, menu = call(client, {"X-CSRF": csrf(client)}, "GET", "/menu")
    assert any(p["name"] == "Gelato" and p["price"] == 350 for p in menu["products"])


def panel_csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def test_product_and_category_rename(shop):
    client, _ = shop
    page = client.get("/dashboard/menu").get_data(as_text=True)
    pid = re.search(r'name="id" value="(\d+)"', page).group(1)
    tok = panel_csrf(client, "/dashboard/menu")
    client.post("/dashboard/menu/product-edit", data={"csrf": tok, "id": pid, "name": "Bruschetta al pomodoro", "price": "7,00"})
    page = client.get("/dashboard/menu").get_data(as_text=True)
    assert "Bruschetta al pomodoro" in page
    r = client.post("/dashboard/menu/product-edit", data={"csrf": tok, "id": pid, "name": "  ", "price": "7"}, follow_redirects=True)
    assert "non può essere vuoto" in r.get_data(as_text=True)


def test_comanda_flow(shop):
    client, h = shop
    t, prods = first(client, h)
    _, o = call(client, h, "POST", "/orders", {"table_id": t})
    oid = o["id"]
    assert call(client, h, "POST", f"/orders/{oid}/comanda")[0] == 400  # niente da inviare
    call(client, h, "POST", f"/orders/{oid}/items", {"product_id": prods[0]["id"], "qty": 2, "note": "senza glutine"})
    code, o = call(client, h, "GET", f"/orders/{oid}")
    assert o["unsent"] == 1
    code, o = call(client, h, "POST", f"/orders/{oid}/comanda")
    assert code == 200 and o["unsent"] == 0 and o["items"][0]["sent"] and "batch=" in o["comanda_url"]
    page = client.get(o["comanda_url"]).get_data(as_text=True)
    assert "COMANDA" in page and "senza glutine" in page and prods[0]["name"] in page
    assert client.get(f"/cassa/comanda/{oid}?batch=2000-01-01T00:00:00Z").status_code == 404
    assert app.test_client().get(o["comanda_url"]).status_code == 302  # serve il login
    other, _ = register(app, "Altra cucina", f"cucina{next(_n)}@example.com")
    assert other.get(o["comanda_url"]).status_code == 404


def test_staff_pin_roles_and_device_lock(shop):
    client, h = shop
    tok = panel_csrf(client, "/dashboard/menu?tab=personale")
    for name, pin, role in (("Marco", "1111", "cameriere"), ("Giulia", "2222", "cassiere"), ("Anna", "3333", "titolare")):
        client.post("/dashboard/menu/staff", data={"csrf": tok, "name": name, "pin": pin, "role": role})
    client.get("/cassa")  # il tablet entra in modalità cassa
    assert call(client, h, "GET", "/floor")[0] == 403  # bloccata
    code, body = call(client, h, "GET", "/staff")
    assert code == 200 and body["locked"] and {s["name"] for s in body["staff"]} == {"Marco", "Giulia", "Anna"}
    ids = {s["name"]: s["id"] for s in body["staff"]}
    assert "pin_hash" not in str(body)
    assert call(client, h, "POST", "/unlock", {"staff_id": ids["Marco"], "pin": "9999"})[0] == 403
    # il pannello è chiuso mentre la cassa è bloccata
    assert client.get("/dashboard/menu").status_code == 302
    assert call(client, h, "POST", "/unlock", {"staff_id": ids["Marco"], "pin": "1111"})[0] == 200
    assert client.get("/dashboard/menu").status_code == 302  # un cameriere non apre il pannello
    t, prods = first(client, h)
    _, o = call(client, h, "POST", "/orders", {"table_id": t})
    oid = o["id"]
    call(client, h, "POST", f"/orders/{oid}/items", {"product_id": prods[0]["id"], "qty": 1})
    assert call(client, h, "POST", f"/orders/{oid}/discount", {"discount_cents": 50})[0] == 403
    assert call(client, h, "POST", f"/orders/{oid}/cancel")[0] == 403
    assert call(client, h, "POST", f"/orders/{oid}/custom", {"name": "x", "price_cents": 100})[0] == 403
    assert call(client, h, "POST", f"/orders/{oid}/bill")[0] == 200
    assert call(client, h, "POST", f"/orders/{oid}/confirm")[0] == 200
    assert call(client, h, "POST", f"/orders/{oid}/pay", {"method": "carta", "amount_cents": 100})[0] == 403
    # cassiere: può incassare, ma non apre il pannello
    call(client, h, "POST", "/lock")
    assert call(client, h, "GET", "/floor")[0] == 403
    call(client, h, "POST", "/unlock", {"staff_id": ids["Giulia"], "pin": "2222"})
    assert call(client, h, "POST", f"/orders/{oid}/pay", {"method": "carta", "amount_cents": prods[0]["price"]})[0] == 200
    assert call(client, h, "POST", f"/orders/{oid}/close")[0] == 200
    assert client.get("/dashboard/menu").status_code == 302
    # titolare: apre il pannello dal tablet
    call(client, h, "POST", "/lock")
    call(client, h, "POST", "/unlock", {"staff_id": ids["Anna"], "pin": "3333"})
    assert client.get("/dashboard/menu").status_code == 200


def test_pin_rate_limit_and_isolation(shop):
    client, h = shop
    tok = panel_csrf(client, "/dashboard/menu?tab=personale")
    client.post("/dashboard/menu/staff", data={"csrf": tok, "name": "Luca", "pin": "4444", "role": "cameriere"})
    client.get("/cassa")
    _, body = call(client, h, "GET", "/staff")
    sid_ = body["staff"][0]["id"]
    codes = [call(client, h, "POST", "/unlock", {"staff_id": sid_, "pin": "0000"})[0] for _ in range(24)]
    assert 429 in codes
    # un'altra azienda non può usare il dipendente di questa
    other, _ = register(app, "Altra pin", f"pin{next(_n)}@example.com")
    h2 = {"X-CSRF": csrf(other)}
    assert call(other, h2, "POST", "/unlock", {"staff_id": sid_, "pin": "4444"})[0] == 403


def test_delivery_preference_customer_and_staff(shop):
    client, h = shop
    t, prods = first(client, h)
    _, o = call(client, h, "POST", "/orders", {"table_id": t})
    oid = o["id"]
    call(client, h, "POST", f"/orders/{oid}/items", {"product_id": prods[0]["id"], "qty": 1})
    assert call(client, h, "POST", f"/orders/{oid}/delivery", {"pref": "digital"})[0] == 409  # prima il preconto
    _, o = call(client, h, "POST", f"/orders/{oid}/bill")
    token = o["bill"]["url"].rsplit("/", 1)[1]
    anon = app.test_client()
    page = anon.get(f"/p/{token}").get_data(as_text=True)
    assert "Come vuoi il documento fiscale" not in page  # compare solo dopo la conferma del conto
    ctok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    anon.post(f"/p/{token}/reply", data={"csrf": ctok, "answer": "ok"})
    page = anon.get(f"/p/{token}").get_data(as_text=True)
    assert "Come vuoi il documento fiscale" in page and "Solo digitale, niente carta" in page
    assert "Hai scelto" not in page  # nessuna scelta preselezionata
    anon.post(f"/p/{token}/reply", data={"csrf": ctok, "delivery": "digital"})
    _, o = call(client, h, "GET", f"/orders/{oid}")
    assert o["delivery"] == "digital" and o["delivery_by"] == "cliente"
    anon.post(f"/p/{token}/reply", data={"csrf": ctok, "delivery": "boh"})
    assert call(client, h, "GET", f"/orders/{oid}")[1]["delivery"] == "digital"  # valore non valido ignorato
    code, o = call(client, h, "POST", f"/orders/{oid}/delivery", {"pref": "paper"})  # il cliente cambia idea a voce
    assert code == 200 and o["delivery"] == "paper" and o["delivery_by"] != "cliente"
    assert call(client, h, "POST", f"/orders/{oid}/delivery", {"pref": "x"})[0] == 400
