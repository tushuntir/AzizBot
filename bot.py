import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

import downloader as dl  # noqa: E402  (after load_dotenv so COOKIES_FILE is read)
import db  # noqa: E402
import admin as admin_panel  # noqa: E402
from strings import LANG_NAMES, TEXTS  # noqa: E402

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")
if DEFAULT_LANG not in TEXTS:
    DEFAULT_LANG = "en"
PAGE_SIZE = 10
jobs = asyncio.Semaphore(int(os.getenv("MAX_JOBS", "3")))

IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/[\w\-]+[^\s]*", re.I)
YT_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch[^\s]*|shorts/[\w\-]+|live/[\w\-]+)|youtu\.be/[\w\-]+)",
    re.I,
)

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


async def check_sub(message: Message, bot: Bot) -> bool:
    """Track user + enforce force-sub. Returns True if user may continue."""
    await asyncio.to_thread(db.ensure_user, message.from_user.id)
    if admin_panel.is_admin(message.from_user.id):
        return True
    missing = await admin_panel.missing_channels(bot, message.from_user.id)
    if not missing:
        return True
    lang = await lang_of(message.from_user)
    await message.answer(TEXTS[lang]["sub_required"],
                         reply_markup=admin_panel.join_markup(missing))
    return False


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


def results_text(query: str, results: list, page: int) -> str:
    chunk = results[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    lines = [f"🔍 {query}\n"]
    for i, r in enumerate(chunk):
        lines.append(f"{page * PAGE_SIZE + i + 1}. {r['title']} {fmt_dur(r['duration'])}".rstrip())
    return "\n".join(lines)


def results_markup(sid: str, page: int) -> InlineKeyboardMarkup:
    query, results = search_cache[sid]
    chunk = results[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    kb = InlineKeyboardBuilder()
    row = []
    for i in range(len(chunk)):
        row.append(InlineKeyboardButton(text=str(i + 1), callback_data=f"n:{sid}:{page * PAGE_SIZE + i}"))
        if len(row) == 5:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"p:{sid}:{page - 1}"))
    nav.append(InlineKeyboardButton(text="❌", callback_data="x"))
    if (page + 1) * PAGE_SIZE < len(results):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"p:{sid}:{page + 1}"))
    kb.row(*nav)
    return kb.as_markup()


@router.message(CommandStart())
async def start(m: Message, bot: Bot):
    await asyncio.to_thread(db.ensure_user, m.from_user.id)
    if not admin_panel.is_admin(m.from_user.id):
        missing = await admin_panel.missing_channels(bot, m.from_user.id)
        if missing:
            lang = await lang_of(m.from_user)
            if await asyncio.to_thread(db.get_lang, m.from_user.id) is None:
                await m.answer("🌐 Choose your language / Tilni tanlang / Выберите язык:",
                               reply_markup=lang_picker())
            return await m.answer(TEXTS[lang]["sub_required"],
                                   reply_markup=admin_panel.join_markup(missing))
    if await asyncio.to_thread(db.get_lang, m.from_user.id) is None:
        return await m.answer("🌐 Choose your language / Tilni tanlang / Выберите язык:",
                              reply_markup=lang_picker())
    await m.answer(TEXTS[await lang_of(m.from_user)]["start"])


@router.message(Command("help"))
async def help_cmd(m: Message, bot: Bot):
    if not await check_sub(m, bot):
        return
    await m.answer(TEXTS[await lang_of(m.from_user)]["start"])


@router.message(Command("lang"))
async def lang_cmd(m: Message, bot: Bot):
    await asyncio.to_thread(db.ensure_user, m.from_user.id)
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
async def on_instagram(m: Message, bot: Bot, state: FSMContext):
    if await admin_busy(m, state):
        return
    if not await check_sub(m, bot):
        return
    url = IG_RE.search(m.text).group(0)
    await send_video(m, url, TEXTS[await lang_of(m.from_user)], bot)


@router.message(F.text.regexp(YT_RE))
async def on_youtube(m: Message, bot: Bot, state: FSMContext):
    if await admin_busy(m, state):
        return
    if not await check_sub(m, bot):
        return
    url = YT_RE.search(m.text).group(0)
    await send_video(m, url, TEXTS[await lang_of(m.from_user)], bot)


