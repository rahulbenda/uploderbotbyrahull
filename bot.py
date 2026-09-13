import os
import re
import time
import asyncio
import socket
import socketserver
import threading
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

from dotenv import load_dotenv
from pypdf import PdfReader

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from telegram.request import HTTPXRequest

from queue_manager import UploadQueue

from database import (
    init_database,
    add_history,
    update_history,
    get_history,
    get_history_item,
    find_duplicate_url,
    find_duplicate_file,
    get_statistics,
    clear_history,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

ALLOWED_USER_ID = 7747240557

DOWNLOAD_FOLDER = Path("downloads")

DOWNLOAD_FOLDER.mkdir(
    parents=True,
    exist_ok=True,
)

MAX_FILE_SIZE = 100 * 1024 * 1024

HTTP_TIMEOUT = httpx.Timeout(
    connect=30.0,
    read=300.0,
    write=300.0,
    pool=30.0,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/151.0 Safari/537.36"
    )
}


# ============================================================
# GLOBAL QUEUE
# ============================================================

upload_queue = UploadQueue()


# ============================================================
# HELPERS
# ============================================================

def is_allowed_user(user_id: int) -> bool:

    return user_id == ALLOWED_USER_ID


def format_bytes(size):

    if not size:
        return "0 B"

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    value = float(size)

    for unit in units:

        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PB"


def sanitize_filename(filename):

    if not filename:

        filename = "download"

    filename = unquote(filename)

    filename = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        filename,
    )

    filename = filename.strip()

    if not filename:

        filename = "download"

    return filename[:180]


def get_extension_from_url(url):

    path = urlparse(url).path

    extension = Path(path).suffix.lower()

    return extension


def detect_file_type(
    filename=None,
    content_type=None,
    url=None,
):

    filename = filename or ""
    content_type = content_type or ""

    extension = Path(filename).suffix.lower()

    if not extension and url:

        extension = get_extension_from_url(url)

    if extension in (
        ".pdf",
    ):
        return "pdf"

    if extension in (
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".webm",
        ".m4v",
    ):
        return "video"

    if (
        ".m3u8" in (url or "").lower()
        or "mpegurl" in content_type.lower()
    ):
        return "video"

    if "pdf" in content_type.lower():
        return "pdf"

    if content_type.startswith("video/"):
        return "video"

    return "document"


def clean_title(title):

    if not title:
        return "Untitled"

    title = title.strip()

    title = re.sub(
        r"\s+",
        " ",
        title,
    )

    return title.strip()


def build_filename(
    title,
    file_type,
):

    title = sanitize_filename(
        clean_title(title)
    )

    if file_type == "pdf":

        if not title.lower().endswith(".pdf"):
            title += ".pdf"

    elif file_type == "video":

        if not title.lower().endswith(
            (
                ".mp4",
                ".mkv",
                ".avi",
                ".mov",
                ".webm",
                ".m4v",
            )
        ):
            title += ".mp4"

    return title


# ============================================================
# TXT PARSER
# ============================================================

def parse_txt_content(text):

    entries = []

    lines = text.splitlines()

    for raw_line in lines:

        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        match = re.match(
            r"^(.*?)\s*:\s*(https?://\S+)\s*$",
            line,
        )

        if not match:
            continue

        title = match.group(1).strip()

        url = match.group(2).strip()

        if not title or not url:
            continue

        file_type = detect_file_type(
            url=url
        )

        entries.append(
            {
                "title": clean_title(title),
                "url": url,
                "file_type": file_type,
            }
        )

    return entries


# ============================================================
# PDF CAPTION EXTRACTION
# ============================================================

