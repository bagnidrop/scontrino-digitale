"""Test end-to-end:  python -m pytest -q

Di default usano SQLite. Per provarli su Postgres:
  TEST_DATABASE_URL=postgresql://utente:password@localhost/db_di_prova python -m pytest -q
(le tabelle del database indicato vengono cancellate e ricreate)."""
import os
import re
import unittest

from testutil import TMP as _tmp, VALID_PIVA, appmod, csrf_of, make_config, register  # noqa: F401


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = appmod.create_app(make_config("t.db"))
        cls.c = cls.app.test_client()
        tok = csrf_of(cls.c, "/register")
        r = cls.c.post("/register", data={"csrf": tok, "name": "Trattoria Da Mario",
                       "email": "mario@example.com", "password": "password123"}, follow_redirects=True)
        html = r.get_data(as_text=True)
        cls.key = re.search(r'id="apikey">([^<]+)<', html).group(1)

    def hdr(self):
        return {"X-API-Key": self.key}

    def sale(self, **extra):
        body = {"items": [{"name": "Caffè", "qty": 2, "unit_price": 1.2, "vat_rate": 10},
                          {"name": "Vino", "qty": 1, "unit_price_cents": 550, "vat_rate": 22}]}
        body.update(extra)
        return self.c.post("/api/v1/receipts", json=body, headers=self.hdr())

    def test_01_auth_required(self):
        self.assertEqual(self.c.post("/api/v1/receipts", json={}).status_code, 401)
        self.assertEqual(self.c.post("/api/v1/receipts", json={}, headers={"X-API-Key": "sd_x"}).status_code, 401)

    def test_02_create_receipt_and_totals(self):
        r = self.sale()
        self.assertEqual(r.status_code, 201)
        j = r.get_json()
        self.assertEqual(j["total_cents"], 240 + 550)
        self.assertEqual(j["number"], 1)
        page = self.c.get(j["url"].replace("http://localhost", ""))
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Trattoria Da Mario", html)
        self.assertIn("7,90 €", html)
        qr = self.c.get(f"/r/{j['token']}/qr.svg")
        self.assertEqual(qr.status_code, 200)
        self.assertIn("image/svg+xml", qr.content_type)

    def test_03_validation(self):
        self.assertEqual(self.c.post("/api/v1/receipts", json={"items": []}, headers=self.hdr()).status_code, 400)
        bad = self.c.post("/api/v1/receipts", json={"items": [{"name": "x", "qty": -1, "unit_price": 1}]},
                          headers=self.hdr())
        self.assertEqual(bad.status_code, 400)
        mism = self.sale(total_cents=999)
        self.assertEqual(mism.status_code, 400)
        nan = self.c.post("/api/v1/receipts", json={"items": [{"name": "x", "qty": 1, "unit_price": "NaN"}]},
                          headers=self.hdr())
        self.assertEqual(nan.status_code, 400)

    def test_04_idempotency(self):
        a = self.sale(external_id="CASSA1-77")
        b = self.sale(external_id="CASSA1-77")
        self.assertEqual(a.status_code, 201)
        self.assertEqual(b.status_code, 200)
        self.assertEqual(a.get_json()["token"], b.get_json()["token"])

    def test_05_send_message_and_privacy(self):
        tok = self.sale().get_json()["token"]
        bad = self.c.post(f"/api/v1/receipts/{tok}/send", json={"phone": "abc"}, headers=self.hdr())
        self.assertEqual(bad.status_code, 400)
        ok = self.c.post(f"/api/v1/receipts/{tok}/send", json={"phone": "333 123 4567"}, headers=self.hdr())
        self.assertEqual(ok.status_code, 200)
        log = open(os.environ["OUTBOX_PATH"], encoding="utf-8").read()
        self.assertIn(f"/r/{tok}", log)
        self.assertNotIn("3331234567", log)  # numero mascherato anche nel log
        with self.app.app_context():
            n = appmod.get_db().execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        self.assertEqual(n, 0)  # nessun consenso => numero NON salvato
        self.c.post(f"/api/v1/receipts/{tok}/send",
                    json={"phone": "+393331234567", "marketing_consent": True}, headers=self.hdr())
        with self.app.app_context():
            n = appmod.get_db().execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        self.assertEqual(n, 1)

    def test_06_invoice_request(self):
        tok = self.sale().get_json()["token"]
        client = self.app.test_client()
        t = csrf_of(client, f"/r/{tok}")
        form = {"csrf": t, "company_name": "ACME Srl", "vat_number": "IT " + VALID_PIVA, "sdi_code": "ABCDEFG",
                "address": "Via Roma 1, Milano", "email": "amm@acme.it"}
        bad = client.post(f"/r/{tok}/invoice", data={**form, "vat_number": "12345678900"})
        self.assertEqual(bad.status_code, 422)
        ok = client.post(f"/r/{tok}/invoice", data=form, follow_redirects=True)
        self.assertIn("Richiesta di fattura inviata", ok.get_data(as_text=True))
        dup = client.post(f"/r/{tok}/invoice", data=form)
        self.assertEqual(dup.status_code, 422)  # una sola richiesta per scontrino
        nocsrf = client.post(f"/r/{tok}/invoice", data={k: v for k, v in form.items() if k != "csrf"})
        self.assertEqual(nocsrf.status_code, 400)

    def test_07_panel_and_csv(self):
        for path in ("/dashboard", "/dashboard/receipts", "/dashboard/invoices", "/dashboard/contacts",
                     "/dashboard/settings"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        csv_resp = self.c.get("/dashboard/invoices.csv")
        self.assertIn("ACME Srl", csv_resp.get_data(as_text=True))
        anon = self.app.test_client()
        self.assertEqual(anon.get("/dashboard").status_code, 302)

    def test_08_tenant_isolation(self):
        other = self.app.test_client()
        t = csrf_of(other, "/register")
        r = other.post("/register", data={"csrf": t, "name": "Bar Sport", "email": "bar@example.com",
                       "password": "password123"}, follow_redirects=True)
        key2 = re.search(r'id="apikey">([^<]+)<', r.get_data(as_text=True)).group(1)
        tok = self.sale().get_json()["token"]
        # il negozio 2 non può inviare scontrini del negozio 1
        res = other.post(f"/api/v1/receipts/{tok}/send", json={"phone": "3331234567"}, headers={"X-API-Key": key2})
        self.assertEqual(res.status_code, 404)
        # e non vede le richieste di fattura altrui
        self.assertNotIn("ACME Srl", other.get("/dashboard/invoices").get_data(as_text=True))

    def test_09_xss_escaped(self):
        r = self.c.post("/api/v1/receipts", json={"items": [{"name": "<script>alert(1)</script>", "qty": 1,
                        "unit_price": 1}]}, headers=self.hdr())
        html = self.c.get(f"/r/{r.get_json()['token']}").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_10_unknown_token(self):
        self.assertEqual(self.c.get("/r/nonexistent").status_code, 404)

    def test_11_key_regeneration(self):
        client = self.app.test_client()
        t = csrf_of(client, "/login")
        client.post("/login", data={"csrf": t, "email": "mario@example.com", "password": "password123"})
        t = csrf_of(client, "/dashboard/settings")
        r = client.post("/dashboard/settings/regenerate-key", data={"csrf": t}, follow_redirects=True)
        new = re.search(r'id="apikey">([^<]+)<', r.get_data(as_text=True)).group(1)
        self.assertNotEqual(new, self.key)
        self.assertEqual(self.c.post("/api/v1/receipts", json={}, headers=self.hdr()).status_code, 401)


class Voiding(unittest.TestCase):
    """Annullo scontrini: API, pannello, pagina cliente, statistiche, isolamento tra locali."""

    @classmethod
    def setUpClass(cls):
        cls.app = appmod.create_app(make_config("v.db"))
        cls.c, cls.key = register(cls.app, "Osteria Uno", "uno@example.com")
        cls.other, cls.key2 = register(cls.app, "Bar Due", "due@example.com")

    def sale(self, price="10.00", key=None):
        r = self.c.post("/api/v1/receipts", headers={"X-API-Key": key or self.key}, json={
            "items": [{"name": "Piatto", "qty": 1, "unit_price": price, "vat_rate": 10}]})
        self.assertEqual(r.status_code, 201)
        return r.get_json()

    def test_01_api_void_is_idempotent_and_blocks_actions(self):
        s = self.sale()
        self.assertFalse(s["voided"])
        for _ in range(2):  # la seconda chiamata non deve dare errore
            r = self.c.post(f"/api/v1/receipts/{s['token']}/void", headers={"X-API-Key": self.key})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["voided"])
        page = self.c.get(f"/r/{s['token']}").get_data(as_text=True)
        self.assertIn("SCONTRINO ANNULLATO", page)
        self.assertNotIn('name="company_name"', page)  # niente modulo fattura
        send = self.c.post(f"/api/v1/receipts/{s['token']}/send", json={"phone": "3331234567"},
                           headers={"X-API-Key": self.key})
        self.assertEqual(send.status_code, 409)
        client = self.app.test_client()
        inv = client.post(f"/r/{s['token']}/invoice", data={
            "csrf": csrf_of(client, "/login"), "company_name": "ACME", "vat_number": VALID_PIVA,
            "address": "Via Roma 1", "email": "a@b.it"})
        self.assertEqual(inv.status_code, 422)

    def test_02_panel_void_and_restore(self):
        s = self.sale()
        t = csrf_of(self.c, "/dashboard/settings")
        r = self.c.post(f"/dashboard/receipts/{s['id']}/void", data={"csrf": t}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("annullato", html)
        self.assertIn("Ripristina", html)
        self.assertIn("SCONTRINO ANNULLATO", self.c.get(f"/r/{s['token']}").get_data(as_text=True))
        r = self.c.post(f"/dashboard/receipts/{s['id']}/restore", data={"csrf": t}, follow_redirects=True)
        self.assertIn("ripristinato", r.get_data(as_text=True))
        self.assertNotIn("SCONTRINO ANNULLATO", self.c.get(f"/r/{s['token']}").get_data(as_text=True))

    def test_03_panel_void_requires_csrf_and_login(self):
        s = self.sale()
        self.assertEqual(self.c.post(f"/dashboard/receipts/{s['id']}/void", data={}).status_code, 400)
        anon = self.app.test_client()
        t = csrf_of(anon, "/login")
        r = anon.post(f"/dashboard/receipts/{s['id']}/void", data={"csrf": t})
        self.assertEqual(r.status_code, 302)  # rimandato al login, non annullato
        self.assertNotIn("SCONTRINO ANNULLATO", self.c.get(f"/r/{s['token']}").get_data(as_text=True))

    def test_04_other_shop_cannot_void(self):
        s = self.sale()
        r = self.other.post(f"/api/v1/receipts/{s['token']}/void", headers={"X-API-Key": self.key2})
        self.assertEqual(r.status_code, 404)
        t = csrf_of(self.other, "/dashboard/settings")
        r = self.other.post(f"/dashboard/receipts/{s['id']}/void", data={"csrf": t})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("SCONTRINO ANNULLATO", self.c.get(f"/r/{s['token']}").get_data(as_text=True))

    def test_05_voided_excluded_from_stats(self):
        client, key = register(self.app, "Pizzeria Tre", "tre@example.com")
        keep = self.sale("5.00", key)
        gone = self.sale("10.00", key)
        client.post(f"/api/v1/receipts/{gone['token']}/void", headers={"X-API-Key": key})
        html = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("5,00 €", html)
        self.assertNotIn("15,00 €", html)  # 5 + 10 senza lo scontrino annullato
        self.assertEqual(keep["number"], 1)
        self.assertEqual(gone["number"], 2)  # la numerazione non ha buchi

    def test_06_unknown_receipt(self):
        r = self.c.post("/api/v1/receipts/nonexistent/void", headers={"X-API-Key": self.key})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.c.post("/api/v1/receipts/x/void").status_code, 401)


