
from hypy_utils.dict_utils import deep_dict
from telethon.tl.types import MessageEntityBold, MessageEntityItalic, MessageEntityCode, MessageEntityPre, MessageEntityTextUrl, MessageEntityUrl, MessageEntityMention, MessageEntityHashtag, MessageEntityCashtag, MessageEntityBotCommand, MessageEntityEmail, MessageEntityPhone, MessageEntityUnderline, MessageEntityStrike, MessageEntitySpoiler, Message
from tgc.pyro.consts import MEDIA_TYPE_MAP

def convert_media_dict(msg: Message) -> dict:
    def helper():
        media = getattr(msg, 'media', None)
        if media:
            return dict(vars(media))
        return {}
    d = deep_dict(helper(), {'_client'})
    if d:
        d['media_type'] = type(getattr(msg, 'media', None)).__name__
        # Telethon 没有 has_media_spoiler，需自定义
    # Telethon 没有 venue/location 结构，需自定义
    return d



def entity_start_end(text: str, en: Message) -> tuple[str, str] | None:
    """
    Convert a message entity to a start tag and an end tag for HTML
    """
    # Telethon 的实体类型用 isinstance 判断
    if isinstance(en, MessageEntityBold):
        return ("<b>", "</b>")
    if isinstance(en, MessageEntityItalic):
        return ("<i>", "</i>")
    if isinstance(en, MessageEntityCode):
        return ("<code>", "</code>")
    if isinstance(en, MessageEntityPre):
        return ("<pre>", "</pre>")
    if isinstance(en, MessageEntityTextUrl):
        return (f'<a href="{en.url}">', "</a>")
    if isinstance(en, MessageEntityUrl):
        return ("<a>", "</a>")
    if isinstance(en, MessageEntityMention):
        return ("<span class='mention'>", "</span>")
    if isinstance(en, MessageEntityHashtag):
        return ("<span class='hashtag'>", "</span>")
    if isinstance(en, MessageEntityCashtag):
        return ("<span class='cashtag'>", "</span>")
    if isinstance(en, MessageEntityBotCommand):
        return ("<span class='botcommand'>", "</span>")
    if isinstance(en, MessageEntityEmail):
        return ("<span class='email'>", "</span>")
    if isinstance(en, MessageEntityPhone):
        return ("<span class='phone'>", "</span>")
    if isinstance(en, MessageEntityUnderline):
        return ("<u>", "</u>")
    if isinstance(en, MessageEntityStrike):
        return ("<s>", "</s>")
    if isinstance(en, MessageEntitySpoiler):
        return ("<span class='spoiler'>", "</span>")
    return None


from typing import Any

def convert_text(text: str, entities: list[Any]) -> str:
    """
    Convert text to HTML
    """
    # Pyrogram的surrogates工具可省略，直接处理字符串
    entities_offsets = []
    if entities is None:
        entities = []
    for entity in entities:
        start = entity.offset
        end = start + entity.length
        tags = entity_start_end(text, entity)
        if tags is None:
            continue
        entities_offsets.append((tags[0], start,))
        entities_offsets.append((tags[1], end,))
    entities_offsets = sorted(entities_offsets, key=lambda x: x[1], reverse=True)
    for tag, offset in entities_offsets:
        text = text[:offset] + tag + text[offset:]
    return text