async def send_video(msg: Message, url: str, T: dict, bot: Bot):
    status = await msg.answer(T["working"])
    tmp = None
    try:
        me = await bot.get_me()
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=T["audio_btn"], callback_data=f"a:{short_id(url)}")
        ]])
        lid = short_id(url)
        link_cache.put(lid, url)
        cached = await asyncio.to_thread(db.get_media, url, "video")
        if cached:
            await msg.answer_video(cached["file_id"],
                                   caption=T["caption"].format(bot=me.username),
                                   reply_markup=markup, supports_streaming=True)
            return
        async with jobs:
            tmp, path, info = await asyncio.to_thread(dl.download_video, url)
        _cc = db.get_cache_channel()
        if _cc:
            sent = await bot.send_video(_cc, FSInputFile(path), supports_streaming=True)
            await asyncio.to_thread(db.put_media, url, "video", sent.video.file_id)
            await msg.answer_video(sent.video.file_id,
                                   caption=T["caption"].format(bot=me.username),
                                   reply_markup=markup, supports_streaming=True)
        else:
            await msg.answer_video(FSInputFile(path), caption=T["caption"].format(bot=me.username),
                                   reply_markup=markup, supports_streaming=True)
    except dl.TooBig:
        await msg.answer(T["too_big"])
    except Exception as e:
        logging.exception("video download failed")
        err = str(e).lower()
        if "sign in to confirm" in err or "not a bot" in err:
            await msg.answer(T.get("bot_check", T["failed"]))
        else:
            await msg.answer(T["failed"])
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        await status.delete()


async def check_sub_cb(cb: CallbackQuery, bot: Bot) -> bool:
    """Force-sub check for callback queries. Alerts + blocks if not joined."""
    if admin_panel.is_admin(cb.from_user.id):
        return True
    missing = await admin_panel.missing_channels(bot, cb.from_user.id)
    if not missing:
        return True
    lang = await lang_of(cb.from_user)
    await cb.answer(TEXTS[lang]["sub_required"], show_alert=True)
    try:
        await cb.message.answer(TEXTS[lang]["sub_required"],
                                reply_markup=admin_panel.join_markup(missing))
    except Exception:
        pass
    return False


async def admin_busy(m: Message, state: FSMContext) -> bool:
    """True if an admin is in the middle of a broadcast/add-channel flow.

    Prevents the normal search/download handlers from also firing on
    the admin's control messages.
    """
    if not admin_panel.is_admin(m.from_user.id):
        return False
    return await state.get_state() is not None


@router.callback_query(F.data.startswith("a:"))
async def on_audio(cb: CallbackQuery, bot: Bot):
    if not await check_sub_cb(cb, bot):
        return
    T = TEXTS[await lang_of(cb.from_user)]
    url = link_cache.get(cb.data[2:])
    if not url:
        return await cb.answer(T["expired"], show_alert=True)
    await cb.answer(T["working"])
    await send_audio(cb.message, url, T)


@router.message(F.text & ~F.text.startswith("/"))
async def on_search(m: Message, bot: Bot, state: FSMContext):
    if await admin_busy(m, state):
        return
    if not await check_sub(m, bot):
        return
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
    await status.edit_text(results_text(query, results, 0), reply_markup=results_markup(sid, 0))


@router.callback_query(F.data.startswith("p:"))
async def on_page(cb: CallbackQuery, bot: Bot):
    if not await check_sub_cb(cb, bot):
        return
    _, sid, page = cb.data.split(":")
    if sid not in search_cache:
        T = TEXTS[await lang_of(cb.from_user)]
        return await cb.answer(T["expired"], show_alert=True)
    query, results = search_cache[sid]
    await cb.message.edit_text(results_text(query, results, int(page)),
                               reply_markup=results_markup(sid, int(page)))
    await cb.answer()


@router.callback_query(F.data.startswith("n:"))
async def on_num(cb: CallbackQuery, bot: Bot):
    if not await check_sub_cb(cb, bot):
        return
    T = TEXTS[await lang_of(cb.from_user)]
    _, sid, idx = cb.data.split(":")
    if sid not in search_cache:
        return await cb.answer(T["expired"], show_alert=True)
    _, results = search_cache[sid]
    idx = int(idx)
    if idx >= len(results):
        return await cb.answer(T["expired"], show_alert=True)
    await cb.answer(T["working"])
    await send_audio(cb.message, f"https://www.youtube.com/watch?v={results[idx]['id']}", T)


