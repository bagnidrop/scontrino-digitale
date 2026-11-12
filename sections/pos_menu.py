"""«Menu e sala»: prodotti della cassa e tavoli. Endpoint: pos_menu (+ pos_menu_post per le azioni)."""
from flask import flash, g, redirect, render_template, request, url_for

import pos_service as pos


def register(app, ctx):
    get_db = ctx.get_db

    @app.get("/dashboard/menu", endpoint="pos_menu")
    @ctx.login_required
    def pos_menu():
        db, sid = get_db(), g.panel_shop["id"]
        cats = db.execute("SELECT * FROM pos_categories WHERE shop_id = ? ORDER BY sort, id", (sid,)).fetchall()
        prods = db.execute("SELECT * FROM pos_products WHERE shop_id = ? ORDER BY sort, id", (sid,)).fetchall()
        tables = db.execute("SELECT * FROM pos_tables WHERE shop_id = ? ORDER BY sort, id", (sid,)).fetchall()
        tab = "sala" if request.args.get("tab") == "sala" else "menu"
        return render_template("panel/pos_menu.html", cats=cats, prods=prods, tables=tables, tab=tab)

    @app.post("/dashboard/menu/<action>", endpoint="pos_menu_post")
    @ctx.login_required
    def pos_menu_post(action):
        db, sid, f = get_db(), g.panel_shop["id"], request.form
        tab = "menu"
        try:
            if action == "seed":
                if not pos.seed_default_menu(db, sid):
                    flash("Il menu ha già dei prodotti.", "err")
                else:
                    flash("Menu e sala d'esempio caricati: modificali come vuoi.", "ok")
            elif action == "category":
                pos.add_category(db, sid, f.get("name"))
                flash("Categoria aggiunta.", "ok")
            elif action == "product":
                price = ctx_price(f.get("price"))
                cat = int(f["category_id"]) if f.get("category_id") else None
                pos.add_product(db, sid, cat, f.get("name"), price, float(f.get("vat_rate", 10)))
                flash("Prodotto aggiunto.", "ok")
            elif action == "product-toggle":
                db.execute("UPDATE pos_products SET active = 1 - active WHERE id = ? AND shop_id = ?",
                           (int(f.get("id", 0)), sid))
                db.commit()
            elif action == "product-price":
                db.execute("UPDATE pos_products SET price_cents = ? WHERE id = ? AND shop_id = ?",
                           (ctx_price(f.get("price")), int(f.get("id", 0)), sid))
                db.commit()
                flash("Prezzo aggiornato.", "ok")
            elif action == "table":
                tab = "sala"
                pos.add_table(db, sid, f.get("label"), f.get("area"), f.get("seats", 2))
                flash("Tavolo aggiunto.", "ok")
            elif action == "table-toggle":
                tab = "sala"
                db.execute("UPDATE pos_tables SET active = 1 - active WHERE id = ? AND shop_id = ?",
                           (int(f.get("id", 0)), sid))
                db.commit()
            else:
                return redirect(url_for("pos_menu"))
        except (ValueError, KeyError):
            db.rollback()
            flash("Controlla i dati inseriti.", "err")
        except pos.PosError as e:
            flash(e.message, "err")
        return redirect(url_for("pos_menu", tab=tab))


def ctx_price(text):
    """«8,50» o «8.5» (euro) -> centesimi."""
    from decimal import Decimal, InvalidOperation
    try:
        return int((Decimal(str(text).replace(",", ".").strip()) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise pos.PosError("Prezzo non valido: scrivilo come 8,50.")
