"""Render extracted slip data into a dark, company-branded confirmation PNG:
purple header band, green success check, dark body with the transaction fields.

Only fields that were actually read off the source slip are shown. The issuer
line ("by <company>") and the one-line footer note stay on every image.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from config import CONFIG
from models import SlipData, nice_label

IST = ZoneInfo("Asia/Kolkata")

WIDTH = 880
PAD = 56

_FONT_DIRS = [
    os.path.join(os.path.dirname(__file__), "assets", "fonts"),
    "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
    "/usr/share/fonts", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
]
_FONT_CANDIDATES = {
    "regular": ["Inter-Regular.ttf", "Roboto-Regular.ttf", "DejaVuSans.ttf",
                "LiberationSans-Regular.ttf", "Arial Unicode.ttf", "Arial.ttf"],
    "medium": ["Inter-Medium.ttf", "Inter-SemiBold.ttf", "Roboto-Medium.ttf",
               "LiberationSans-Regular.ttf", "Arial Unicode.ttf", "Arial.ttf"],
    "bold": ["Inter-Bold.ttf", "Roboto-Bold.ttf", "DejaVuSans-Bold.ttf",
             "LiberationSans-Bold.ttf", "Arial Bold.ttf", "Arial.ttf"],
}

# --- dark palette ---
BG = (24, 27, 38)
INK = (243, 244, 249)
SUBTLE = (170, 175, 190)
FAINT = (128, 134, 154)
LINE = (44, 48, 65)
HEADER_SUB = (228, 230, 240)


def _find_font(names: list[str]) -> str | None:
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path = _find_font(_FONT_CANDIDATES[kind])
    if path:
        return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def _hex(c: str, fallback: str) -> tuple[int, int, int]:
    c = (c or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        c = fallback.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _tracked(d: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: float = 1.4) -> None:
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking


def _group_inr(num: str) -> str:
    m = re.fullmatch(r"(\d+)(\.\d+)?", num.strip())
    if not m:
        return num.strip()
    intp, dec = m.group(1), m.group(2) or ""
    if len(intp) <= 3:
        return intp + dec
    head, tail = intp[:-3], intp[-3:]
    head = re.sub(r"(?<=\d)(?=(?:\d\d)+$)", ",", head)
    return f"{head},{tail}{dec}"


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three(n: int) -> str:
    h, r = divmod(n, 100)
    parts = []
    if h:
        parts.append(_ONES[h] + " Hundred")
    if r:
        parts.append(_two(r))
    return " ".join(parts)


def _indian_words(n: int) -> str:
    if n == 0:
        return "Zero"
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    out = []
    if crore:
        out.append(_indian_words(crore) + " Crore")
    if lakh:
        out.append(_two(lakh) + " Lakh")
    if thousand:
        out.append(_two(thousand) + " Thousand")
    if n:
        out.append(_three(n))
    return " ".join(out)


def amount_in_words(data: SlipData) -> str | None:
    amt = re.sub(r"[₹,\s]|rs\.?|inr|rp", "", (data.amount or ""), flags=re.IGNORECASE)
    m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", amt)
    if not m:
        return None
    rupees = int(m.group(1))
    paise = int((m.group(2) or "").ljust(2, "0")) if m.group(2) else 0
    words = f"Rupees {_indian_words(rupees)}"
    if paise:
        words += f" and {_indian_words(paise)} Paise"
    return words + " Only"


def _amount_text(data: SlipData) -> str:
    amt = (data.amount or "").strip()
    amt = re.sub(r"^\s*(?:₹|rs\.?|inr|rp)\s*", "", amt, flags=re.IGNORECASE).strip()
    cur = (data.currency or "").strip().upper()
    prefix = "INR" if not cur or cur in {"₹", "RS", "RS.", "INR", "RP"} else cur
    return f"{prefix} {_group_inr(amt) or '-'}"


def confirmation_ref(data: SlipData) -> str:
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", CONFIG.branding.name))[:3].upper() or "PAY"
    day = datetime.now(IST).strftime("%y%m%d")
    seed = (data.transaction_id or "") + (data.account_number or "") + (data.amount or "")
    tail = hashlib.sha1(seed.encode()).hexdigest()[:6].upper() if seed else os.urandom(3).hex().upper()
    return f"{initials}-{day}-{tail}"


def status_text(data: SlipData) -> str:
    s = (data.status or "SUCCESS").strip()
    if re.search(r"success", s, re.IGNORECASE):
        return "SUCCESS"
    if re.search(r"complet", s, re.IGNORECASE):
        return "COMPLETED"
    return s.upper()[:24]


def _rows(data: SlipData) -> list[tuple[str, str, bool]]:
    """(label, value, emphasised). Reference numbers and status are emphasised."""
    rows: list[tuple[str, str, bool]] = []

    def add(label: str, value: str | None, strong: bool = False) -> None:
        if value and str(value).strip():
            rows.append((label, str(value).strip(), strong))

    add("Beneficiary Name", data.account_holder_name)
    add("Account Number", data.account_number)
    add("Bank", data.bank_name)
    add("IFSC Code", data.ifsc_code)
    for r in data.all_references():
        add(nice_label(r.label), r.value, strong=True)
    add("Transfer Mode", data.transfer_mode or CONFIG.default_transfer_mode or None)
    add("Date", data.date)
    add("Time", data.time)
    add("Status", status_text(data), strong=True)
    return rows


def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if d.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _check(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, colour) -> None:
    w = max(2, int(r / 3))
    d.line([(cx - r * 0.55, cy), (cx - r * 0.15, cy + r * 0.45), (cx + r * 0.6, cy - r * 0.5)],
           fill=colour, width=w, joint="curve")


def render(data: SlipData) -> bytes:
    b = CONFIG.branding
    header = _hex(b.primary, "#5B6BF0")
    green = _hex(b.accent, "#3ED598")
    rows = _rows(data)

    f_title = _font("bold", 35)
    f_by = _font("regular", 16)
    f_kicker = _font("medium", 12)
    f_amount = _font("bold", 55)
    f_words = _font("regular", 17)
    f_label = _font("medium", 13)
    f_value = _font("regular", 24)
    f_value_b = _font("bold", 24)
    f_foot = _font("regular", 13)
    f_foot_b = _font("medium", 13)
    f_fine = _font("regular", 12)

    lx = PAD
    rx = WIDTH - PAD
    inner = WIDTH - 2 * PAD

    words = amount_in_words(data)
    head_h = 156
    amount_h = 178 if words else 146
    row_h = 74
    disclaimer = b.footer_note
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    disc_lines = _wrap(dummy, disclaimer, f_fine, inner)
    footer_h = 132 + 22 * len(disc_lines)
    height = head_h + amount_h + row_h * len(rows) + footer_h

    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)

    # ---- header band ----
    d.rectangle((0, 0, WIDTH, head_h), fill=header)
    d.text((lx, 44), b.tagline or "Payment Confirmation", font=f_title, fill=(255, 255, 255))
    if b.name:
        d.text((lx, 96), f"by {b.name}", font=f_by, fill=HEADER_SUB)
    cx, cy, r = rx - 34, head_h / 2, 34
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=green)
    _check(d, cx, cy, r * 0.62, (255, 255, 255))

    # ---- amount ----
    y = head_h + 30
    _tracked(d, (lx, y), "AMOUNT", f_kicker, FAINT, 2.4)
    d.text((lx, y + 24), _amount_text(data), font=f_amount, fill=INK)
    if words:
        d.text((lx, y + 98), words, font=f_words, fill=SUBTLE)

    # ---- rows ----
    y = head_h + amount_h
    for i, (label, value, strong) in enumerate(rows):
        if i:
            d.line((lx, y, rx, y), fill=LINE, width=1)
        _tracked(d, (lx, y + 15), label.upper(), f_label, FAINT, 1.4)
        colour = green if label == "Status" else INK
        d.text((lx, y + 36), value, font=f_value_b if strong else f_value, fill=colour)
        y += row_h

    # ---- footer ----
    d.line((lx, y, rx, y), fill=LINE, width=1)
    y += 22
    ref = confirmation_ref(data)
    gen = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    d.text((lx, y), "Confirmation Ref", font=f_foot, fill=FAINT)
    d.text((lx + 175, y), ref, font=f_foot_b, fill=INK)
    d.text((lx, y + 22), "Generated", font=f_foot, fill=FAINT)
    d.text((lx + 175, y + 22), gen, font=f_foot, fill=INK)

    y += 58
    for line in disc_lines:
        d.text((lx, y), line, font=f_fine, fill=FAINT)
        y += 22
    contact = "  ·  ".join(x for x in (b.website, b.support) if x)
    if contact:
        d.text((lx, y + 6), contact, font=f_fine, fill=SUBTLE)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
