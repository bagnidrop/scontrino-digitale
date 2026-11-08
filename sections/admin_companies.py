"""Amministrazione · Aziende: elenco, nuova azienda, dettaglio e azioni (solo proprietario del servizio).

Endpoint: admin_companies, admin_company_new, admin_company_detail e le azioni POST
  /admin/companies/<cid>/suspend | activate | invite | notes | impersonate | delete

Regole (DESIGN.md §9.4): ogni route è @ctx.owner_required; il link d'invito si vede UNA volta sola
(session["invite_once"], mai nel database, nel registro o in un URL); gli aggregati sono SQL portabile.
Questo modulo NON deve importare `app` (importazione circolare).
"""
from datetime import timedelta
from urllib.parse import quote

from flask import abort, current_app, flash, g, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

PAGE_SIZE = 20
INACTIVE_DAYS = 14
STATUS_KEYS = ("all", "active", "invited", "suspended", "inactive")
SORT_KEYS = ("recent", "name", "activity")
STATUS_LABELS = {"active": ("Attiva", "ok"), "invited": ("In attesa d'invito", "info"),
                 "suspended": ("Sospesa", "danger")}
# a quale pagina del pannello porta la card di uso quando il proprietario entra come l'azienda
GOTO = {"receipts": ("receipts_list", {"period": "30d"}),
        "messages": ("messages_list", {"period": "30d"}),
        "invoices": ("invoices_list", {"status": "nuova"}),
        "contacts": ("contacts_list", {}),
        "dashboard": ("dashboard", {})}
ACTION_LABELS = {
    "company.create": "Azienda creata",
    "company.invite_reissued": "Nuovo link d'invito generato",
    "company.invite_accepted": "Invito accettato: l'azienda è entrata",
    "company.password_reset": "L'azienda ha scelto una nuova password",
    "company.notes": "Note interne aggiornate",
    "company.suspend": "Azienda sospesa",
    "company.activate": "Azienda riattivata",
    "impersonation.start": "Sei entrato nel suo pannello",
    "impersonation.stop": "Sei tornato all'amministrazione",
    "impersonation.action": "Azione fatta mentre eri nel suo pannello",
}


