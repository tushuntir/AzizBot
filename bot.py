import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

import downloader as dl  # noqa: E402  (after load_dotenv so COOKIES_FILE is read)
import db  # noqa: E402
from strings import LANG_NAMES, TEXTS  # noqa: E402

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")
if DEFAULT_LANG not in TEXTS:
    DEFAULT_LANG = "en"
PAGE_SIZE = 5
jobs = asyncio.Semaphore(int(os.getenv("MAX_JOBS", "3")))

IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/[\w\-]+[^\s]*", re.I)

router = Router()


class Cache(OrderedDict):
    """Tiny bounded cache so callback_data stays under Telegram's 64-byte limit."""
    def __init__(self, maxsize=1000):
        super().__init__()
        self.maxsize = maxsize

    def put(self, key, value):
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.maxsize:
            self.popitem(last=False)


waiting_round = Cache(5000)  # user ids who sent /round and owe us a video
link_cache = Cache()    # short id -> instagram url
search_cache = Cache()  # short id -> (query, results)


async def lang_of(user) -> str:
    stored = await asyncio.to_thread(db.get_lang, user.id)
    if stored in TEXTS:
        return stored
    code = (user.language_code or "")[:2]
    return code if code in TEXTS else DEFAULT_LANG


def lang_picker() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, name in LANG_NAMES.items():
        kb.button(text=name, callback_data=f"l:{code}")
    kb.adjust(1)
    return kb.as_markup()


def short_id(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:10]


def fmt_dur(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def results_markup(sid: str, page: int) -> InlineKeyboardMarkup:
    query, results = search_cache[sid]
    kb = InlineKeyboardBuilder()
    chunk = results[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    for r in chunk:
        label = f"{r['title'][:45]} · {fmt_dur(r['duration'])}".strip(" ·")
        kb.row(InlineKeyboardButton(text=label, callback_data=f"m:{r['id']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"p:{sid}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(results):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"p:{sid}:{page + 1}"))
    if nav:
        kb.row(*nav)
    return kb.as_markup()


@router.message(CommandStart())
async def start(m: Message):
    if await asyncio.to_thread(db.get_lang, m.from_user.id) is None:
        return await m.answer("🌐 Choose your language / Tilni tanlang / Выберите язык:",
                              reply_markup=lang_picker())
    await m.answer(TEXTS[await lang_of(m.from_user)]["start"])


@router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(TEXTS[await lang_of(m.from_user)]["start"])


@router.message(Command("lang"))
async def lang_cmd(m: Message):
    t = TEXTS[await lang_of(m.from_user)]
    await m.answer(t["choose_lang"], reply_markup=lang_picker())


@router.callback_query(F.data.startswith("l:"))
async def on_lang(cb: CallbackQuery):
    code = cb.data[2:]
    if code not in TEXTS:
        return await cb.answer()
    await asyncio.to_thread(db.set_lang, cb.from_user.id, code)
    await cb.answer()
    await cb.message.edit_text(TEXTS[code]["lang_set"])
    await cb.message.answer(TEXTS[code]["start"])


@router.message(F.text.regexp(IG_RE))
async def on_instagram(m: Message, bot: Bot):
    url = IG_RE.search(m.text).group(0)
    T = TEXTS[await lang_of(m.from_user)]
    status = await m.answer(T["working"])
    tmp = None
    try:
        async with jobs:
            tmp, path, info = await asyncio.to_thread(dl.download_video, url)
        lid = short_id(url)
        link_cache.put(lid, url)
        me = await bot.get_me()
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=T["audio_btn"], callback_data=f"a:{lid}")
        ]])
        await m.answer_video(FSInputFile(path), caption=T["caption"].format(bot=me.username),
                             reply_markup=markup, supports_streaming=True)
    except dl.TooBig:
        await m.answer(T["too_big"])
    except Exception:
        logging.exception("video download failed")
        await m.answer(T["failed"])
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        await status.delete()


@router.callback_query(F.data.startswith("a:"))
async def on_audio(cb: CallbackQuery):
    T = TEXTS[await lang_of(cb.from_user)]
    url = link_cache.get(cb.data[2:])
    if not url:
        return await cb.answer(T["expired"], show_alert=True)
    await cb.answer(T["working"])
    await send_audio(cb.message, url, T)


