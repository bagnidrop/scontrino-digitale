"""Controllo e ridimensionamento del logo caricato dalle aziende.

Il file caricato NON viene mai salvato né servito così com'è: viene decodificato, ridimensionato e
ricodificato da zero come PNG. Questo elimina metadati (EXIF, posizione GPS), codice nascosto nei
file e formati pericolosi. L'SVG è escluso di proposito: può contenere script.
"""
import hashlib
import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 2 * 1024 * 1024        # byte del file caricato (Vercel accetta richieste fino a ~4,5 MB)
BODY_LIMIT = 3 * 1024 * 1024        # limite della richiesta intera: file + campi del modulo
MAX_SIDE = 480                      # lato massimo dopo il ridimensionamento (nitido anche su schermi 2x)
MIN_SIDE = 32
MAX_PIXELS = 25_000_000             # protezione da «bombe di decompressione»
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class LogoError(ValueError):
    """Errore da mostrare così com'è all'utente (messaggio in italiano)."""


def process_logo(raw):
    """Ritorna (png_bytes, larghezza, altezza, etag) oppure solleva LogoError."""
    if not raw:
        raise LogoError("Scegli un file da caricare.")
    if len(raw) > MAX_UPLOAD:
        raise LogoError("Il file è troppo pesante: il massimo è 2 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(raw))
        if img.format not in ALLOWED_FORMATS:
            raise LogoError("Formato non supportato: usa un'immagine PNG, JPG o WebP.")
        if img.width * img.height > MAX_PIXELS:  # si controlla prima di decodificare
            raise LogoError("L'immagine è troppo grande: riducila sotto i 25 megapixel.")
        img.load()  # decodifica davvero: fa emergere i file corrotti o troncati
    except LogoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError, EOFError):
        raise LogoError("Il file non è un'immagine valida.") from None

    img = ImageOps.exif_transpose(img)      # rispetta l'orientamento delle foto
    if min(img.size) < MIN_SIDE:
        raise LogoError("L'immagine è troppo piccola: servono almeno 32 pixel per lato.")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)   # non ingrandisce mai, e per una GIF usa il primo fotogramma
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")           # mantiene la trasparenza di palette, grigi e simili
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)     # nessun metadato viene copiato
    data = out.getvalue()
    return data, img.width, img.height, hashlib.sha256(data).hexdigest()[:24]
