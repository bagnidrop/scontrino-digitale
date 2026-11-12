"""Pannello del ristoratore, diviso in sezioni.

Ogni sezione è un modulo con una funzione register(app, ctx). Gli helper condivisi
(database, login, formattazione...) arrivano da `ctx`, costruito in app.create_app().

REGOLA: i moduli di una sezione non importano `app` e non toccano i file delle altre sezioni.
"""
from flask import g, request

from . import (admin_companies, admin_overview, contacts, integration, invoices, messages, overview,
               receipts, settings, stats, pos_fiscal, pos_menu, pos_public, pos_shifts, pos_terminal)

# Voci del menu laterale: (gruppo, [(chiave, etichetta, icona, endpoint, prefisso percorso)])
NAV = [
    ("Principale", [
        ("overview", "Panoramica", "home", "dashboard", "/dashboard"),
        ("receipts", "Scontrini", "receipt", "receipts_list", "/dashboard/receipts"),
        ("invoices", "Fatture", "file", "invoices_list", "/dashboard/invoices"),
        ("contacts", "Clienti", "users", "contacts_list", "/dashboard/contacts"),
    ]),
    ("Cassa", [
        ("pos_terminal", "Apri la cassa", "receipt", "pos_terminal", "/cassa"),
        ("pos_menu", "Menu e sala", "building", "pos_menu", "/dashboard/menu"),
        ("pos_shifts", "Turni e chiusura", "chart", "pos_shifts", "/dashboard/turni"),
        ("pos_fiscal", "Da fiscalizzare", "shield", "pos_fiscal", "/dashboard/fiscal"),
    ]),
    ("Analisi", [
        ("messages", "Messaggi", "message", "messages_list", "/dashboard/messages"),
        ("stats", "Statistiche", "chart", "stats_page", "/dashboard/stats"),
    ]),
    ("Configurazione", [
        ("integration", "Integrazione", "plug", "integration", "/dashboard/integration"),
        ("settings", "Impostazioni", "settings", "settings", "/dashboard/settings"),
    ]),
]


# Menu del proprietario del servizio: compare solo a lui (vedi DESIGN.md, «Amministrazione»).
OWNER_NAV = ("Amministrazione", [
    ("admin_overview", "Panoramica servizio", "chart", "admin_overview", "/admin"),
    ("admin_companies", "Aziende", "building", "admin_companies", "/admin/companies"),
    ("admin_activity", "Registro attività", "shield", "admin_activity", "/admin/activity"),
])


def register_all(app, ctx):
    for module in (overview, receipts, invoices, contacts, messages, stats, integration, settings,
                   pos_fiscal, pos_menu, pos_public, pos_shifts, pos_terminal, admin_overview, admin_companies):
        module.register(app, ctx)

    @app.context_processor
    def panel_context():
        """Variabili per il layout del pannello (menu, contatori)."""
        if getattr(g, "panel_shop", None) is None or not request.path.startswith(("/dashboard", "/admin")):
            return {}
        path = request.path.rstrip("/") or "/"
        pending = ctx.get_db().execute(
            "SELECT COUNT(*) FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? AND i.status = 'nuova' AND r.voided_at IS NULL",
            (g.panel_shop["id"],)).fetchone()[0]
        groups = []
        menu = list(NAV) + ([OWNER_NAV] if ctx.is_owner(g.panel_shop) else [])
        for title, items in menu:
            rows = []
            for key, label, icon, endpoint, prefix in items:
                current = path == prefix if prefix in ("/dashboard", "/admin") else path.startswith(prefix)
                rows.append({"key": key, "label": label, "icon": icon, "endpoint": endpoint,
                             "current": current, "badge": pending if key == "invoices" and pending else 0})
            groups.append({"title": title, "items": rows})
        return {"panel_nav": groups, "panel_shop": g.panel_shop}
