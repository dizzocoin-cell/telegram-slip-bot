"""Pull structured fields off a payment-slip image using an OpenAI vision model."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging

from openai import AsyncOpenAI, BadRequestError
from PIL import Image

from config import CONFIG
from models import SlipData

log = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=CONFIG.openai_api_key)

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
    "'transaction_id' / "
    "'transaction_id_label' to the single most traceable one - prefer a 12- or 16-digit "
    "UTR or RRN. Never leave 'reference_numbers' empty if any reference-like number is "
    "visible.\n"
    "- If only an IFSC is shown, you may fill 'bank_name' from the well-known IFSC prefix "
    "(e.g. UBIN = Union Bank of India, SIBL = South Indian Bank); otherwise null.\n"
    "- If the image is not a payment slip, return all nulls."
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


def _messages(image_bytes: bytes, detail: str) -> list[dict]:
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    keys = ", ".join(SlipData.JSON_SCHEMA["properties"])
    return [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Extract the transaction fields from this slip. Respond with a single "
                        f"JSON object using exactly these keys: {keys}. Use null for anything "
                        "not printed on the slip."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{_media_type(image_bytes)};base64,{b64}", "detail": detail},
                },
            ],
        },
    ]


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


_calls = 0  # rough per-process API-call counter, for cost visibility in the log


async def _extract_once(image_bytes: bytes, detail: str) -> SlipData:
    global _calls
    _calls += 1
    messages = _messages(image_bytes, detail)
    schema = {
        "type": "json_schema",
        "json_schema": {"name": "payment_slip", "strict": True, "schema": SlipData.JSON_SCHEMA},
    }
    try:
        resp = await _client.chat.completions.create(
            model=CONFIG.extraction_model, messages=messages, max_tokens=1024, response_format=schema
        )
    except BadRequestError:
        log.warning("structured output rejected; retrying as plain JSON")
        resp = await _client.chat.completions.create(
            model=CONFIG.extraction_model, messages=messages, max_tokens=1024,
            response_format={"type": "json_object"},
        )

    choice = resp.choices[0]
    if choice.finish_reason == "content_filter":
        raise RuntimeError("extraction blocked by content filter")
    try:
        return SlipData.model_validate(_loads(choice.message.content or ""))
    except (json.JSONDecodeError, IndexError) as exc:
        log.warning("bad JSON from model: %s", (choice.message.content or "")[:500])
        raise RuntimeError("could not parse extraction result") from exc


def _good(d: SlipData) -> bool:
    return bool(d.is_usable() and d.all_references() and _field_count(d) >= 4)


def _needs_high(d: SlipData) -> bool:
    """Low detail can drop a digit in a long alphanumeric reference - verify those."""
    return not _good(d) or any(len(r.value) >= 14 for r in d.all_references())


async def extract(image_bytes: bytes) -> SlipData:
    """Cheap first (low detail), then escalate only when the read looks thin or
    carries a long reference: high detail, then rotated copies. A clean screenshot
    with short references costs one cheap call."""
    start = _calls
    best = await _extract_once(image_bytes, "low")

    if _needs_high(best):
        try:
            cand = await _extract_once(image_bytes, "high")
            if _field_count(cand) >= _field_count(best):
                best = cand
        except (RuntimeError, OSError) as exc:
            log.warning("high-detail pass failed: %s", exc)

    if not _good(best):
        for deg in (270, 90, 180):
            try:
                cand = await _extract_once(await asyncio.to_thread(_rotate, image_bytes, deg), "high")
            except (RuntimeError, OSError) as exc:
                log.warning("rotation %s failed: %s", deg, exc)
                continue
            if _field_count(cand) > _field_count(best):
                best = cand
            if _good(best):
                break

    log.info("extraction used %d model call(s)", _calls - start)
    return best
