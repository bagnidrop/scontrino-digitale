"""Amministrazione · Aziende: elenco, nuova azienda, dettaglio e azioni (solo proprietario).

SEGNAPOSTO: verrà sostituito dalla schermata vera. Endpoint da mantenere:
  admin_companies        GET  /admin/companies
  admin_company_detail   GET  /admin/companies/<int:cid>
"""
from flask import render_template


def register(app, ctx):
    @app.get("/admin/companies")
    @ctx.owner_required
    def admin_companies():
        return render_template("panel/admin_stub.html", title="Aziende")

    @app.get("/admin/companies/<int:cid>")
    @ctx.owner_required
    def admin_company_detail(cid):
        return render_template("panel/admin_stub.html", title="Azienda")