def extract_pdf_caption(
    pdf_path,
    fallback_title,
):

    try:

        reader = PdfReader(
            str(pdf_path)
        )

        collected = []

        max_pages = min(
            len(reader.pages),
            3,
        )

        for page_number in range(
            max_pages
        ):

            page = reader.pages[
                page_number
            ]

            text = page.extract_text()

            if not text:
                continue

            for line in text.splitlines():

                line = line.strip()

                if not line:
                    continue

                line = re.sub(
                    r"\s+",
                    " ",
                    line,
                )

                if len(line) < 3:
                    continue

                collected.append(line)

        if not collected:

            return clean_title(
                fallback_title
            )

        # Remove common generic PDF headings.
        ignored = {
            "pdf",
            "contents",
            "index",
            "syllabus",
        }

        for line in collected:

            lower = line.lower()

            if lower in ignored:
                continue

            if len(line) > 8:

                return line[:1000]

        return collected[0][:1000]

    except Exception:

        return clean_title(
            fallback_title
        )


# ============================================================
# DOWNLOAD PROGRESS
# ============================================================

def make_progress_bar(percent):

    total_blocks = 20

    filled = int(
        total_blocks
        * percent
        / 100
    )

    empty = (
        total_blocks
        - filled
    )

    return (
        "█" * filled
        + "░" * empty
    )


def calculate_speed(
    downloaded,
    start_time,
):

    elapsed = max(
        time.time() - start_time,
        0.1,
    )

    return downloaded / elapsed


def calculate_eta(
    downloaded,
    total,
    start_time,
):

    speed = calculate_speed(
        downloaded,
        start_time,
    )

    if speed <= 0 or not total:
        return None

    remaining = max(
        total - downloaded,
        0,
    )

    return remaining / speed


