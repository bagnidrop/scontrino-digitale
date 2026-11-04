"""Helper condivisi tra le sezioni del pannello (periodi e giorni dei filtri).

Contratto dei parametri URL usati da TUTTE le sezioni per collegarsi tra loro:
  period = today | 7d | 30d | 90d | all     (today = da mezzanotte locale; 7d = oggi + 6 giorni prima; 30d = oggi + 29)
  day    = AAAA-MM-GG                        (un singolo giorno, ora locale)
  status = (dipende dalla sezione, vedi DESIGN.md)
  q      = testo di ricerca
  page   = numero di pagina, da 1
"""
from datetime import datetime, timedelta, timezone

PERIODS = {"today": 0, "7d": 6, "30d": 29, "90d": 89}
PERIOD_LABELS = {"today": "Oggi", "7d": "Ultimi 7 giorni", "30d": "Ultimi 30 giorni", "90d": "Ultimi 90 giorni", "all": "Sempre"}


def _iso_utc(local_dt):
    return local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def local_midnight(tz, days_back=0):
    now = datetime.now(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back)


def period_since(period, tz, default="30d"):
    """Ritorna (chiave_valida, inizio_ISO_UTC oppure None per 'all'). Valori sconosciuti -> default."""
    key = period if period in PERIODS or period == "all" else default
    if key == "all":
        return key, None
    return key, _iso_utc(local_midnight(tz, PERIODS[key]))


def day_bounds(day, tz):
    """'2026-11-03' -> (inizio_ISO_UTC, fine_ISO_UTC) del giorno locale; None se non valido."""
    try:
        d = datetime.strptime(day or "", "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        return None
    return _iso_utc(d), _iso_utc(d + timedelta(days=1))


def local_date(iso, tz):
    """created_at ISO UTC -> datetime locale."""
    return datetime.fromisoformat(iso).astimezone(tz)
