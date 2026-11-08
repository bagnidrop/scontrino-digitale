"""Test del logo dell'azienda: caricamento, controlli di sicurezza, scontrino, isolamento."""
import hashlib
import html as htmllib
import io
import itertools
import os
import random
import re

import pytest
from PIL import Image

from testutil import appmod, make_config, csrf_of, register, seed_receipts

app = appmod.create_app(make_config("logo.db"))
_n = itertools.count(1)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def fresh():
    """Locale nuovo con uno scontrino. Ritorna (client, key, token_scontrino, shop_id)."""
    n = next(_n)
    email = f"logo{n}@example.com"
    client, key = register(app, f"Locale Logo {n}", email)
    token = seed_receipts(client, key, [dict(price="10.00")])[0]["token"]
    with app.app_context():
        sid = appmod.get_db().execute("SELECT id FROM shops WHERE email = ?", (email,)).fetchone()["id"]
    return client, key, token, sid


def q(sql, params=()):
    with app.app_context():
        db = appmod.get_db()
        cur = db.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        db.commit()
        return rows


def image_bytes(size=(200, 100), mode="RGBA", color=None, fmt="PNG", **save_kw):
    color = color or ((255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0))
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt, **save_kw)
    return buf.getvalue()


def upload(client, data, filename="logo.png", follow=True, csrf=True):
    form = {"logo": (io.BytesIO(data), filename)}
    if csrf:
        form["csrf"] = csrf_of(client, "/dashboard/settings")
    return client.post("/dashboard/settings/logo", data=form, content_type="multipart/form-data",
                       follow_redirects=follow)


def stored(sid):
    rows = q("SELECT data, width, height, etag FROM shop_logos WHERE shop_id = ?", (sid,))
    return rows[0] if rows else None


def stored_image(sid):
    return Image.open(io.BytesIO(bytes(stored(sid)["data"])))


def unique_color():
    """Colore mai usato prima: lo stesso contenuto ha lo stesso indirizzo pubblico, anche tra aziende diverse."""
    return (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255), 255)


def text(response):
    return htmllib.unescape(response.get_data(as_text=True))


def receipt_html(token):
    return app.test_client().get(f"/r/{token}").get_data(as_text=True)


# ------------------------------------------------------------------ caso normale
def test_upload_shows_on_settings_and_on_customer_receipt_and_is_served():
    c, key, token, sid = fresh()
    assert 'class="paper-logo"' not in receipt_html(token)
    r = upload(c, image_bytes((200, 100)))
    assert r.status_code == 200 and "Logo aggiornato" in r.get_data(as_text=True)
    assert 'alt="Il tuo logo attuale"' in c.get("/dashboard/settings").get_data(as_text=True)

    html = receipt_html(token)
    m = re.search(r'<img class="paper-logo" src="(/logo/[0-9a-f]{24}\.png)"', html)
    assert m, "il logo deve comparire sullo scontrino del cliente"
    img = app.test_client().get(m.group(1))            # anonimo: è l'immagine pubblica dello scontrino
    assert img.status_code == 200 and img.mimetype == "image/png"
    assert img.data.startswith(PNG_SIGNATURE)
    assert "immutable" in img.headers["Cache-Control"] and "public" in img.headers["Cache-Control"]
    assert Image.open(io.BytesIO(img.data)).size == (200, 100)


def test_receipt_without_logo_has_no_image_tag():
    c, key, token, sid = fresh()
    assert "paper-logo" not in receipt_html(token)


def test_etag_is_derived_from_stored_content():
    c, key, token, sid = fresh()
    upload(c, image_bytes((64, 64)))
    row = stored(sid)
    assert row["etag"] == hashlib.sha256(bytes(row["data"])).hexdigest()[:24]


def test_voided_receipt_still_shows_logo():
    c, key, token, sid = fresh()
    upload(c, image_bytes())
    c.post(f"/api/v1/receipts/{token}/void", headers={"X-API-Key": key})
    assert 'class="paper-logo"' in receipt_html(token)


