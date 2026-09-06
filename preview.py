"""Render a sample confirmation without Telegram or OpenAI, for design tweaks.

    python preview.py            # uses built-in sample data
    python preview.py slip.jpg   # runs the real OpenAI extraction on an image
"""
import asyncio
import sys

from models import SlipData
from slip_renderer import render

SAMPLE = SlipData(
    account_holder_name="M BATHISH",
    account_number="520291023336226",
    ifsc_code="UBIN0907235",
    bank_name="Union Bank of India",
    amount="50,000.00",
    currency="₹",
    date="03 Sep 2026",
    time="04:35:27 PM",
    transaction_id="624685514402",
    transaction_id_label="UTR",
    transfer_mode="IMPS",
    status="SUCCESS",
)


async def _from_image(path: str) -> SlipData:
    from extractor import extract

    with open(path, "rb") as fh:
        data = await extract(fh.read())
    print(data.model_dump_json(indent=2))
    return data


def main() -> None:
    data = asyncio.run(_from_image(sys.argv[1])) if len(sys.argv) > 1 else SAMPLE
    with open("preview.png", "wb") as fh:
        fh.write(render(data))
    print("wrote preview.png")


if __name__ == "__main__":
    main()