def format_eta(seconds):

    if seconds is None:
        return "--"

    seconds = int(
        max(seconds, 0)
    )

    minutes, seconds = divmod(
        seconds,
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    if hours:
        return (
            f"{hours}h "
            f"{minutes}m"
        )

    if minutes:
        return (
            f"{minutes}m "
            f"{seconds}s"
        )

    return f"{seconds}s"


# ============================================================
# DOWNLOAD NORMAL FILE
# ============================================================

async def download_http_file(
    url,
    destination,
    progress_callback=None,
):

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:

        async with client.stream(
            "GET",
            url,
        ) as response:

            response.raise_for_status()

            total = int(
                response.headers.get(
                    "content-length",
                    0,
                )
            )

            downloaded = 0

            start_time = time.time()

            last_update = 0

            with open(
                destination,
                "wb",
            ) as file:

                async for chunk in response.aiter_bytes(
                    chunk_size=1024 * 1024
                ):

                    if not chunk:
                        continue

                    file.write(chunk)

                    downloaded += len(
                        chunk
                    )

                    now = time.time()

                    if (
                        now - last_update
                        >= 1
                    ):

                        last_update = now

                        if progress_callback:

                            await progress_callback(
                                downloaded,
                                total,
                                start_time,
                            )

            return {
                "size": downloaded,
                "content_type": response.headers.get(
                    "content-type",
                    "",
                ),
                "headers": dict(
                    response.headers
                ),
            }


# ============================================================
# DOWNLOAD M3U8 USING FFMPEG
# ============================================================

async def download_m3u8(
    url,
    destination,
    progress_callback=None,
):

    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto",
        "-i",
        url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(destination),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    while True:

        try:

            await asyncio.wait_for(
                process.wait(),
                timeout=1,
            )

            break

        except asyncio.TimeoutError:

            if progress_callback:

                size = (
                    destination.stat().st_size
                    if destination.exists()
                    else 0
                )

                await progress_callback(
                    size,
                    0,
                    0,
                )

    stderr = await process.stderr.read()

    if process.returncode != 0:

        error_text = (
            stderr.decode(
                "utf-8",
                errors="ignore",
            )
            .strip()
        )

        raise RuntimeError(
            "FFmpeg failed: "
            + (
                error_text
                or "Unknown error"
            )
        )

    if not destination.exists():

        raise RuntimeError(
            "FFmpeg did not create output file."
        )

    return destination.stat().st_size


# ============================================================
# PROGRESS MESSAGE
# ============================================================

async def update_download_message(
    message,
    downloaded,
    total,
    start_time,
    file_type,
):

    if total:

        percent = (
            downloaded
            / total
            * 100
        )

        percent = min(
            percent,
            100,
        )

        speed = calculate_speed(
            downloaded,
            start_time,
        )

        eta = calculate_eta(
            downloaded,
            total,
            start_time,
        )

        text = (
            f"📥 Downloading {file_type}\n\n"
            f"{make_progress_bar(percent)} "
            f"{percent:.1f}%\n\n"
            f"📦 {format_bytes(downloaded)} / "
            f"{format_bytes(total)}\n"
            f"⚡ {format_bytes(speed)}/s\n"
            f"⏱ ETA: {format_eta(eta)}"
        )

    else:

        text = (
            f"📥 Downloading {file_type}\n\n"
            f"📦 {format_bytes(downloaded)}"
        )

    try:

        await message.edit_text(
            text
        )

    except Exception:
        pass


# ============================================================
# QUEUE PROCESSOR
# ============================================================

async def process_upload(
    item_id,
    item,
):

    message = None

    temp_path = None

    try:

        item.status = "processing"

        bot = application.bot

        message = await bot.send_message(
            chat_id=item.chat_id,
            text=(
                f"▶️ Processing Queue #{item_id}\n\n"
                f"📌 {item.caption or item.file_name or 'File'}"
            ),
        )

        # ----------------------------------------------------
        # DUPLICATE URL CHECK
        # ----------------------------------------------------

        duplicate = find_duplicate_url(
            item.user_id,
            item.url,
        )

        if duplicate and not item.force_download:

            item.status = "failed"

            await message.edit_text(
                "⚠️ Duplicate URL detected.\n\n"
                "Use DOWNLOAD AGAIN from the original warning."
            )

            if item.history_id:

                update_history(
                    item.history_id,
                    "failed",
                    error="Duplicate URL",
                )

            return

        # ----------------------------------------------------
        # FILE TYPE
        # ----------------------------------------------------

        item.file_type = detect_file_type(
            filename=item.file_name,
            url=item.url,
        )

        title = (
            item.caption
            or item.file_name
            or "download"
        )

        filename = build_filename(
            title,
            item.file_type,
        )

        item.file_name = filename

        temp_path = (
            DOWNLOAD_FOLDER
            / f"{item_id}_{filename}"
        )

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        if item.file_type == "video" and ".m3u8" in item.url.lower():

            await message.edit_text(
                f"🎥 Queue #{item_id}\n\n"
                f"📌 {title}\n\n"
                f"🎬 Converting M3U8 → MP4..."
            )

            async def video_progress(
                downloaded,
                total,
                start_time,
            ):

                try:

                    await message.edit_text(
                        f"🎥 Queue #{item_id}\n\n"
                        f"📌 {title}\n\n"
                        f"🎬 Downloading video...\n"
                        f"📦 {format_bytes(downloaded)}"
                    )

                except Exception:
                    pass

            item.file_size = await download_m3u8(
                item.url,
                temp_path,
                video_progress,
            )

        else:

            await message.edit_text(
                f"📥 Queue #{item_id}\n\n"
                f"📌 {title}\n\n"
                f"Starting download..."
            )

            async def download_progress(
                downloaded,
                total,
                start_time,
            ):

                await update_download_message(
                    message,
                    downloaded,
                    total,
                    start_time,
                    item.file_type,
                )

            result = await download_http_file(
                item.url,
                temp_path,
                download_progress,
            )

            item.file_size = result["size"]

        # ----------------------------------------------------
        # FILE SIZE CHECK
        # ----------------------------------------------------

        if (
            item.file_size
            and item.file_size > MAX_FILE_SIZE
        ):

            raise RuntimeError(
                "File is larger than Telegram Bot API limit of 100 MB."
            )

        # ----------------------------------------------------
        # PDF AUTO CAPTION
        # ----------------------------------------------------

        if item.file_type == "pdf":

            await message.edit_text(
                f"📖 Queue #{item_id}\n\n"
                f"Reading PDF title..."
            )

            extracted_caption = (
                extract_pdf_caption(
                    temp_path,
                    title,
                )
            )

            if extracted_caption:

                item.caption = (
                    extracted_caption
                )

        # ----------------------------------------------------
        # DUPLICATE FILE CHECK
        # ----------------------------------------------------

        if not item.force_download:

            duplicate_file = find_duplicate_file(
                item.user_id,
                item.file_name,
                item.file_size,
            )

            if duplicate_file:

                item.status = "failed"

                await message.edit_text(
                    "⚠️ Same file already exists in upload history."
                )

                if item.history_id:

                    update_history(
                        item.history_id,
                        "failed",
                        error="Duplicate file",
                    )

                return

        # ----------------------------------------------------
        # UPLOAD
        # ----------------------------------------------------

        item.status = "uploading"

        caption = (
            item.caption
            or title
        )

        await message.edit_text(
            f"📤 Queue #{item_id}\n\n"
            f"Uploading...\n\n"
            f"📄 {item.file_name}\n"
            f"📝 {caption}"
        )

        with open(
            temp_path,
            "rb",
        ) as file:

            if item.file_type == "video":

                await bot.send_video(
                    chat_id=item.chat_id,
                    video=file,
                    filename=item.file_name,
                    caption=caption[:1024],
                    supports_streaming=True,
                )

            else:

                await bot.send_document(
                    chat_id=item.chat_id,
                    document=file,
                    filename=item.file_name,
                    caption=caption[:1024],
                )

        # ----------------------------------------------------
        # COMPLETE
        # ----------------------------------------------------

        item.status = "completed"

        if item.history_id:

            update_history(
                item.history_id,
                "completed",
                file_name=item.file_name,
                file_type=item.file_type,
                file_size=item.file_size,
            )

        await message.edit_text(
            f"✅ Queue #{item_id} Completed\n\n"
            f"📄 {item.file_name}\n"
            f"📦 {format_bytes(item.file_size)}\n\n"
            f"📝 Caption:\n{caption}"
        )

    except asyncio.CancelledError:

        item.status = "cancelled"

        item.error = (
            "Cancelled by user."
        )

        if item.history_id:

            update_history(
                item.history_id,
                "cancelled",
                error=item.error,
            )

        if message:

            try:

                await message.edit_text(
                    f"🛑 Queue #{item_id} Cancelled."
                )

            except Exception:
                pass

    except Exception as error:

        item.status = "failed"

        item.error = str(error)

        if item.history_id:

            update_history(
                item.history_id,
                "failed",
                file_name=item.file_name,
                file_type=item.file_type,
                file_size=item.file_size,
                error=item.error,
            )

        if message:

            try:

                await message.edit_text(
                    f"❌ Queue #{item_id} Failed\n\n"
                    f"{item.error}"
                )

            except Exception:
                pass

    finally:

        if temp_path:

            try:

                if temp_path.exists():

                    temp_path.unlink()

            except Exception:
                pass


# ============================================================
# DUPLICATE KEYBOARD
# ============================================================

def duplicate_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬇️ DOWNLOAD AGAIN",
                    callback_data="download_again",
                ),
                InlineKeyboardButton(
                    "❌ CANCEL",
                    callback_data="duplicate_cancel",
                ),
            ]
        ]
    )


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):

        await update.message.reply_text(
            "⛔ This bot is private."
        )

        return

    await update.message.reply_text(
        "🤖 Advanced Telegram Uploader\n\n"
        "Ready.\n\n"
        "📄 Send a PDF/video URL\n"
        "📚 OR send a TXT batch file.\n\n"
        "TXT format:\n"
        "Title : URL\n\n"
        "Bot will automatically:\n"
        "• Read title\n"
        "• Detect PDF/video\n"
        "• Create queue\n"
        "• Download\n"
        "• Upload\n"
        "• Use title as filename\n"
        "• Use title as caption\n\n"
        "Commands:\n"
        "/queue\n"
        "/cancel <queue_id>\n"
        "/history\n"
        "/stats\n"
        "/clearhistory"
    )