def register(app, ctx):
    get_db = ctx.get_db
    owner_required = ctx.owner_required

    # ------------------------------------------------------------------ utilità
    def _owner_emails():
        return current_app.config.get("OWNER_EMAILS", set())

    def _shop_or_404(cid):
        """Azienda gestibile: esiste e non è l'account del proprietario (per lui la pagina non esiste)."""
        shop = get_db().execute("SELECT * FROM shops WHERE id = ?", (cid,)).fetchone()
        if shop is None or shop["email"].lower() in _owner_emails():
            abort(404)
        return shop

    def _count(sql, params):
        return get_db().execute(sql, params).fetchone()[0] or 0

    def _since(days):
        return (ctx.now_utc() - timedelta(days=days)).isoformat(timespec="seconds")

    def _usage(cid):
        """Conteggi aggregati di UNA azienda (nessun dato dei clienti, solo numeri)."""
        d30 = _since(30)
        return {
            "receipts30": _count("SELECT COUNT(*) FROM receipts WHERE shop_id = ? AND created_at >= ?"
                                 " AND voided_at IS NULL", (cid, d30)),
            "messages30": _count("SELECT COUNT(*) FROM messages m JOIN receipts r ON r.id = m.receipt_id"
                                 " WHERE r.shop_id = ? AND m.created_at >= ? AND m.status = 'sent'", (cid, d30)),
            "invoices_new": _count("SELECT COUNT(*) FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
                                   " WHERE r.shop_id = ? AND i.status = 'nuova' AND r.voided_at IS NULL", (cid,)),
            "contacts": _count("SELECT COUNT(*) FROM contacts WHERE shop_id = ?", (cid,)),
            "receipts_all": _count("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (cid,)),
            "invoices_all": _count("SELECT COUNT(*) FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
                                   " WHERE r.shop_id = ?", (cid,)),
            "last_receipt": get_db().execute(
                "SELECT MAX(created_at) FROM receipts WHERE shop_id = ? AND voided_at IS NULL", (cid,)).fetchone()[0],
        }

    def _invite_text(name, url, reset):
        what = "scegliere una nuova password" if reset else "scegliere la tua password ed entrare"
        intro = (f"Ciao! Ecco il link per {what} su ScontrinoDigitale ({name})."
                 if reset else
                 f"Ciao! Ho creato l'account di {name} su ScontrinoDigitale. Apri questo link per {what}.")
        return (f"{intro}\n{url}\nIl link vale {ctx.INVITE_DAYS} giorni e si può usare una sola volta.")

    def _show_invite(shop, url, reset):
        """Schermata «link mostrato una sola volta». Non finisce mai in cache né nel database."""
        fresh = get_db().execute("SELECT invite_expires_at FROM shops WHERE id = ?", (shop["id"],)).fetchone()
        text = _invite_text(shop["name"], url, reset)
        resp = make_response(render_template(
            "panel/admin_company_new.html", mode="success", shop=shop, invite_url=url, invite_text=text,
            expires_at=fresh["invite_expires_at"] if fresh else None, reset=reset,
            wa_url="https://wa.me/?text=" + quote(text),
            mail_url="mailto:" + quote(shop["email"]) + "?subject=" + quote("Il tuo accesso a ScontrinoDigitale")
                     + "&body=" + quote(text)))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ elenco
    def _load_companies():
        """Tutte le aziende (non il proprietario) con gli aggregati: 4 query in tutto, qualunque sia il numero di righe."""
        db = get_db()
        shops = db.execute("SELECT id, name, email, status, created_at, last_login_at, invite_expires_at"
                           " FROM shops").fetchall()
        d30, d14 = _since(30), _since(INACTIVE_DAYS)
        last = {r[0]: r[1] for r in db.execute(
            "SELECT shop_id, MAX(created_at) FROM receipts WHERE voided_at IS NULL GROUP BY shop_id").fetchall()}
        r30 = {r[0]: r[1] for r in db.execute(
            "SELECT shop_id, COUNT(*) FROM receipts WHERE created_at >= ? AND voided_at IS NULL GROUP BY shop_id",
            (d30,)).fetchall()}
        m30 = {r[0]: r[1] for r in db.execute(
            "SELECT r.shop_id, COUNT(*) FROM messages m JOIN receipts r ON r.id = m.receipt_id"
            " WHERE m.created_at >= ? AND m.status = 'sent' GROUP BY r.shop_id", (d30,)).fetchall()}
        owners = _owner_emails()
        rows = []
        for s in shops:
            if s["email"].lower() in owners:
                continue
            la = last.get(s["id"])
            rows.append({
                "id": s["id"], "name": s["name"], "email": s["email"], "status": s["status"],
                "created_at": s["created_at"], "last_login_at": s["last_login_at"],
                "invite_expires_at": s["invite_expires_at"], "last_receipt": la,
                "receipts30": r30.get(s["id"], 0), "messages30": m30.get(s["id"], 0),
                # inattiva solo se esiste da almeno 14 giorni: chi è appena entrato non ha ancora emesso nulla
                "inactive": (s["status"] == "active" and s["created_at"] < d14
                             and (la is None or la < d14)),
            })
        return rows

    @app.get("/admin/companies")
    @owner_required
    def admin_companies():
        status = request.args.get("status", "all")
        status = status if status in STATUS_KEYS else "all"
        sort = request.args.get("sort", "recent")
        sort = sort if sort in SORT_KEYS else "recent"
        q = (request.args.get("q") or "").strip()[:80]
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1

        everyone = _load_companies()
        counts = {"all": len(everyone),
                  "active": sum(1 for r in everyone if r["status"] == "active"),
                  "invited": sum(1 for r in everyone if r["status"] == "invited"),
                  "suspended": sum(1 for r in everyone if r["status"] == "suspended"),
                  "inactive": sum(1 for r in everyone if r["inactive"])}
        rows = everyone
        if status == "inactive":
            rows = [r for r in rows if r["inactive"]]
        elif status != "all":
            rows = [r for r in rows if r["status"] == status]
        if q:
            low = q.lower()
            rows = [r for r in rows if low in r["name"].lower() or low in r["email"].lower()]
        if sort == "name":
            rows = sorted(rows, key=lambda r: (r["name"].lower(), r["id"]))
        elif sort == "activity":
            rows = sorted(rows, key=lambda r: (r["receipts30"], r["last_receipt"] or "", r["id"]), reverse=True)
        else:
            rows = sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)

        total = len(rows)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(page, pages)
        rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

        def url(**kw):
            args = {"status": status, "sort": sort, "q": q}
            args.update(kw)
            return url_for("admin_companies", **{k: v for k, v in args.items() if v and v != "all"
                                                 and not (k == "sort" and v == "recent")})

        labels = [("Tutte", "all"), ("Attive", "active"), ("In attesa d'invito", "invited"),
                  ("Sospese", "suspended"), (f"Inattive da {INACTIVE_DAYS} giorni", "inactive")]
        tab_items = [(lab, url(status=key, page=None), status == key, counts[key]) for lab, key in labels]
        return render_template(
            "panel/admin_companies.html", rows=rows, total=total, page=page, pages=pages, status=status,
            sort=sort, q=q, tab_items=tab_items, counts=counts, STATUS_LABELS=STATUS_LABELS,
            has_filters=bool(q or status != "all"), clear_url=url_for("admin_companies"),
            inactive_days=INACTIVE_DAYS)

    # ------------------------------------------------------------------ nuova azienda
    @app.route("/admin/companies/new", methods=["GET", "POST"])
    @owner_required
    def admin_company_new():
        form = {"name": "", "email": "", "notes": ""}
        errors = {}
        if request.method == "POST":
            form = {"name": request.form.get("name", "").strip(),
                    "email": request.form.get("email", "").strip().lower(),
                    "notes": request.form.get("notes", "").strip()}
            if not form["name"]:
                errors["name"] = "Scrivi il nome dell'azienda."
            elif len(form["name"]) > 120:
                errors["name"] = "Il nome è troppo lungo (massimo 120 caratteri)."
            if not form["email"] or len(form["email"]) > 254 or not ctx.valid_email(form["email"]):
                errors["email"] = "Scrivi un'email valida, per esempio info@trattoria.it."
            elif get_db().execute("SELECT 1 FROM shops WHERE LOWER(email) = ?", (form["email"],)).fetchone():
                errors["email"] = "Questa email è già in uso: scegline un'altra."
            if len(form["notes"]) > 500:
                errors["notes"] = "Le note sono troppo lunghe (massimo 500 caratteri)."
            if not errors:
                try:
                    cid, token = ctx.create_invited_shop(form["name"], form["email"], form["notes"])
                except Exception:  # per esempio email creata nello stesso istante da un'altra richiesta
                    get_db().rollback()
                    errors["email"] = "Questa email è già in uso: scegline un'altra."
                else:
                    shop = get_db().execute("SELECT * FROM shops WHERE id = ?", (cid,)).fetchone()
                    ctx.audit("company.create", shop, "Azienda creata con invito")
                    session["invite_once"] = {"cid": cid, "url": ctx.invite_url(token), "reset": False}
                    return redirect(url_for("admin_company_detail", cid=cid))
            if errors:
                flash("Controlla i campi evidenziati.", "err")
        return render_template("panel/admin_company_new.html", mode="form", form=form, errors=errors,
                               invite_days=ctx.INVITE_DAYS)

    # ------------------------------------------------------------------ dettaglio
    @app.get("/admin/companies/<int:cid>")
    @owner_required
    def admin_company_detail(cid):
        shop = _shop_or_404(cid)
        once = session.pop("invite_once", None)  # si vede UNA volta: la rimuoviamo comunque
        if once and once.get("cid") == cid and shop["status"] != "suspended":
            return _show_invite(shop, once["url"], bool(once.get("reset")))
        usage = _usage(cid)
        activity = get_db().execute(
            "SELECT action, actor_email, detail, created_at FROM admin_audit WHERE target_shop_id = ?"
            " ORDER BY id DESC LIMIT 12", (cid,)).fetchall()
        expired = bool(shop["invite_expires_at"] and ctx.parse_iso(shop["invite_expires_at"]) < ctx.now_utc())
        label, tone = STATUS_LABELS.get(shop["status"], (shop["status"], "neutral"))
        return render_template(
            "panel/admin_company_detail.html", shop=shop, usage=usage, activity=activity, expired=expired,
            status_label=label, status_tone=tone, ACTION_LABELS=ACTION_LABELS)

    # ------------------------------------------------------------------ azioni
    def _back(cid, anchor=""):
        return redirect(url_for("admin_company_detail", cid=cid) + anchor)

    @app.post("/admin/companies/<int:cid>/suspend")
    @owner_required
    def admin_company_suspend(cid):
        shop = _shop_or_404(cid)
        try:
            ctx.set_shop_status(cid, "suspended")
            flash(f"{shop['name']} è stata sospesa: non può più entrare né inviare scontrini.")
        except ValueError:
            flash("Questa azienda non si può sospendere ora (si sospendono solo le aziende attive).", "err")
        return _back(cid)

    @app.post("/admin/companies/<int:cid>/activate")
    @owner_required
    def admin_company_activate(cid):
        shop = _shop_or_404(cid)
        try:
            ctx.set_shop_status(cid, "active")
            flash(f"{shop['name']} è di nuovo attiva.")
        except ValueError:
            flash("Questa azienda non si può riattivare: non è sospesa.", "err")
        return _back(cid)

    @app.post("/admin/companies/<int:cid>/invite")
    @owner_required
    def admin_company_invite(cid):
        shop = _shop_or_404(cid)
        if shop["status"] == "suspended":
            flash("L'azienda è sospesa: riattivala prima di generare un nuovo link.", "err")
            return _back(cid)
        try:
            token = ctx.issue_invite(cid)
        except ValueError:
            flash("Non è possibile generare il link per questa azienda.", "err")
            return _back(cid)
        reset = shop["status"] == "active"
        ctx.audit("company.invite_reissued", shop, "Nuovo link per reimpostare la password" if reset
                  else "Nuovo link d'invito")
        session["invite_once"] = {"cid": cid, "url": ctx.invite_url(token), "reset": reset}
        return _back(cid)

    @app.post("/admin/companies/<int:cid>/notes")
    @owner_required
    def admin_company_notes(cid):
        shop = _shop_or_404(cid)
        notes = request.form.get("notes", "").strip()
        if len(notes) > 500:
            flash("Le note sono troppo lunghe (massimo 500 caratteri): non sono state salvate.", "err")
            return _back(cid, "#note")
        db = get_db()
        db.execute("UPDATE shops SET owner_notes = ? WHERE id = ?", (notes, cid))
        db.commit()
        ctx.audit("company.notes", shop, "Note interne aggiornate")
        flash("Note salvate.")
        return _back(cid, "#note")

    @app.post("/admin/companies/<int:cid>/impersonate")
    @owner_required
    def admin_company_impersonate(cid):
        shop = _shop_or_404(cid)
        if shop["status"] != "active":
            flash("Puoi entrare solo nel pannello di un'azienda attiva.", "err")
            return _back(cid)
        ctx.begin_impersonation(cid)
        endpoint, args = GOTO.get(request.form.get("goto", ""), GOTO["dashboard"])
        return redirect(url_for(endpoint, **args))

    @app.post("/admin/companies/<int:cid>/delete")
    @owner_required
    def admin_company_delete(cid):
        shop = _shop_or_404(cid)
        typed = request.form.get("confirm_name", "").strip()
        password = request.form.get("password", "")
        if typed != shop["name"]:
            flash("Il nome scritto non coincide con quello dell'azienda: non è stato eliminato nulla.", "err")
            return _back(cid, "#zona-pericolosa")
        if not password or not check_password_hash(g.panel_shop["password_hash"], password):
            flash("La tua password non è corretta: non è stato eliminato nulla.", "err")
            return _back(cid, "#zona-pericolosa")
        name = shop["name"]
        try:
            ctx.delete_shop(cid)
        except ValueError:
            flash("Questa azienda non si può eliminare.", "err")
            return _back(cid)
        flash(f"{name} è stata eliminata, con tutti i suoi dati.")
        return redirect(url_for("admin_companies"))