@router.message(F.text & ~F.text.startswith("/"))
async def on_search(m: Message):
    T = TEXTS[await lang_of(m.from_user)]
    query = m.text.strip()[:100]
    status = await m.answer(T["searching"])
    try:
        results = await asyncio.to_thread(dl.search_music, query)
    except Exception:
        logging.exception("search failed")
        results = []
    if not results:
        return await status.edit_text(T["no_results"])
    sid = short_id(query)
    search_cache.put(sid, (query, results))
    await status.edit_text(T["results"].format(q=query), reply_markup=results_markup(sid, 0))


@router.callback_query(F.data.startswith("p:"))
async def on_page(cb: CallbackQuery):
    _, sid, page = cb.data.split(":")
    if sid not in search_cache:
        T = TEXTS[await lang_of(cb.from_user)]
        return await cb.answer(T["expired"], show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=results_markup(sid, int(page)))
    await cb.answer()


@router.callback_query(F.data.startswith("m:"))
async def on_music(cb: CallbackQuery):
    T = TEXTS[await lang_of(cb.from_user)]
    await cb.answer(T["working"])
    await send_audio(cb.message, f"https://www.youtube.com/watch?v={cb.data[2:]}", T)


async def send_audio(msg: Message, url: str, T: dict):
    status = await msg.answer(T["working"])
    tmp = None
    try:
        async with jobs:
            tmp, path, info = await asyncio.to_thread(dl.download_audio, url)
        await msg.answer_audio(
            FSInputFile(path),
            title=(info.get("track") or info.get("title") or "")[:64],
            performer=(info.get("artist") or info.get("uploader") or "")[:64],
            duration=int(info.get("duration") or 0),
        )
    except dl.TooBig:
        await msg.answer(T["too_big"])
    except Exception:
        logging.exception("audio download failed")
        await msg.answer(T["failed"])
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        await status.delete()


# ---------- /round: turn any video into a Telegram circle video (video note) ----------
ROUND_DL_LIMIT = 20 * 1024 * 1024  # Bot API download limit


async def make_round(src: str, dst: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", src, "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=640:640,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-maxrate", "1200k", "-bufsize", "2400k",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


@router.message(Command("round"))
async def round_cmd(m: Message):
    T = TEXTS[await lang_of(m.from_user)]
    waiting_round.put(m.from_user.id, True)
    await m.answer(T["round_prompt"])


@router.message(Command("cancel"))
async def cancel_cmd(m: Message):
    T = TEXTS[await lang_of(m.from_user)]
    waiting_round.pop(m.from_user.id, None)
    await m.answer(T["round_cancelled"])


@router.message(F.video | F.animation | F.document.mime_type.startswith("video/"))
async def on_round_video(m: Message, bot: Bot):
    T = TEXTS[await lang_of(m.from_user)]
    if m.from_user.id not in waiting_round:
        return await m.answer(T["round_hint"])
    media = m.video or m.animation or m.document
    if (media.file_size or 0) > ROUND_DL_LIMIT:
        return await m.answer(T["round_too_big"])
    waiting_round.pop(m.from_user.id, None)
    status = await m.answer(T["working"])
    tmp = tempfile.mkdtemp(prefix="round_")
    try:
        src, dst = os.path.join(tmp, "in"), os.path.join(tmp, "out.mp4")
        await bot.download(media, destination=src)
        async with jobs:
            ok = await make_round(src, dst)
        if not ok:
            raise RuntimeError("ffmpeg failed")
        if (getattr(media, "duration", 0) or 0) > 60:
            await m.answer(T["round_trimmed"])
        await m.answer_video_note(FSInputFile(dst), length=640)
    except Exception:
        logging.exception("round conversion failed")
        await m.answer(T["round_failed"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await status.delete()


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(os.environ["BOT_TOKEN"])
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Start"),
        BotCommand(command="round", description="Make a circle video"),
        BotCommand(command="lang", description="Language"),
        BotCommand(command="cancel", description="Cancel"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
