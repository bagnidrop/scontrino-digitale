"""Sezione «Messaggi» del pannello (segnaposto: da completare)."""
from flask import render_template


def register(app, ctx):
    login_required = ctx.login_required

    @app.get("/dashboard/messages")
    @login_required
    def messages_list():
        return render_template("panel/messages.html")
