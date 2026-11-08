"""Test di Panoramica servizio e Registro attività (solo proprietario)."""
import re
from datetime import datetime, timedelta, timezone

from testutil import appmod, make_config, csrf_of, register, seed_receipts

cfg = make_config("admin_overview")
cfg["OWNER_EMAILS"] = {"owner@x.it"}
app = appmod.create_app(cfg)

owner, _ = register(app, "Gestore Servizio", "owner@x.it")
a_client, a_key = register(app, "Trattoria Alfa", "alfa@x.it")
b_client, b_key = register(app, "Bar Beta", "beta@x.it")
seed_receipts(a_client, a_key, [dict(price="5.00"), dict(price="7.50", payment="Carta"), dict(price="10.00")])
seed_receipts(b_client, b_key, [dict(price="2.00")])
b_client.post  # (solo per chiarezza: Beta ha uno scontrino)

NOW = datetime.now(timezone.utc)
iso = lambda d: (NOW - timedelta(days=d)).isoformat(timespec="seconds")  # noqa: E731


def q(sql, params=()):
    with app.app_context():
        db = appmod.get_db()
        cur = db.execute(sql, params)
        rows = cur.fetchall() if cur.description else []   # Postgres non permette fetch dopo UPDATE/INSERT
        db.commit()
        return rows


def sid(email):
    return q("SELECT id FROM shops WHERE email = ?", (email,))[0][0]


# uno scontrino di Alfa annullato (10 €), uno invitato scaduto, una sospesa
q("UPDATE receipts SET voided_at = ? WHERE shop_id = ? AND total_cents = 1000", (iso(0), sid("alfa@x.it")))
with app.app_context():
    inv_id, _tok = appmod.create_invited_shop("Pizzeria Invitata", "inv@x.it", "")
    exp_id, _tok2 = appmod.create_invited_shop("Osteria Scaduta", "exp@x.it", "")
q("UPDATE shops SET invite_expires_at = ? WHERE id = ?", (iso(3), exp_id))
with app.app_context():
    sus_id, _ = appmod.create_invited_shop("Caffè Sospeso", "sus@x.it", "")
q("UPDATE shops SET status = 'suspended', invite_expires_at = NULL WHERE id = ?", (sus_id,))
q("UPDATE shops SET created_at = ? WHERE email = 'owner@x.it'", (iso(100),))
# messaggi di Alfa: 1 inviato (con numero mascherato), 3 falliti
rid = q("SELECT id FROM receipts WHERE shop_id = ? LIMIT 1", (sid("alfa@x.it"),))[0][0]
for st in ("sent", "failed", "failed", "failed"):
    q("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at) VALUES (?,?,?,?,?,?)",
      (rid, "sms", "+39 ***7654", st, "", iso(0)))
# registro
q("DELETE FROM admin_audit")
ow = sid("owner@x.it")
rows = [("company.create", sid("alfa@x.it"), "Trattoria Alfa", ""),
        ("company.suspend", sus_id, "Caffè Sospeso", ""),
        ("company.delete", 99999, "Locale Eliminato", ""),
        ("impersonation.action", sid("alfa@x.it"), "Trattoria Alfa", "POST /dashboard/settings"),
        ("company.notes", sid("beta@x.it"), "Bar Beta", "TOKEN" + "x" * 40)]
for i, (act, tid, name, det) in enumerate(rows):
    q("INSERT INTO admin_audit (actor_shop_id, actor_email, action, target_shop_id, target_name, detail, created_at)"
      " VALUES (?,?,?,?,?,?,?)", (ow, "owner@x.it", act, tid, name, det, iso(5 - i)))
for i in range(30):
    q("INSERT INTO admin_audit (actor_shop_id, actor_email, action, target_shop_id, target_name, detail, created_at)"
      " VALUES (?,?,?,?,?,?,?)", (ow, "owner@x.it", "company.activate", sid("beta@x.it"), "Bar Beta", "", iso(40 + i)))

SUSPENDED_STATUS = q("SELECT status FROM shops WHERE id = ?", (sus_id,))[0][0]


