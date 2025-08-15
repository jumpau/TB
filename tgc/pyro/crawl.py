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
    # 验证并转换聊天ID
    chat_id = validate_chat_id(chat_id_input)
    printc(f"&aTrying to access chat: {chat_id}")
    # 读取历史消息、分组
    # ...（省略前置代码，保留主循环体修复）...
    results = []
    # 假设 group_msgs 已分组，遍历每组
    for group in group_msgs:  # group_msgs 应为分组后的消息列表
        media_files = []
        caption = None
        post_id = None
        gid = None
        for m in group:
            if has_media(m):
                fp, name = await download_media_urlsafe(client, m, directory=path/str(post_id), max_file_size=int((export.get('size_limit_mb') or 0) * 1000_000))
                if fp:
                    from .config import load_config
                    cfg = load_config()
                    from .download_media import upload_file_with_retry
                    remote_path = upload_file_with_retry(str(fp), cfg)
                    # 分片上传，remote_path 为 list，每个分片都生成一个 video 类型
                    if isinstance(remote_path, list):
                        for part in remote_path:
                            info = {
                                'type': 'video',
                                'url': part.get('url'),
                                'caption': effective_text(m),
                                'id': getattr(m, 'id', None),
                                'date': getattr(m, 'date', None),
                                'width': part.get('width'),
                                'height': part.get('height'),
                                'duration': part.get('duration'),
                                'mime_type': part.get('mime_type'),
                                'original_name': part.get('original_name'),
                                'size': part.get('size')
                            }
                            # 视频不设置 thumb 字段
                            media_files.append(info)
                    # 普通上传，remote_path 为 tuple
                    elif isinstance(remote_path, tuple):
                        url, file_size = remote_path
                        ext = Path(str(url)).suffix.lower()
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
                            'url': url,
                            'caption': effective_text(m),
                            'id': getattr(m, 'id', None),
                            'date': getattr(m, 'date', None),
                            'size': file_size
                        }
                        if media_type == 'image':
                            try:
                                from PIL import Image
                                img = Image.open(fp)
                                info['width'], info['height'] = img.size
                            except Exception:
                                pass
                            # 图片缩略图直接用本体
                            info['thumb'] = info['url']
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
                                    # 视频不设置 thumb 字段
                                    if 'thumb' in info:
                                        del info['thumb']
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
                        media_files.append(info)
            if not caption and (getattr(m, 'message', None) or getattr(m, 'text', None)):
                caption = effective_text(m)
            if not post_id:
                post_id = getattr(m, 'id', None)
            if not gid:
                gid = getattr(m, 'grouped_id', None)
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
