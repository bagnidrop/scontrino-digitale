"""Sezione «Statistiche» del pannello (segnaposto: da completare)."""
from flask import render_template


def register(app, ctx):
    login_required = ctx.login_required

    @app.get("/dashboard/stats")
    @login_required
    def stats_page():
        return render_template("panel/stats.html")
