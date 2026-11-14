"""Registratore telematico: coda, ponte, esiti incerti, e un giro completo con un RT simulato (protocollo Epson)."""
import importlib.util
import itertools
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from werkzeug.serving import make_server

import pos_service as pos
import rt
from testutil import appmod, make_config, register

app = appmod.create_app(make_config("rt.db"))
_n = itertools.count(1)

spec = importlib.util.spec_from_file_location("rt_bridge", "public/static/rt_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def ctx():
    n = next(_n)
    email = f"rt{n}@example.com"
    register(app, f"RT {n}", email)
    with app.app_context():
        db = appmod.get_db()
        sid = db.execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
        pos.seed_default_menu(db, sid)
        yield db, sid
        db.rollback()


def paid_order(db, sid, label="Tavolo 1", methods=(("carta", 1600),), extra=None):
    t = db.execute("SELECT id FROM pos_tables WHERE shop_id = ? AND label = ?", (sid, label)).fetchone()["id"]
    p = db.execute("SELECT id FROM pos_products WHERE shop_id = ? AND name = 'Margherita'", (sid,)).fetchone()["id"]
    b = db.execute("SELECT id FROM pos_products WHERE shop_id = ? AND name = 'Birra media'", (sid,)).fetchone()["id"]
    oid = pos.open_order(db, sid, t)
    pos.add_item(db, sid, oid, p, 2)  # 16,00 (10%)
    if extra:
        pos.add_item(db, sid, oid, b, 1)  # 5,00 (22%)
    pos.customer_reply(db, pos.request_bill(db, sid, oid), True)
    for m, a in methods:
        pos.add_payment(db, sid, oid, m, a)
    pos.close_order(db, sid, oid, appmod.insert_receipt)
    return oid


def test_payload_shape_and_discount(ctx):
    db, sid = ctx
    oid = paid_order(db, sid, methods=(("contanti", 1000), ("carta", 600), ("buoni", 500)), extra=True)
    rt.enable(db, sid)
    pl = rt.build_payload(db, sid, pos.get_order(db, sid, oid))
    assert pl["total_cents"] == 2100
    assert [l["department"] for l in pl["lines"]] == [2, 1]  # 10% -> reparto 2, 22% -> reparto 1
    assert sorted(p["type"] for p in pl["payments"]) == [0, 2, 3]
    assert all(len(l["description"]) <= 38 for l in pl["lines"])
    assert sum(l["amount_cents"] for l in pl["lines"]) == sum(p["amount_cents"] for p in pl["payments"])


def test_altro_payment_not_sendable(ctx):
    db, sid = ctx
    oid = paid_order(db, sid, methods=(("altro", 1600),))
    rt.enable(db, sid)
    with pytest.raises(rt.RtError, match="altro"):
        rt.enqueue(db, sid, oid)
    assert rt.enqueue_if_enabled(db, sid, oid) is None  # la vendita non si blocca


def test_enqueue_requires_enabled_and_is_idempotent(ctx):
    db, sid = ctx
    oid = paid_order(db, sid)
    with pytest.raises(rt.RtError):
        rt.enqueue(db, sid, oid)
    rt.enable(db, sid)
    a = rt.enqueue(db, sid, oid)
    assert rt.enqueue(db, sid, oid) == a
    assert db.execute("SELECT COUNT(*) FROM rt_jobs WHERE order_id = ?", (oid,)).fetchone()[0] == 1


def test_token_auth_rotation_and_isolation(ctx):
    db, sid = ctx
    t1 = rt.enable(db, sid)
    assert rt.shop_by_token(db, t1)["id"] == sid and rt.shop_by_token(db, "x") is None
    t2 = rt.enable(db, sid)
    assert rt.shop_by_token(db, t1) is None and rt.shop_by_token(db, t2)
    assert t2 not in str(dict(db.execute("SELECT * FROM rt_settings WHERE shop_id = ?", (sid,)).fetchone()))  # solo hash
    rt.disable(db, sid)
    assert rt.shop_by_token(db, t2) is None


def test_outcomes_and_never_double_issue(ctx):
    db, sid = ctx
    rt.enable(db, sid)
    oid = paid_order(db, sid)
    jid = rt.enqueue(db, sid, oid)
    job = rt.claim_next(db, sid)
    assert job["id"] == jid and rt.claim_next(db, sid) is None  # già preso: nessun altro lo riceve
    assert rt.report(db, sid, jid, "unknown", error="timeout") == "uncertain"
    assert rt.claim_next(db, sid) is None  # un documento incerto NON si rimanda da solo
    assert pos.get_order(db, sid, oid)["fiscal_status"] == "da_emettere"
    rt.retry(db, sid, jid)  # l'utente ha verificato che non è uscito
    job2 = rt.claim_next(db, sid)
    assert job2 and job2["id"] != jid
    assert rt.report(db, sid, job2["id"], "issued", number="0012-0034") == "done"
    assert rt.report(db, sid, job2["id"], "issued", number="0012-0034") == "done"  # risposta ripetuta: nessun effetto
    o = pos.get_order(db, sid, oid)
    assert o["fiscal_status"] == "emesso" and o["fiscal_number"] == "0012-0034"
    with pytest.raises(rt.RtError):
        rt.enqueue(db, sid, oid)  # già emesso


def test_not_sent_requeues_then_fails_and_stale_printing_is_uncertain(ctx):
    db, sid = ctx
    rt.enable(db, sid)
    oid = paid_order(db, sid)
    jid = rt.enqueue(db, sid, oid)
    for i in range(rt.MAX_AUTO_RETRIES):
        db.execute("UPDATE rt_jobs SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ? AND status = 'queued'", (jid,))
        db.commit()
        assert rt.claim_next(db, sid)["id"] == jid
        st = rt.report(db, sid, jid, "not_sent", error="spento")
    assert st == "failed"
    oid2 = paid_order(db, sid, "Tavolo 2")
    j2 = rt.enqueue(db, sid, oid2)
    rt.claim_next(db, sid)
    db.execute("UPDATE rt_jobs SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (j2,))
    db.commit()
    rt.sweep(db, sid)
    assert db.execute("SELECT status FROM rt_jobs WHERE id = ?", (j2,)).fetchone()[0] == "uncertain"


def test_departments_validation(ctx):
    db, sid = ctx
    with pytest.raises(rt.RtError):
        rt.set_departments(db, sid, {"22": 1, "10": 2, "5": 3, "4": 4, "0": 5})  # non attivo
    rt.enable(db, sid)
    with pytest.raises(rt.RtError):
        rt.set_departments(db, sid, {"22": 0, "10": 2, "5": 3, "4": 4, "0": 5})
    rt.set_departments(db, sid, {"22": 7, "10": 8, "5": 3, "4": 4, "0": 5})
    assert rt.dept_map(rt.get_settings(db, sid))["22"] == 7


# ------------------------------------------------------------------ giro completo: server vero + RT simulato + ponte
class FakeRT:
    """Simula l'fpmate.cgi dell'Epson. mode: ok | reject | hang"""
    def __init__(self):
        self.mode, self.docs = "ok", []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if outer.mode == "hang":
                    self.close_connection = True
                    return  # chiude senza rispondere
                outer.docs.append(body)
                if outer.mode == "reject":
                    xml = '<s:Envelope><s:Body><response success="false" code="EPTR_REC_EMPTY" status="x"/></s:Body></s:Envelope>'
                else:
                    n = len(outer.docs)
                    xml = ('<s:Envelope><s:Body><response success="true" code="" status="x"><addInfo><elementList>'
                           f'<fiscalReceiptNumber>{n}</fiscalReceiptNumber><zRepNumber>12</zRepNumber>'
                           '</elementList></addInfo></response></s:Body></s:Envelope>')
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                self.wfile.write(xml.encode())

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/cgi-bin/fpmate.cgi"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


@pytest.fixture
def live(tmp_path):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake = FakeRT()
    yield SimpleNamespace(url=f"http://127.0.0.1:{srv.server_port}", fake=fake, tmp=tmp_path)
    srv.shutdown()
    fake.stop()


def args_for(live, token, **kw):
    return SimpleNamespace(server=live.url, token=token, rt=live.fake.url, rt_insecure=False, operator=1,
                           dry_run=False, pending_file=str(live.tmp / "pending.json"), **kw)


def test_bridge_end_to_end(ctx, live):
    db, sid = ctx
    token = rt.enable(db, sid)
    oid = paid_order(db, sid, "Tavolo 3", methods=(("contanti", 1600),), extra=None)
    rt.enqueue(db, sid, oid)
    a = args_for(live, token)
    assert bridge.process_once(a) is True
    assert len(live.fake.docs) == 1
    xml = live.fake.docs[0]
    assert '<printRecItem' in xml and 'unitPrice="16.00"' in xml and 'department="2"' in xml
    assert 'paymentType="0"' in xml and 'payment="16.00"' in xml and xml.count("<beginFiscalReceipt") == 1
    db.rollback()
    o = pos.get_order(db, sid, oid)
    assert o["fiscal_status"] == "emesso" and o["fiscal_number"] == "0012-0001"
    assert bridge.process_once(a) is False  # niente altro da fare: non rimanda
    assert len(live.fake.docs) == 1


def test_bridge_wrong_token_and_rt_rejects_or_hangs(ctx, live):
    db, sid = ctx
    token = rt.enable(db, sid)
    oid = paid_order(db, sid)
    jid = rt.enqueue(db, sid, oid)
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        bridge.process_once(args_for(live, "rt_sbagliato"))
    assert e.value.code == 401
    live.fake.mode = "reject"
    bridge.process_once(args_for(live, token))
    db.rollback()
    assert db.execute("SELECT status FROM rt_jobs WHERE id = ?", (jid,)).fetchone()[0] == "failed"
    assert pos.get_order(db, sid, oid)["fiscal_status"] == "da_emettere"
    # RT che non risponde dopo aver ricevuto: incerto, mai rimandato
    oid2 = paid_order(db, sid, "Tavolo 2")
    j2 = rt.enqueue(db, sid, oid2)
    live.fake.mode = "hang"
    bridge.process_once(args_for(live, token))
    db.rollback()
    assert db.execute("SELECT status FROM rt_jobs WHERE id = ?", (j2,)).fetchone()[0] == "uncertain"
    assert bridge.process_once(args_for(live, token)) is False


def test_bridge_rt_offline_requeues_and_server_down_keeps_result(ctx, live):
    db, sid = ctx
    token = rt.enable(db, sid)
    oid = paid_order(db, sid)
    jid = rt.enqueue(db, sid, oid)
    a = args_for(live, token)
    a.rt = "http://127.0.0.1:9/cgi-bin/fpmate.cgi"  # nessuno in ascolto
    bridge.process_once(a)
    db.rollback()
    assert db.execute("SELECT status FROM rt_jobs WHERE id = ?", (jid,)).fetchone()[0] == "queued"
    # esito salvato su file se il server non risponde, poi comunicato
    db.execute("UPDATE rt_jobs SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (jid,))
    db.commit()
    b = args_for(live, token)
    real_call = bridge.call
    state = {"fail": False}

    def flaky(server, tok, path, body=None, timeout=20):
        if state["fail"] and path.endswith("/result"):
            raise OSError("rete caduta")
        return real_call(server, tok, path, body, timeout)
    bridge.call = flaky
    try:
        orig_send = bridge.send_epson

        def send_then_drop(*x, **k):
            r = orig_send(*x, **k)
            state["fail"] = True  # la rete cade subito dopo l'emissione
            return r
        bridge.send_epson = send_then_drop
        bridge.process_once(b)
        assert len(live.fake.docs) == 1 and json.load(open(b.pending_file))[0]["body"]["outcome"] == "issued"
        state["fail"] = False
        bridge.send_epson = orig_send
        assert bridge.process_once(b) is False  # prima comunica l'esito in sospeso
    finally:
        bridge.call = real_call
        bridge.send_epson = orig_send
    db.rollback()
    assert pos.get_order(db, sid, oid)["fiscal_status"] == "emesso" and len(live.fake.docs) == 1


def test_panel_page_and_terminal_flow(live):
    client, _ = register(app, "Pannello RT", f"panrt{next(_n)}@example.com")
    page = client.get("/dashboard/fiscal").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert "Attiva il collegamento" in page
    r = client.post("/dashboard/fiscal/rt/enable", data={"csrf": tok}, follow_redirects=True).get_data(as_text=True)
    token = re.search(r"<code>(rt_[A-Za-z0-9_-]+)</code>", r).group(1)
    assert "rt_bridge.py" in r and "Reparti dell'RT" in r
    assert token not in client.get("/dashboard/fiscal").get_data(as_text=True)  # il token si vede una volta sola
    assert client.post("/dashboard/fiscal/rt/depts", data={"csrf": tok, "dept_22": 3, "dept_10": 4, "dept_5": 5, "dept_4": 6, "dept_0": 7}).status_code == 302
    # vendita dal terminale: alla chiusura parte da sola la coda
    client.post("/dashboard/menu/seed", data={"csrf": tok})
    ct = re.search(r'name="csrf" content="([^"]+)"', client.get("/cassa").get_data(as_text=True)).group(1)
    h = {"X-CSRF": ct}
    tid = client.get("/cassa/api/floor").get_json()["tables"][0]["id"]
    prods = client.get("/cassa/api/menu").get_json()["products"]
    oid = client.post("/cassa/api/orders", json={"table_id": tid}, headers=h).get_json()["id"]
    client.post(f"/cassa/api/orders/{oid}/items", json={"product_id": prods[0]["id"], "qty": 1}, headers=h)
    client.post(f"/cassa/api/orders/{oid}/bill", headers=h)
    client.post(f"/cassa/api/orders/{oid}/confirm", headers=h)
    client.post(f"/cassa/api/orders/{oid}/pay", json={"method": "carta", "amount_cents": prods[0]["price"]}, headers=h)
    o = client.post(f"/cassa/api/orders/{oid}/close", headers=h).get_json()
    assert o["rt"] == "queued" and o["fiscal_status"] == "da_emettere"
    a = args_for(live, token)
    assert bridge.process_once(a) is True
    assert 'department="4"' in live.fake.docs[0]  # IVA 10% -> reparto scelto dal pannello (4)