class Migration(unittest.TestCase):
    def test_existing_database_gets_new_column_without_losing_data(self):
        import db as dbmod
        cfg = make_config("m.db")
        app = appmod.create_app(cfg)
        client, key = register(app, "Vecchio Locale", "old@example.com")
        r = client.post("/api/v1/receipts", headers={"X-API-Key": key}, json={
            "items": [{"name": "Caffè", "qty": 1, "unit_price": "1.20", "vat_rate": 10}]})
        self.assertEqual(r.status_code, 201)
        # simula un database creato dalla versione precedente: senza la colonna
        db = dbmod.connect(app.config)
        db.execute("ALTER TABLE receipts DROP COLUMN voided_at")
        db.commit()
        self.assertNotIn("voided_at", dbmod._columns(db, "receipts"))
        db.close()
        dbmod.init_db(app.config)  # è ciò che succede a ogni avvio
        dbmod.init_db(app.config)  # e deve essere ripetibile
        db = dbmod.connect(app.config)
        self.assertIn("voided_at", dbmod._columns(db, "receipts"))
        n = db.execute("SELECT COUNT(*) FROM receipts WHERE voided_at IS NULL").fetchone()[0]
        db.close()
        self.assertEqual(n, 1)  # lo scontrino esistente è ancora lì


if __name__ == "__main__":
    unittest.main()