# ============================================================
# TXT FILE HANDLER
# ============================================================

async def txt_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):

        await update.message.reply_text(
            "⛔ This bot is private."
        )

        return

    document = update.message.document

    filename = (
        document.file_name
        or "batch.txt"
    )

    if not filename.lower().endswith(
        ".txt"
    ):

        await update.message.reply_text(
            "❌ Please send a .txt file."
        )

        return

    status_message = await update.message.reply_text(
        "📚 Reading TXT batch..."
    )

    telegram_file = await context.bot.get_file(
        document.file_id
    )

    temp_txt = (
        DOWNLOAD_FOLDER
        / f"batch_{document.file_unique_id}.txt"
    )

    try:

        await telegram_file.download_to_drive(
            custom_path=str(temp_txt)
        )

        text = temp_txt.read_text(
            encoding="utf-8",
            errors="ignore",
        )

        entries = parse_txt_content(
            text
        )

        if not entries:

            await status_message.edit_text(
                "❌ No valid `Title : URL` entries found in TXT."
            )

            return

        pdf_count = sum(
            1
            for entry in entries
            if entry["file_type"] == "pdf"
        )

        video_count = sum(
            1
            for entry in entries
            if entry["file_type"] == "video"
        )

        other_count = (
            len(entries)
            - pdf_count
            - video_count
        )

        added = 0

        duplicate_count = 0

        for entry in entries:

            duplicate = find_duplicate_url(
                user_id,
                entry["url"],
            )

            if duplicate:

                duplicate_count += 1

                continue

            history_id = add_history(
                queue_id=0,
                user_id=user_id,
                url=entry["url"],
                file_name=build_filename(
                    entry["title"],
                    entry["file_type"],
                ),
                file_type=entry["file_type"],
                status="waiting",
            )

            item_id = await upload_queue.add(
                user_id=user_id,
                chat_id=update.effective_chat.id,
                url=entry["url"],
                message_id=update.message.message_id,
                caption=entry["title"],
                file_name=build_filename(
                    entry["title"],
                    entry["file_type"],
                ),
                force_download=False,
            )

            item = upload_queue.get(
                item_id
            )

            if item:

                item.history_id = history_id

                update_history(
                    history_id,
                    "waiting",
                )

            added += 1

        await status_message.edit_text(
            f"📚 TXT Batch Added Successfully\n\n"
            f"📦 Total entries: {len(entries)}\n"
            f"📄 PDF: {pdf_count}\n"
            f"🎥 Video: {video_count}\n"
            f"📎 Other: {other_count}\n\n"
            f"➕ Added to queue: {added}\n"
            f"⏭️ Already uploaded/skipped: {duplicate_count}\n\n"
            f"▶️ Uploading one by one..."
        )

    except Exception as error:

        await status_message.edit_text(
            f"❌ TXT processing failed:\n\n{error}"
        )

    finally:

        try:

            if temp_txt.exists():

                temp_txt.unlink()

        except Exception:
            pass


