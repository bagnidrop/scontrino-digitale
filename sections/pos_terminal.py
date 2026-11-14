"""Terminale di cassa /cassa e la sua API JSON (/cassa/api/...). Logica di conto: pos_service.py.

Endpoint: pos_terminal, pos_api_* . Non importa `app`.
"""
from flask import g, jsonify, redirect, render_template, request, session, url_for

import pos_service as pos
import rt


def register(app, ctx):
    get_db = ctx.get_db

    # Azioni che un cameriere non può fare (servono cassiere o titolare).
    CASHIER_ONLY = {"discount", "custom", "pay", "unpay", "close", "cancel"}

    def staff():
        return session.get("pos_staff")

    def locked():
        """Con il personale configurato la cassa chiede il PIN."""
        return pos.staff_mode(get_db(), g.panel_shop["id"]) and not staff()

    def api(view=None, *, open_to_locked=False, perm=None):
        """Login richiesto, errori PosError -> JSON. Il CSRF lo controlla app.csrf_protect (header X-CSRF).
        Con il personale attivo serve il PIN; `perm` indica un'azione riservata a cassiere/titolare."""
        from functools import wraps

        def deco(view):
            @wraps(view)
            def wrapped(*a, **kw):
                if g.panel_shop is None:
                    return jsonify(error="Sessione scaduta: accedi di nuovo."), 401
                if not open_to_locked and locked():
                    return jsonify(error="Inserisci il PIN.", locked=True), 403
                if perm and staff() and staff()["role"] == "cameriere":
                    return jsonify(error="Questa azione richiede il PIN di un cassiere o del titolare."), 403
                try:
                    return view(*a, **kw)
                except pos.PosError as e:
                    get_db().rollback()
                    return jsonify(error=e.message), e.status
            return wrapped
        return deco(view) if view else deco

    @app.before_request
    def pos_device_lock():
        """Un tablet su cui la cassa è in modalità PIN resta sulla cassa: il pannello si riapre solo
        con la password dell'azienda (si esce dalla cassa e si accede di nuovo)."""
        if (g.get("panel_shop") is not None and session.get("pos_device")
                and request.path.startswith("/dashboard")
                and not (staff() and staff()["role"] == "titolare")):
            return redirect(url_for("pos_terminal"))
        return None

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
            "delivery": o["delivery_pref"], "delivery_by": o["delivery_by"],
            "fiscal_number": o["fiscal_number"], "rt": rt.job_status_for_order(db, oid)[0],
            "rt_error": rt.job_status_for_order(db, oid)[1],
            "status": o["status"], "customer_status": o["customer_status"], "customer_note": o["customer_note"],
            "confirmed_by": o["confirmed_by"], "fiscal_status": o["fiscal_status"], "totals": t, "bill": bill,
            "receipt": receipt, "discount_cents": o["discount_cents"],
            "items": [{"id": i["id"], "name": i["name"], "qty": i["qty"], "unit": i["unit_price_cents"],
                       "note": i["note"], "total": i["qty"] * i["unit_price_cents"],
                       "sent": bool(i["sent_at"])} for i in items],
            "unsent": sum(1 for i in items if not i["sent_at"]),
            "payments": [{"id": p["id"], "method": p["method"], "amount": p["amount_cents"]} for p in pays],
        }

    @app.get("/cassa", endpoint="pos_terminal")
    @ctx.login_required
    def pos_terminal():
        if pos.staff_mode(get_db(), sid()):
            session["pos_device"] = True
        else:
            session.pop("pos_device", None)
            session.pop("pos_staff", None)
        return render_template("pos/terminal.html", staff_mode=bool(session.get("pos_device")), me_staff=staff())

    @app.get("/cassa/api/floor")
    @api
    def pos_api_floor():
        return jsonify(tables=pos.floor_overview(get_db(), sid()), staff=staff())

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
        oid = pos.open_order(get_db(), sid(), tid, b.get("covers", 0), (staff() or {}).get("name") or g.panel_shop["name"])
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
    @api(perm=True)
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
    @api(perm=True)
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
        if action in CASHIER_ONLY and staff() and staff()["role"] == "cameriere":
            return jsonify(error="Questa azione richiede il PIN di un cassiere o del titolare."), 403
        if action == "bill":
            pos.request_bill(db, sid(), oid)
        elif action == "reopen":
            pos.reopen_order(db, sid(), oid)
        elif action == "confirm":
            pos.staff_confirm(db, sid(), oid, (staff() or {}).get("name") or g.panel_shop["name"])
        elif action == "delivery":
            pos.set_delivery(db, sid(), oid, b.get("pref"), (staff() or {}).get("name") or g.panel_shop["name"])
        elif action == "comanda":
            batch = pos.send_to_kitchen(db, sid(), oid)
            out = order_json(oid)
            out["comanda_url"] = url_for("pos_comanda", oid=oid, batch=batch)
            return jsonify(out)
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
            rt.enqueue_if_enabled(db, sid(), oid)  # se il collegamento all'RT è attivo, parte l'emissione
        else:
            return jsonify(error="Azione sconosciuta."), 404
        return jsonify(order_json(oid))

    @app.delete("/cassa/api/orders/<int:oid>/payments/<int:pid>")
    @api(perm=True)
    def pos_api_unpay(oid, pid):
        pos.remove_payment(get_db(), sid(), oid, pid)
        return jsonify(order_json(oid))

    # ---------------------------------------------------------------- PIN del personale
    @app.get("/cassa/api/staff")
    @api(open_to_locked=True)
    def pos_api_staff():
        rows = get_db().execute("SELECT id, name, role FROM pos_staff WHERE shop_id = ? AND active = 1 ORDER BY name",
                                (sid(),)).fetchall()
        return jsonify(locked=locked(), staff=[{"id": r["id"], "name": r["name"], "role": r["role"]} for r in rows])

    @app.post("/cassa/api/unlock")
    @api(open_to_locked=True)
    def pos_api_unlock():
        b = body()
        if ctx.rate_limited(f"pin:{sid()}:{request.remote_addr}", 20, 600, shared=True):
            return jsonify(error="Troppi tentativi: riprova tra qualche minuto."), 429
        try:
            staff_id = int(b.get("staff_id"))
        except (TypeError, ValueError):
            return jsonify(error="Scegli chi sei."), 400
        st = pos.check_pin(get_db(), sid(), staff_id, b.get("pin"))
        if not st:
            return jsonify(error="PIN sbagliato."), 403
        session["pos_staff"] = {"id": st["id"], "name": st["name"], "role": st["role"]}
        return jsonify(ok=True, staff=session["pos_staff"])

    @app.post("/cassa/api/lock")
    @api(open_to_locked=True)
    def pos_api_lock():
        session.pop("pos_staff", None)
        return jsonify(ok=True)

    # ---------------------------------------------------------------- comanda stampabile
    @app.get("/cassa/comanda/<int:oid>", endpoint="pos_comanda")
    @ctx.login_required
    def pos_comanda(oid):
        if locked():
            return redirect(url_for("pos_terminal"))
        batch = request.args.get("batch", "")
        db = get_db()
        try:
            o = pos.get_order(db, sid(), oid)
            items = pos.comanda_items(db, sid(), oid, batch)
        except pos.PosError:
            items = []
        if not items:
            return render_template("error.html", code=404, text="Comanda non trovata."), 404
        return render_template("pos/comanda.html", o=o, items=items, batch=batch)
