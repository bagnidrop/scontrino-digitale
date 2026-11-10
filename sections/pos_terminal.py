"""Segnaposto: lo sostituisce l'agente (vedi POS-DESIGN.md)."""


def register(app, ctx):
    @app.get("/cassa", endpoint="pos_terminal")
    @ctx.login_required
    def pos_terminal():
        return "in costruzione"
