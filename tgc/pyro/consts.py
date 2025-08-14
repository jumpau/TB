from pathlib import Path

from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaContact, MessageMediaPoll, MessageMediaWebPage, MessageMediaGeo, MessageMediaVenue

MEDIA_TYPE_MAP = {
    MessageMediaPhoto: "photo",
    MessageMediaDocument: "document",
    MessageMediaContact: "contact",
    MessageMediaPoll: "poll",
    MessageMediaWebPage: "web_page",
    MessageMediaGeo: "location",
    MessageMediaVenue: "location",
}

HTML = (Path(__file__).parent.parent / "tg-blog.html").read_text()
