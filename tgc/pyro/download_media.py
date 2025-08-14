import asyncio
import shutil
from tempfile import TemporaryDirectory
from telethon.sync import TelegramClient
from telethon.tl.types import Message
from pathlib import Path
from typing import Optional, Dict
from hypy_utils import ensure_dir, md5
from hypy_utils.file_utils import escape_filename
from telethon.errors import FloodWaitError

def get_file_name(client: TelegramClient, message: Message) -> str:
    media = has_media(message)
    if not media:
        return None
    # 优先从 attributes 获取真实文件名
    file_name = None
    mime_type = getattr(media, 'mime_type', None)
    if hasattr(media, 'document') and hasattr(media.document, 'attributes'):
        for attr in media.document.attributes:
            if hasattr(attr, 'file_name'):
                file_name = attr.file_name
                break
        if not mime_type:
            mime_type = getattr(media.document, 'mime_type', None)
    # 兜底 file_name
    if not file_name:
        file_name = getattr(media, 'file_name', None)
    ext = guess_ext(client, mime_type, file_name)
    if file_name:
        file_name = escape_filename(Path(file_name).stem + ext)
    else:
        file_name = escape_filename(f"media_{getattr(message, 'id', '')}{ext}")
    return file_name

def guess_ext(client: TelegramClient, mime_type: Optional[str], file_name: Optional[str] = None) -> str:
    # 优先用文件名后缀
    if file_name:
        ext = Path(file_name).suffix
        if ext and len(ext) <= 8:
            return ext
    # 常见 mime_type 映射表
    mime_map = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'video/mp4': '.mp4',
        'video/x-matroska': '.mkv',
        'audio/mpeg': '.mp3',
        'audio/ogg': '.ogg',
        'audio/wav': '.wav',
        'application/pdf': '.pdf',
        'application/zip': '.zip',
        'application/x-tgsticker': '.tgs',
        'application/msword': '.doc',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.ms-excel': '.xls',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
        'application/vnd.ms-powerpoint': '.ppt',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
        'text/plain': '.txt',
        'text/html': '.html',
    }
    if mime_type and mime_type in mime_map:
        return mime_map[mime_type]
    if mime_type:
        if mime_type.startswith('image/'):
            return '.jpg'
        elif mime_type.startswith('video/'):
            return '.mp4'
        elif mime_type.startswith('audio/'):
            return '.mp3'
    # 兜底：如果 file_name 有点后缀但太长（如 .bin），只取最后 5 个字符
    if file_name and '.' in file_name:
        ext = Path(file_name).suffix
        if ext and len(ext) > 8:
            return ext[-5:]
    return '.bin'

def has_media(message: Message) -> Optional[object]:
    # Telethon Message 直接判断 media 字段
    return getattr(message, 'media', None)

async def download_media(
    client: TelegramClient,
    message: Message,
    directory: str | Path = "media",
    fname: Optional[str] = None,
    progress: Optional[callable] = None,
    progress_args: tuple = (),
    max_file_size: int = 0
) -> Optional[Path]:
    directory = ensure_dir(directory)
    media = has_media(message)
    if not media:
        return None
    fsize = getattr(media, 'size', 0)
    if max_file_size and fsize > max_file_size:
        print(f"Skipped {fname} because of file size limit ({fsize} > {max_file_size})")
        return None
    file_name = fname or get_file_name(client, message)
    p = directory / file_name
    if p.exists():
        return p
    print(f"Downloading {p.name}...")
    try:
        # 下载前适当延迟，避免被 Telegram 限速或封号
        import random
        await asyncio.sleep(random.uniform(0.5, 2.0))
        await client.download_media(message, file=p)
        return p
    except FloodWaitError as e:
        print(f"Sleeping for {e.seconds} seconds...")
        await asyncio.sleep(e.seconds)
        return await download_media(client, message, directory, fname, progress, progress_args, max_file_size)

async def download_media_urlsafe(
    client: TelegramClient,
    message: Message,
    directory: str | Path = "media",
    fname: Optional[str] = None,
    progress: Optional[callable] = None,
    progress_args: tuple = (),
    max_file_size: int = 0
) -> tuple:
    file_name = get_file_name(client, message)
    renamed = str(getattr(message, 'id', '')) + Path(file_name).suffix
    return await download_media(client, message, directory, renamed, progress, progress_args, max_file_size), file_name
