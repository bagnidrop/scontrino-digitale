"""Test end-to-end:  python -m pytest -q

Di default usano SQLite. Per provarli su Postgres:
  TEST_DATABASE_URL=postgresql://utente:password@localhost/db_di_prova python -m pytest -q
(le tabelle del database indicato vengono cancellate e ricreate)."""
import os
import re
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["OUTBOX_PATH"] = os.path.join(_tmp, "outbox.log")
os.environ["MESSAGING_PROVIDER"] = "console"

import app as appmod  # noqa: E402

VALID_PIVA = "12345678903"  # cifra di controllo valida
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


def make_config(name):
    """Configurazione di prova: file SQLite temporaneo oppure Postgres pulito."""
    if TEST_DB_URL:
        import psycopg
        with psycopg.connect(TEST_DB_URL, autocommit=True) as con:
            con.execute("DROP SCHEMA public CASCADE")
            con.execute("CREATE SCHEMA public")
    return {"DATABASE": os.path.join(_tmp, name), "DATABASE_URL": TEST_DB_URL, "TESTING": True}


def csrf_of(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


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


if __name__ == "__main__":
    unittest.main()
