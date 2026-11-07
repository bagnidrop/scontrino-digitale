"""Sezione «Messaggi» del pannello: registro degli invii dello scontrino su WhatsApp e SMS.

Endpoint: messages_list. Questo modulo NON deve importare `app` (importazione circolare).
"""
from flask import g, render_template, request

from .common import PERIOD_LABELS, period_since

PER_PAGE = 25
STATUSES = ("all", "sent", "failed")
CHANNELS = ("all", "whatsapp", "sms")


def explain_error(channel, error):
    """Traduce l'errore tecnico del provider in una frase semplice, con un consiglio."""
    e = (error or "").lower()
    if not e:
        return "Il messaggio non è partito. Riprova tra poco."
    if "21211" in e or "non valido" in e or "invalid" in e:
        return "Il numero non sembra valido. Controlla che sia scritto bene e riprova."
    if "non raggiungibile" in e or "63016" in e or "63018" in e or "unreachable" in e:
        if channel == "whatsapp":
            return "Il cliente non risulta su WhatsApp. Prova a inviare lo scontrino via SMS."
        return "Il numero non è raggiungibile in questo momento. Riprova più tardi."
    if "timed out" in e or "errore di rete" in e or "timeout" in e:
        return "Problema di connessione: il messaggio non è arrivato al servizio di invio. Riprova tra poco."
    if "credenziali" in e or "mancante" in e or "401" in e or "403" in e or "20003" in e:
        return "L'invio non è ancora configurato correttamente. Controlla la pagina Integrazione."
    if "429" in e or "rate" in e or "troppi" in e:
        return "Troppi invii in poco tempo. Aspetta un attimo e riprova."
    if "opt" in e and "out" in e or "21610" in e:
        return "Il cliente ha chiesto di non ricevere messaggi."
    return "Il servizio di invio ha rifiutato il messaggio. Riprova o usa l'altro canale."


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    LOCAL_TZ = ctx.LOCAL_TZ

    @app.get("/dashboard/messages")
    @login_required
    def messages_list():
        db = get_db()
        sid = g.panel_shop["id"]
        period, since = period_since(request.args.get("period"), LOCAL_TZ, default="30d")
        status = request.args.get("status", "all")
        status = status if status in STATUSES else "all"
        channel = request.args.get("channel", "all")
        channel = channel if channel in CHANNELS else "all"
        page = max(1, request.args.get("page", 1, type=int) or 1)

        base, bparams = "r.shop_id = ?", [sid]
        if since:
            base += " AND m.created_at >= ?"
            bparams.append(since)
        FROM = " FROM messages m JOIN receipts r ON r.id = m.receipt_id WHERE "

        # Conteggi del periodo (le schede mostrano il totale, non cambiano con gli altri filtri)
        counts = {"sent": 0, "failed": 0, "whatsapp": 0, "sms": 0}
        for st, ch in db.execute("SELECT m.status, m.channel" + FROM + base, bparams).fetchall():
            counts["sent" if st == "sent" else "failed"] += 1
            if ch in ("whatsapp", "sms"):
                counts[ch] += 1
        total = counts["sent"] + counts["failed"]

        where, params = base, list(bparams)
        if status != "all":
            where += " AND m.status = ?"
            params.append(status)
        if channel != "all":
            where += " AND m.channel = ?"
            params.append(channel)
        count = db.execute("SELECT COUNT(*)" + FROM + where, params).fetchone()[0]
        pages = max(1, -(-count // PER_PAGE))
        page = min(page, pages)
        rows = db.execute(
            "SELECT m.id, m.channel, m.phone_masked, m.status, m.error, m.created_at,"
            " r.id AS rid, r.number, r.voided_at" + FROM + where +
            " ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?",
            params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
        items = [{"id": m["id"], "channel": m["channel"], "phone": m["phone_masked"], "status": m["status"],
                  "error": m["error"], "why": explain_error(m["channel"], m["error"]) if m["status"] != "sent" else "",
                  "created_at": m["created_at"], "rid": m["rid"], "number": m["number"],
                  "voided": bool(m["voided_at"])} for m in rows]
        return render_template("panel/messages.html", rows=items, period=period, status=status, channel=channel,
                               page=page, pages=pages, count=count, counts=counts, total=total,
                               period_labels=PERIOD_LABELS, periods=("today", "7d", "30d", "90d", "all"))
