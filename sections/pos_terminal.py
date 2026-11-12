"""Terminale di cassa /cassa e la sua API JSON (/cassa/api/...). Logica di conto: pos_service.py.

Endpoint: pos_terminal, pos_api_* . Non importa `app`.
"""
from flask import g, jsonify, render_template, request, url_for

import pos_service as pos


def register(app, ctx):
    get_db = ctx.get_db

    def api(view):
        """Login richiesto, errori PosError -> JSON. Il CSRF lo controlla app.csrf_protect (header X-CSRF)."""
        from functools import wraps

        @wraps(view)
        def wrapped(*a, **kw):
            if g.panel_shop is None:
                return jsonify(error="Sessione scaduta: accedi di nuovo."), 401
            try:
                return view(*a, **kw)
            except pos.PosError as e:
                get_db().rollback()
                return jsonify(error=e.message), e.status
        return wrapped

    def body():
        return request.get_json(silent=True) or {}

    def sid():
        return g.panel_shop["id"]

    def order_json(oid):
        db = get_db()
        o = pos.get_order(db, sid(), oid)
        items = pos.order_items(db, oid)
        pays = db.execute("SELECT * FROM pos_payments WHERE order_id = ? ORDER BY id", (oid,)).fetchall()
        t = pos.totals(db, o)
        receipt = None
        if o["receipt_id"]:
            r = db.execute("SELECT token, number FROM receipts WHERE id = ?", (o["receipt_id"],)).fetchone()
            receipt = {"number": r["number"], "url": ctx.public_url(r["token"]),
                       "qr": url_for("receipt_qr", token=r["token"])}
        bill = None
        if o["bill_token"]:
            bill = {"url": ctx.public_url(o["bill_token"]).replace("/r/", "/p/"),
                    "qr": url_for("pos_bill_qr", token=o["bill_token"])}
        return {
            "id": o["id"], "table_id": o["table_id"], "table": o["table_label"], "covers": o["covers"],
            "status": o["status"], "customer_status": o["customer_status"], "customer_note": o["customer_note"],
            "confirmed_by": o["confirmed_by"], "fiscal_status": o["fiscal_status"], "totals": t, "bill": bill,
            "receipt": receipt, "discount_cents": o["discount_cents"],
            "items": [{"id": i["id"], "name": i["name"], "qty": i["qty"], "unit": i["unit_price_cents"],
                       "note": i["note"], "total": i["qty"] * i["unit_price_cents"]} for i in items],
            "payments": [{"id": p["id"], "method": p["method"], "amount": p["amount_cents"]} for p in pays],
        }

    @app.get("/cassa", endpoint="pos_terminal")
    @ctx.login_required
    def pos_terminal():
        return render_template("pos/terminal.html")

    @app.get("/cassa/api/floor")
    @api
    def pos_api_floor():
        return jsonify(tables=pos.floor_overview(get_db(), sid()))

    @app.get("/cassa/api/menu")
    @api
    def pos_api_menu():
        db = get_db()
        cats = db.execute("SELECT id, name FROM pos_categories WHERE shop_id = ? AND active = 1 ORDER BY sort, id",
                          (sid(),)).fetchall()
        prods = db.execute("SELECT id, category_id, name, price_cents, vat_rate FROM pos_products"
                           " WHERE shop_id = ? AND active = 1 ORDER BY sort, id", (sid(),)).fetchall()
        return jsonify(categories=[{"id": c["id"], "name": c["name"]} for c in cats],
                       products=[{"id": p["id"], "category_id": p["category_id"], "name": p["name"],
                                  "price": p["price_cents"]} for p in prods])

    @app.post("/cassa/api/orders")
    @api
    def pos_api_open():
        b = body()
        try:
            tid = int(b.get("table_id"))
        except (TypeError, ValueError):
            raise pos.PosError("Scegli un tavolo.")
        oid = pos.open_order(get_db(), sid(), tid, b.get("covers", 0), g.panel_shop["name"])
        return jsonify(order_json(oid))

    @app.get("/cassa/api/orders/<int:oid>")
    @api
    def pos_api_order(oid):
        return jsonify(order_json(oid))

    @app.post("/cassa/api/orders/<int:oid>/items")
    @api
    def pos_api_add_item(oid):
        b = body()
        pos.add_item(get_db(), sid(), oid, b.get("product_id"), b.get("qty", 1), b.get("note", ""))
        return jsonify(order_json(oid))

    @app.post("/cassa/api/orders/<int:oid>/custom")
    @api
    def pos_api_custom(oid):
        b = body()
        pos.add_custom_item(get_db(), sid(), oid, b.get("name"), b.get("price_cents"), b.get("vat_rate", 10))
        return jsonify(order_json(oid))

    @app.patch("/cassa/api/orders/<int:oid>/items/<int:iid>")
    @api
    def pos_api_item_qty(oid, iid):
        pos.set_item_qty(get_db(), sid(), oid, iid, body().get("qty"))
        return jsonify(order_json(oid))

    @app.post("/cassa/api/orders/<int:oid>/discount")
    @api
    def pos_api_discount(oid):
        pos.set_discount(get_db(), sid(), oid, body().get("discount_cents", 0))
        return jsonify(order_json(oid))

    @app.post("/cassa/api/orders/<int:oid>/covers")
    @api
    def pos_api_covers(oid):
        pos.set_covers(get_db(), sid(), oid, body().get("covers", 0))
        return jsonify(order_json(oid))

    @app.post("/cassa/api/orders/<int:oid>/<action>")
    @api
    def pos_api_action(oid, action):
        db, b = get_db(), body()
        if action == "bill":
            pos.request_bill(db, sid(), oid)
        elif action == "reopen":
            pos.reopen_order(db, sid(), oid)
        elif action == "confirm":
            pos.staff_confirm(db, sid(), oid, g.panel_shop["name"])
        elif action == "cancel":
            pos.cancel_order(db, sid(), oid)
        elif action == "move":
            pos.move_order(db, sid(), oid, b.get("table_id"))
        elif action == "pay":
            change = pos.add_payment(db, sid(), oid, b.get("method"), b.get("amount_cents"), b.get("tendered_cents"))
            out = order_json(oid)
            out["change_cents"] = change
            return jsonify(out)
        elif action == "close":
            pos.close_order(db, sid(), oid, ctx.insert_receipt)
        else:
            return jsonify(error="Azione sconosciuta."), 404
        return jsonify(order_json(oid))

    @app.delete("/cassa/api/orders/<int:oid>/payments/<int:pid>")
    @api
    def pos_api_unpay(oid, pid):
        pos.remove_payment(get_db(), sid(), oid, pid)
        return jsonify(order_json(oid))
