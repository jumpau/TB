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
    
    # 获取已有贴文的所有ID集合和范围
    existing_ids = set()
    existing_min_id = None
    existing_max_id = None
    
    if posts_path.exists():
        with open(posts_path, "r", encoding="utf-8") as f:
            try:
                old_posts = json.load(f)
                if old_posts:
                    existing_ids = set(int(post.get('id', 0)) for post in old_posts if post.get('id'))
                    if existing_ids:
                        existing_min_id = min(existing_ids)
                        existing_max_id = max(existing_ids)
                        print(f"Found {len(existing_ids)} existing posts, ID range: {existing_min_id} - {existing_max_id}")
                    else:
                        print("Found existing posts.json but no valid IDs")
            except Exception as e:
                print(f"Warning: Could not load existing posts.json: {e}")
                old_posts = []
    else:
        old_posts = []
        print("No existing posts.json found, starting fresh")
    
    # 保存原始的existing_ids，用于最终去重检查
    original_existing_ids = existing_ids.copy()
    
    # 计算起始ID：从最大已有ID开始向上采集新贴文
    start_id = existing_max_id if existing_max_id else 0
    print(f"Starting crawl from ID > {start_id}")

    msgs = []
    last_id = start_id
    max_total = 20  # 每次最多执行20个有效贴文
    no_new_messages_count = 0  # 连续没有新消息的批次计数
    max_empty_batches = 3  # 连续3个批次都没有新消息才停止向上采集
    
    # 第一阶段：向上采集新贴文（ID > start_id）
    print("=== Phase 1: Crawling newer posts (向上采集) ===")
    while len(msgs) < max_total:
        batch = await client.get_messages(chat.id, limit=min(100, max_total - len(msgs)), min_id=last_id)
        batch = [m for m in batch if hasattr(m, 'id') and not getattr(m, 'empty', False)]
        if not batch:
            print("> No more newer messages available.")
            break
            
        # 按ID从小到大排序
        batch = sorted(batch, key=lambda x: x.id)
        
        # 过滤掉已存在的贴文，但不停止采集
        new_batch = []
        skipped_count = 0
        for m in batch:
            if m.id in existing_ids:
                skipped_count += 1
                print(f"> Skipping existing post ID {m.id}")
            else:
                new_batch.append(m)
                existing_ids.add(m.id)  # 添加到已存在集合，避免重复处理
        
        if new_batch:
            msgs.extend(new_batch)
            last_id = new_batch[-1].id
            no_new_messages_count = 0  # 重置计数
            print(f"> Added {len(new_batch)} newer messages (skipped {skipped_count} existing), total: {len(msgs)} (last ID: {last_id})")
        else:
            # 如果这批消息都是已存在的，继续向前推进
            last_id = batch[-1].id if batch else last_id
            no_new_messages_count += 1
            print(f"> No new messages in this batch (skipped {skipped_count} existing), advancing to ID: {last_id} (empty batches: {no_new_messages_count})")
            
            # 只有连续多个批次都没有新消息才停止向上采集
            if no_new_messages_count >= max_empty_batches:
                print(f"> No new messages found in {max_empty_batches} consecutive batches, stopping upward crawl.")
                break
    
    # 第二阶段：如果没有采集满，向下采集历史贴文（ID < start_id）
    if len(msgs) < max_total and (existing_min_id is None or existing_min_id > 1):
        remaining_quota = max_total - len(msgs)
        print(f"=== Phase 2: Crawling older posts (向下采集) - Need {remaining_quota} more ===")
        
        # 从已有的最小ID开始向下采集
        max_id = existing_min_id - 1 if existing_min_id else start_id
        print(f"Starting downward crawl from ID < {max_id + 1}")
        
        downward_no_new_count = 0
        downward_max_empty = 3
        
        while len(msgs) < max_total:
            # 向下采集：使用max_id限制上限
            batch = await client.get_messages(chat.id, limit=min(100, max_total - len(msgs)), max_id=max_id)
            batch = [m for m in batch if hasattr(m, 'id') and not getattr(m, 'empty', False)]
            if not batch:
                print("> No more older messages available.")
                break
                
            # 按ID从大到小排序（向下采集）
            batch = sorted(batch, key=lambda x: x.id, reverse=True)
            
            # 过滤掉已存在的贴文
            new_batch = []
            skipped_count = 0
            for m in batch:
                if m.id in existing_ids:
                    skipped_count += 1
                    print(f"> Skipping existing post ID {m.id}")
                else:
                    new_batch.append(m)
                    existing_ids.add(m.id)
            
            if new_batch:
                msgs.extend(new_batch)
                max_id = min(m.id for m in new_batch) - 1  # 更新max_id为最小ID-1
                downward_no_new_count = 0
                print(f"> Added {len(new_batch)} older messages (skipped {skipped_count} existing), total: {len(msgs)} (next max_id: {max_id + 1})")
            else:
                # 如果这批都是已存在的，继续向下
                max_id = min(m.id for m in batch) - 1 if batch else max_id - 100
                downward_no_new_count += 1
                print(f"> No new messages in downward batch (skipped {skipped_count} existing), next max_id: {max_id + 1} (empty batches: {downward_no_new_count})")
                
                if downward_no_new_count >= downward_max_empty:
                    print(f"> No new messages found in {downward_max_empty} consecutive downward batches, stopping.")
                    break
    
    if not msgs:
        print("No new messages to process.")
        return

    print(f"Successfully collected {len(msgs)} new messages for processing")
    if msgs:
        msg_ids = [m.id for m in msgs]
        print(f"Message ID range: {min(msg_ids)} - {max(msg_ids)}")

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
    print(f"Processing {len(msg_groups)} message groups...")
    
    for gid, group in msg_groups.items():
        # 组内收集所有媒体和附言
        media_files = []
        caption = None
        # 取主消息ID作为文件夹名（最小ID）
        post_id = min(m.id for m in group if hasattr(m, 'id'))
        print(f"Processing group {gid} with post_id {post_id}")
        
        for m in group:
            if has_media(m):
                fp, name = await download_media_urlsafe(client, m, directory=path/str(post_id), max_file_size=int((export.get('size_limit_mb') or 0) * 1000_000))
                if fp:
                    from .config import load_config
                    cfg = load_config()
                    from .download_media import upload_file_with_retry
                    
                    # 在上传之前，先检查是否为视频并生成缩略图
                    video_thumb_info = None
                    ext = fp.suffix.lower()
                    
                    if ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi']:
                        print(f"Pre-generating thumbnail for video before upload: {name}")
                        try:
                            import subprocess
                            from PIL import Image
                            
                            thumb_path = fp.with_suffix('.jpg')
                            
                            # 使用ffmpeg生成缩略图
                            ffmpeg_cmd = [
                                'ffmpeg', '-i', str(fp), '-ss', '00:00:01.000', 
                                '-vframes', '1', '-y', str(thumb_path)
                            ]
                            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
                            
                            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                                print(f"Generated thumbnail before upload: {thumb_path}")
                                
                                # 获取缩略图尺寸
                                try:
                                    img = Image.open(thumb_path)
                                    thumb_width, thumb_height = img.size
                                    print(f"Pre-upload thumbnail size: {thumb_width}x{thumb_height}")
                                    
                                    # 上传缩略图
                                    thumb_upload_result = upload_file_with_retry(str(thumb_path), cfg)
                                    thumb_url = None
                                    if isinstance(thumb_upload_result, dict) and 'url' in thumb_upload_result:
                                        thumb_url = thumb_upload_result['url']
                                    elif isinstance(thumb_upload_result, str):
                                        thumb_url = thumb_upload_result
                                    
                                    if thumb_url:
                                        video_thumb_info = {
                                            'thumb_url': thumb_url,
                                            'thumb_width': thumb_width,
                                            'thumb_height': thumb_height
                                        }
                                        print(f"Pre-uploaded thumbnail: {thumb_url}")
                                    
                                    # 清理本地缩略图文件
                                    try:
                                        thumb_path.unlink()
                                    except:
                                        pass
                                except Exception as e:
                                    print(f"Failed to get thumbnail size: {e}")
                            else:
                                print(f"Failed to generate thumbnail before upload for {name}")
                        except Exception as e:
                            print(f"Error generating thumbnail before upload for {name}: {e}")
                    
                    # 现在进行视频上传
                    upload_result = upload_file_with_retry(str(fp), cfg)
                    
                    # 处理上传返回结果，确保结构正确
                    if isinstance(upload_result, list):
                        # 分片视频，使用预生成的缩略图信息
                        for idx, item in enumerate(upload_result):
                            if isinstance(item, dict) and 'url' in item:
                                # download_media.py 返回的是包含完整参数的 dict
                                info = {
                                    'mime_type': item.get('mime_type', 'video/mp4'),
                                    'date': getattr(m, 'date', None),
                                    'width': video_thumb_info['thumb_width'] if video_thumb_info else item.get('width'),  # 使用预生成的缩略图尺寸
                                    'height': video_thumb_info['thumb_height'] if video_thumb_info else item.get('height'),
                                    'duration': item.get('duration', 0),
                                    'media_type': 'video',
                                    'original_name': item.get('original_name', name),
                                    'url': item['url'],  # 外链
                                    'size': item.get('size'),
                                    'thumb': video_thumb_info['thumb_url'] if video_thumb_info else None  # 使用预生成的缩略图
                                }
                                
                                media_files.append(info)
                    elif isinstance(upload_result, dict) and 'url' in upload_result:
                        # 单文件上传，download_media.py 返回包含完整参数的 dict
                        ext = Path(str(upload_result['url'])).suffix.lower()
                        
                        info = {
                            'date': getattr(m, 'date', None),
                            'original_name': upload_result.get('original_name', name),
                            'url': upload_result['url'],  # 外链
                            'size': upload_result.get('size')
                        }
                        
                        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.ico']:
                            # 图片 - 缩略图直接使用图片本身的URL
                            info.update({
                                'width': upload_result.get('width'),
                                'height': upload_result.get('height'),
                                'media_type': 'photo',
                                'thumb': upload_result['url'],  # 缩略图直接使用图片本身的URL
                                'mime_type': 'image/jpeg'
                            })
                        elif ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi']:
                            # 视频 - 使用预生成的缩略图信息
                            info.update({
                                'mime_type': 'video/mp4',
                                'width': video_thumb_info['thumb_width'] if video_thumb_info else upload_result.get('width'),  # 使用预生成的缩略图尺寸
                                'height': video_thumb_info['thumb_height'] if video_thumb_info else upload_result.get('height'),
                                'duration': upload_result.get('duration', 0),
                                'media_type': 'video',
                                'thumb': video_thumb_info['thumb_url'] if video_thumb_info else None  # 使用预生成的缩略图
                            })
                        elif ext in ['.mp3', '.ogg', '.wav', '.aac', '.flac', '.m4a', '.wma']:
                            # 音频
                            info.update({
                                'mime_type': 'audio/mpeg',
                                'duration': upload_result.get('duration', 0),
                                'media_type': 'audio',
                                'thumb': None
                            })
                        else:
                            # 其他文件
                            info.update({
                                'mime_type': 'application/octet-stream',
                                'media_type': 'file',
                                'thumb': None
                            })
                        
                        media_files.append(info)
                    elif upload_result:
                        # 旧格式兼容：直接是外链字符串
                        ext = Path(str(upload_result)).suffix.lower()
                        info = {
                            'date': getattr(m, 'date', None),
                            'original_name': name,
                            'url': upload_result,  # 外链
                            'size': fp.stat().st_size if fp.exists() else None
                        }
                        
                        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.ico']:
                            # 图片
                            try:
                                from PIL import Image
                                img = Image.open(fp)
                                info['width'], info['height'] = img.size
                            except Exception:
                                pass
                            info.update({
                                'media_type': 'photo',
                                'thumb': upload_result + '_thumb.jpg',
                                'mime_type': 'image/jpeg'
                            })
                        elif ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi']:
                            # 视频
                            try:
                                import subprocess, json as _json
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
                            except Exception:
                                pass
                            info.update({
                                'mime_type': 'video/mp4',
                                'media_type': 'video',
                                'thumb': video_thumb_info['thumb_url'] if video_thumb_info else None,
                                'width': video_thumb_info['thumb_width'] if video_thumb_info else info.get('width'),
                                'height': video_thumb_info['thumb_height'] if video_thumb_info else info.get('height')
                            })
                        elif ext in ['.mp3', '.ogg', '.wav', '.aac', '.flac', '.m4a', '.wma']:
                            # 音频
                            try:
                                import subprocess, json as _json
                                ffprobe_cmd = [
                                    'ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                    '-show_entries', 'stream=duration',
                                    '-of', 'json', str(fp)
                                ]
                                result = subprocess.run(ffprobe_cmd, capture_output=True, text=True)
                                meta = _json.loads(result.stdout)
                                stream = meta.get('streams', [{}])[0]
                                info['duration'] = int(float(stream.get('duration', 0)))
                            except Exception:
                                pass
                            info.update({
                                'mime_type': 'audio/mpeg',
                                'media_type': 'audio',
                                'thumb': None
                            })
                        else:
                            # 其他文件
                            info.update({
                                'mime_type': 'application/octet-stream',
                                'media_type': 'file',
                                'thumb': None
                            })
                        
                        media_files.append(info)
            if not caption and (getattr(m, 'message', None) or getattr(m, 'text', None)):
                caption = effective_text(m)
        # 取该组所有消息的最早日期作为贴文日期
        group_dates = [getattr(m, 'date', None) for m in group if getattr(m, 'date', None)]
        post_date = min(group_dates) if group_dates else None
        # 分离图片和其他文件，匹配参考格式
        images = []
        files = []
        
        for m in media_files:
            if m.get('media_type') == 'photo':
                # 图片格式 - 精简字段，匹配参考格式
                image_info = {
                    'width': m.get('width'),
                    'height': m.get('height'),
                    'date': m.get('date'),
                    'media_type': 'photo',
                    'original_name': m.get('original_name'),
                    'url': m.get('url'),
                    'size': m.get('size'),
                    'thumb': m.get('thumb')
                }
                # 移除None值
                image_info = {k: v for k, v in image_info.items() if v is not None}
                images.append(image_info)
            else:
                # 视频/文件格式 - 匹配参考格式
                file_info = {}
                
                # 基础尺寸信息（如果存在）
                if m.get('width'):
                    file_info['width'] = m['width']
                if m.get('height'):
                    file_info['height'] = m['height']
                if m.get('duration'):
                    file_info['duration'] = m['duration']
                
                # 视频特定字段
                if m.get('media_type') == 'video':
                    file_info['file_name'] = m.get('original_name')  # 使用file_name而不是original_name
                    file_info['mime_type'] = 'video/mp4'
                    file_info['supports_streaming'] = True
                    file_info['media_type'] = 'video_file'  # 匹配参考格式
                else:
                    # 其他文件类型
                    file_info['file_name'] = m.get('original_name')
                    file_info['mime_type'] = m.get('mime_type', 'application/octet-stream')
                
                # 通用字段
                file_info.update({
                    'date': m.get('date'),
                    'original_name': m.get('original_name'),
                    'url': m.get('url'),
                    'size': m.get('size')
                })
                
                # 缩略图（仅当存在时）
                if m.get('thumb'):
                    file_info['thumb'] = m['thumb']
                    
                # 移除None值
                file_info = {k: v for k, v in file_info.items() if v is not None}
                files.append(file_info)

        results.append({
            'id': post_id,
            'media_group_id': gid,
            'date': post_date,
            'text': caption,
            'images': images,  # 图片数组，参数扁平化
            'files': files     # 其他文件数组，参数扁平化
        })

    # 最终去重检查：确保不返回已存在的贴文
    original_count = len(results)
    
    # 显示即将检查的贴文ID范围
    if results:
        result_ids = [int(post.get('id', 0)) for post in results]
        result_min_id = min(result_ids)
        result_max_id = max(result_ids)
        print(f"Checking {original_count} results with ID range: {result_min_id} - {result_max_id}")
        print(f"Against original existing {len(original_existing_ids)} posts with ID range: {existing_min_id} - {existing_max_id}")
    
    # 使用原始的existing_ids集合进行去重
    results_before_dedup = list(results)
    results = []
    
    for post in results_before_dedup:
        post_id = int(post.get('id', 0))
        if post_id not in original_existing_ids:
            results.append(post)
        else:
            print(f"Final check: Removing duplicate post ID {post_id}")
    
    removed_count = original_count - len(results)
    
    if removed_count > 0:
        print(f"Removed {removed_count} duplicate posts during final check")
    
    if not results:
        print("No new posts to add after final deduplication check.")
        if existing_min_id is not None and existing_max_id is not None:
            print(f"All {original_count} processed posts were already in existing range {existing_min_id}-{existing_max_id}")
        else:
            print(f"All {original_count} processed posts were already processed before")
        return
    
    print(f"Final result: {len(results)} new posts to add (from {original_count} processed)")

    # 兼容原有 emoji 下载和分组
    await download_custom_emojis(msgs, results, path, client)

    # 智能插入逻辑：根据ID范围决定插入位置和排序
    from datetime import datetime
    
    # 统一时间格式处理
    def format_date(date_obj):
        """统一时间格式为ISO字符串"""
        if isinstance(date_obj, datetime):
            return date_obj.isoformat()
        elif isinstance(date_obj, str):
            return date_obj
        return None
    
    # 格式化所有结果的时间
    for post in results:
        if post.get('date'):
            post['date'] = format_date(post['date'])
        # 格式化媒体文件的时间
        for img in post.get('images', []):
            if img.get('date'):
                img['date'] = format_date(img['date'])
        for file in post.get('files', []):
            if file.get('date'):
                file['date'] = format_date(file['date'])
    
    # 格式化旧贴文的时间
    for post in old_posts:
        if post.get('date'):
            post['date'] = format_date(post['date'])
        for img in post.get('images', []):
            if img.get('date'):
                img['date'] = format_date(img['date'])
        for file in post.get('files', []):
            if file.get('date'):
                file['date'] = format_date(file['date'])
    
    # 去重：移除已存在的ID
    new_ids = set(int(post.get('id', 0)) for post in results)
    old_posts = [post for post in old_posts if int(post.get('id', 0)) not in new_ids]
    
    if not results:
        merged_posts = old_posts
    elif not old_posts:
        # 如果没有旧贴文，直接按ID从小到大排序
        merged_posts = sorted(results, key=lambda x: int(x.get('id', 0)))
    else:
        # 计算ID范围
        new_min_id = min(int(post.get('id', 0)) for post in results)
        new_max_id = max(int(post.get('id', 0)) for post in results)
        old_min_id = min(int(post.get('id', 0)) for post in old_posts)
        old_max_id = max(int(post.get('id', 0)) for post in old_posts)
        
        print(f"新贴文ID范围: {new_min_id} - {new_max_id}")
        print(f"现有贴文ID范围: {old_min_id} - {old_max_id}")
        
        if new_min_id > old_max_id:
            # 新贴文ID都比现有最大ID大 → 插入到最下面，按从小到大排序
            print("→ 新贴文插入到底部，按ID从小到大排序")
            sorted_new = sorted(results, key=lambda x: int(x.get('id', 0)))
            merged_posts = old_posts + sorted_new
        elif new_max_id < old_min_id:
            # 新贴文ID都比现有最小ID小 → 插入到最上面，按从小到大排序
            print("→ 新贴文插入到顶部，按ID从小到大排序")
            sorted_new = sorted(results, key=lambda x: int(x.get('id', 0)))
            merged_posts = sorted_new + old_posts
        else:
            # 有重叠或混合情况 → 全部合并后按ID从小到大排序
            print("→ ID范围有重叠，全部重新排序（ID从小到大）")
            merged_posts = sorted(results + old_posts, key=lambda x: int(x.get('id', 0)))
    # 保存所有格式的文件，使用统一的智能插入逻辑
    write(posts_path, json_stringify(merged_posts, indent=2))
    # 生成 index.html 但不写入数据（使用空数组）
    write(path / "index.html", HTML.replace("$$POSTS_DATA$$", "[]"))
    
    # 同样的数据和排序逻辑应用到所有输出格式
    if 'rss' in export:
        print("Exporting RSS feed with same post order...")
        # 确保RSS使用相同的贴文顺序
        rss_meta = FeedMeta(**export['rss'])
        posts_to_feed(path, rss_meta, posts_data=merged_posts)
        
        # 自动从RSS配置生成站点地图
        print("Auto-generating XML sitemap from RSS configuration...")
        from tgc.rss.posts_to_feed import posts_to_sitemap_from_rss, generate_robots_txt
        
        posts_to_sitemap_from_rss(path, rss_meta, posts_data=merged_posts)
        
        # 生成robots.txt
        sitemap_url = f"{rss_meta.link.rstrip('/')}/sitemap.xml"
        generate_robots_txt(path, rss_meta.link, sitemap_url)

    printc(f"&aDone! Saved {len(merged_posts)} posts to:")
    printc(f"  - {path / 'posts.json'}")
    printc(f"  - {path / 'index.html'} (without data)")
    if 'rss' in export:
        printc(f"  - {path / 'rss.xml'}")
        printc(f"  - {path / 'atom.xml'}")
        printc(f"  - {path / 'sitemap.xml'}")
        printc(f"  - {path / 'robots.txt'}")


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
