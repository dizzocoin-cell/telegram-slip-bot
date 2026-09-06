"""Schema for the fields we pull off an incoming payment slip."""
from __future__ import annotations

import re
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

# A traceable UPI/IMPS/NEFT bank reference (UTR / RRN) is 12 or 16 digits.
_UTR_LABEL = re.compile(r"\b(utr|rrn)\b", re.IGNORECASE)
_REF_LABEL = re.compile(r"utr|rrn|reference|txn|transaction|order|ref\b", re.IGNORECASE)
_JUNK = {"", "null", "none", "n/a", "na", "-", "--", "nil", "not available", "not found"}
_ACRONYMS = {"utr", "rrn", "upi", "imps", "neft", "rtgs", "id", "no", "ref", "txn", "ifsc"}
# App / wallet brand words we strip out of a reference label ("PhonePe Transaction ID"
# -> "Transaction ID").
_BRANDS = re.compile(
    r"\b(phonepe|phone pe|g ?pay|google ?pay|paytm|bhim|amazon ?pay|cred|mobikwik|"
    r"freecharge|whatsapp( ?pay)?|navi|slice|fampay|jupiter|fi money|fi)\b",
    re.IGNORECASE,
)


def clean(value: object) -> str | None:
    """Normalise model output: junk / placeholder strings become None."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in _JUNK else s


def nice_label(raw: str | None) -> str:
    """'transaction id' -> 'Transaction ID', 'rrn' -> 'RRN', '' -> 'Reference No.'"""
    raw = _BRANDS.sub(" ", (raw or "")).strip().rstrip(":").strip()
    if not raw:
        return "Reference No."
    words = re.split(r"[\s_]+", raw)
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w.capitalize() for w in words)


class RefNumber(BaseModel):
    label: str | None = None
    value: str


class SlipData(BaseModel):
    account_holder_name: str | None = Field(None, description="Beneficiary / payee name")
    account_number: str | None = Field(None, description="Beneficiary account number")
    ifsc_code: str | None = Field(None, description="Beneficiary bank IFSC code")
    bank_name: str | None = Field(None, description="Beneficiary bank name if shown or inferable from IFSC")
    amount: str | None = Field(None, description="Amount as printed, e.g. '50,000.00'")
    currency: str | None = Field(None, description="Currency symbol or code, e.g. 'INR' or '₹'")
    date: str | None = Field(None, description="Transaction date exactly as printed")
    time: str | None = Field(None, description="Transaction time exactly as printed, if present")
    transaction_id: str | None = Field(None, description="Primary UTR / RRN / reference number")
    transaction_id_label: str | None = Field(None, description="Printed label of that number")
    reference_numbers: list[RefNumber] = Field(default_factory=list)
    transfer_mode: str | None = Field(None, description="IMPS / NEFT / RTGS / UPI, if shown")
    status: str | None = Field(None, description="e.g. 'SUCCESS', 'Completed'")

    @field_validator(
        "account_holder_name", "account_number", "ifsc_code", "bank_name", "amount",
        "currency", "date", "time", "transaction_id", "transaction_id_label",
        "transfer_mode", "status", mode="before",
    )
    @classmethod
    def _clean_str(cls, v: object) -> str | None:
        return clean(v)

    @field_validator("reference_numbers", mode="before")
    @classmethod
    def _clean_refs(cls, v: object) -> list:
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, dict):
                label, value = item.get("label"), item.get("value")
            else:
                label, value = getattr(item, "label", None), getattr(item, "value", None)
            value = clean(value)
            if value:
                out.append({"label": clean(label), "value": value})
        return out

    JSON_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "account_holder_name": {"type": ["string", "null"]},
            "account_number": {"type": ["string", "null"]},
            "ifsc_code": {"type": ["string", "null"]},
            "bank_name": {"type": ["string", "null"]},
            "amount": {"type": ["string", "null"]},
            "currency": {"type": ["string", "null"]},
            "date": {"type": ["string", "null"]},
            "time": {"type": ["string", "null"]},
            "transaction_id": {"type": ["string", "null"]},
            "transaction_id_label": {"type": ["string", "null"]},
            "reference_numbers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": ["string", "null"]},
                        "value": {"type": "string"},
                    },
                    "required": ["label", "value"],
                },
            },
            "transfer_mode": {"type": ["string", "null"]},
            "status": {"type": ["string", "null"]},
        },
        "required": [
            "account_holder_name", "account_number", "ifsc_code", "bank_name",
            "amount", "currency", "date", "time", "transaction_id",
            "transaction_id_label", "reference_numbers", "transfer_mode", "status",
        ],
    }

    def all_references(self) -> list[RefNumber]:
        """Merged, de-duplicated reference numbers, best (traceable) first.
        Drops anything that is really the account number or IFSC."""
        blocked = {v.strip() for v in (self.account_number, self.ifsc_code) if v}
        pool = list(self.reference_numbers)
        if self.transaction_id:
            pool.insert(0, RefNumber(label=self.transaction_id_label, value=self.transaction_id))
        seen: dict[str, RefNumber] = {}
        for r in pool:
            v = (r.value or "").strip()
            label = (r.label or "").strip()
            if not v or v in seen or v in blocked:
                continue
            if re.search(r"ifsc|account", label, re.IGNORECASE):
                continue
            seen[v] = RefNumber(label=label or None, value=v)

        def score(r: RefNumber) -> tuple[int, int, int]:
            digits = re.sub(r"\D", "", r.value)
            all_digits = digits == r.value.strip()
            return (
                2 if r.label and _UTR_LABEL.search(r.label) else 0,
                2 if (all_digits and len(digits) in (12, 16))
                else (1 if all_digits and 10 <= len(digits) <= 22 else 0),
                1 if r.label and _REF_LABEL.search(r.label) else 0,
            )

        return sorted(seen.values(), key=score, reverse=True)

    def primary_reference(self) -> RefNumber | None:
        refs = self.all_references()
        return refs[0] if refs else None

    def is_usable(self) -> bool:
        """Enough signal that this really was a payment slip."""
        has_ref = bool(self.primary_reference())
        has_target = bool(self.account_number or self.account_holder_name)
        return bool(self.amount) and (has_ref or has_target)
