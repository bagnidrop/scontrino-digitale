"""Sezione «Integrazione» del pannello (segnaposto: da completare)."""
from flask import render_template


def register(app, ctx):
    login_required = ctx.login_required

    @app.get("/dashboard/integration")
    @login_required
    def integration():
        return render_template("panel/integration.html")
