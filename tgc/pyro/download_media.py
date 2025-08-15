import requests
import time
import os
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

# 上传本地文件到远程，失败重试3次，返回外链并删除本地文件
def upload_file_with_retry(local_path, cfg, upload_folder=None, max_retry=3):
    url = getattr(cfg, 'upload_url', None)
    auth_code = getattr(cfg, 'upload_auth_code', None)
    base_url = getattr(cfg, 'image_base_url', None)
    if not url or not auth_code or not base_url:
        print(f"[上传] 缺少上传配置，跳过 {local_path}")
        return None
    # 自动根据文件类型设置 upload_folder
    ext = Path(local_path).suffix.lower()
    # 只根据文件类型自动设置 uploadFolder
    if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.ico']:
        upload_folder = 'image'
    elif ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi']:
        upload_folder = 'video'
    elif ext in ['.mp3', '.ogg', '.wav', '.aac', '.flac', '.m4a', '.wma']:
        upload_folder = 'audio'
    elif ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.html', '.zip', '.rar', '.7z', '.tar', '.bz2', '.gz']:
        upload_folder = 'doc'
    else:
        upload_folder = 'other'
    file_size = os.path.getsize(local_path)
    chunk_size = 20 * 1024 * 1024  # 20MB
    is_video = ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi']
    if is_video and file_size > chunk_size:
        # 仅视频分片上传，分片前识别参数
        def ffprobe_info(file_path):
            import subprocess, json
            try:
                cmd = [
                    'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_entries', 'stream=width,height,duration,codec_name,codec_type',
                    '-show_entries', 'format=size',
                    '-of', 'json', file_path
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                info = json.loads(result.stdout)
                stream = info.get('streams', [{}])[0]
                fmt = info.get('format', {})
                width = int(stream.get('width', 0))
                height = int(stream.get('height', 0))
                duration = float(stream.get('duration', 0))
                mime_type = 'video/mp4'  # 默认
                if 'codec_name' in stream:
                    codec = stream['codec_name']
                    if codec == 'h264':
                        mime_type = 'video/mp4'
                    elif codec == 'vp9':
                        mime_type = 'video/webm'
                size = int(fmt.get('size', 0))
                return {
                    'width': width,
                    'height': height,
                    'duration': duration,
                    'mime_type': mime_type,
                    'size': size
                }
            except Exception as e:
                print(f'[ffprobe] 获取参数失败: {e}')
                return {'width': 0, 'height': 0, 'duration': 0, 'mime_type': '', 'size': 0}

        part_results = []
        with open(local_path, 'rb') as f:
            part_num = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                part_num += 1
                stem = str(Path(local_path).stem)
                suffix = str(Path(local_path).suffix)
                part_path = f"{stem}.part{part_num}{suffix}"
                with open(part_path, 'wb') as pf:
                    pf.write(chunk)
                print(f"[分片上传] part{part_num}: {part_path}")
                # 分片参数识别
                info = ffprobe_info(part_path)
                part_result = {
                    'original_name': os.path.basename(part_path),
                    'width': info['width'],
                    'height': info['height'],
                    'duration': info['duration'],
                    'mime_type': info['mime_type'],
                    'size': info['size'],
                    'url': None
                }
                for attempt in range(max_retry):
                    try:
                        files = {'file': open(part_path, 'rb')}
                        data = {
                            'authCode': auth_code,
                            'serverCompress': True,
                            'uploadChannel': 'telegram',
                            'autoRetry': True,
                            'uploadNameType': 'origin',
                            'returnFormat': 'default',
                            'uploadFolder': upload_folder,
                        }
                        print(f"  上传参数: {data}")
                        resp = requests.post(url, files=files, params=data, timeout=60)
                        files['file'].close()
                        print(f"  响应状态码: {resp.status_code}")
                        print(f"  响应内容: {resp.text}")
                        if resp.status_code == 200:
                            j = resp.json()
                            if isinstance(j, list) and j and 'src' in j[0]:
                                remote_path = base_url + j[0]['src']
                                print(f"  分片上传成功，外链: {remote_path}")
                                part_result['url'] = remote_path
                                break
                            elif isinstance(j, dict) and 'data' in j and j['data'] and 'src' in j['data'][0]:
                                remote_path = base_url + j['data'][0]['src']
                                print(f"  分片上传成功，外链: {remote_path}")
                                part_result['url'] = remote_path
                                break
                            else:
                                print(f"[分片上传] 响应无 src 字段: {j}")
                        else:
                            print(f"[分片上传] 状态码 {resp.status_code}，内容: {resp.text}")
                    except Exception as e:
                        print(f"[分片上传] 第{attempt+1}次失败: {e}")
                        time.sleep(2)
                os.remove(part_path)
                part_results.append(part_result)
        os.remove(local_path)
        print(f"[分片上传] 所有分片结果: {part_results}")
        return part_results if part_results else None
    else:
        # 其他类型或小视频，按原逻辑上传
        for attempt in range(max_retry):
            try:
                print(f"[上传] 尝试第{attempt+1}次：")
                print(f"  文件路径: {local_path}")
                print(f"  上传接口: {url}")
                print(f"  认证码: {auth_code}")
                print(f"  上传文件夹: {upload_folder}")
                files = {'file': open(local_path, 'rb')}
                data = {
                    'authCode': auth_code,
                    'serverCompress': True,
                    'uploadChannel': 'telegram',
                    'autoRetry': True,
                    'uploadNameType': 'origin',
                    'returnFormat': 'default',
                    'uploadFolder': upload_folder,
                }
                print(f"  上传参数: {data}")
                resp = requests.post(url, files=files, params=data, timeout=30)
                files['file'].close()
                print(f"  响应状态码: {resp.status_code}")
                print(f"  响应内容: {resp.text}")
                if resp.status_code == 200:
                    j = resp.json()
                    if isinstance(j, list) and j and 'src' in j[0]:
                        remote_path = base_url + j[0]['src']
                        print(f"  上传成功，外链: {remote_path}")
                        os.remove(local_path)
                        return remote_path
                    elif isinstance(j, dict) and 'data' in j and j['data'] and 'src' in j['data'][0]:
                        remote_path = base_url + j['data'][0]['src']
                        print(f"  上传成功，外链: {remote_path}")
                        os.remove(local_path)
                        return remote_path
                    else:
                        print(f"[上传] 响应无 src 字段: {j}")
                else:
                    print(f"[上传] 状态码 {resp.status_code}，内容: {resp.text}")
            except Exception as e:
                print(f"[上传] 第{attempt+1}次失败: {e}")
                time.sleep(2)
        print(f"[上传] 文件 {local_path} 上传失败，已重试{max_retry}次")
        return None

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
        'image/bmp': '.bmp',
        'image/tiff': '.tif',
        'image/x-icon': '.ico',
        'video/mp4': '.mp4',
        'video/x-matroska': '.mkv',
        'video/quicktime': '.mov',
        'video/webm': '.webm',
        'video/x-msvideo': '.avi',
        'audio/mpeg': '.mp3',
        'audio/ogg': '.ogg',
        'audio/wav': '.wav',
        'audio/aac': '.aac',
        'audio/flac': '.flac',
        'audio/mp4': '.m4a',
        'audio/x-ms-wma': '.wma',
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
        'application/x-rar-compressed': '.rar',
        'application/x-7z-compressed': '.7z',
        'application/x-tar': '.tar',
        'application/x-bzip2': '.bz2',
        'application/x-gzip': '.gz',
    }
    if mime_type and mime_type in mime_map:
        return mime_map[mime_type]
    # 优先判断 media 类型
    import inspect
    frame = inspect.currentframe()
    outer_frames = inspect.getouterframes(frame)
    media_obj = None
    for f in outer_frames:
        if 'media' in f.frame.f_locals:
            media_obj = f.frame.f_locals['media']
            break
    # Telegram 图片消息
    if media_obj and (media_obj.__class__.__name__ == 'MessageMediaPhoto' or media_obj.__class__.__name__ == 'Photo'):
        return '.jpg'
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
    # 输出未识别 bin 文件的 message 信息，便于后续完善
    import inspect
    frame = inspect.currentframe()
    outer_frames = inspect.getouterframes(frame)
    for f in outer_frames:
        if 'message' in f.frame.f_locals:
            msg = f.frame.f_locals['message']
            print(f"[未识别格式] message.id={getattr(msg, 'id', None)} mime_type={mime_type} file_name={file_name} media={getattr(msg, 'media', None)}")
            break
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