# ------------------------------------------------------------------ ricodifica e ridimensionamento
def test_large_image_is_downscaled_keeping_aspect_ratio():
    c, key, token, sid = fresh()
    upload(c, image_bytes((3000, 2000), mode="RGB", fmt="JPEG"), "foto.jpg")
    row = stored(sid)
    assert (row["width"], row["height"]) == (480, 320)
    assert stored_image(sid).size == (480, 320)


def test_small_image_is_not_enlarged():
    c, key, token, sid = fresh()
    upload(c, image_bytes((120, 60)))
    assert stored_image(sid).size == (120, 60)


def test_transparency_is_preserved():
    c, key, token, sid = fresh()
    upload(c, image_bytes((100, 100), color=(0, 0, 0, 0)))
    im = stored_image(sid)
    assert im.mode == "RGBA" and im.getpixel((5, 5))[3] == 0


def test_jpeg_orientation_is_respected():
    c, key, token, sid = fresh()
    im = Image.new("RGB", (200, 100), (10, 120, 60))
    exif = im.getexif()
    exif[274] = 6                       # ruotata di 90°
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    upload(c, buf.getvalue(), "foto.jpg")
    assert stored_image(sid).size == (100, 200)


def test_metadata_is_stripped():
    c, key, token, sid = fresh()
    im = Image.new("RGB", (200, 100), (10, 120, 60))
    exif = im.getexif()
    exif[271] = "FotocameraSegreta"     # produttore
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    assert b"FotocameraSegreta" in buf.getvalue()
    upload(c, buf.getvalue(), "foto.jpg")
    assert b"FotocameraSegreta" not in bytes(stored(sid)["data"])
    assert not stored_image(sid).getexif()


def test_animated_gif_uses_first_frame_and_is_stored_as_png():
    c, key, token, sid = fresh()
    a, b = Image.new("RGB", (80, 80), (255, 0, 0)), Image.new("RGB", (80, 80), (0, 0, 255))
    buf = io.BytesIO()
    a.save(buf, "GIF", save_all=True, append_images=[b], duration=100, loop=0)
    r = upload(c, buf.getvalue(), "anim.gif")
    assert "Logo aggiornato" in r.get_data(as_text=True)
    im = stored_image(sid)
    assert im.format == "PNG" and not getattr(im, "is_animated", False)


def test_webp_is_accepted():
    c, key, token, sid = fresh()
    r = upload(c, image_bytes((90, 90), mode="RGB", fmt="WEBP"), "logo.webp")
    assert "Logo aggiornato" in r.get_data(as_text=True)


# ------------------------------------------------------------------ rifiuti
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><script>alert(1)</script></svg>'


@pytest.mark.parametrize("name,data,message", [
    ("logo.svg", SVG, "non è un'immagine valida"),                                # l'SVG può contenere script
    ("logo.png", b"questo non e' un PNG, e' testo", "non è un'immagine valida"),  # estensione ingannevole
    ("logo.png", b"<html><script>alert(1)</script></html>", "non è un'immagine valida"),
    ("logo.png", image_bytes((200, 100))[:150], "non è un'immagine valida"),     # file troncato
    ("piccolo.png", image_bytes((10, 10)), "troppo piccola"),
    ("vuoto.png", b"", "Scegli un file"),
])
def test_invalid_files_are_rejected_with_a_clear_message(name, data, message):
    c, key, token, sid = fresh()
    r = upload(c, data, name)
    assert r.status_code == 200 and message in text(r)
    assert stored(sid) is None and "paper-logo" not in receipt_html(token)


def test_too_heavy_file_is_rejected():
    c, key, token, sid = fresh()
    noise = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3))
    buf = io.BytesIO()
    noise.save(buf, "PNG")
    assert 2 * 1024 * 1024 < len(buf.getvalue()) < 3 * 1024 * 1024
    assert "troppo pesante" in upload(c, buf.getvalue()).get_data(as_text=True)
    assert stored(sid) is None


def test_request_larger_than_the_hard_limit_is_refused():
    c, key, token, sid = fresh()
    assert upload(c, os.urandom(4 * 1024 * 1024), follow=False).status_code == 413
    assert stored(sid) is None


