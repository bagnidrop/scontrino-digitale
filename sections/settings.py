"""Sezione «Impostazioni» del pannello.

Una sola pagina a schede (`?tab=`): locale, messaggio, fatture, account.
Le route stanno dentro register(): gli helper condivisi arrivano da `ctx` (vedi sections/__init__.py).
Questo modulo NON deve importare `app` (importazione circolare).
"""
import re

from flask import Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import logo

TABS = [("locale", "Locale"), ("messaggio", "Messaggio ai clienti"), ("fatture", "Fatture"), ("account", "Account e sicurezza")]
TAB_KEYS = [k for k, _ in TABS]
DEFAULT_TEMPLATE = "Grazie da {negozio}! Ecco il tuo scontrino digitale: {link}"
SAMPLE_TOKEN = "esempio1234"


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    valid_piva = ctx.valid_piva
    new_api_key = ctx.new_api_key
    messaging = ctx.messaging

    def base_url():
        return app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")

    def current_tab():
        t = request.values.get("tab", "locale")
        return t if t in TAB_KEYS else "locale"

    def load_shop(db, sid):
        return db.execute("SELECT * FROM shops WHERE id = ?", (sid,)).fetchone()

    def render_page(tab, values=None, errors=None, status=200):
        db = get_db()
        sid = g.panel_shop["id"]
        s = load_shop(db, sid)
        v = {
            "name": s["name"] or "", "address": s["address"] or "", "vat_number": s["vat_number"] or "",
            "review_url": s["review_url"] or "", "footer_note": s["footer_note"] or "",
            "message_template": s["message_template"] or DEFAULT_TEMPLATE,
            "invoice_window_days": s["invoice_window_days"] if s["invoice_window_days"] is not None else 3,
        }
        v.update(values or {})
        sample_link = base_url() + "/r/" + SAMPLE_TOKEN
        preview = messaging.render_template_text(v["message_template"], s["name"] or "", sample_link)
        tab_items = [(label, url_for("settings", tab=k), k == tab, None) for k, label in TABS]
        shop_logo = db.execute("SELECT etag, width, height, updated_at FROM shop_logos WHERE shop_id = ?",
                               (sid,)).fetchone()
        html = render_template(
            "panel/settings.html", tab=tab, tab_items=tab_items, s=s, v=v, errors=errors or {}, shop_logo=shop_logo,
            new_key=session.pop("new_api_key", None), base_url=base_url(), preview=preview,
            sample_link=sample_link, default_template=DEFAULT_TEMPLATE)
        return html, status

    @app.route("/dashboard/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        tab = current_tab()
        if request.method == "GET":
            return render_page(tab)
        db = get_db()
        sid = g.panel_shop["id"]
        f = request.form
        cur = load_shop(db, sid)
        values, errors = {}, {}

        if tab == "locale":
            name = f.get("name", "").strip()[:80]
            address = f.get("address", "").strip()[:200]
            review = f.get("review_url", "").strip()[:300]
            vat = re.sub(r"\s", "", f.get("vat_number", "")).upper().removeprefix("IT")
            note = f.get("footer_note", "").strip()[:300]
            values = {"name": name, "address": address, "review_url": review, "vat_number": vat, "footer_note": note}
            if not name:
                errors["name"] = "Il nome del locale è obbligatorio."
            if review and not review.startswith("https://"):
                errors["review_url"] = "Il link recensioni deve iniziare con https://"
            if vat and not valid_piva(vat):
                errors["vat_number"] = "La partita IVA non è valida."
            if not errors:
                db.execute("UPDATE shops SET name=?, address=?, vat_number=?, review_url=?, footer_note=? WHERE id=?",
                           (name, address, vat, review, note, sid))
                db.commit()
                flash("Dati del locale salvati.", "ok")
                return redirect(url_for("settings", tab=tab))

        elif tab == "messaggio":
            template = f.get("message_template", "").strip()[:300]
            values = {"message_template": template}
            if not template:
                errors["message_template"] = "Scrivi il messaggio da inviare ai clienti."
            else:
                if "{link}" not in template:
                    template = (template + " {link}").strip()
                db.execute("UPDATE shops SET message_template=? WHERE id=?", (template, sid))
                db.commit()
                flash("Messaggio salvato." + (" Ho aggiunto {link}, che serve al cliente per aprire lo scontrino."
                                              if "{link}" not in f.get("message_template", "") else ""), "ok")
                return redirect(url_for("settings", tab=tab))

        elif tab == "fatture":
            raw = f.get("invoice_window_days", "").strip()
            try:
                window = int(raw)
            except ValueError:
                window = None
            values = {"invoice_window_days": raw}
            if window is None or not 0 <= window <= 30:
                errors["invoice_window_days"] = "Inserisci un numero di giorni da 0 a 30."
            else:
                db.execute("UPDATE shops SET invoice_window_days=? WHERE id=?", (window, sid))
                db.commit()
                flash("Finestra per le fatture salvata.", "ok")
                return redirect(url_for("settings", tab=tab))
        else:
            return redirect(url_for("settings", tab=tab))

        flash("Controlla i campi evidenziati: non ho salvato nulla.", "err")
        return render_page(tab, values, errors, 400)

    # ------------------------------------------------------------------ logo dell'azienda
    @app.post("/dashboard/settings/logo")
    @login_required
    def settings_logo():
        """Carica (o sostituisce) il logo. Il file viene controllato e ricodificato: vedi logo.py."""
        f = request.files.get("logo")
        raw = f.read(logo.MAX_UPLOAD + 1) if f else b""
        try:
            data, width, height, etag = logo.process_logo(raw)
        except logo.LogoError as e:
            flash(str(e), "err")
            return redirect(url_for("settings", tab="locale"))
        db = get_db()
        sid = g.panel_shop["id"]
        db.execute("DELETE FROM shop_logos WHERE shop_id = ?", (sid,))
        db.execute("INSERT INTO shop_logos (shop_id, data, width, height, etag, updated_at)"
                   " VALUES (?,?,?,?,?,?)", (sid, data, width, height, etag, ctx.now_iso()))
        db.commit()
        flash("Logo aggiornato: da ora compare in cima agli scontrini.", "ok")
        return redirect(url_for("settings", tab="locale"))

    @app.post("/dashboard/settings/logo/delete")
    @login_required
    def settings_logo_delete():
        db = get_db()
        db.execute("DELETE FROM shop_logos WHERE shop_id = ?", (g.panel_shop["id"],))
        db.commit()
        flash("Logo rimosso.", "ok")
        return redirect(url_for("settings", tab="locale"))

    @app.get("/logo/<etag>.png")
    def logo_image(etag):
        """Pubblico: l'indirizzo dipende dal contenuto (non dall'azienda), quindi non si può indovinare
        né elencare, e può essere memorizzato per sempre: se il logo cambia, cambia l'indirizzo."""
        if not re.fullmatch(r"[0-9a-f]{24}", etag):
            abort(404)
        row = get_db().execute("SELECT data FROM shop_logos WHERE etag = ? LIMIT 1", (etag,)).fetchone()
        if row is None:
            abort(404)
        resp = Response(bytes(row["data"]), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.post("/dashboard/settings/password")
    @login_required
    def settings_password():
        db = get_db()
        sid = g.panel_shop["id"]
        row = db.execute("SELECT password_hash FROM shops WHERE id = ?", (sid,)).fetchone()
        old = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        error = None
        if not old or not check_password_hash(row["password_hash"], old):
            error = "La password attuale non è corretta."
        elif len(new) < 8:
            error = "La nuova password deve avere almeno 8 caratteri."
        elif new != confirm:
            error = "La conferma non coincide con la nuova password."
        elif new == old:
            error = "La nuova password deve essere diversa da quella attuale."
        if error:
            flash(error, "err")
            return render_page("account", errors={"password": error}, status=400)
        db.execute("UPDATE shops SET password_hash = ? WHERE id = ?", (generate_password_hash(new), sid))
        db.commit()
        flash("Password cambiata. Da ora usa quella nuova per accedere.", "ok")
        return redirect(url_for("settings", tab="account"))

    @app.post("/dashboard/settings/regenerate-key")
    @login_required
    def regenerate_key():
        key, key_hash, prefix = new_api_key()
        db = get_db()
        db.execute("UPDATE shops SET api_key_hash = ?, api_key_prefix = ? WHERE id = ?",
                   (key_hash, prefix, g.panel_shop["id"]))
        db.commit()
        session["new_api_key"] = key
        flash("Nuova chiave API generata: quella precedente non funziona più.", "ok")
        return redirect(url_for("settings", tab="account"))
