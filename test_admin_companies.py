"""Test di Amministrazione · Aziende: percorso dell'invito, sicurezza, elenco, dettaglio, azioni del proprietario."""
import html as htmllib
import itertools
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from testutil import appmod, csrf_of, make_config, register, seed_receipts

OWNER_EMAIL = "owner@x.it"
OWNER_PW = "password123"
cfg = make_config("admin_companies.db")
cfg["OWNER_EMAILS"] = {OWNER_EMAIL}
app = appmod.create_app(cfg)
_n = itertools.count(1)
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat()


@pytest.fixture(autouse=True)
def _no_rate_limit():
    with app.app_context():
        db = appmod.get_db()
        db.execute("DELETE FROM rate_hits")
        db.commit()
    yield


# ------------------------------------------------------------------ helpers
def sql(query, params=()):
    with app.app_context():
        db = appmod.get_db()
        cur = db.execute(query, params)
        if query.lstrip().upper().startswith("SELECT"):
            return [tuple(r) for r in cur.fetchall()]
        db.commit()


def one(query, params=()):
    rows = sql(query, params)
    return rows[0] if rows else None


_owner_client = {}


def owner():
    """Client del proprietario (una sola registrazione per modulo)."""
    if "c" not in _owner_client:
        _owner_client["c"], _ = register(app, "Proprietario", OWNER_EMAIL)
    return _owner_client["c"]


def post(client, path, **data):
    data.setdefault("csrf", csrf_of(client, "/login"))
    return client.post(path, data=data)


def page(client, path, status=200):
    r = client.get(path)
    assert r.status_code == status, (path, r.status_code)
    return r.get_data(as_text=True)


def unique_email():
    return f"az{next(_n)}@example.com"


def new_company(name=None, email=None, notes="", client=None):
    """Il proprietario crea un'azienda. Ritorna (cid, token, html della pagina «link mostrato una volta»)."""
    c = client or owner()
    n = next(_n)
    name = name or f"Trattoria {n}"
    email = email or f"az{n}@example.com"
    r = post(c, "/admin/companies/new", name=name, email=email, notes=notes)
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    cid = int(r.headers["Location"].rsplit("/", 1)[1])
    html = page(c, f"/admin/companies/{cid}")
    m = re.search(r'id="ac-link"[^>]*>https?://[^/]+/invito/([^<]+)<', html)
    assert m, "link d'invito non mostrato"
    return cid, htmllib.unescape(m.group(1)), html


def accept(token, password="segretissima1", client=None):
    c = client or app.test_client()
    r = c.post(f"/invito/{token}", data={"csrf": csrf_of(c, f"/invito/{token}"), "password": password,
                                         "password2": password}, follow_redirects=True)
    return c, r


def active_company(name=None, receipts=0):
    """Azienda che ha già accettato l'invito. Ritorna (cid, client azienda, api_key, email, password)."""
    email = unique_email()
    cid, token, _ = new_company(name=name, email=email)
    c, r = accept(token)
    key = re.search(r'id="apikey">([^<]+)<', r.get_data(as_text=True)).group(1)
    if receipts:
        seed_receipts(c, key, [dict(price="5.00")] * receipts)
    return cid, c, key, email, "segretissima1"


def status_of(cid):
    return one("SELECT status FROM shops WHERE id = ?", (cid,))[0]


def audit_rows(cid=None):
    if cid is None:
        return sql("SELECT action, target_name, detail, actor_email FROM admin_audit ORDER BY id")
    return sql("SELECT action, target_name, detail, actor_email FROM admin_audit WHERE target_shop_id = ? ORDER BY id",
               (cid,))


