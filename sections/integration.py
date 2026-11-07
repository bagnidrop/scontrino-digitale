"""Sezione «Integrazione» del pannello: «Collega la cassa».

Stato del collegamento, percorso guidato, esempi copiabili, riferimento API e risoluzione dei problemi.
Gli esempi sono costruiti qui in Python (così le graffe di JSON non confliggono con Jinja).
Non mostra mai la chiave API intera (nel database c'è solo l'hash): solo il prefisso.
"""
import json
import os
from datetime import timedelta

from flask import g, render_template, request

KEY_PLACEHOLDER = "LA_TUA_API_KEY"
TOKEN_PLACEHOLDER = "TOKEN_DELLO_SCONTRINO"
RECENT = timedelta(hours=24)
# Firma dello scontrino creato dal bottone «Crea scontrino di prova» (sections/overview.py).
TEST_SIGNATURE = ("Tavolo 7", "Carta", 4600)


def _ago(delta):
    """«3 minuti», «2 ore», «5 giorni»: durata leggibile in italiano."""
    s = max(0, int(delta.total_seconds()))
    if s < 60:
        return "pochi secondi"
    m = s // 60
    if m < 60:
        return f"{m} minut{'o' if m == 1 else 'i'}"
    h = m // 60
    if h < 48:
        return f"{h} or{'a' if h == 1 else 'e'}"
    return f"{h // 24} giorni"


def build_examples(base, key=KEY_PLACEHOLDER, token=TOKEN_PLACEHOLDER):
    """Esempi curl + JSON per creare, inviare e annullare uno scontrino."""
    api = f"{base}/api/v1"
    key = key or KEY_PLACEHOLDER
    create_body = {
        "external_id": "CASSA1-0001", "table": "Tavolo 4", "payment_method": "Carta",
        "items": [
            {"name": "Margherita", "qty": 2, "unit_price": "8.50", "vat_rate": 10},
            {"name": "Birra media", "qty": 2, "unit_price": "5.00", "vat_rate": 22},
        ]}
    send_body = {"phone": "333 1234567", "channel": "whatsapp", "marketing_consent": False}
    create_json = json.dumps(create_body, indent=2, ensure_ascii=False)
    send_json = json.dumps(send_body, indent=2)
    hdr = f'  -H "X-API-Key: {key}" \\\n'
    ctype = '  -H "Content-Type: application/json" \\\n'
    return {
        "create": {
            "curl": f"curl -X POST {api}/receipts \\\n{hdr}{ctype}  -d '{json.dumps(create_body, ensure_ascii=False)}'",
            "body": create_json,
            "response": json.dumps({
                "id": 1, "token": "AbC123xyz", "number": 1, "total_cents": 2700, "voided": False,
                "url": f"{base}/r/AbC123xyz", "qr_svg_url": f"{base}/r/AbC123xyz/qr.svg"}, indent=2),
        },
        "send": {
            "curl": f"curl -X POST {api}/receipts/{token}/send \\\n{hdr}{ctype}  -d '{json.dumps(send_body)}'",
            "body": send_json,
            "response": json.dumps({"status": "sent", "url": f"{base}/r/{token}"}, indent=2),
        },
        "void": {
            "curl": f'curl -X POST {api}/receipts/{token}/void \\\n  -H "X-API-Key: {key}"',
            "body": "",
            "response": json.dumps({"id": 1, "token": token, "number": 1, "total_cents": 2700, "voided": True,
                                   "url": f"{base}/r/{token}", "qr_svg_url": f"{base}/r/{token}/qr.svg"}, indent=2),
        },
    }


def register(app, ctx):
    login_required = ctx.login_required
    get_db = ctx.get_db
    parse_iso = ctx.parse_iso
    now_utc = ctx.now_utc
    public_url = ctx.public_url

    @app.get("/dashboard/integration")
    @login_required
    def integration():
        db = get_db()
        shop = g.panel_shop
        sid = shop["id"]
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        now = now_utc()

        # Dati reali, sempre filtrati per locale; gli annullati non contano.
        last = db.execute("SELECT id, token, number, created_at FROM receipts WHERE shop_id = ?"
                          " AND voided_at IS NULL AND NOT (table_label = ? AND payment_method = ? AND total_cents = ?)"
                          " ORDER BY created_at DESC, id DESC LIMIT 1", (sid, *TEST_SIGNATURE)).fetchone()
        last_info = None
        if last:
            created = parse_iso(last["created_at"])
            last_info = {"id": last["id"], "number": last["number"], "ago": _ago(now - created),
                         "when": created.astimezone(ctx.LOCAL_TZ).strftime("%d/%m/%Y %H:%M")}
        since = (now - RECENT).isoformat(timespec="seconds")
        count_24h = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ? AND voided_at IS NULL"
                               " AND created_at >= ? AND NOT (table_label = ? AND payment_method = ? AND total_cents = ?)",
                               (sid, since, *TEST_SIGNATURE)).fetchone()[0]
        total_count = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ? AND voided_at IS NULL",
                                 (sid,)).fetchone()[0]
        tests = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ? AND voided_at IS NULL"
                           " AND table_label = ? AND payment_method = ? AND total_cents = ?",
                           (sid, *TEST_SIGNATURE)).fetchone()[0]
        real_count = total_count - tests

        provider = os.environ.get("MESSAGING_PROVIDER", "console").strip().lower()
        twilio_ok = all(os.environ.get(v) for v in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"))
        return render_template(
            "panel/integration.html", shop=shop, base_url=base, api_url=base + "/api/v1",
            key_prefix=shop["api_key_prefix"], ex=build_examples(base), last=last_info,
            count_24h=count_24h, total_count=total_count, real_count=real_count, test_count=tests,
            connected=real_count > 0, provider=provider, messaging_ready=provider != "twilio" or twilio_ok,
            support_email=os.environ.get("SUPPORT_EMAIL", "assistenza@scontrinodigitale.it"))