@router.callback_query(F.data == "x")
async def on_cancel(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("m:"))
async def on_music(cb: CallbackQuery, bot: Bot):
    if not await check_sub_cb(cb, bot):
        return
    T = TEXTS[await lang_of(cb.from_user)]
    await cb.answer(T["working"])
    await send_audio(cb.message, f"https://www.youtube.com/watch?v={cb.data[2:]}", T)


@router.callback_query(F.data.startswith("v:"))
async def on_music_video(cb: CallbackQuery, bot: Bot):
    if not await check_sub_cb(cb, bot):
        return
    T = TEXTS[await lang_of(cb.from_user)]
    await cb.answer(T["working"])
    await send_video(cb.message, f"https://www.youtube.com/watch?v={cb.data[2:]}", T, bot)


async def send_audio(msg: Message, url: str, T: dict):
    status = await msg.answer(T["working"])
    tmp = None
    try:
        cached = await asyncio.to_thread(db.get_media, url, "audio")
        if cached:
            await msg.answer_audio(
                cached["file_id"],
                title=(cached["title"] or "")[:64],
                performer=(cached["performer"] or "")[:64],
                duration=int(cached["duration"] or 0),
            )
            return
        async with jobs:
            tmp, path, info = await asyncio.to_thread(dl.download_audio, url)
        title = (info.get("track") or info.get("title") or "")[:64]
        performer = (info.get("artist") or info.get("uploader") or "")[:64]
        duration = int(info.get("duration") or 0)
        _cc = db.get_cache_channel()
        if _cc:
            sent = await bot.send_audio(_cc, FSInputFile(path),
                                        title=title, performer=performer, duration=duration)
            await asyncio.to_thread(db.put_media, url, "audio", sent.audio.file_id,
                                    title, performer, duration)
            await msg.answer_audio(sent.audio.file_id, title=title,
                                   performer=performer, duration=duration)
        else:
            await msg.answer_audio(FSInputFile(path), title=title,
                                   performer=performer, duration=duration)
    except dl.TooBig:
        await msg.answer(T["too_big"])
    except Exception as e:
        logging.exception("audio download failed")
        err = str(e).lower()
        if "sign in to confirm" in err or "not a bot" in err:
            await msg.answer(T.get("bot_check", T["failed"]))
        else:
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
async def round_cmd(m: Message, bot: Bot):
    if not await check_sub(m, bot):
        return
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
    if not await check_sub(m, bot):
        return
    T = TEXTS[await lang_of(m.from_user)]
    if m.from_user.id not in waiting_round:
        return await m.answer(T["round_hint"])
    media = m.video or m.animation or m.document
    if (media.file_size or 0) > ROUND_DL_LIMIT:
        return await m.answer(T["round_too_big"])
    cached = await asyncio.to_thread(db.get_media, media.file_unique_id, "video_note")
    if cached:
        waiting_round.pop(m.from_user.id, None)
        await m.answer_video_note(cached["file_id"], length=640)
        return
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
        _cc = db.get_cache_channel()
        if _cc:
            sent = await bot.send_video_note(_cc, FSInputFile(dst), length=640)
            await asyncio.to_thread(db.put_media, media.file_unique_id, "video_note",
                                    sent.video_note.file_id)
            await m.answer_video_note(sent.video_note.file_id, length=640)
        else:
            await m.answer_video_note(FSInputFile(dst), length=640)
    except Exception:
        logging.exception("round conversion failed")
        await m.answer(T["round_failed"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await status.delete()


async def main():
    logging.basicConfig(level=logging.INFO)
    diag = dl.diagnostics()
    logging.info("downloader diagnostics: %s", diag)
    if not diag.get("cookies_file"):
        logging.warning("No cookies found (COOKIES_FILE/COOKIES_B64/cookies.txt) — "
                        "YouTube downloads will hit 'Sign in to confirm you're not a bot'")
    if not admin_panel._parse_admin_ids():
        logging.warning("ADMIN_IDS is empty — /admin will be inaccessible. Set ADMIN_IDS in .env")
    bot = Bot(os.environ["BOT_TOKEN"],
              default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_panel.router)
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
