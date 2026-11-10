"""Segnaposto: lo sostituisce l'agente (vedi POS-DESIGN.md)."""


def register(app, ctx):
    @app.get("/dashboard/menu", endpoint="pos_menu")
    @ctx.login_required
    def pos_menu():
        return "in costruzione"
