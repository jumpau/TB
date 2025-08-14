import argparse
import asyncio
from pathlib import Path
from typing import Union
from PIL import Image
from hypy_utils import printc, json_stringify, write
from hypy_utils.dict_utils import remove_keys
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Message, DocumentAttributeSticker

from .config import load_config, Config
from .consts import HTML
from .convert import convert_text, convert_media_dict
from .download_media import download_media, has_media, guess_ext, download_media_urlsafe
from .grouper import group_msgs
from ..convert_export import remove_nones
from ..convert_media_types import tgs_to_apng
from ..rss.posts_to_feed import posts_to_feed, FeedMeta


def effective_text(msg: Message) -> str:
    """
    Get effective text of a message in HTML
    """
    if getattr(msg, 'message', None):
        return convert_text(msg.message, getattr(msg, 'entities', []))
    if getattr(msg, 'text', None):
        return convert_text(msg.text, getattr(msg, 'entities', []))
    # Telethon 没有 caption 属性，图片/视频/文件消息文本也在 message 字段
    if getattr(msg, 'action', None):
        return str(msg.action).split(".")[-1].replace("_", " ").capitalize()


def _download_media_helper(client, args: list) -> Path:
    return asyncio.run(download_media(client, *args))


def get_user_name(user: User) -> str:
    name = user.first_name or ""
    if user.last_name:
        name += " " + user.last_name
    return name

def validate_chat_id(chat_id_str) -> Union[str, int]:
    """
    验证并转换聊天ID
    支持以下格式：
    - 数字ID: -1001234567890
    - 用户名: @channelname 或 channelname
    """
    if isinstance(chat_id_str, int):
        return chat_id_str
    
    chat_id_str = str(chat_id_str).strip()
    
    # 如果是用户名格式
    if chat_id_str.startswith('@'):
        return chat_id_str[1:]  # 移除@符号
    elif not chat_id_str.lstrip('-').isdigit():
        return chat_id_str  # 返回用户名
    
    # 如果是数字ID
    try:
        return int(chat_id_str)
    except ValueError:
        raise ValueError(f"Invalid chat_id format: {chat_id_str}")


async def process_message(msg: Message, path: Path, export: dict, client):
    media_path = path / "media"

    m = {
        "id": msg.id,
        "date": msg.date,
        # Telethon 没有 service 属性，可用 is_service 或 type 判断
        "type": 'service' if getattr(msg, 'is_service', False) else None,
        "text": effective_text(msg),
        "author": getattr(msg, 'post_author', None),
        "views": getattr(msg, 'views', None),
        "forwards": getattr(msg, 'forwards', None),
        "forwarded_from": {
            "name": get_user_name(getattr(msg, 'forward_from', None)) if getattr(msg, 'forward_from', None) else None,
            "url": f'https://t.me/{getattr(msg.forward_from, "username", "")}' if getattr(msg, 'forward_from', None) and getattr(msg.forward_from, 'username', None) else None,
        } if getattr(msg, 'forward_from', None) else {
            "name": getattr(getattr(msg, 'forward_from_chat', None), 'title', None),
        } if getattr(msg, 'forward_from_chat', None) else {
            "name": getattr(msg, 'forward_sender_name', None),
        } if getattr(msg, 'forward_sender_name', None) else None,
        "media_group_id": getattr(msg, 'grouped_id', None),
        "reply_id": getattr(msg, 'reply_to_msg_id', None),
        "file": convert_media_dict(msg)
    }

    # Download file
    f = m.get('file')

    async def dl_media():
        fp, name = await download_media_urlsafe(client, msg, directory=media_path,
                                                max_file_size=int((export.get('size_limit_mb') or 0) * 1000_000))
        if fp is None:
            return
        f['original_name'] = name

        # Convert tgs sticker
        if fp.suffix == '.tgs':
            fp = Path(tgs_to_apng(fp))

        f['url'] = str(fp.absolute().relative_to(path.absolute()))
        f['size'] = f.pop('file_size', None)

        # Download the largest thumbnail
        if f.get('thumbs'):
            thumb: dict = max(f['thumbs'], key=lambda x: x['file_size'])
            # Telethon 没有 FileId.decode，直接用 mime_type 判断扩展名
            ext = guess_ext(client, thumb.get('mime_type', None))
            fp = await download_media(client, thumb['file_id'], directory=media_path,
                                      fname=fp.with_suffix(fp.suffix + f'_thumb{ext}').name)
            f['thumb'] = str(fp.absolute().relative_to(path.absolute()))
            del f['thumbs']

    if has_media(msg):
        await dl_media()

    # Move photo to its own key
    if f:
        mt = f.get('media_type')
        if mt == 'photo' or (not mt and (f.get('mime_type') or "").startswith("image")):
            img = m['image'] = m.pop('file')

            # Read image size
            img['width'], img['height'] = Image.open(path / img['url']).size

    return remove_keys(remove_nones(m), {'file_id', 'file_unique_id'})