def test_decompression_bomb_is_rejected_without_decoding():
    c, key, token, sid = fresh()
    buf = io.BytesIO()
    Image.new("1", (6000, 6000)).save(buf, "PNG")        # 36 megapixel, pochi KB di file
    assert len(buf.getvalue()) < 100_000
    assert "troppo grande" in upload(c, buf.getvalue()).get_data(as_text=True)
    assert stored(sid) is None


def test_other_endpoints_still_refuse_large_requests():
    c, key, token, sid = fresh()
    r = c.post("/dashboard/settings", data={"csrf": "x", "footer_note": "a" * (300 * 1024)})
    assert r.status_code == 413


# ------------------------------------------------------------------ sostituzione e rimozione
def test_replacing_keeps_a_single_logo_and_old_address_stops_working():
    c, key, token, sid = fresh()
    upload(c, image_bytes((100, 100), color=unique_color()))
    old = stored(sid)["etag"]
    upload(c, image_bytes((100, 100), color=unique_color()))
    new = stored(sid)["etag"]
    assert old != new
    assert q("SELECT COUNT(*) FROM shop_logos WHERE shop_id = ?", (sid,))[0][0] == 1
    anon = app.test_client()
    assert anon.get(f"/logo/{old}.png").status_code == 404
    assert anon.get(f"/logo/{new}.png").status_code == 200
    assert f"/logo/{new}.png" in receipt_html(token)


def test_remove_logo():
    c, key, token, sid = fresh()
    upload(c, image_bytes(color=unique_color()))
    etag = stored(sid)["etag"]
    r = c.post("/dashboard/settings/logo/delete", data={"csrf": csrf_of(c, "/dashboard/settings")},
               follow_redirects=True)
    assert "Logo rimosso" in r.get_data(as_text=True)
    assert stored(sid) is None
    assert "paper-logo" not in receipt_html(token)
    assert app.test_client().get(f"/logo/{etag}.png").status_code == 404


# ------------------------------------------------------------------ sicurezza e isolamento
def test_each_company_has_its_own_logo_and_cannot_touch_the_others():
    a, ka, ta, sa = fresh()
    b, kb, tb, sb = fresh()
    upload(a, image_bytes((100, 100), color=unique_color()))
    assert "paper-logo" not in receipt_html(tb)          # lo scontrino di B non mostra il logo di A
    upload(b, image_bytes((100, 100), color=unique_color()))
    assert stored(sa)["etag"] != stored(sb)["etag"]
    b.post("/dashboard/settings/logo/delete", data={"csrf": csrf_of(b, "/dashboard/settings")})
    assert stored(sb) is None and stored(sa) is not None  # B rimuove il suo, quello di A resta
    assert f"/logo/{stored(sa)['etag']}.png" in receipt_html(ta)


def test_upload_requires_csrf_and_login():
    c, key, token, sid = fresh()
    assert upload(c, image_bytes(), csrf=False, follow=False).status_code == 400
    anon = app.test_client()
    t = csrf_of(anon, "/login")
    r = anon.post("/dashboard/settings/logo", data={"csrf": t, "logo": (io.BytesIO(image_bytes()), "l.png")},
                  content_type="multipart/form-data")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert stored(sid) is None
    r = anon.post("/dashboard/settings/logo/delete", data={"csrf": t})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


@pytest.mark.parametrize("etag", ["zzz", "g" * 24, "A" * 24, "a" * 25, "a" * 23, "0" * 24, "../../etc/passwd"])
def test_malformed_or_unknown_logo_addresses_are_404(etag):
    assert app.test_client().get(f"/logo/{etag}.png").status_code == 404


def test_deleting_a_company_removes_its_logo():
    c, key, token, sid = fresh()
    upload(c, image_bytes())
    assert stored(sid) is not None
    q("DELETE FROM shops WHERE id = ?", (sid,))
    assert stored(sid) is None


def test_pages_with_logo_have_no_inline_styles_or_scripts():
    c, key, token, sid = fresh()
    upload(c, image_bytes())
    for html in (c.get("/dashboard/settings").get_data(as_text=True), receipt_html(token)):
        assert ' style="' not in html and "<style" not in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