# ============================================================
# URL HANDLER
# ============================================================

async def url_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):

        await update.message.reply_text(
            "⛔ This bot is private."
        )

        return

    text = (
        update.message.text
        or ""
    ).strip()

    urls = re.findall(
        r"https?://\S+",
        text,
    )

    if not urls:

        await update.message.reply_text(
            "❌ No valid URL found."
        )

        return

    for url in urls:

        duplicate = find_duplicate_url(
            user_id,
            url,
        )

        if duplicate:

            warning = await update.message.reply_text(
                "⚠️ This URL has already been uploaded.\n\n"
                f"📄 {duplicate['file_name'] or 'File'}\n"
                f"📦 {format_bytes(duplicate['file_size'])}\n\n"
                "What do you want to do?",
                reply_markup=duplicate_keyboard(),
            )

            context.bot_data[
                f"duplicate_{warning.message_id}"
            ] = {
                "url": url,
                "user_id": user_id,
                "chat_id": update.effective_chat.id,
            }

            continue

        file_type = detect_file_type(
            url=url
        )

        title = Path(
            urlparse(url).path
        ).stem

        if not title:

            title = "Telegram Upload"

        filename = build_filename(
            title,
            file_type,
        )

        history_id = add_history(
            queue_id=0,
            user_id=user_id,
            url=url,
            file_name=filename,
            file_type=file_type,
            status="waiting",
        )

        item_id = await upload_queue.add(
            user_id=user_id,
            chat_id=update.effective_chat.id,
            url=url,
            message_id=update.message.message_id,
            caption=title,
            file_name=filename,
            force_download=False,
        )

        item = upload_queue.get(
            item_id
        )

        if item:

            item.history_id = history_id

        position = upload_queue.get_position(
            item_id
        )

        await update.message.reply_text(
            f"📥 Added to queue\n\n"
            f"🔢 Queue #{item_id}\n"
            f"📌 {title}\n"
            f"📁 Type: {file_type}\n"
            f"📍 Position: {position or 0}"
        )


