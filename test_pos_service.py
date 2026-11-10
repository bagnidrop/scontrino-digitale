"""Test della logica di cassa (pos_service): conto, preconto, verifica del cliente, pagamenti, chiusura."""
import itertools

import pytest

import pos_service as pos
from testutil import appmod, make_config, register

app = appmod.create_app(make_config("pos_service.db"))
_n = itertools.count(1)


@pytest.fixture
def ctx():
    n = next(_n)
    email = f"pos{n}@example.com"
    register(app, f"Ristorante {n}", email)
    with app.app_context():
        db = appmod.get_db()
        sid = db.execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
        pos.seed_default_menu(db, sid)
        yield db, sid
        db.rollback()


def prod(db, sid, name):
    return db.execute("SELECT * FROM pos_products WHERE shop_id = ? AND name = ?", (sid, name)).fetchone()["id"]


def table(db, sid, label="Tavolo 1"):
    return db.execute("SELECT id FROM pos_tables WHERE shop_id = ? AND label = ?", (sid, label)).fetchone()["id"]


def flow_to_bill(db, sid):
    oid = pos.open_order(db, sid, table(db, sid), covers=2)
    pos.add_item(db, sid, oid, prod(db, sid, "Margherita"), 2)    # 16,00 (10%)
    pos.add_item(db, sid, oid, prod(db, sid, "Birra media"), 2)   # 10,00 (22%)
    return oid


def test_seed_menu_once(ctx):
    db, sid = ctx
    assert pos.seed_default_menu(db, sid) is False
    assert db.execute("SELECT COUNT(*) FROM pos_tables WHERE shop_id = ?", (sid,)).fetchone()[0] == 9


def test_open_order_is_idempotent_per_table(ctx):
    db, sid = ctx
    t = table(db, sid)
    assert pos.open_order(db, sid, t) == pos.open_order(db, sid, t)


