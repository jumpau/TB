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
    ids = set()
    for msg in msgs:
        if hasattr(msg, 'entities') and msg.entities:
            for e in msg.entities:
                if hasattr(e, 'custom_emoji_id') and e.custom_emoji_id:
                    ids.add(e.custom_emoji_id)
        if hasattr(msg, 'caption_entities') and msg.caption_entities:
            for e in msg.caption_entities:
                if hasattr(e, 'custom_emoji_id') and e.custom_emoji_id:
                    ids.add(e.custom_emoji_id)
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
    import json
    posts_path = path / "posts.json"
    # 获取已有最大ID
    max_existing_id = 0
    if posts_path.exists():
        with open(posts_path, "r", encoding="utf-8") as f:
            try:
                old_posts = json.load(f)
                if old_posts:
                    max_existing_id = max(int(post.get('id', 0)) for post in old_posts if post.get('id'))
            except Exception:
                old_posts = []
    else:
        old_posts = []

    msgs = []
    last_id = max_existing_id
    max_total = 20  # 每次最多执行20个有效贴文
    while len(msgs) < max_total:
        batch = await client.get_messages(chat.id, limit=min(100, max_total - len(msgs)), min_id=last_id)
        batch = [m for m in batch if hasattr(m, 'id') and not getattr(m, 'empty', False)]
        if not batch:
            print("> No more valid messages, we're done.")
            break
        # 按ID从小到大采集
        batch = sorted(batch, key=lambda x: x.id)
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
        # 取主消息ID作为文件夹名（最小ID）
        post_id = min(m.id for m in group if hasattr(m, 'id'))
        for m in group:
            if has_media(m):
                fp, name = await download_media_urlsafe(client, m, directory=path/str(post_id), max_file_size=int((export.get('size_limit_mb') or 0) * 1000_000))
                if fp:
                    from .config import load_config
                    cfg = load_config()
                    from .download_media import upload_file_with_retry
                    remote_path = upload_file_with_retry(str(fp), cfg)
                    # 兼容分片返回，path 字段为外链或分片列表
                    path_val = remote_path if remote_path else str(fp)
                    # 自动转换为 tg-blog 兼容 media 数组
                    if isinstance(path_val, list):
                        # 分片视频每个分片都生成一个 video 类型
                        # 分片完成后立即识别参数，缓存到分片 info
                        part_infos = []
                        import subprocess, json as _json
                        for part_num, url in enumerate(path_val, 1):
                            part_name = f"{Path(fp).stem}.part{part_num}{Path(fp).suffix}"
                            part_path = Path(fp.parent) / part_name
                            info = {
                                'type': 'video',
                                'url': url,
                                'caption': effective_text(m),
                                'id': getattr(m, 'id', None),
                                'date': getattr(m, 'date', None)
                            }
                            # ffprobe 识别参数
                            try:
                                ffprobe_cmd = [
                                    'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                    '-show_entries', 'stream=width,height,duration',
                                    '-of', 'json', str(part_path)
                                ]
                                result = subprocess.run(ffprobe_cmd, capture_output=True, text=True)
                                meta = _json.loads(result.stdout)
                                stream = meta.get('streams', [{}])[0]
                                info['width'] = stream.get('width')
                                info['height'] = stream.get('height')
                                info['duration'] = int(float(stream.get('duration', 0)))
                            except Exception:
                                pass
                            info['mime_type'] = 'video/mp4'
                            info['original_name'] = part_name
                            info['size'] = part_path.stat().st_size if part_path.exists() else None
                            thumb_path = str(part_path).replace('.mp4', '_thumb.jpg')
                            if Path(thumb_path).exists():
                                info['thumb'] = thumb_path
                            part_infos.append(info)
                        # 上传后直接用缓存参数
                        for info in part_infos:
                            media_files.append(info)
                    else:
                        # 判断类型
                        ext = Path(str(path_val)).suffix.lower()
                        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.ico']:
                            media_type = 'image'
                        elif ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi']:
                            media_type = 'video'
                        elif ext in ['.mp3', '.ogg', '.wav', '.aac', '.flac', '.m4a', '.wma']:
                            media_type = 'audio'
                        else:
                            media_type = 'file'
                        info = {
                            'type': media_type,
                            'url': path_val,
                            'caption': effective_text(m),
                            'id': getattr(m, 'id', None),
                            'date': getattr(m, 'date', None)
                        }
                        # 图片 width/height
                        if media_type == 'image':
                            try:
                                from PIL import Image
                                img = Image.open(fp)
                                info['width'], info['height'] = img.size
                            except Exception:
                                pass
                        # 视频/音频/文件 width/height/duration/mime_type/size/original_name/thumb
                        if media_type in ['video', 'audio', 'file']:
                            try:
                                import subprocess, json as _json
                                if media_type == 'video':
                                    ffprobe_cmd = [
                                        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                        '-show_entries', 'stream=width,height,duration',
                                        '-of', 'json', str(fp)
                                    ]
                                    result = subprocess.run(ffprobe_cmd, capture_output=True, text=True)
                                    meta = _json.loads(result.stdout)
                                    stream = meta.get('streams', [{}])[0]
                                    info['width'] = stream.get('width')
                                    info['height'] = stream.get('height')
                                    info['duration'] = int(float(stream.get('duration', 0)))
                                    info['mime_type'] = 'video/mp4'
                                elif media_type == 'audio':
                                    ffprobe_cmd = [
                                        'ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                        '-show_entries', 'stream=duration',
                                        '-of', 'json', str(fp)
                                    ]
                                    result = subprocess.run(ffprobe_cmd, capture_output=True, text=True)
                                    meta = _json.loads(result.stdout)
                                    stream = meta.get('streams', [{}])[0]
                                    info['duration'] = int(float(stream.get('duration', 0)))
                                    info['mime_type'] = 'audio/mpeg'
                                else:
                                    info['mime_type'] = 'application/octet-stream'
                            except Exception:
                                pass
                            info['original_name'] = name
                            info['size'] = fp.stat().st_size if hasattr(fp, 'stat') else None
                            thumb_path = str(fp).replace(ext, '_thumb.jpg')
                            if Path(thumb_path).exists():
                                info['thumb'] = thumb_path
                        media_files.append(info)
            if not caption and (getattr(m, 'message', None) or getattr(m, 'text', None)):
                caption = effective_text(m)
        # 取该组所有消息的最早日期作为贴文日期
        group_dates = [getattr(m, 'date', None) for m in group if getattr(m, 'date', None)]
        post_date = min(group_dates) if group_dates else None
        results.append({
                'id': post_id,
                'media_group_id': gid,
                'date': post_date,
                'text': caption,
                'images': [m for m in media_files if m['type'] == 'image'],
                'files': [m for m in media_files if m['type'] != 'image']
            })

    # 兼容原有 emoji 下载和分组
    await download_custom_emojis(msgs, results, path, client)

    # 追加模式：新数据插入最前面
    new_ids = set(str(post.get('id')) for post in results)
    old_posts = [post for post in old_posts if str(post.get('id')) not in new_ids]
    merged_posts = results + old_posts
    # 按date从大到小排序（最新时间在最上面）
    from datetime import datetime
    def parse_date(post):
        d = post.get('date')
        if isinstance(d, str):
            try:
                return datetime.fromisoformat(d.replace('Z', '+00:00'))
            except Exception:
                return datetime.min
        return d if isinstance(d, datetime) else datetime.min
    merged_posts = sorted(merged_posts, key=lambda post: parse_date(post), reverse=True)
    write(posts_path, json_stringify(merged_posts, indent=2))
    write(path / "index.html", HTML.replace("$$POSTS_DATA$$", json_stringify(merged_posts)))

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
