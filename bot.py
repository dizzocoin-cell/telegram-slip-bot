"""Telegram bot: read payment slips posted in a group, reply with a
company-branded payment confirmation image.

Setup notes:
- In @BotFather run /setprivacy -> Disable, otherwise the bot never receives
  group photos.
- Add the bot to the group. Send /id in the group to get its chat ID if you
  want to lock the bot to specific groups via ALLOWED_CHAT_IDS.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time

from openai import APIStatusError, RateLimitError
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import CONFIG
from documents import to_page_images
from extractor import extract
from models import nice_label
from slip_renderer import confirmation_ref, render

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("slipbot")

_sem = asyncio.Semaphore(CONFIG.max_concurrency)
MAX_DOWNLOAD = 20 * 1024 * 1024  # bytes


def _allowed(chat_id: int) -> bool:
    return not CONFIG.allowed_chat_ids or chat_id in CONFIG.allowed_chat_ids


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send a payment slip — photo, image file (JPG/PNG/WEBP/HEIC) or PDF — and "
        f"I'll reply with a {CONFIG.branding.name} payment confirmation."
    )


def _is_pdf(doc) -> bool:
    return bool(doc) and (
        (doc.mime_type or "") == "application/pdf"
        or (doc.file_name or "").lower().endswith(".pdf")
    )


def _is_image_doc(doc) -> bool:
    return bool(doc) and (
        (doc.mime_type or "").startswith("image/")
        or (doc.file_name or "").lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")
        )
    )


# Telegram puts an album's caption on only one of its images. Remember it per
# media_group_id so every reply in the album can carry the same caption.
_ALBUM_CAPTIONS: dict[str, tuple[str, float]] = {}
_ALBUM_TTL = 600.0


def _remember_album_caption(msg) -> None:
    if msg.media_group_id and msg.caption and msg.caption.strip():
        _ALBUM_CAPTIONS[msg.media_group_id] = (msg.caption.strip(), time.monotonic())
    if len(_ALBUM_CAPTIONS) > 300:
        old = time.monotonic() - _ALBUM_TTL
        for k in [k for k, (_, t) in _ALBUM_CAPTIONS.items() if t < old]:
            _ALBUM_CAPTIONS.pop(k, None)


async def _sender_caption(msg) -> str | None:
    if msg.caption and msg.caption.strip():
        return msg.caption.strip()
    gid = msg.media_group_id
    if not gid:
        return None
    # the captioned image of the album may arrive a moment after this one
    for _ in range(15):
        hit = _ALBUM_CAPTIONS.get(gid)
        if hit:
            return hit[0]
        await asyncio.sleep(0.1)
    return None


def _feed_code(text: str | None) -> str | None:
    """The team code (AK, ROU, ...) in the caption, if the feed channel is on."""
    if not text or not CONFIG.slip_feed_channel or not CONFIG.route_codes:
        return None
    for token in re.findall(r"[A-Za-z]{2,6}", text):
        if token.upper() in CONFIG.route_codes:
            return token.upper()
    return None


async def cmd_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"chat_id: `{update.effective_chat.id}`", parse_mode="Markdown")


async def handle_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or not _allowed(msg.chat_id):
        return

    is_pdf = False
    if msg.photo:
        tg_file = msg.photo[-1]
    elif _is_pdf(msg.document):
        tg_file, is_pdf = msg.document, True
    elif _is_image_doc(msg.document):
        tg_file = msg.document
    else:
        return

    _remember_album_caption(msg)

    if getattr(tg_file, "file_size", 0) and tg_file.file_size > MAX_DOWNLOAD:
        await msg.reply_text("That file is too large to process.", do_quote=True)
        return

    await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_PHOTO)
    sent = False
    try:
        f = await context.bot.get_file(tg_file.file_id)
        raw = bytes(await f.download_as_bytearray())
        pages = await asyncio.to_thread(to_page_images, raw, is_pdf)

        if not pages:
            await msg.reply_text(
                "Couldn't open that file. Send the slip as a photo, image or PDF.",
                do_quote=True,
            )
            return

        data = None
        for page in pages:
            async with _sem:
                data = await extract(page)
            if data.is_usable():
                break

        if data is None or not data.is_usable():
            await msg.reply_text(
                "Couldn't read a payment slip from that file. Please resend a clearer copy.",
                do_quote=True,
            )
            return

        png = await asyncio.to_thread(render, data)
        ref = confirmation_ref(data)
        primary = data.primary_reference()

        lines = []
        sender_text = await _sender_caption(msg)       # own caption, or the album's
        if sender_text:
            lines.append(sender_text)
        if primary:
            lines.append(f"{nice_label(primary.label)}: {primary.value}")
        caption = "\n".join(lines)[:1024] or None

        await msg.reply_photo(
            photo=io.BytesIO(png),
            filename=f"{ref}.png",
            caption=caption,
            do_quote=True,
        )
        sent = True
        log.info("chat=%s msg=%s ref=%s primary=%s", msg.chat_id, msg.message_id, ref,
                 primary.value if primary else None)

        if _feed_code(sender_text):
            try:
                await context.bot.send_photo(
                    chat_id=CONFIG.slip_feed_channel,
                    photo=io.BytesIO(png),
                    filename=f"{ref}.png",
                    caption=caption,
                )
                log.info("forwarded ref=%s to feed channel %s", ref, CONFIG.slip_feed_channel)
            except Exception as exc:  # noqa: BLE001
                log.warning("feed-channel forward failed (ref=%s): %s", ref, exc)

    except (RateLimitError, APIStatusError) as exc:
        log.error("extraction API error chat=%s msg=%s: %s", msg.chat_id, msg.message_id, exc)
        if not sent:
            await msg.reply_text(
                "Extraction service is unavailable right now (quota or rate limit). "
                "Please resend this slip in a few minutes.",
                do_quote=True,
            )
    except Exception:  # noqa: BLE001 - keep the bot alive, report to the group
        log.exception("failed to process slip chat=%s msg=%s", msg.chat_id, msg.message_id)
        if not sent:
            await msg.reply_text(
                "Something went wrong processing that slip. Please try again.",
                do_quote=True,
            )


async def log_update(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    log.info(
        "update id=%s type=%s chat=%s(%s) from=%s text=%r",
        update.update_id,
        "message" if update.message else update.__class__.__name__,
        getattr(update.effective_chat, "id", None),
        getattr(update.effective_chat, "type", None),
        getattr(update.effective_user, "username", None),
        (m.text or m.caption or ("<photo>" if m and m.photo else "<other>")) if m else None,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "What this bot does"),
        BotCommand("id", "Show this chat's ID"),
    ])


def main() -> None:
    CONFIG.validate()
    app = (
        Application.builder()
        .token(CONFIG.telegram_token)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_slip))
    app.add_error_handler(on_error)

    log.info("bot starting: model=%s groups=%s", CONFIG.extraction_model,
             CONFIG.allowed_chat_ids or "any")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
