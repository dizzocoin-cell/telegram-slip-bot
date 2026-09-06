"""Read the transaction fields off a payment-slip image.

Provider is chosen by PROVIDER in .env:
  openai  - OpenAI vision (default)
  gemini  - Google Gemini vision
  auto    - OpenAI until its credit runs out, then Gemini automatically
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging

from openai import AsyncOpenAI, BadRequestError, RateLimitError
from PIL import Image

from config import CONFIG
from models import SlipData

log = logging.getLogger(__name__)

_SYSTEM = (
    "You read Indian bank / UPI payment transfer receipts (screenshots or photos) and "
    "return the transaction fields exactly as printed. Slips come from many different "
    "banks and apps in many layouts, and the photo may be rotated, skewed or low quality "
    "- read it whatever its orientation. Rules:\n"
    "- Copy values verbatim. Never invent, correct, complete or reformat a value.\n"
    "- If a field is not visible on the slip, return null for it. Do not guess.\n"
    "- 'amount' is only the number as printed (e.g. '50,000.00'). Put any currency "
    "symbol or code ('Rs.', '₹', 'INR') in 'currency', not in 'amount'.\n"
    "- 'account_holder_name' is the beneficiary/payee (the money receiver), not the sender.\n"
    "- Reference numbers: a slip almost always carries at least one, often several. Look "
    "everywhere for UTR, RRN, UPI transaction ID, transaction ID / number, transaction "
    "reference, bank reference number, reference number, order ID / number, or a long "
    "WEB... code. Put EVERY such number you can see into 'reference_numbers' as "
    "{label, value}, using the exact printed label. Do NOT put the beneficiary account "
    "number or the IFSC code in 'reference_numbers' - they have their own fields. Also set "
    "'transaction_id' / 'transaction_id_label' to the single most traceable one - prefer a "
    "12- or 16-digit UTR or RRN. Never leave 'reference_numbers' empty if any "
    "reference-like number is visible.\n"
    "- If only an IFSC is shown, you may fill 'bank_name' from the well-known IFSC prefix "
    "(e.g. UBIN = Union Bank of India, SIBL = South Indian Bank); otherwise null.\n"
    "- If the image is not a payment slip, return all nulls."
)

_KEYS = ", ".join(SlipData.JSON_SCHEMA["properties"])
_USER = (
    f"Extract the transaction fields from this slip. Respond with a single JSON object "
    f"using exactly these keys: {_KEYS}. Use null for anything not printed on the slip."
)

_MEDIA = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"RIFF": "image/webp",
    b"GIF8": "image/gif",
}


def _media_type(raw: bytes) -> str:
    for sig, mt in _MEDIA.items():
        if raw.startswith(sig):
            return mt
    return "image/jpeg"


def _rotate(raw: bytes, degrees: int) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im = im.rotate(-degrees, expand=True)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _loads(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip("` \n")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


def _field_count(d: SlipData) -> int:
    core = [d.account_holder_name, d.account_number, d.ifsc_code, d.amount, d.date, d.time]
    return sum(x is not None and str(x).strip() != "" for x in core) + len(d.all_references())


def _good(d: SlipData) -> bool:
    return bool(d.is_usable() and d.all_references() and _field_count(d) >= 4)


def _needs_high(d: SlipData) -> bool:
    return not _good(d) or any(len(r.value) >= 14 for r in d.all_references())


class _QuotaExhausted(Exception):
    """OpenAI has no credit / is rate-limited on quota."""


# --------------------------------------------------------------------------- #
#  OpenAI
# --------------------------------------------------------------------------- #
_openai = AsyncOpenAI(api_key=CONFIG.openai_api_key) if CONFIG.openai_api_key else None
_calls = 0


def _openai_messages(image_bytes: bytes, detail: str) -> list[dict]:
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    return [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _USER},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{_media_type(image_bytes)};base64,{b64}", "detail": detail},
                },
            ],
        },
    ]


async def _openai_once(image_bytes: bytes, detail: str) -> SlipData:
    global _calls
    _calls += 1
    messages = _openai_messages(image_bytes, detail)
    schema = {
        "type": "json_schema",
        "json_schema": {"name": "payment_slip", "strict": True, "schema": SlipData.JSON_SCHEMA},
    }
    try:
        try:
            resp = await _openai.chat.completions.create(
                model=CONFIG.extraction_model, messages=messages, max_tokens=1024,
                response_format=schema,
            )
        except BadRequestError:
            resp = await _openai.chat.completions.create(
                model=CONFIG.extraction_model, messages=messages, max_tokens=1024,
                response_format={"type": "json_object"},
            )
    except RateLimitError as exc:
        if "insufficient_quota" in str(exc) or "exceeded your current quota" in str(exc):
            raise _QuotaExhausted(str(exc)) from exc
        raise

    choice = resp.choices[0]
    if choice.finish_reason == "content_filter":
        raise RuntimeError("extraction blocked by content filter")
    try:
        return SlipData.model_validate(_loads(choice.message.content or ""))
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError("could not parse extraction result") from exc


async def _extract_openai(image_bytes: bytes) -> SlipData:
    start = _calls
    best = await _openai_once(image_bytes, "low")
    if _needs_high(best):
        try:
            cand = await _openai_once(image_bytes, "high")
            if _field_count(cand) >= _field_count(best):
                best = cand
        except (RuntimeError, OSError) as exc:
            log.warning("high-detail pass failed: %s", exc)
    if not _good(best):
        for deg in (270, 90, 180):
            try:
                cand = await _openai_once(await asyncio.to_thread(_rotate, image_bytes, deg), "high")
            except (RuntimeError, OSError) as exc:
                log.warning("rotation %s failed: %s", deg, exc)
                continue
            if _field_count(cand) > _field_count(best):
                best = cand
            if _good(best):
                break
    log.info("openai: %d model call(s)", _calls - start)
    return best


# --------------------------------------------------------------------------- #
#  Gemini
# --------------------------------------------------------------------------- #
_gemini = None
if CONFIG.gemini_api_key:
    from google import genai

    _gemini = genai.Client(api_key=CONFIG.gemini_api_key)


async def _gemini_once(image_bytes: bytes) -> SlipData:
    from google.genai import types

    resp = await _gemini.aio.models.generate_content(
        model=CONFIG.gemini_model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=_media_type(image_bytes)),
            f"{_SYSTEM}\n\n{_USER}",
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    try:
        return SlipData.model_validate(_loads(resp.text or ""))
    except (json.JSONDecodeError, IndexError, AttributeError) as exc:
        raise RuntimeError("could not parse extraction result") from exc


async def _extract_gemini(image_bytes: bytes) -> SlipData:
    best = await _gemini_once(image_bytes)
    if not _good(best):
        for deg in (270, 90, 180):
            try:
                cand = await _gemini_once(await asyncio.to_thread(_rotate, image_bytes, deg))
            except (RuntimeError, OSError) as exc:
                log.warning("gemini rotation %s failed: %s", deg, exc)
                continue
            if _field_count(cand) > _field_count(best):
                best = cand
            if _good(best):
                break
    log.info("gemini extraction done")
    return best


# --------------------------------------------------------------------------- #
#  dispatch
# --------------------------------------------------------------------------- #
_openai_dead = False


def _order() -> list[str]:
    if CONFIG.provider == "gemini":
        return ["gemini"]
    if CONFIG.provider == "openai":
        return ["openai"]
    # auto
    if _openai_dead or not _openai:
        return ["gemini"] if _gemini else ["openai"]
    return ["openai", "gemini"] if _gemini else ["openai"]


async def extract(image_bytes: bytes) -> SlipData:
    global _openai_dead
    last: Exception | None = None
    for provider in _order():
        try:
            if provider == "openai":
                return await _extract_openai(image_bytes)
            return await _extract_gemini(image_bytes)
        except _QuotaExhausted as exc:
            log.warning("OpenAI credit exhausted — switching to Gemini for all further slips")
            _openai_dead = True
            last = exc
            continue
    raise last or RuntimeError("no extraction provider available")
