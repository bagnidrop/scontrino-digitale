"""Sezione «Scontrini» del pannello: elenco filtrabile, esportazione CSV, dettaglio e invio al cliente.

Le route stanno dentro register(): gli helper condivisi arrivano da `ctx` (vedi sections/__init__.py).
Questo modulo NON deve importare `app` (importazione circolare).
"""
import csv
import io
from datetime import datetime

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

from .common import PERIOD_LABELS, day_bounds, period_since

PER_PAGE = 30
SEND_LIMIT = 3  # come l'API: massimo invii per scontrino
CSV_MAX_ROWS = 50000


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    LOCAL_TZ = ctx.LOCAL_TZ
    fmt_dt = ctx.fmt_dt
    csv_safe = ctx.csv_safe
    now_iso = ctx.now_iso
    messaging = ctx.messaging
    public_url = ctx.public_url
    vat_breakdown = ctx.vat_breakdown

    # ------------------------------------------------------------------ filtri
    def _filters():
        """Legge i parametri URL e ritorna un dict con i filtri normalizzati."""
        a = request.args
        period, _since = period_since(a.get("period", ""), LOCAL_TZ, default="all")
        day = a.get("day", "").strip()
        if day and day_bounds(day, LOCAL_TZ) is None:
            day = ""
        status = a.get("status", "all")
        if status not in ("all", "valid", "void"):
            status = "all"
        return {"period": period, "day": day, "status": status,
                "payment": a.get("payment", "").strip()[:30], "q": a.get("q", "").strip()[:60]}

    def _where(f, with_status=True):
        sql, params = ["r.shop_id = ?"], [g.panel_shop["id"]]
        if f["day"]:
            start, end = day_bounds(f["day"], LOCAL_TZ)
            sql.append("r.created_at >= ? AND r.created_at < ?")
            params += [start, end]
        else:
            _k, since = period_since(f["period"], LOCAL_TZ, default="all")
            if since:
                sql.append("r.created_at >= ?")
                params.append(since)
        if f["payment"]:
            sql.append("r.payment_method = ?")
            params.append(f["payment"])
        if f["q"]:
            raw = f["q"].lstrip("#").strip().lower()
            like = "%" + raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            esc = " ESCAPE '\\'"
            parts = ["LOWER(r.table_label) LIKE ?" + esc, "LOWER(r.lottery_code) LIKE ?" + esc,
                     "EXISTS (SELECT 1 FROM receipt_items it WHERE it.receipt_id = r.id"
                     " AND LOWER(it.name) LIKE ?" + esc + ")"]
            qparams = [like, like, like]
            if len(raw) >= 8:  # cerca nel codice del link solo se sembra un codice, non una cifra qualsiasi
                parts.append("LOWER(r.token) LIKE ?" + esc)
                qparams.append(like)
            if raw.isdigit() and len(raw) <= 9:
                parts.insert(0, "r.number = ?")
                qparams.insert(0, int(raw))
            cond = " OR ".join(parts)
            params += qparams
            sql.append("(" + cond + ")")
        if with_status and f["status"] == "valid":
            sql.append("r.voided_at IS NULL")
        elif with_status and f["status"] == "void":
            sql.append("r.voided_at IS NOT NULL")
        return " AND ".join(sql), params

    SELECT = ("SELECT r.*,"
              " (SELECT COUNT(*) FROM invoice_requests i WHERE i.receipt_id = r.id) AS has_invoice,"
              " (SELECT COUNT(*) FROM messages m WHERE m.receipt_id = r.id AND m.status = 'sent') AS sent_count"
              " FROM receipts r WHERE ")

    def _url(**over):
        """URL dell'elenco con i parametri correnti, sostituiti/rimossi da `over` (None = rimuovi)."""
        args = {k: v for k, v in request.args.items() if v not in ("", None)}
        args.pop("page", None)
        for k, v in over.items():
            if v is None or v == "":
                args.pop(k, None)
            else:
                args[k] = v
        return url_for("receipts_list", **args)

    def _chips(f):
        chips = []
        if f["day"]:
            try:
                label = datetime.strptime(f["day"], "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                label = f["day"]
            chips.append(("Giorno " + label, _url(day=None)))
        elif f["period"] != "all":
            chips.append((PERIOD_LABELS[f["period"]], _url(period=None)))
        if f["status"] != "all":
            chips.append(("Solo validi" if f["status"] == "valid" else "Solo annullati", _url(status=None)))
        if f["payment"]:
            chips.append((f["payment"], _url(payment=None)))
        if f["q"]:
            chips.append(("Cerca: " + f["q"], _url(q=None)))
        return chips

    @app.get("/dashboard/receipts")
    @login_required
    def receipts_list():
        db = get_db()
        sid = g.panel_shop["id"]
        f = _filters()
        page = max(1, request.args.get("page", 1, type=int) or 1)

        base_where, base_params = _where(f, with_status=False)
        agg = db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN r.voided_at IS NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN r.voided_at IS NULL THEN r.total_cents ELSE 0 END)"
            " FROM receipts r WHERE " + base_where, base_params).fetchone()
        n_all, n_valid = int(agg[0] or 0), int(agg[1] or 0)
        n_void = n_all - n_valid
        count = {"all": n_all, "valid": n_valid, "void": n_void}[f["status"]]
        income = 0 if f["status"] == "void" else int(agg[2] or 0)

        where, params = _where(f)
        pages = max(1, -(-count // PER_PAGE))
        page = min(page, pages)
        rows = db.execute(SELECT + where + " ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
                          params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

        payments = [r[0] for r in db.execute(
            "SELECT DISTINCT payment_method FROM receipts WHERE shop_id = ? AND payment_method <> ''"
            " ORDER BY payment_method", (sid,)).fetchall()]
        if f["payment"] and f["payment"] not in payments:
            payments.append(f["payment"])
        total_any = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]

        chips = _chips(f)
        export = url_for("receipts_csv", **{k: v for k, v in request.args.items() if k != "page" and v})
        status_tabs = [
            ("Tutti", _url(status=None), f["status"] == "all", n_all),
            ("Validi", _url(status="valid"), f["status"] == "valid", n_valid),
            ("Annullati", _url(status="void"), f["status"] == "void", n_void),
        ]
        return render_template(
            "panel/receipts.html", rows=rows, f=f, page=page, pages=pages, count=count, income=income,
            payments=payments, chips=chips, clear_url=url_for("receipts_list"), export_url=export,
            status_tabs=status_tabs, total_any=total_any, period_labels=PERIOD_LABELS,
            hidden_status=f["status"] if f["status"] != "all" else "",
            return_to=request.full_path.rstrip("?"))

    @app.get("/dashboard/receipts.csv")
    @login_required
    def receipts_csv():
        db = get_db()
        f = _filters()
        where, params = _where(f)
        rows = db.execute(SELECT + where + " ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
                          params + [CSV_MAX_ROWS]).fetchall()
        out = io.StringIO()
        out.write("﻿")  # BOM: Excel legge correttamente gli accenti
        w = csv.writer(out, delimiter=";")
        w.writerow(["Numero", "Data", "Ora", "Tavolo", "Pagamento", "Totale €", "IVA €", "Stato",
                    "Aperto dal cliente", "Messaggi inviati", "Richiesta fattura", "Codice lotteria"])
        for r in rows:
            w.writerow([csv_safe(x) for x in (
                r["number"], fmt_dt(r["created_at"], "%d/%m/%Y"), fmt_dt(r["created_at"], "%H:%M"),
                r["table_label"], r["payment_method"],
                f"{r['total_cents'] / 100:.2f}".replace(".", ","), f"{r['vat_cents'] / 100:.2f}".replace(".", ","),
                "annullato" if r["voided_at"] else "valido", r["views"] or 0, r["sent_count"] or 0,
                "sì" if r["has_invoice"] else "no", r["lottery_code"])])
        return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=scontrini.csv"})

    # ---------------------------------------------------------------- dettaglio
    def _own_receipt(rid):
        r = get_db().execute("SELECT * FROM receipts WHERE id = ? AND shop_id = ?",
                             (rid, g.panel_shop["id"])).fetchone()
        if r is None:
            abort(404)
        return r

    @app.get("/dashboard/receipts/<int:rid>")
    @login_required
    def receipt_detail(rid):
        db = get_db()
        sid = g.panel_shop["id"]
        r = _own_receipt(rid)
        items = db.execute("SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY id", (rid,)).fetchall()
        inv = db.execute("SELECT * FROM invoice_requests WHERE receipt_id = ?", (rid,)).fetchone()
        msgs = db.execute("SELECT * FROM messages WHERE receipt_id = ? ORDER BY created_at, id", (rid,)).fetchall()

        def local_day_of(iso):
            return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")

        events = [{"at": r["created_at"], "kind": "ok", "icon": "receipt", "title": "Scontrino creato",
                   "text": "Emesso dalla cassa" + (f" · {r['table_label']}" if r["table_label"] else ""),
                   "href": url_for("receipts_list", day=local_day_of(r["created_at"]))}]
        for m in msgs:
            ch = "WhatsApp" if m["channel"] == "whatsapp" else "SMS"
            if m["status"] == "sent":
                events.append({"at": m["created_at"], "kind": "ok", "icon": "send",
                               "title": f"Inviato via {ch}", "text": "A " + m["phone_masked"],
                               "href": url_for("messages_list", status="sent", channel=m["channel"])})
            else:
                events.append({"at": m["created_at"], "kind": "danger", "icon": "alert",
                               "title": f"Invio via {ch} non riuscito",
                               "text": ("A " + m["phone_masked"] + (" · " + m["error"] if m["error"] else "")),
                               "href": url_for("messages_list", status="failed", channel=m["channel"])})
        if inv:
            events.append({"at": inv["created_at"], "kind": "info", "icon": "file",
                           "title": "Richiesta di fattura", "text": inv["company_name"] + " · " + inv["status"],
                           "href": url_for("invoice_detail", inv_id=inv["id"])})
        if r["voided_at"]:
            events.append({"at": r["voided_at"], "kind": "danger", "icon": "x", "title": "Scontrino annullato",
                           "text": "Non conta più negli incassi", "href": url_for("receipts_list", status="void")})
        if r["views"]:
            n = int(r["views"])
            events.append({"at": r["created_at"], "kind": "info", "icon": "eye", "after_create": True,
                           "title": "Aperto dal cliente" + (f" {n} volte" if n > 1 else " 1 volta"),
                           "text": "Ha guardato lo scontrino dal link o dal QR", "no_time": True,
                           "href": public_url(r["token"])})
        events.sort(key=lambda e: (e["at"], 0 if e.get("no_time") else -1))
        sent_ok = sum(1 for m in msgs if m["status"] == "sent")

        prev_r = db.execute("SELECT id, number FROM receipts WHERE shop_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                            (sid, rid)).fetchone()
        next_r = db.execute("SELECT id, number FROM receipts WHERE shop_id = ? AND id > ? ORDER BY id LIMIT 1",
                            (sid, rid)).fetchone()
        return render_template(
            "panel/receipt_detail.html", r=r, items=items, vat_rows=vat_breakdown(items), inv=inv,
            msgs=msgs, events=events, sent_ok=sent_ok, sends_left=max(0, SEND_LIMIT - len(msgs)),
            send_limit=SEND_LIMIT, day_url=url_for("receipts_list", day=fmt_dt(r["created_at"], "%Y-%m-%d")), day_label=fmt_dt(r["created_at"], "%d/%m/%Y"),
            qr_url=url_for("receipt_qr", token=r["token"]), prev_r=prev_r, next_r=next_r, link=public_url(r["token"]),
            return_to=url_for("receipt_detail", rid=rid), vat_total=sum(i["vat_cents"] for i in items))

    @app.post("/dashboard/receipts/<int:rid>/send")
    @login_required
    def receipt_send(rid):
        """Invia lo scontrino al cliente: stessa logica di POST /api/v1/receipts/<token>/send."""
        db = get_db()
        shop = g.panel_shop
        r = _own_receipt(rid)
        back = redirect(url_for("receipt_detail", rid=rid) + "#invia")
        if r["voided_at"]:
            flash("Scontrino annullato: non si può inviare. Ripristinalo prima.", "err")
            return back
        phone = messaging.normalize_phone(request.form.get("phone"))
        if not phone:
            flash("Il numero di telefono non sembra valido. Scrivilo come 333 1234567 oppure +39 333 1234567.", "err")
            return back
        channel = request.form.get("channel", "whatsapp")
        if channel not in ("whatsapp", "sms"):
            flash("Scegli WhatsApp oppure SMS.", "err")
            return back
        already = db.execute("SELECT COUNT(*) FROM messages WHERE receipt_id = ?", (rid,)).fetchone()[0]
        if already >= SEND_LIMIT:
            flash(f"Hai già fatto {SEND_LIMIT} invii per questo scontrino: è il massimo. "
                  "Il cliente può comunque aprire il link o il QR.", "err")
            return back
        link = public_url(r["token"])
        body = messaging.render_template_text(shop["message_template"], shop["name"], link)
        status, error = messaging.send(channel, phone, body, template_vars={"1": shop["name"], "2": link})
        masked = messaging.mask_phone(phone)
        db.execute("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at)"
                   " VALUES (?,?,?,?,?,?)", (rid, channel, masked, status, error, now_iso()))
        consent = request.form.get("marketing_consent") in ("1", "on", "true")
        saved = False
        if consent and status == "sent":  # il numero si conserva SOLO con consenso esplicito
            db.execute("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                       (shop["id"], phone, now_iso()))
            saved = True
        db.commit()
        if status == "sent":
            flash(f"Scontrino inviato a {masked}." + (" Contatto salvato tra i clienti." if saved else ""), "ok")
        else:
            flash("Invio non riuscito" + (f": {error}" if error else "") + ". Riprova tra poco.", "err")
        return back

    # ---------------------------------------------------------- annulla / ripristina
    def _safe_next(value):
        """Torna solo a pagine dell'elenco/dettaglio scontrini (niente redirect aperti)."""
        if value and value.startswith("/dashboard/receipts") and not value.startswith("//") and "\\" not in value:
            return value
        return None

    def _set_voided(rid, voided):
        db = get_db()
        row = db.execute("SELECT id, number FROM receipts WHERE id = ? AND shop_id = ?",
                         (rid, g.panel_shop["id"])).fetchone()
        if row is None:
            abort(404)
        db.execute("UPDATE receipts SET voided_at = ? WHERE id = ?", (now_iso() if voided else None, rid))
        db.commit()
        flash(f"Scontrino n. {row['number']} {'annullato' if voided else 'ripristinato'}.", "ok")
        nxt = _safe_next(request.form.get("next"))
        if nxt:
            return redirect(nxt)
        page = request.form.get("page", 1, type=int) or 1
        return redirect(url_for("receipts_list", page=max(1, page)))

    @app.post("/dashboard/receipts/<int:rid>/void")
    @login_required
    def receipt_void(rid):
        return _set_voided(rid, True)

    @app.post("/dashboard/receipts/<int:rid>/restore")
    @login_required
    def receipt_restore(rid):
        return _set_voided(rid, False)
