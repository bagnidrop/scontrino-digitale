"""Emissione del documento fiscale (documento commerciale).

OGGI questo software NON emette documenti fiscali: la legge richiede un Registratore Telematico (RT)
oppure la procedura web «Documento commerciale online» dell'Agenzia delle Entrate, finché non
esisteranno software certificati (art. 24 D.Lgs. 1/2024). Per questo ogni conto pagato passa da
«da emettere» a «emesso» solo quando qualcuno registra il numero del documento vero.

L'interfaccia sotto serve per collegare in futuro un emettitore automatico (RT con API,
software certificato) senza toccare la cassa: basta un adattatore nuovo.
"""
import pos_service as pos


class ManualAdapter:
    """Predefinito: il documento commerciale lo emette una persona (RT o portale AdE) e ne
    registra qui il numero."""
    name = "manuale"
    automatic = False
    label = "Emissione manuale (registratore telematico o portale Agenzia delle Entrate)"

    def issue(self, db, shop_id, order_id):
        """Non emette nulla: il conto resta «da emettere»."""
        return None


ADAPTERS = {"manuale": ManualAdapter()}


def adapter_for(shop=None):
    return ADAPTERS["manuale"]


def try_issue(db, shop_id, order_id, shop=None):
    """Chiama l'adattatore; se restituisce un numero lo registra. Ritorna il numero o None."""
    number = adapter_for(shop).issue(db, shop_id, order_id)
    if number:
        pos.mark_fiscal_issued(db, shop_id, order_id, number)
    return number
