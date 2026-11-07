import re
from datetime import datetime, timedelta, timezone

from testutil import appmod, make_config, register, seed_receipts, csrf_of

app = appmod.create_app(make_config("stats.db"))
c1, key1 = register(app, "Trattoria Uno", "uno@example.com")
c2, key2 = register(app, "Bar Due", "due@example.com")
c3, key3 = register(app, "Locale Nuovo", "nuovo@example.com")

NOW = datetime.now(timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago, hours=0)).replace(microsecond=0).isoformat()


def setup():
    # locale 1: oggi 10,00 Carta (+ annullato 100) ; 20 giorni fa 30,00 Contanti ; 45 giorni fa 50,00 Carta
    rs = seed_receipts(c1, key1, [dict(price="10.00", payment="Carta", name="Pizza", vat=10),
                                  dict(price="100.00", payment="Carta", name="Annullato", vat=10),
                                  dict(price="30.00", payment="Contanti", name="Pasta", vat=22),
                                  dict(price="50.00", payment="Carta", name="Vecchio", vat=10)])
    seed_receipts(c2, key2, [dict(price="999.00", payment="Carta", name="Altrui")])
    with app.app_context():
        db = appmod.get_db()
        rows = db.execute("SELECT id FROM receipts WHERE shop_id = (SELECT id FROM shops WHERE email = ?) ORDER BY id",
                          ("uno@example.com",)).fetchall()
        ids = [r["id"] for r in rows]
        db.execute("UPDATE receipts SET voided_at = ? WHERE id = ?", (iso(0), ids[1]))
        db.execute("UPDATE receipts SET created_at = ? WHERE id = ?", (iso(20), ids[2]))
        db.execute("UPDATE receipts SET created_at = ? WHERE id = ?", (iso(45), ids[3]))
        db.commit()


setup()


def page(client, period=None, path="/dashboard/stats"):
    r = client.get(path + (f"?period={period}" if period else ""))
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_login_required():
    anon = app.test_client()
    assert anon.get("/dashboard/stats").status_code == 302
    assert anon.get("/dashboard/stats.csv").status_code == 302


def test_periods_and_voided_excluded():
    h7 = page(c1, "7d")
    assert "10,00 €" in h7 and "100,00 €" not in h7 and "Annullato" not in h7
    h30 = page(c1, "30d")
    assert "40,00 €" in h30 and "Pasta" in h30 and "Vecchio" not in h30
    h90 = page(c1, "90d")
    assert "90,00 €" in h90 and "Vecchio" in h90
    assert "Annullato" not in h90 and "999,00" not in h90


def test_invalid_period_defaults_to_30d():
    assert "40,00 €" in page(c1, "boh")


def test_comparison_with_text():
    h = page(c1, "30d")  # periodo precedente: 50 € -> 40 € = -20%
    assert "▼" in h and "20%" in h and "rispetto a i 30 giorni precedenti" in h


def test_links_to_filtered_lists():
    h = page(c1, "30d")
    assert "/dashboard/receipts?" in h and "period=30d" in h
    assert "payment=Carta" in h and "payment=Contanti" in h
    assert re.search(r"/dashboard/receipts\?[^\"]*day=\d{4}-\d{2}-\d{2}", h)
    assert "/dashboard/invoices" in h


def test_vat_summary_and_csv():
    h = page(c1, "30d")
    assert "Riepilogo IVA per aliquota" in h and "stats.csv?period=30d" in h
    r = c1.get("/dashboard/stats.csv?period=30d")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    body = r.get_data(as_text=True)
    assert "Aliquota IVA" in body
    assert "10,00" in body and "30,00" in body and "40,00" in body
    assert "100,00" not in body and "999" not in body
    assert "attachment" in r.headers["Content-Disposition"]


def test_top_dishes():
    h = page(c1, "30d")
    assert "Piatti più venduti" in h and "Pizza" in h and "Pasta" in h


def test_empty_state():
    h = page(c3, "30d")
    assert "Ancora nessuno scontrino" in h and "Crea scontrino di prova" in h
    assert "Incassi per giorno" not in h
    assert c3.get("/dashboard/stats.csv").status_code == 200


def test_isolation():
    h = page(c2, "30d")
    assert "999,00 €" in h and "Pizza" not in h and "Pasta" not in h
    assert "Altrui" in h


def test_chart_accessible():
    h = page(c1, "30d")
    assert 'role="group"' in h and "<title" in h and "Vedi i dati in tabella" in h
    assert "style=" not in h and "<style" not in h