def test_add_items_merges_and_totals(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    pos.add_item(db, sid, oid, prod(db, sid, "Margherita"), 1)
    o = pos.get_order(db, sid, oid)
    t = pos.totals(db, o)
    assert t["gross"] == 3 * 800 + 2 * 500 and t["total"] == t["due"] == 3400
    assert len(pos.order_items(db, oid)) == 2


def test_isolation_between_shops(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    with pytest.raises(pos.PosError):
        pos.get_order(db, sid + 9999, oid)
    with pytest.raises(pos.PosError):
        pos.add_item(db, sid + 9999, oid, prod(db, sid, "Caffè"))


def test_cannot_edit_after_preconto_until_reopened(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    token = pos.request_bill(db, sid, oid)
    assert pos.request_bill(db, sid, oid) == token
    with pytest.raises(pos.PosError):
        pos.add_item(db, sid, oid, prod(db, sid, "Caffè"))
    pos.reopen_order(db, sid, oid)
    pos.add_item(db, sid, oid, prod(db, sid, "Caffè"))
    assert pos.get_order(db, sid, oid)["customer_status"] == "none"


def test_empty_order_cannot_request_bill(ctx):
    db, sid = ctx
    oid = pos.open_order(db, sid, table(db, sid))
    with pytest.raises(pos.PosError):
        pos.request_bill(db, sid, oid)


def test_no_payment_before_customer_confirms(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    pos.request_bill(db, sid, oid)
    with pytest.raises(pos.PosError, match="confermato"):
        pos.add_payment(db, sid, oid, "carta", 100)


def test_customer_disputes_then_staff_fixes_and_reissues(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    token = pos.request_bill(db, sid, oid)
    assert pos.customer_reply(db, token, False, "La birra è una sola") == "disputed"
    o = pos.get_order(db, sid, oid)
    assert o["customer_status"] == "disputed" and "una sola" in o["customer_note"]
    with pytest.raises(pos.PosError):
        pos.add_payment(db, sid, oid, "carta", 100)
    pos.reopen_order(db, sid, oid)
    pos.set_item_qty(db, sid, oid, [i for i in pos.order_items(db, oid) if i["name"] == "Birra media"][0]["id"], 1)
    pos.request_bill(db, sid, oid)
    assert pos.customer_reply(db, token, True) == "confirmed"
    assert pos.totals(db, pos.get_order(db, sid, oid))["total"] == 2100


def test_customer_reply_only_while_waiting(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    with pytest.raises(pos.PosError):
        pos.customer_reply(db, "inesistente", True)
    token = pos.request_bill(db, sid, oid)
    pos.reopen_order(db, sid, oid)
    with pytest.raises(pos.PosError):
        pos.customer_reply(db, token, True)


def test_full_flow_cash_with_change_creates_receipt(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    token = pos.request_bill(db, sid, oid)
    pos.customer_reply(db, token, True)
    assert pos.add_payment(db, sid, oid, "contanti", 2600, tendered_cents=3000) == 400
    receipt = pos.close_order(db, sid, oid, appmod.insert_receipt)
    assert receipt["total_cents"] == 2600 and receipt["payment_method"] == "Contanti"
    assert receipt["table_label"] == "Tavolo 1"
    items = db.execute("SELECT * FROM receipt_items WHERE receipt_id = ?", (receipt["id"],)).fetchall()
    assert sum(i["line_total_cents"] for i in items) == 2600
    o = pos.get_order(db, sid, oid)
    assert o["status"] == "paid" and o["fiscal_status"] == "da_emettere" and o["receipt_id"] == receipt["id"]
    assert pos.open_order_for_table(db, sid, table(db, sid)) is None  # il tavolo si libera
    # richiamare la chiusura non crea doppioni
    assert pos.close_order(db, sid, oid, appmod.insert_receipt)["id"] == receipt["id"]
    assert db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0] == 1


def test_split_bill_and_mixed_methods(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    pos.customer_reply(db, pos.request_bill(db, sid, oid), True)
    pos.add_payment(db, sid, oid, "carta", 1300)
    with pytest.raises(pos.PosError, match="Resta"):
        pos.close_order(db, sid, oid, appmod.insert_receipt)
    with pytest.raises(pos.PosError, match="supera"):
        pos.add_payment(db, sid, oid, "contanti", 1400)
    pos.add_payment(db, sid, oid, "contanti", 1300)
    assert pos.close_order(db, sid, oid, appmod.insert_receipt)["payment_method"] == "Misto"


def test_discount_is_spread_and_receipt_matches_total(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    pos.set_discount(db, sid, oid, 333)
    with pytest.raises(pos.PosError):
        pos.set_discount(db, sid, oid, 999999)
    pos.customer_reply(db, pos.request_bill(db, sid, oid), True)
    due = pos.totals(db, pos.get_order(db, sid, oid))["due"]
    assert due == 2600 - 333
    pos.add_payment(db, sid, oid, "carta", due)
    r = pos.close_order(db, sid, oid, appmod.insert_receipt)
    assert r["total_cents"] == due
    assert sum(i["line_total_cents"] for i in db.execute(
        "SELECT * FROM receipt_items WHERE receipt_id = ?", (r["id"],)).fetchall()) == due


def test_staff_can_confirm_in_person(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    with pytest.raises(pos.PosError):
        pos.staff_confirm(db, sid, oid)
    pos.request_bill(db, sid, oid)
    pos.staff_confirm(db, sid, oid, "Marco")
    o = pos.get_order(db, sid, oid)
    assert o["customer_status"] == "confirmed" and o["confirmed_by"] == "Marco"


def test_fiscal_queue_and_mark_issued(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    pos.customer_reply(db, pos.request_bill(db, sid, oid), True)
    pos.add_payment(db, sid, oid, "carta", 2600)
    pos.close_order(db, sid, oid, appmod.insert_receipt)
    assert [q["id"] for q in pos.fiscal_queue(db, sid)] == [oid]
    with pytest.raises(pos.PosError):
        pos.mark_fiscal_issued(db, sid, oid, "")
    pos.mark_fiscal_issued(db, sid, oid, "0012-0345")
    o = pos.get_order(db, sid, oid)
    assert o["fiscal_status"] == "emesso" and o["fiscal_number"] == "0012-0345"
    assert pos.fiscal_queue(db, sid) == []


def test_cancel_and_move(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    with pytest.raises(pos.PosError):
        pos.move_order(db, sid, oid, table(db, sid, "Banco")) if False else pos.move_order(db, sid, oid, 10**9)
    pos.move_order(db, sid, oid, table(db, sid, "Tavolo 2"))
    assert pos.get_order(db, sid, oid)["table_label"] == "Tavolo 2"
    pos.cancel_order(db, sid, oid)
    assert pos.get_order(db, sid, oid)["status"] == "void"


def test_floor_overview(ctx):
    db, sid = ctx
    oid = flow_to_bill(db, sid)
    pos.request_bill(db, sid, oid)
    t1 = [t for t in pos.floor_overview(db, sid) if t["label"] == "Tavolo 1"][0]
    assert t1["status"] == "preconto" and t1["total_cents"] == 2600 and t1["customer_status"] == "pending"
    assert [t for t in pos.floor_overview(db, sid) if t["label"] == "Tavolo 2"][0]["status"] == "free"
