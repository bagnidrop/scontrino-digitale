"""Utility condivise dai test (ogni file test_*.py può importarle).

    from testutil import appmod, make_config, csrf_of, register, seed_receipts, VALID_PIVA

Per provare su Postgres: TEST_DATABASE_URL=postgresql://... (le tabelle vengono cancellate).
"""
import os
import re
import tempfile

TMP = tempfile.mkdtemp()
os.environ["OUTBOX_PATH"] = os.path.join(TMP, "outbox.log")
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
    return {"DATABASE": os.path.join(TMP, name), "DATABASE_URL": TEST_DB_URL, "TESTING": True}


def csrf_of(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def register(app, name, email):
    client = app.test_client()
    r = client.post("/register", data={"csrf": csrf_of(client, "/register"), "name": name, "email": email,
                    "password": "password123"}, follow_redirects=True)
    key = re.search(r'id="apikey">([^<]+)<', r.get_data(as_text=True)).group(1)
    return client, key


def seed_receipts(client, key, rows):
    """Crea scontrini via API. rows = [dict(price="10.00", table="Tavolo 1", payment="Carta"), ...]. Ritorna i payload."""
    out = []
    for i, r in enumerate(rows):
        resp = client.post("/api/v1/receipts", headers={"X-API-Key": key}, json={
            "table": r.get("table", ""), "payment_method": r.get("payment", ""),
            "items": [{"name": r.get("name", "Piatto"), "qty": r.get("qty", 1), "unit_price": r.get("price", "10.00"),
                       "vat_rate": r.get("vat", 10)}]})
        assert resp.status_code == 201, resp.get_data(as_text=True)
        out.append(resp.get_json())
    return out