# ============================================================
# QUEUE COMMAND
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):
        return

    lines = []

    for item_id, item in upload_queue.items.items():

        if item.user_id != user_id:
            continue

        lines.append(
            f"#{item_id} | "
            f"{item.status} | "
            f"{item.file_name or item.caption or 'File'}"
        )

    if not lines:

        await update.message.reply_text(
            "📭 Queue is empty."
        )

        return

    await update.message.reply_text(
        "📚 Current Queue\n\n"
        + "\n".join(lines[-30:])
    )


# ============================================================
# CANCEL COMMAND
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):
        return

    # Usage:
    # /cancel 59
    # /cancel all
    if not context.args:

        await update.message.reply_text(
            "❌ Queue ID missing.\n\n"
            "Use:\n"
            "/cancel 59\n"
            "/cancel all"
        )

        return

    cancel_target = context.args[0].strip().lower()

    # --------------------------------------------------------
    # CANCEL ALL ACTIVE / WAITING ITEMS
    # --------------------------------------------------------

    if cancel_target == "all":

        cancelled_ids = []

        for item_id, item in list(
            upload_queue.items.items()
        ):

            if item.user_id != user_id:
                continue

            if item.status in (
                "completed",
                "failed",
                "cancelled",
            ):
                continue

            cancelled = upload_queue.cancel(
                item_id
            )

            if cancelled:

                cancelled_ids.append(item_id)

                if item.history_id:

                    update_history(
                        item.history_id,
                        "cancelled",
                        error="Cancelled by user using /cancel all.",
                    )

        if not cancelled_ids:

            await update.message.reply_text(
                "ℹ️ There are no active/waiting queue items to cancel."
            )

            return

        await update.message.reply_text(
            "🛑 All active/waiting queue items cancelled.\n\n"
            f"📦 Cancelled: {len(cancelled_ids)}\n"
            f"🔢 Queue IDs: "
            + ", ".join(
                f"#{item_id}"
                for item_id in cancelled_ids
            )
        )

        return

    # --------------------------------------------------------
    # CANCEL ONE ITEM
    # --------------------------------------------------------

    try:

        item_id = int(cancel_target)

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid Queue ID.\n\n"
            "Use:\n"
            "/cancel 59\n"
            "/cancel all"
        )

        return

    item = upload_queue.get(item_id)

    if not item:

        await update.message.reply_text(
            f"❌ Queue #{item_id} not found."
        )

        return

    if item.user_id != user_id:

        await update.message.reply_text(
            "⛔ You cannot cancel this queue item."
        )

        return

    if item.status in (
        "completed",
        "failed",
        "cancelled",
    ):

        await update.message.reply_text(
            f"ℹ️ Queue #{item_id} is already "
            f"{item.status}."
        )

        return

    cancelled = upload_queue.cancel(item_id)

    if not cancelled:

        await update.message.reply_text(
            f"❌ Could not cancel Queue #{item_id}.\n\n"
            f"Current status: {item.status}"
        )

        return

    if item.history_id:

        update_history(
            item.history_id,
            "cancelled",
            error="Cancelled by user.",
        )

    await update.message.reply_text(
        f"🛑 Queue #{item_id} cancelled.\n\n"
        f"📌 {item.file_name or item.caption or 'File'}"
    )