def html(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


def stat(h, label):
    return re.search(r'stat-label">' + re.escape(label) + r'</span>.*?stat-value">([^<]*)</span>', h, re.S).group(1).strip()


def test_only_owner():
    anon = app.test_client()
    for path in ("/admin", "/admin/activity"):
        r = anon.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        assert a_client.get(path).status_code == 404
        assert owner.get(path).status_code == 200


def test_overview_numbers():
    h = html(owner, "/admin")
    assert SUSPENDED_STATUS == "suspended"
    # 5 aziende (Alfa, Beta, Invitata, Scaduta, Sospesa): il proprietario non conta
    assert stat(h, "Aziende") == "5"
    assert stat(h, "Attive") == "2"
    assert stat(h, "Invitate, non ancora entrate") == "2"
    assert "1 invito scaduto" in h
    assert stat(h, "Sospese") == "1"
    # scontrini validi: Alfa 2 (l'annullato non conta) + Beta 1 = 3; incasso 5+7,5+2 = 14,50 EUR
    assert stat(h, "Scontrini gestiti") == "3"
    assert "14,50" in h and "24,50" not in h
    assert stat(h, "Messaggi inviati") == "1"
    assert "3 falliti" in h
    assert "/admin/companies?status=suspended" in h and "/admin/companies?status=invited" in h


def test_attention_and_top():
    h = html(owner, "/admin")
    assert "Osteria Scaduta" in h and "Invito scaduto" in h
    assert "Messaggi falliti" in h
    assert f"/admin/companies/{exp_id}" in h
    # la classifica: Alfa (2) prima di Beta (1); la sospesa e il proprietario non compaiono con scontrini
    top = h[h.index("Le più attive"):]
    assert top.index("Trattoria Alfa") < top.index("Bar Beta")
    assert "Gestore Servizio" not in h.split("Le più attive")[1].split("Ultime attività")[0]


def test_period_invalid_ok():
    for p in ("x", "", "7d", "90d", "../../etc"):
        assert html(owner, "/admin?period=" + p)


def test_no_sensitive_no_inline():
    for path in ("/admin", "/admin/activity"):
        h = html(owner, path)
        assert "7654" not in h and "+39" not in h
        assert not re.search(r"<style|<script(?![^>]*src=)|\sstyle=\"", h)
        assert "TOKENxxxx" not in h


def test_activity_listing_and_filters():
    h = html(owner, "/admin/activity")
    assert "Ha creato l&#39;azienda" in h and "Ha eliminato l&#39;azienda" in h
    assert "Locale Eliminato" in h and "Eliminata: non c" in h
    assert "[nascosto]" in h
    assert f"/admin/companies/{sid('alfa@x.it')}" in h
    assert f"/admin/companies/99999" not in h  # eliminata: niente link
    h = html(owner, "/admin/activity?action=deletions")
    assert "Locale Eliminato" in h and "Ha creato l&#39;azienda" not in h
    h = html(owner, "/admin/activity?q=caff%C3%A8")
    assert "Caffè Sospeso" in h and "Locale Eliminato" not in h
    h = html(owner, "/admin/activity?q=zzzznonesiste")
    assert "Nessuna voce trovata" in h


def test_activity_pagination_and_invalid():
    p1 = html(owner, "/admin/activity")
    assert "Pagina 1 di 2" in p1
    p2 = html(owner, "/admin/activity?page=2")
    assert "Pagina 2 di 2" in p2
    for bad in ("page=abc", "page=-4", "page=999", "action=%27%3B--", "q=%25%5F", "action=company.create&page=0"):
        assert owner.get("/admin/activity?" + bad).status_code == 200


def test_company_without_audit_shows_empty_state():
    q("DELETE FROM admin_audit")
    assert "Il registro è ancora vuoto" in html(owner, "/admin/activity")
    assert "Ancora nessuna attività" in html(owner, "/admin")


def test_empty_service():
    c2 = make_config("admin_overview_empty")
    c2["OWNER_EMAILS"] = {"boss@x.it"}
    app2 = appmod.create_app(c2)
    boss, _ = register(app2, "Boss", "boss@x.it")
    h = boss.get("/admin").get_data(as_text=True)
    assert "Crea la prima azienda" in h and "/admin/companies/new" in h
    assert boss.get("/admin/activity").status_code == 200
