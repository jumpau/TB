from pathlib import Path

from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaContact, MessageMediaPoll, MessageMediaWebPage, MessageMediaGeo, MessageMediaVenue, MessageMediaAnimation, MessageMediaVideo, MessageMediaAudio, MessageMediaVoice

MEDIA_TYPE_MAP = {
    MessageMediaPhoto: "photo",
    MessageMediaDocument: "document",
    MessageMediaContact: "contact",
    MessageMediaPoll: "poll",
    MessageMediaWebPage: "web_page",
    MessageMediaGeo: "location",
    MessageMediaVenue: "location",
    MessageMediaAnimation: "animation",
    MessageMediaVideo: "video_file",
    MessageMediaAudio: "audio_file",
    MessageMediaVoice: "voice_message",
}

HTML = (Path(__file__).parent.parent / "tg-blog.html").read_text()