# ============================================================
# HISTORY
# ============================================================

async def history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):
        return

    rows = get_history(
        user_id,
        limit=20,
    )

    if not rows:

        await update.message.reply_text(
            "📭 No upload history."
        )

        return

    for row in rows:

        text = (
            f"📄 {row['file_name'] or 'Unknown'}\n"
            f"📁 {row['file_type'] or 'Unknown'}\n"
            f"📦 {format_bytes(row['file_size'])}\n"
            f"📊 Status: {row['status']}\n"
            f"🕒 {row['created_at']}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "DETAILS",
                        callback_data=f"history_{row['id']}",
                    )
                ]
            ]
        )

        await update.message.reply_text(
            text,
            reply_markup=keyboard,
        )


# ============================================================
# STATS
# ============================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):
        return

    stats = get_statistics(
        user_id
    )

    total = stats["total"]

    completed = stats["completed"]

    success_rate = (
        completed / total * 100
        if total
        else 0
    )

    await update.message.reply_text(
        f"📊 Upload Statistics\n\n"
        f"📦 Total: {total}\n"
        f"✅ Completed: {completed}\n"
        f"❌ Failed: {stats['failed']}\n"
        f"🛑 Cancelled: {stats['cancelled']}\n"
        f"📈 Success rate: {success_rate:.1f}%\n"
        f"💾 Uploaded: {format_bytes(stats['total_bytes'])}"
    )


# ============================================================
# CLEAR HISTORY
# ============================================================

