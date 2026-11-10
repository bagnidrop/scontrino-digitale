"""Segnaposto: lo sostituisce l'agente (vedi POS-DESIGN.md)."""


def register(app, ctx):
    @app.get("/dashboard/fiscal", endpoint="pos_fiscal")
    @ctx.login_required
    def pos_fiscal():
        return "in costruzione"
