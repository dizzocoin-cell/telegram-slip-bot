"""Turn whatever lands in the chat (photo, image file, PDF, HEIC) into a list of
normalised page images ready for extraction."""
from __future__ import annotations

import io
import logging

import pymupdf
from PIL import Image, ImageOps

try:  # iPhone HEIC/HEIF sent as a document
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001 - optional
    pass

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 4
_MAX_SIDE = 1400


def normalize_image(raw: bytes) -> bytes:
    """Downscale, fix orientation, boost contrast, re-encode as JPEG."""
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)
    im = im.convert("RGB")
    im.thumbnail((_MAX_SIDE, _MAX_SIDE))
    im = ImageOps.autocontrast(im, cutoff=1)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _pdf_to_images(raw: bytes) -> list[bytes]:
    pages: list[bytes] = []
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        for page in list(doc)[:MAX_PDF_PAGES]:
            pix = page.get_pixmap(dpi=200)
            pages.append(normalize_image(pix.tobytes("png")))
    return pages


def to_page_images(raw: bytes, is_pdf: bool) -> list[bytes]:
    """One entry for an image; one per page (capped) for a PDF."""
    if is_pdf:
        try:
            return _pdf_to_images(raw) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("PDF rasterise failed: %s", exc)
            return []
    try:
        return [normalize_image(raw)]
    except OSError:
        return [raw]