# ------------------------------------------------------------------ percorso completo (senza browser)
def test_full_journey_invite_to_delete():
    c = owner()
    email = unique_email()
    cid, token, html = new_company(name="Osteria <Prova> & Co", email=email, notes="referente Mario")
    # stato iniziale e link mostrato con bottone copia, messaggio pronto, scadenza
    assert status_of(cid) == "invited"
    assert f"/invito/{token}" in html and 'data-copy="#ac-link"' in html
    assert "lo vedi solo questa volta" in html and "Messaggio già pronto" in html
    assert "Osteria &lt;Prova&gt; &amp; Co" in html and "Osteria <Prova>" not in html
    assert "wa.me" in html
    # una sola volta: la pagina ricaricata non lo mostra più
    again = page(c, f"/admin/companies/{cid}")
    assert token not in again and 'id="ac-link"' not in again
    assert "In attesa d'invito" in htmllib.unescape(again)
    # nel database c'è solo l'hash
    row = one("SELECT invite_hash, invite_expires_at FROM shops WHERE id = ?", (cid,))
    assert row[0] == appmod.hash_token(token) and token not in row[0]
    # l'azienda apre il link
    anon = app.test_client()
    form = page(anon, f"/invito/{token}")
    assert "Benvenuto" in form and email in form and 'name="password2"' in form
    # password corta -> errore e nulla cambia
    r = anon.post(f"/invito/{token}", data={"csrf": csrf_of(anon, f"/invito/{token}"), "password": "corta",
                                            "password2": "corta"})
    assert r.status_code == 200 and "almeno 8 caratteri" in r.get_data(as_text=True)
    assert status_of(cid) == "invited"
    # password diverse
    r = anon.post(f"/invito/{token}", data={"csrf": csrf_of(anon, f"/invito/{token}"), "password": "password123",
                                            "password2": "password124"})
    assert "non coincidono" in r.get_data(as_text=True) and status_of(cid) == "invited"
    # password giusta -> entra, vede la chiave API una sola volta
    anon, r = accept(token, "segretissima1", anon)
    h = r.get_data(as_text=True)
    key = re.search(r'id="apikey">(sd_[^<]+)<', h).group(1)
    assert status_of(cid) == "active"
    assert not re.search(r'id="apikey">', anon.get("/dashboard/settings").get_data(as_text=True))
    row = one("SELECT invite_hash, invite_expires_at, last_login_at, api_key_hash FROM shops WHERE id = ?", (cid,))
    assert row[0] is None and row[1] is None and row[2]
    assert row[3] == appmod.hash_token(key)  # solo hash, mai la chiave
    assert key not in str(sql("SELECT * FROM shops WHERE id = ?", (cid,)))
    # il link non è riutilizzabile
    assert app.test_client().get(f"/invito/{token}").status_code == 404
    r = app.test_client().post(f"/invito/{token}", data={"csrf": "x", "password": "altrapassword1", "password2": "altrapassword1"})
    assert r.status_code in (400, 404)
    # la nuova password funziona al login
    fresh = app.test_client()
    r = fresh.post("/login", data={"csrf": csrf_of(fresh), "email": email, "password": "segretissima1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    # il proprietario la vede attiva
    detail = page(c, f"/admin/companies/{cid}")
    assert "Attiva" in detail and "Entra come questa azienda" in detail
    assert "referente Mario" in detail
    # l'azienda emette uno scontrino
    rec = seed_receipts(anon, key, [dict(price="8.00")])[0]
    # sospensione
    r = post(c, f"/admin/companies/{cid}/suspend")
    assert r.status_code == 302 and status_of(cid) == "suspended"
    assert anon.get("/dashboard").status_code == 302  # la sessione dell'azienda cade subito
    f2 = app.test_client()
    r = f2.post("/login", data={"csrf": csrf_of(f2), "email": email, "password": "segretissima1"})
    assert r.status_code == 403 and "sospeso" in r.get_data(as_text=True)
    api = app.test_client().post("/api/v1/receipts", headers={"X-API-Key": key}, json={"items": [
        {"name": "x", "qty": 1, "unit_price": "1.00", "vat_rate": 10}]})
    assert api.status_code == 403 and "sospeso" in api.get_json()["error"]
    # gli scontrini già inviati restano visibili ai loro clienti
    pub = app.test_client().get(f"/r/{rec['token']}")
    assert pub.status_code == 200 and "8,00" in pub.get_data(as_text=True)
    # sospesa: niente impersonation, niente nuovo link
    assert "Riattiva" in page(c, f"/admin/companies/{cid}")
    post(c, f"/admin/companies/{cid}/impersonate")
    assert "impersonator_id" not in _session(c)
    # riattiva
    post(c, f"/admin/companies/{cid}/activate")
    assert status_of(cid) == "active"
    f3 = app.test_client()
    r = f3.post("/login", data={"csrf": csrf_of(f3), "email": email, "password": "segretissima1"})
    assert r.status_code == 302
    # nuovo link per reimpostare la password
    r = post(c, f"/admin/companies/{cid}/invite")
    assert r.status_code == 302
    h = page(c, f"/admin/companies/{cid}")
    token2 = re.search(r'id="ac-link"[^>]*>https?://[^/]+/invito/([^<]+)<', h).group(1)
    assert "Nuova password per" in h and token2 != token
    assert status_of(cid) == "active"
    # la vecchia password vale ancora finché non usa il link
    f4 = app.test_client()
    assert f4.post("/login", data={"csrf": csrf_of(f4), "email": email, "password": "segretissima1"}).status_code == 302
    c2, r = accept(token2, "nuovissima123")
    assert not re.search(r'id="apikey">', r.get_data(as_text=True))  # nessuna nuova chiave sul reset
    f5 = app.test_client()
    assert f5.post("/login", data={"csrf": csrf_of(f5), "email": email, "password": "segretissima1"}).status_code == 200
    f6 = app.test_client()
    assert f6.post("/login", data={"csrf": csrf_of(f6), "email": email, "password": "nuovissima123"}).status_code == 302
    # l'API continua a funzionare con la stessa chiave
    assert app.test_client().post("/api/v1/receipts", headers={"X-API-Key": key}, json={"items": [
        {"name": "x", "qty": 1, "unit_price": "1.00", "vat_rate": 10}]}).status_code == 201
    # eliminazione con conferma
    d = page(c, f"/admin/companies/{cid}")
    assert "Zona pericolosa" in d and "<strong>2</strong> scontrini" in d
    post(c, f"/admin/companies/{cid}/delete", confirm_name="Osteria <Prova> & Co", password=OWNER_PW)
    assert one("SELECT 1 FROM shops WHERE id = ?", (cid,)) is None
    assert one("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (cid,))[0] == 0
    assert app.test_client().get(f"/r/{rec['token']}").status_code == 404
    assert page(c, f"/admin/companies/{cid}", 404)
    # il registro resta
    actions = [a[0] for a in sql(
        "SELECT action FROM admin_audit WHERE target_name = ? ORDER BY id", ("Osteria <Prova> & Co",))]
    assert actions == ["company.create", "company.invite_accepted", "company.suspend", "company.activate",
                       "company.invite_reissued", "company.password_reset", "company.delete"]


def _session(client):
    with client.session_transaction() as s:
        return dict(s)


# ------------------------------------------------------------------ nuova azienda: convalida
@pytest.mark.parametrize("data,msg", [
    ({"name": "", "email": "a@b.it"}, "Scrivi il nome"),
    ({"name": "   ", "email": "a@b.it"}, "Scrivi il nome"),
    ({"name": "x" * 121, "email": "a@b.it"}, "troppo lungo"),
    ({"name": "Ok", "email": ""}, "email valida"),
    ({"name": "Ok", "email": "non-una-email"}, "email valida"),
    ({"name": "Ok", "email": "a@b.it", "notes": "n" * 501}, "note sono troppo lunghe"),
])
def test_new_company_validation(data, msg):
    c = owner()
    before = one("SELECT COUNT(*) FROM shops")[0]
    r = post(c, "/admin/companies/new", **data)
    assert r.status_code == 200
    h = r.get_data(as_text=True)
    assert msg in h and 'aria-invalid="true"' in h
    assert one("SELECT COUNT(*) FROM shops")[0] == before


def test_new_company_form_keeps_values_on_error():
    r = post(owner(), "/admin/companies/new", name="Bar Sport", email="rotta", notes="ciao")
    h = r.get_data(as_text=True)
    assert 'value="Bar Sport"' in h and ">ciao</textarea>" in h


def test_new_company_duplicate_email_rejected_case_insensitive():
    c = owner()
    cid, _, _ = new_company(email="dup@example.com")
    for e in ("dup@example.com", "DUP@Example.com", OWNER_EMAIL):
        r = post(c, "/admin/companies/new", name="Altra", email=e)
        assert r.status_code == 200 and "già in uso" in r.get_data(as_text=True)
    assert one("SELECT COUNT(*) FROM shops WHERE LOWER(email) = 'dup@example.com'")[0] == 1


def test_new_company_form_page_renders_with_csrf():
    h = page(owner(), "/admin/companies/new")
    assert 'name="csrf"' in h and "Crea azienda" in h and "style=" not in h


def test_invite_url_is_not_in_redirect_audit_or_database():
    c = owner()
    r = post(c, "/admin/companies/new", name="Segreta Srl", email="segreta@example.com")
    assert r.status_code == 302 and "invito" not in r.headers["Location"]
    cid = int(r.headers["Location"].rsplit("/", 1)[1])
    html = page(c, f"/admin/companies/{cid}")
    token = re.search(r"/invito/([^<\s]+)<", html).group(1)
    blob = str(sql("SELECT * FROM admin_audit")) + str(sql("SELECT * FROM shops"))
    assert token not in blob
    # la risposta con il link non va in cache
    r = c.get(f"/admin/companies/{cid}")
    assert "no-store" in r.headers["Cache-Control"]


def test_invite_page_not_cached_and_link_shown_only_once_per_creation():
    c = owner()
    r = post(c, "/admin/companies/new", name="Una Volta", email="unavolta@example.com")
    cid = int(r.headers["Location"].rsplit("/", 1)[1])
    r1 = c.get(f"/admin/companies/{cid}")
    assert "no-store" in r1.headers["Cache-Control"] and "/invito/" in r1.get_data(as_text=True)
    assert "/invito/" not in c.get(f"/admin/companies/{cid}").get_data(as_text=True)


# ------------------------------------------------------------------ token d'invito
def test_invite_token_invalid_cases_give_clear_error():
    cid, token, _ = new_company()
    anon = app.test_client()
    for bad in ("inesistente", "x" * 43, token[:-2] + "zz"):
        r = anon.get(f"/invito/{bad}")
        assert r.status_code == 404
        t = r.get_data(as_text=True)
        assert "non è valido o è scaduto" in t and "Chiedi un nuovo link" in t
    # scaduto
    sql("UPDATE shops SET invite_expires_at = ? WHERE id = ?", (iso(1), cid))
    r = anon.get(f"/invito/{token}")
    assert r.status_code == 404 and "scaduto" in r.get_data(as_text=True)
    r = anon.post(f"/invito/{token}", data={"csrf": csrf_of(anon), "password": "password123", "password2": "password123"})
    assert r.status_code == 404 and status_of(cid) == "invited"
    # il proprietario lo vede scaduto e può rigenerarlo
    h = page(owner(), f"/admin/companies/{cid}")
    assert "Invito scaduto" in h and "Rigenera invito" in h
    post(owner(), f"/admin/companies/{cid}/invite")
    h = page(owner(), f"/admin/companies/{cid}")
    token2 = re.search(r"/invito/([^<\s]+)<", h).group(1)
    assert anon.get(f"/invito/{token}").status_code == 404  # il vecchio è annullato
    assert anon.get(f"/invito/{token2}").status_code == 200


def test_invite_token_of_suspended_company_is_dead():
    cid, c, key, email, _pw = active_company()
    post(owner(), f"/admin/companies/{cid}/invite")
    token = re.search(r"/invito/([^<\s]+)<", page(owner(), f"/admin/companies/{cid}")).group(1)
    post(owner(), f"/admin/companies/{cid}/suspend")
    assert app.test_client().get(f"/invito/{token}").status_code == 404


def test_cannot_reissue_invite_for_suspended_company():
    cid, c, key, email, _pw = active_company()
    post(owner(), f"/admin/companies/{cid}/suspend")
    h = post(owner(), f"/admin/companies/{cid}/invite")
    assert h.status_code == 302
    assert "riattivala prima" in page(owner(), f"/admin/companies/{cid}")
    assert one("SELECT invite_hash FROM shops WHERE id = ?", (cid,))[0] is None


def test_invited_company_cannot_log_in_and_no_account_enumeration():
    email = unique_email()
    cid, token, _ = new_company(email=email)
    msgs = set()
    for e, pw in ((email, "qualsiasi123"), ("nessuno@example.com", "qualsiasi123"), (email.upper(), "")):
        c = app.test_client()
        r = c.post("/login", data={"csrf": csrf_of(c), "email": e, "password": pw})
        assert r.status_code == 200
        t = r.get_data(as_text=True)
        assert "Email o password non corrette" in t
        msgs.add(re.sub(r"\s+", " ", re.search(r"Email o password non corrette", t).group(0)))
    # un'azienda attiva che sbaglia password riceve lo stesso messaggio generico
    cid2, c2, key, email2, pw = active_company()
    c = app.test_client()
    r = c.post("/login", data={"csrf": csrf_of(c), "email": email2, "password": "sbagliata123"})
    assert "Email o password non corrette" in r.get_data(as_text=True)
    # nemmeno una sospesa rivela il proprio stato senza la password giusta
    post(owner(), f"/admin/companies/{cid2}/suspend")
    c = app.test_client()
    r = c.post("/login", data={"csrf": csrf_of(c), "email": email2, "password": "sbagliata123"})
    assert r.status_code == 200 and "sospeso" not in r.get_data(as_text=True)


def test_invite_form_pages_do_not_leak_other_data():
    cid, token, _ = new_company(name="Riservata Srl", email="riservata@example.com", notes="NOTA-SEGRETA-XYZ")
    h = page(app.test_client(), f"/invito/{token}")
    assert "NOTA-SEGRETA-XYZ" not in h and "api" not in h.lower().replace("apple", "")


# ------------------------------------------------------------------ accesso: non proprietario / anonimo / CSRF
ADMIN_POSTS = ["suspend", "activate", "invite", "notes", "impersonate", "delete"]


def test_normal_company_gets_404_on_all_admin_pages_get_and_post():
    cid, c, key, email, pw = active_company(name="Normale")
    other, _, _ = new_company()
    csrf = csrf_of(c, "/dashboard")
    for path in ["/admin/companies", "/admin/companies/new", f"/admin/companies/{other}"]:
        assert c.get(path).status_code == 404, path
    assert c.post("/admin/companies/new", data={"csrf": csrf, "name": "x", "email": "hack@example.com"}).status_code == 404
    for act in ADMIN_POSTS:
        r = c.post(f"/admin/companies/{other}/{act}", data={"csrf": csrf, "confirm_name": "x", "password": pw})
        assert r.status_code == 404, act
        r = c.post(f"/admin/companies/{cid}/{act}", data={"csrf": csrf})
        assert r.status_code == 404, act
    assert status_of(other) == "invited" and one("SELECT 1 FROM shops WHERE email = 'hack@example.com'") is None
    # la pagina 404 non rivela che esiste un'area amministrativa
    assert "Pagina non trovata" in c.get("/admin/companies").get_data(as_text=True)


def test_normal_company_dashboard_has_no_admin_menu():
    cid, c, *_ = active_company()
    h = page(c, "/dashboard")
    assert "/admin" not in h and "Amministrazione" not in h


def test_anonymous_is_redirected_to_login_on_every_admin_route():
    anon = app.test_client()
    csrf = csrf_of(anon)
    for path in ["/admin/companies", "/admin/companies/new", "/admin/companies/1"]:
        r = anon.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    r = anon.post("/admin/companies/new", data={"csrf": csrf, "name": "x", "email": "anon@example.com"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    for act in ADMIN_POSTS:
        r = anon.post(f"/admin/companies/1/{act}", data={"csrf": csrf})
        assert r.status_code == 302 and "/login" in r.headers["Location"], act
    assert one("SELECT 1 FROM shops WHERE email = 'anon@example.com'") is None


def test_every_post_requires_csrf():
    c = owner()
    cid, tok, _ = new_company()
    cid2, c2, *_ = active_company()
    r = c.post("/admin/companies/new", data={"name": "NoCsrf", "email": "nocsrf@example.com"})
    assert r.status_code == 400 and one("SELECT 1 FROM shops WHERE email = 'nocsrf@example.com'") is None
    r = c.post("/admin/companies/new", data={"csrf": "sbagliato", "name": "NoCsrf", "email": "nocsrf@example.com"})
    assert r.status_code == 400
    for act in ADMIN_POSTS:
        r = c.post(f"/admin/companies/{cid2}/{act}", data={"confirm_name": "x", "password": OWNER_PW})
        assert r.status_code == 400, act
        r = c.post(f"/admin/companies/{cid2}/{act}", data={"csrf": "sbagliato"})
        assert r.status_code == 400, act
    assert status_of(cid2) == "active" and one("SELECT 1 FROM shops WHERE id = ?", (cid2,))
    # il form pubblico d'invito è protetto anch'esso
    r = app.test_client().post(f"/invito/{tok}", data={"password": "password123", "password2": "password123"})
    assert r.status_code == 400 and status_of(cid) == "invited"
    # e i form della pagina portano il campo csrf
    h = page(c, f"/admin/companies/{cid2}")
    assert h.count('name="csrf"') >= 6


def test_unknown_company_is_404_never_500():
    c = owner()
    for path in ("/admin/companies/99999",):
        assert c.get(path).status_code == 404
    for act in ADMIN_POSTS:
        assert post(c, f"/admin/companies/99999/{act}", confirm_name="x", password=OWNER_PW).status_code == 404
    assert c.get("/admin/companies/abc").status_code == 404


# ------------------------------------------------------------------ il proprietario non tocca se stesso
def test_owner_cannot_manage_own_account():
    c = owner()
    oid = one("SELECT id FROM shops WHERE email = ?", (OWNER_EMAIL,))[0]
    assert c.get(f"/admin/companies/{oid}").status_code == 404
    for act in ADMIN_POSTS:
        r = post(c, f"/admin/companies/{oid}/{act}", confirm_name="Proprietario", password=OWNER_PW)
        assert r.status_code == 404, act
    assert status_of(oid) == "active" and one("SELECT 1 FROM shops WHERE id = ?", (oid,))
    # e non compare nell'elenco, nemmeno cercandolo
    for q in ("", "?q=owner", "?q=Proprietario"):
        assert f'href="/admin/companies/{oid}"' not in page(c, "/admin/companies" + q)
    # dopo tutto è ancora proprietario
    assert c.get("/admin/companies").status_code == 200


# ------------------------------------------------------------------ elenco
def test_list_shows_companies_with_counts_and_links():
    c = owner()
    cid, cl, key, email, pw = active_company(name="Pizzeria Elenco", receipts=3)
    h = page(c, "/admin/companies?q=Pizzeria+Elenco")
    assert "Pizzeria Elenco" in h and email in h and f'href="/admin/companies/{cid}"' in h
    row = re.search(rf'<tr data-href="/admin/companies/{cid}">.*?</tr>', h, re.S).group(0)
    assert re.search(r'data-label="Scontrini 30 gg" class="num">3<', row)
    assert "Attiva" in row
    assert page(c, f"/admin/companies/{cid}")  # il link risponde 200


def test_brand_new_company_without_receipts_is_not_flagged_inactive():
    c = owner()
    cid, cl, key, email, pw = active_company(name="Appena Nata", receipts=0)
    row = re.search(rf'<tr data-href="/admin/companies/{cid}">.*?</tr>', page(c, "/admin/companies?q=Appena+Nata"), re.S).group(0)
    assert "Attiva" in row and "Ferma da 14 giorni" not in row
    assert f'<tr data-href="/admin/companies/{cid}">' not in page(c, "/admin/companies?status=inactive&q=Appena+Nata")
    # la stessa azienda, ma esistente da 30 giorni e sempre senza scontrini, lo diventa
    sql("UPDATE shops SET created_at = ? WHERE id = ?", (iso(30), cid))
    assert f'<tr data-href="/admin/companies/{cid}">' in page(c, "/admin/companies?status=inactive&q=Appena+Nata")


def test_list_filters_status_search_sort_and_counts():
    c = owner()
    t = next(_n)
    inv, _, _ = new_company(name=f"Zeta Invitata {t}")
    a, _, _, _, _ = active_company(name=f"Alfa Attiva {t}", receipts=1)
    s, _, _, _, _ = active_company(name=f"Beta Sospesa {t}")
    post(c, f"/admin/companies/{s}/suspend")
    for status, yes, no in (("active", f"Alfa Attiva {t}", f"Zeta Invitata {t}"),
                            ("invited", f"Zeta Invitata {t}", f"Alfa Attiva {t}"),
                            ("suspended", f"Beta Sospesa {t}", f"Alfa Attiva {t}")):
        h = page(c, f"/admin/companies?status={status}&q={t}")
        assert yes in h and no not in h, status
    # «inattive»: attiva senza scontrini recenti
    h = page(c, f"/admin/companies?status=inactive&q={t}")
    assert f"Beta Sospesa {t}" not in h and f"Alfa Attiva {t}" not in h
    # un'azienda appena nata non è «inattiva» solo perché non ha ancora emesso nulla: serve un account vecchio
    sql("UPDATE receipts SET created_at = ? WHERE shop_id = ?", (iso(40), a))
    h = page(c, f"/admin/companies?status=inactive&q={t}")
    assert f"Alfa Attiva {t}" not in h
    sql("UPDATE shops SET created_at = ? WHERE id = ?", (iso(60), a))
    h = page(c, f"/admin/companies?status=inactive&q={t}")
    assert f"Alfa Attiva {t}" in h and "Ferma da 14 giorni" in h
    # ricerca per email, senza distinzione di maiuscole
    em = one("SELECT email FROM shops WHERE id = ?", (a,))[0]
    assert f"Alfa Attiva {t}" in page(c, "/admin/companies?q=" + em.upper())
    # ordinamento per nome
    h = page(c, f"/admin/companies?sort=name&q={t}")
    assert h.index(f"Alfa Attiva {t}") < h.index(f"Beta Sospesa {t}") < h.index(f"Zeta Invitata {t}")
    # più recenti: l'ultima creata per prima
    h = page(c, f"/admin/companies?q={t}")
    assert h.index(f"Beta Sospesa {t}") < h.index(f"Alfa Attiva {t}")
    # parametri strani non rompono nulla
    for q in ("?status=boh", "?sort=boh", "?page=abc", "?page=-3", "?page=9999", "?q=" + "x" * 500):
        assert c.get("/admin/companies" + q).status_code == 200, q


def test_list_pagination_and_empty_states():
    c = owner()
    tag = f"pagtest{next(_n)}"
    for i in range(23):
        sql("INSERT INTO shops (name, email, password_hash, api_key_hash, api_key_prefix, created_at, status)"
            " VALUES (?,?,?,?,?,?,?)", (f"{tag} n{i:02d}", f"{tag}{i}@example.com", "x", secrets.token_hex(16), "sd_x",
                                         iso(0), "invited"))
    h1 = page(c, f"/admin/companies?q={tag}&sort=name")
    h2 = page(c, f"/admin/companies?q={tag}&sort=name&page=2")
    assert f"{tag} n00" in h1 and f"{tag} n19" in h1 and f"{tag} n20" not in h1
    assert f"{tag} n20" in h2 and f"{tag} n22" in h2 and f"{tag} n00" not in h2
    assert f"q={tag}" in h2 and "23 aziende" in h1
    # nessun risultato -> stato vuoto con invito all'azione
    h = page(c, "/admin/companies?q=nonesistemai12345")
    assert "Nessuna azienda trovata" in h and "Mostra tutte" in h


def test_list_empty_state_when_no_company_at_all():
    cfg2 = make_config("admin_companies_empty.db")
    cfg2["OWNER_EMAILS"] = {"boss@x.it"}
    app2 = appmod.create_app(cfg2)
    c, _ = register(app2, "Boss", "boss@x.it")
    h = c.get("/admin/companies").get_data(as_text=True)
    assert "Non hai ancora nessuna azienda" in h and "/admin/companies/new" in h and "Crea la prima azienda" in h


def test_list_escapes_html_in_names():
    c = owner()
    cid, _, _ = new_company(name="<script>alert(1)</script>", email=unique_email())
    for p in ("/admin/companies?q=script", f"/admin/companies/{cid}"):
        h = page(c, p)
        assert "<script>alert(1)" not in h and "&lt;script&gt;alert(1)" in h


# ------------------------------------------------------------------ dettaglio nei tre stati
def test_detail_in_three_states_shows_the_right_actions():
    c = owner()
    cid, tok, _ = new_company()
    h = page(c, f"/admin/companies/{cid}")  # invited
    assert "Rigenera invito" in h and "Sospendi" not in h and "Entra come questa azienda" not in h
    assert "Invito valido fino al" in h and 'href="#azioni"' in h
    cid2, cl, key, email, pw = active_company()
    h = page(c, f"/admin/companies/{cid2}")  # active
    assert "Entra come questa azienda" in h and "Sospendi" in h and "Reimposta password" in h and "Riattiva" not in h
    assert "data-confirm" in h
    post(c, f"/admin/companies/{cid2}/suspend")
    h = page(c, f"/admin/companies/{cid2}")  # suspended
    assert "Riattiva" in h and "Sospendi" not in h and "Entra come questa azienda" not in h
    assert "Reimposta password" not in h and "Non può entrare" in h


def test_detail_never_shows_secrets_or_other_company_data():
    c = owner()
    a, ca, key_a, email_a, pw_a = active_company(name="Azienda A", receipts=2)
    b, cb, key_b, email_b, pw_b = active_company(name="Azienda B", receipts=5)
    ha = page(c, f"/admin/companies/{a}")
    assert key_a not in ha and key_b not in ha and email_b not in ha and "Azienda B" not in ha
    assert pw_a not in ha
    for secret in sql("SELECT password_hash, api_key_hash FROM shops WHERE id = ?", (a,))[0]:
        assert secret not in ha
    assert re.search(r"Scontrini · 30 giorni</span>.*?stat-value\">2<", ha, re.S)
    hb = page(c, f"/admin/companies/{b}")
    assert re.search(r"Scontrini · 30 giorni</span>.*?stat-value\">5<", hb, re.S)
    # l'elenco non espone chiavi o hash
    hl = page(c, "/admin/companies")
    assert key_a not in hl and key_b not in hl and "pbkdf2" not in hl and "scrypt" not in hl


def test_detail_usage_numbers_exclude_voided_and_other_companies():
    a, ca, key_a, _, _ = active_company(receipts=2)
    b, cb, key_b, _, _ = active_company(receipts=4)
    toks = sql("SELECT token FROM receipts WHERE shop_id = ?", (a,))
    assert ca.post(f"/api/v1/receipts/{toks[0][0]}/void", headers={"X-API-Key": key_a}).status_code == 200
    h = page(owner(), f"/admin/companies/{a}")
    assert re.search(r"Scontrini · 30 giorni</span>.*?stat-value\">1<", h, re.S)
    assert "<strong>2</strong> scontrini" in h  # nel riepilogo di eliminazione contano tutti


def test_company_a_never_sees_company_b_data():
    a, ca, key_a, _, _ = active_company(name="Locale Alfa", receipts=1)
    b, cb, key_b, email_b, _ = active_company(name="Locale Beta Segreto", receipts=1)
    rid_b = one("SELECT id FROM receipts WHERE shop_id = ?", (b,))[0]
    for p in ("/dashboard", "/dashboard/receipts", "/dashboard/settings", "/dashboard/contacts"):
        t = page(ca, p)
        assert "Beta Segreto" not in t and email_b not in t and key_b not in t, p
    assert ca.get(f"/dashboard/receipts/{rid_b}").status_code == 404
    assert one("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (a,))[0] == 1


# ------------------------------------------------------------------ note interne
def test_notes_saved_audited_and_length_limited():
    c = owner()
    cid, _, _ = new_company()
    r = post(c, f"/admin/companies/{cid}/notes", notes="  referente: Anna  ")
    assert r.status_code == 302 and r.headers["Location"].endswith("#note")
    assert one("SELECT owner_notes FROM shops WHERE id = ?", (cid,))[0] == "referente: Anna"
    assert [a[0] for a in audit_rows(cid)].count("company.notes") == 1
    assert "referente: Anna" not in str(audit_rows(cid))  # il testo delle note non va nel registro
    post(c, f"/admin/companies/{cid}/notes", notes="n" * 501)
    assert one("SELECT owner_notes FROM shops WHERE id = ?", (cid,))[0] == "referente: Anna"
    assert "troppo lunghe" in page(c, f"/admin/companies/{cid}")
    # l'azienda non vede mai le note interne
    cid2, cl, *_ = active_company()
    post(c, f"/admin/companies/{cid2}/notes", notes="NOTA-SOLO-PROPRIETARIO")
    for p in ("/dashboard", "/dashboard/settings", "/dashboard/integration"):
        assert "NOTA-SOLO-PROPRIETARIO" not in page(cl, p)


# ------------------------------------------------------------------ sospensione / riattivazione
def test_suspend_rules_and_session_drop():
    c = owner()
    cid, cl, key, email, pw = active_company()
    assert page(cl, "/dashboard")
    r = post(c, f"/admin/companies/{cid}/activate")  # già attiva: nulla da fare
    assert "non è sospesa" in page(c, f"/admin/companies/{cid}") and status_of(cid) == "active"
    post(c, f"/admin/companies/{cid}/suspend")
    assert status_of(cid) == "suspended"
    r = cl.get("/dashboard")  # la sessione già aperta cade subito
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    # sospendere due volte / un invitato non si può
    post(c, f"/admin/companies/{cid}/suspend")
    assert "solo le aziende attive" in page(c, f"/admin/companies/{cid}")
    inv, _, _ = new_company()
    post(c, f"/admin/companies/{inv}/suspend")
    assert status_of(inv) == "invited"
    assert [a[0] for a in audit_rows(cid)].count("company.suspend") == 1


def test_suspended_company_api_and_public_pages():
    cid, cl, key, email, pw = active_company(receipts=1)
    token = one("SELECT token FROM receipts WHERE shop_id = ?", (cid,))[0]
    post(owner(), f"/admin/companies/{cid}/suspend")
    anon = app.test_client()
    for method, path in (("post", "/api/v1/receipts"), ("post", f"/api/v1/receipts/{token}/send"),
                         ("post", f"/api/v1/receipts/{token}/void")):
        r = getattr(anon, method)(path, headers={"X-API-Key": key}, json={})
        assert r.status_code == 403 and "sospeso" in r.get_json()["error"], path
    assert anon.get(f"/r/{token}").status_code == 200
    assert anon.get(f"/r/{token}/qr.svg").status_code == 200
    post(owner(), f"/admin/companies/{cid}/activate")
    r = anon.post("/api/v1/receipts", headers={"X-API-Key": key}, json={"items": [
        {"name": "x", "qty": 1, "unit_price": "2.00", "vat_rate": 10}]})
    assert r.status_code == 201


# ------------------------------------------------------------------ impersonation
def test_impersonation_banner_actions_audit_and_return():
    c = owner()
    cid, cl, key, email, pw = active_company(name="Da Guardare", receipts=1)
    toks = sql("SELECT id FROM receipts WHERE shop_id = ?", (cid,))
    r = post(c, f"/admin/companies/{cid}/impersonate", goto="receipts")
    assert r.status_code == 302 and "/dashboard/receipts" in r.headers["Location"] and "period=30d" in r.headers["Location"]
    h = page(c, "/dashboard")
    assert "Da Guardare" in h and htmllib.unescape(h).count("Torna all'amministrazione") >= 1
    assert "come amministratore" in h
    # un'azione di scrittura fatta come l'azienda finisce nel registro
    rid = toks[0][0]
    r = post(c, f"/dashboard/receipts/{rid}/void")
    assert r.status_code in (302, 200)
    acts = audit_rows(cid)
    assert [a[0] for a in acts if a[0].startswith("impersonation")][:2] == ["impersonation.start", "impersonation.action"]
    assert any("/void" in a[2] for a in acts if a[0] == "impersonation.action")
    assert all(a[3] == OWNER_EMAIL for a in acts if a[0].startswith("impersonation"))
    # le azioni sulle credenziali sono bloccate
    assert post(c, "/dashboard/settings/regenerate-key").status_code == 403
    assert post(c, "/dashboard/test-receipt").status_code == 403
    # mentre guarda un cliente il proprietario non è «proprietario» (niente /admin)
    assert c.get("/admin/companies").status_code == 404
    # ritorno
    r = c.post("/admin/stop-impersonation", data={"csrf": csrf_of(c, "/dashboard")})
    assert r.status_code == 302 and r.headers["Location"] == f"/admin/companies/{cid}"
    assert c.get("/admin/companies").status_code == 200
    assert [a[0] for a in audit_rows(cid)][-1] == "impersonation.stop"
    # la chiave API dell'azienda non è cambiata
    assert app.test_client().post("/api/v1/receipts", headers={"X-API-Key": key}, json={"items": [
        {"name": "x", "qty": 1, "unit_price": "2.00", "vat_rate": 10}]}).status_code == 201


@pytest.mark.parametrize("goto", ["receipts", "messages", "invoices", "contacts", "dashboard", "", "boh", "../x"])
def test_impersonation_usage_cards_land_on_working_pages(goto):
    c = owner()
    cid, *_ = active_company()
    r = post(c, f"/admin/companies/{cid}/impersonate", goto=goto)
    assert r.status_code == 302 and r.headers["Location"].startswith("/dashboard")
    assert c.get(r.headers["Location"]).status_code == 200
    c.post("/admin/stop-impersonation", data={"csrf": csrf_of(c, "/dashboard")})


def test_impersonation_refused_for_invited_and_missing():
    c = owner()
    cid, _, _ = new_company()
    r = post(c, f"/admin/companies/{cid}/impersonate")
    assert r.status_code == 302 and "solo nel pannello" in page(c, f"/admin/companies/{cid}")
    assert "impersonator_id" not in _session(c)


def test_detail_usage_cards_are_post_forms_when_active_and_links_otherwise():
    c = owner()
    cid, *_ = active_company()
    h = page(c, f"/admin/companies/{cid}")
    assert h.count('class="card is-link stat-card ac-usage') == 4 and h.count('name="goto"') == 4
    inv, _, _ = new_company()
    h = page(c, f"/admin/companies/{inv}")
    assert 'name="goto"' not in h and h.count('<a class="card is-link stat-card ac-usage') == 4


# ------------------------------------------------------------------ eliminazione
def test_delete_requires_exact_name_and_owner_password():
    c = owner()
    cid, cl, key, email, pw = active_company(name="Da Eliminare", receipts=2)
    for kw in (dict(confirm_name="da eliminare", password=OWNER_PW), dict(confirm_name="", password=OWNER_PW),
               dict(confirm_name="Da Eliminare", password=""), dict(confirm_name="Da Eliminare", password="sbagliata"),
               dict(confirm_name="Da Eliminare", password=pw)):  # la password dell'azienda non vale
        r = post(c, f"/admin/companies/{cid}/delete", **kw)
        assert r.status_code == 302, kw
        assert one("SELECT 1 FROM shops WHERE id = ?", (cid,)), kw
    assert one("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (cid,))[0] == 2
    assert "non è stato eliminato nulla" in page(c, f"/admin/companies/{cid}")
    assert not [a for a in audit_rows(cid) if a[0] == "company.delete"]


def test_delete_removes_all_company_data_but_keeps_audit():
    c = owner()
    cid, cl, key, email, pw = active_company(name="Tutto Via", receipts=2)
    other, co, key_o, _, _ = active_company(name="Resta Qui", receipts=1)
    rid, tok = one("SELECT id, token FROM receipts WHERE shop_id = ? ORDER BY id", (cid,))
    sql("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?)", (cid, "+39333000111", iso()))
    sql("INSERT INTO messages (receipt_id, channel, phone_masked, status, created_at) VALUES (?,?,?,?,?)",
        (rid, "sms", "+39 333 ••• 111", "sent", iso()))
    sql("INSERT INTO invoice_requests (receipt_id, company_name, address, email, created_at) VALUES (?,?,?,?,?)",
        (rid, "ACME", "Via Roma", "a@b.it", iso()))
    d = page(c, f"/admin/companies/{cid}")
    assert "<strong>2</strong> scontrini" in d and "<strong>1</strong> richiesta di fattura" in d \
        and "<strong>1</strong> contatto" in d
    r = post(c, f"/admin/companies/{cid}/delete", confirm_name="Tutto Via", password=OWNER_PW)
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/companies")
    assert "è stata eliminata" in page(c, "/admin/companies")
    assert one("SELECT 1 FROM shops WHERE id = ?", (cid,)) is None
    for t in ("receipts", "contacts"):
        assert one(f"SELECT COUNT(*) FROM {t} WHERE shop_id = ?", (cid,))[0] == 0
    assert one("SELECT COUNT(*) FROM messages WHERE receipt_id = ?", (rid,))[0] == 0
    assert one("SELECT COUNT(*) FROM invoice_requests WHERE receipt_id = ?", (rid,))[0] == 0
    assert one("SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?", (rid,))[0] == 0
    assert app.test_client().get(f"/r/{tok}").status_code == 404
    assert app.test_client().post("/api/v1/receipts", headers={"X-API-Key": key}, json={}).status_code == 401
    assert cl.get("/dashboard").status_code == 302
    f = app.test_client()
    assert "non corrette" in f.post("/login", data={"csrf": csrf_of(f), "email": email, "password": pw}).get_data(as_text=True)
    # l'altra azienda è intatta
    assert one("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (other,))[0] == 1
    # registro: resta, con il nome ma senza segreti
    rows = sql("SELECT action, target_name, detail FROM admin_audit WHERE target_shop_id = ?", (cid,))
    assert ("company.delete", "Tutto Via", "") in rows
    assert OWNER_PW not in str(sql("SELECT * FROM admin_audit")) and pw not in str(sql("SELECT * FROM admin_audit"))


def test_delete_form_page_content():
    c = owner()
    cid, *_ = active_company(name="Conferma Srl")
    h = page(c, f"/admin/companies/{cid}")
    assert 'name="confirm_name"' in h and 'name="password"' in h and "Elimina per sempre" in h and "data-confirm" in h
    assert "Conferma Srl" in h


# ------------------------------------------------------------------ registro e segreti
def test_audit_has_no_tokens_passwords_or_keys():
    c = owner()
    cid, cl, key, email, pw = active_company(name="Registro Pulito")
    post(c, f"/admin/companies/{cid}/invite")
    tok = re.search(r"/invito/([^<\s]+)<", page(c, f"/admin/companies/{cid}")).group(1)
    accept(tok, "terzapassword99")
    post(c, f"/admin/companies/{cid}/suspend")
    post(c, f"/admin/companies/{cid}/activate")
    post(c, f"/admin/companies/{cid}/notes", notes="una nota")
    blob = str(audit_rows(cid))
    for secret in (tok, key, pw, "terzapassword99", OWNER_PW, "csrf"):
        assert secret not in blob
    actions = [a[0] for a in audit_rows(cid)]
    for a in ("company.create", "company.invite_accepted", "company.invite_reissued", "company.password_reset",
              "company.suspend", "company.activate", "company.notes"):
        assert a in actions
    h = page(c, f"/admin/companies/{cid}")
    assert "Azienda creata" in h and "Azienda sospesa" in h and "Note interne aggiornate" in h
    assert tok not in h and key not in h


def test_pages_respect_csp_no_inline_style_or_script():
    c = owner()
    cid, *_ = active_company()
    inv, _, _ = new_company()
    for p in ("/admin/companies", "/admin/companies/new", f"/admin/companies/{cid}", f"/admin/companies/{inv}"):
        r = c.get(p)
        t = r.get_data(as_text=True)
        assert " style=" not in t and "<style" not in t
        assert not re.search(r"<script(?![^>]*\ssrc=)", t) and "onclick=" not in t
        assert "style-src 'self'" in r.headers["Content-Security-Policy"]
        assert "no-store" in r.headers["Cache-Control"]