async def download_custom_emojis(msgs: list[Message], results: list[dict], path: Path, client):
    print("Downloading custom emojis...")
    # List custom emoji ids
    ids = {e.custom_emoji_id for msg in msgs if msg.text and msg.text.entities for e in msg.text.entities if e.custom_emoji_id}
    ids.update({e.custom_emoji_id for msg in msgs if msg.caption_entities for e in msg.caption_entities if e.custom_emoji_id})
    ids = list(ids)
    orig_ids = list(ids)

    # Query stickers 200 ids at a time
    stickers = []
    while ids:
        stickers += await client.get_custom_emoji_stickers(ids[:200])
        ids = ids[200:]

    # Download stickers
    for id, s in zip(orig_ids, stickers):
        ext = guess_ext(client, getattr(s, 'mime_type', None))
        op = (await download_media(client, s, path / "emoji", f'{id}{ext}')).absolute().relative_to(path.absolute())

        # Replace sticker paths
        for r in results:
            if "text" in r:
                r['text'] = r['text'].replace(f'<i class="custom-emoji" emoji-src="emoji/{id}">',
                                              f'<i class="custom-emoji" emoji-src="{op}">')


async def process_chat(chat_id_input, path: Path, export: dict, client):
    try:
        # 验证并转换聊天ID
        chat_id = validate_chat_id(chat_id_input)
        printc(f"&aTrying to access chat: {chat_id}")
        chat = await client.get_entity(chat_id)
        printc(f"&aChat obtained. Chat name: {getattr(chat, 'title', str(chat))} | Type: {getattr(chat, 'type', type(chat))} | ID: {getattr(chat, 'id', '')}")
    except ValueError as e:
        if "Peer id invalid" in str(e):
            printc(f"&cError: Invalid chat ID format: {chat_id_input}")
            printc(f"&cPlease check your chat_id in the config file.")
            printc(f"&cFor channels, use the channel username (without @) or the correct numeric ID.")
            return
        else:
            raise
    except KeyError as e:
        if "ID not found" in str(e):
            printc(f"&cError: Chat ID {chat_id_input} not found.")
            printc(f"&cPossible reasons:")
            printc(f"&c  1. The bot doesn't have access to this chat")
            printc(f"&c  2. The chat doesn't exist or has been deleted")
            printc(f"&c  3. The chat ID is incorrect")
            printc(f"&cTry adding the bot to the chat first, or check the chat ID.")
            return
        else:
            raise
    except Exception as e:
        printc(f"&cError accessing chat {chat_id_input}: {e}")
        return

    # 持续爬取直到获取到有效消息或达到最大限制
    print("Crawling channel posts...")
    msgs = []
    last_id = 0
    max_total = 200  # 每次最多执行200个有效贴文
    while len(msgs) < max_total:
        batch = await client.get_messages(chat.id, limit=min(100, max_total - len(msgs)), offset_id=last_id)
        batch = [m for m in batch if hasattr(m, 'id') and not getattr(m, 'empty', False)]
        if not batch:
            print("> No more valid messages, we're done.")
            break
        msgs += batch
        last_id = batch[-1].id if batch else last_id
        print(f"> {len(msgs)} total messages... (last batch up to ID #{last_id})")

    # 按 grouped_id 分组
    from collections import defaultdict
    msg_groups = defaultdict(list)
    for m in msgs:
        gid = getattr(m, 'grouped_id', None)
        if gid:
            msg_groups[gid].append(m)
        else:
            msg_groups[m.id].append(m)

    results = []
    for gid, group in msg_groups.items():
        # 组内收集所有媒体和附言
        media_files = []
        caption = None
        for m in group:
            if has_media(m):
                # 下载所有媒体
                fp, name = await download_media_urlsafe(client, m, directory=path/str(gid), max_file_size=int((export.get('size_limit_mb') or 0) * 1000_000))
                if fp:
                    media_files.append(str(fp))
            # 只取有 text 的那条作为附言
            if not caption and (getattr(m, 'message', None) or getattr(m, 'text', None)):
                caption = effective_text(m)
        # 组结果（不保存为txt，只组合到结构）
        results.append({
            'grouped_id': gid,
            'media_files': media_files,
            'caption': caption
        })

    # 兼容原有 emoji 下载和分组
    await download_custom_emojis(msgs, results, path, client)

    write(path / "posts.json", json_stringify(results, indent=2))
    write(path / "index.html", HTML.replace("$$POSTS_DATA$$", json_stringify(results)))

    if 'rss' in export:
        print("Exporting RSS feed...")
        posts_to_feed(path, FeedMeta(**export['rss']))

    printc(f"&aDone! Saved to {path / 'posts.json'}")


async def run_app(client, cfg):
    me: User = await client.get_me()
    printc(f"&aLogin success! ID: {me.id}")
    for export in cfg.exports:
        await process_chat(export["chat_id"], Path(export["path"]), export, client)





def main():
    parser = argparse.ArgumentParser("Telegram Channel Message to Public API Crawler")
    parser.add_argument("config", help="Config path", nargs="?", default="config.toml")
    args = parser.parse_args()

    from tgc.pyro.config import get_telegram_client, load_config
    client = get_telegram_client(args.config)
    cfg = load_config(args.config)
    client.start()
    asyncio.get_event_loop().run_until_complete(run_app(client, cfg))

def run():
    main()

if __name__ == "__main__":
    main()