async def clear_history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_allowed_user(user_id):
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "YES, CLEAR",
                    callback_data="clear_yes",
                ),
                InlineKeyboardButton(
                    "CANCEL",
                    callback_data="clear_no",
                ),
            ]
        ]
    )

    await update.message.reply_text(
        "⚠️ Delete all upload history?",
        reply_markup=keyboard,
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if not is_allowed_user(user_id):
        return

    data = query.data

    # --------------------------------------------------------
    # DOWNLOAD AGAIN
    # --------------------------------------------------------

    if data == "download_again":

        source = context.bot_data.get(
            f"duplicate_{query.message.message_id}"
        )

        if not source:

            await query.edit_message_text(
                "❌ Duplicate request expired."
            )

            return

        url = source["url"]

        file_type = detect_file_type(
            url=url
        )

        title = Path(
            urlparse(url).path
        ).stem

        filename = build_filename(
            title,
            file_type,
        )

        history_id = add_history(
            queue_id=0,
            user_id=user_id,
            url=url,
            file_name=filename,
            file_type=file_type,
            status="waiting",
        )

        item_id = await upload_queue.add(
            user_id=user_id,
            chat_id=query.message.chat_id,
            url=url,
            message_id=query.message.message_id,
            caption=title,
            file_name=filename,
            force_download=True,
        )

        item = upload_queue.get(
            item_id
        )

        if item:

            item.history_id = history_id

        await query.edit_message_text(
            f"⬇️ Download Again added.\n\n"
            f"🔢 Queue #{item_id}\n"
            f"📌 {title}"
        )

        return

    # --------------------------------------------------------
    # DUPLICATE CANCEL
    # --------------------------------------------------------

    if data == "duplicate_cancel":

        await query.edit_message_text(
            "❌ Cancelled."
        )

        return

    # --------------------------------------------------------
    # HISTORY DETAILS
    # --------------------------------------------------------

    if data.startswith(
        "history_"
    ):

        history_id = int(
            data.split("_")[1]
        )

        row = get_history_item(
            history_id,
            user_id,
        )

        if not row:

            await query.edit_message_text(
                "❌ History item not found."
            )

            return

        await query.edit_message_text(
            f"📄 File: {row['file_name']}\n\n"
            f"📁 Type: {row['file_type']}\n"
            f"📦 Size: {format_bytes(row['file_size'])}\n"
            f"📊 Status: {row['status']}\n\n"
            f"🔗 URL:\n{row['url']}\n\n"
            f"🕒 Created: {row['created_at']}\n"
            f"🏁 Completed: {row['completed_at'] or '-'}\n\n"
            f"❗ Error: {row['error'] or '-'}"
        )

        return

    # --------------------------------------------------------
    # CLEAR HISTORY YES
    # --------------------------------------------------------

    if data == "clear_yes":

        deleted = clear_history(
            user_id
        )

        await query.edit_message_text(
            f"🗑️ History cleared.\n\n"
            f"Deleted records: {deleted}"
        )

        return

    # --------------------------------------------------------
    # CLEAR HISTORY NO
    # --------------------------------------------------------

    if data == "clear_no":

        await query.edit_message_text(
            "❌ Cancelled."
        )

        return


# ============================================================
# KOYEB TCP HEALTH CHECK SERVER
# ============================================================
#
# Koyeb is configured as a Web Service and performs a TCP health
# check on port 8000. Telegram polling itself does not open a
# listening port, so this tiny TCP server keeps that health check
# alive without changing the Telegram bot logic.
#
# Koyeb normally provides PORT automatically. We use 8000 as the
# local/default fallback because that is the configured health-check
# port.
# ============================================================

class _HealthCheckHandler(socketserver.BaseRequestHandler):

    def handle(self):
        try:
            self.request.settimeout(2)
            self.request.recv(1024)
        except Exception:
            pass


class _HealthCheckServer(socketserver.ThreadingTCPServer):

    allow_reuse_address = True
    daemon_threads = True


def start_health_check_server():

    port = int(os.getenv("PORT", "8000"))

    server = _HealthCheckServer(
        ("0.0.0.0", port),
        _HealthCheckHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        name="koyeb-health-check",
        daemon=True,
    )

    thread.start()

    print(
        f"🌐 Koyeb health-check server listening on port {port}."
    )

    return server


# ============================================================
# APPLICATION
# ============================================================

application = None


async def post_init(
    app: Application,
):

    await upload_queue.start()

    upload_queue.process_callback = (
        process_upload
    )

    print(
        "🚀 Upload Queue started."
    )


async def post_shutdown(
    app: Application,
):

    await upload_queue.stop()

    print(
        "🛑 Upload Queue stopped."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global application

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN missing in .env"
        )

    init_database()

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=300,
        write_timeout=300,
        pool_timeout=30,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "queue",
            queue_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "history",
            history_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "clearhistory",
            clear_history_command,
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # TXT
    application.add_handler(
        MessageHandler(
            filters.Document.FileExtension(
                "txt"
            ),
            txt_handler,
        )
    )

    # URLs / text
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            url_handler,
        )
    )

    print(
        "🤖 Bot started successfully."
    )

    print(
        "📚 TXT Batch Upload System: READY"
    )

    print(
        "📄 PDF + 🎥 M3U8 → MP4 supported."
    )

    # Start Koyeb TCP health-check listener.
    # This is required because the Koyeb service is configured
    # as a Web Service with a TCP health check on port 8000.
    start_health_check_server()

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()