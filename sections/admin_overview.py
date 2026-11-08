"""Amministrazione · Panoramica del servizio e Registro attività (solo proprietario).

SEGNAPOSTO: verrà sostituito dalla schermata vera. Endpoint da mantenere:
  admin_overview  GET /admin            admin_activity  GET /admin/activity
"""
from flask import render_template


def register(app, ctx):
    @app.get("/admin")
    @ctx.owner_required
    def admin_overview():
        return render_template("panel/admin_stub.html", title="Panoramica del servizio")

    @app.get("/admin/activity")
    @ctx.owner_required
    def admin_activity():
        return render_template("panel/admin_stub.html", title="Registro attività")
