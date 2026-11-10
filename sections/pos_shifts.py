"""Segnaposto: lo sostituisce l'agente (vedi POS-DESIGN.md)."""


def register(app, ctx):
    @app.get("/dashboard/turni", endpoint="pos_shifts")
    @ctx.login_required
    def pos_shifts():
        return "in costruzione"
