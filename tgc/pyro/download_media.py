#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.
import asyncio
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Any
from typing import Union
from hypy_utils import ensure_dir, md5
from hypy_utils.file_utils import escape_filename
from telethon.sync import TelegramClient
from telethon.tl.types import Message, DocumentAttributeSticker
from telethon.errors import FloodWaitError


def guess_ext(client: TelegramClient, mime_type: str | None) -> str:
    # Telethon 没有 file_type 枚举，需根据 mime_type 判断
    if mime_type:
        if mime_type.startswith('image/'):
            return '.jpg'
        elif mime_type.startswith('video/'):
            return '.mp4'
        elif mime_type == 'application/x-tgsticker':
            return '.tgs'
        elif mime_type == 'audio/ogg':
            return '.ogg'
        elif mime_type == 'audio/mpeg':
            return '.mp3'
        elif mime_type == 'application/zip':
            return '.zip'
        elif mime_type == 'image/webp':
            return '.webp'
    return '.unknown'


def has_media(message: Message) -> object | None:
    # Telethon Message 直接判断 media 字段
    return getattr(message, 'media', None)


def get_file_name(client: TelegramClient, message: Message) -> str:
    media = has_media(message)
    if not media:
        return None
    mime_type = getattr(media, 'mime_type', None)
    file_name = getattr(media, 'file_name', None)
    if not file_name:
        file_name = f"media_{getattr(message, 'id', '')}"
        file_name += guess_ext(client, mime_type)
    file_name = escape_filename(file_name)
    if '.' not in file_name:
        file_name += guess_ext(client, mime_type)
    return file_name


async def download_media(
        client: TelegramClient,
        message: Message,
        directory: str | Path = "media",
        fname: str | None = None,
        progress: Callable = None,
        progress_args: tuple = (),
        max_file_size: int = 0
) -> Path:
    directory: Path = ensure_dir(directory)
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
    fname: str | None = None,
    progress: Callable = None,
    progress_args: tuple = (),
    max_file_size: int = 0
) -> tuple[Path, str]:
    file_name = get_file_name(client, message)
    renamed = str(getattr(message, 'id', '')) + Path(file_name).suffix
    return await download_media(client, message, directory, renamed, progress, progress_args, max_file_size), file_name
