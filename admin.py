"""Admin panel: /admin -> stats, broadcast to all users, force-sub channels."""
import asyncio
import logging
import os

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db

router = Router()


def _parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", ""))
    ids: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_admin(user_id: int) -> bool:
    return user_id in _parse_admin_ids()


def admin_only(func):
    async def wrapper(event: Message | CallbackQuery, *args, **kwargs):
        if not is_admin(event.from_user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Not for you.", show_alert=True)
            else:
                await event.answer("⛔ This command is for admins only.")
            return
        return await func(event, *args, **kwargs)
    return wrapper


class Broadcast(StatesGroup):
    waiting_message = State()


class AddChannel(StatesGroup):
    waiting_input = State()


# ---------- /admin panel ----------

def panel_markup() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Users", callback_data="adm:stats")
    kb.button(text="📢 Broadcast", callback_data="adm:bcast")
    kb.button(text="📣 Channels", callback_data="adm:channels")
    kb.adjust(2)
    return kb.as_markup()


def back_markup() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Back", callback_data="adm:home")
    return kb.as_markup()


PANEL_TEXT = (
    "🛠 <b>Admin panel</b>\n\n"
    "📊 <b>Users</b> — total user count\n"
    "📢 <b>Broadcast</b> — send a message (ad) to everyone\n"
    "📣 <b>Channels</b> — required channels users must join"
)


@router.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ This command is for admins only.")
    await m.answer(PANEL_TEXT, reply_markup=panel_markup())


@router.callback_query(F.data == "adm:home")
async def adm_home(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    await state.clear()
    await cb.message.edit_text(PANEL_TEXT, reply_markup=panel_markup())
    await cb.answer()


# ---------- stats ----------

@router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    total = await asyncio.to_thread(db.count_users)
    with_lang = await asyncio.to_thread(db.count_users_with_lang)
    await cb.message.edit_text(
        f"📊 <b>Users</b>\n\n👥 Total: <b>{total}</b>\n✅ With language set: <b>{with_lang}</b>",
        reply_markup=back_markup(),
    )
    await cb.answer()


# ---------- broadcast ----------

@router.callback_query(F.data == "adm:bcast")
async def adm_bcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    await state.set_state(Broadcast.waiting_message)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data="adm:home")
    await cb.message.edit_text(
        "📢 Send the message to broadcast (text, photo, video — anything).\n"
        "It will be copied to all users exactly as-is.",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.message(Broadcast.waiting_message)
async def bcast_received(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Send to all", callback_data="adm:bcast_send")
    kb.button(text="❌ Cancel", callback_data="adm:home")
    kb.adjust(1)
    await m.answer(
        "👆 Preview above. Send it to everyone?",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "adm:bcast_send")
async def bcast_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    data = await state.get_data()
    await state.clear()
    src_chat = data.get("chat_id")
    src_msg = data.get("message_id")
    if not src_chat or not src_msg:
        await cb.message.edit_text("⚠️ No message to send. Start again.", reply_markup=back_markup())
        return await cb.answer()
    user_ids = await asyncio.to_thread(db.get_all_user_ids)
    await cb.message.edit_text(f"⏳ Broadcasting to {len(user_ids)} users…")
    sent = failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=src_chat, message_id=src_msg)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # avoid flood limits
    logging.info("broadcast done: sent=%d failed=%d", sent, failed)
    await cb.message.answer(
        f"✅ Broadcast finished.\n📬 Delivered: <b>{sent}</b>\n🚫 Failed/blocked: <b>{failed}</b>",
        reply_markup=back_markup(),
    )
    await cb.answer()


# ---------- required channels ----------

@router.callback_query(F.data == "adm:channels")
async def adm_channels(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    await state.clear()
    await show_channels(cb.message, edit=True)
    await cb.answer()


async def show_channels(msg: Message, edit: bool = False):
    channels = await asyncio.to_thread(db.get_channels)
    kb = InlineKeyboardBuilder()
    lines = ["📣 <b>Required channels</b>\n"]
    if not channels:
        lines.append("None yet. Users can use the bot freely.")
    for ch in channels:
        title = ch["title"] or str(ch["chat_id"])
        lines.append(f"• {title} (<code>{ch['chat_id']}</code>)")
        kb.row(InlineKeyboardButton(text=f"🗑 {title[:30]}", callback_data=f"adm:chdel:{ch['id']}"))
    kb.row(InlineKeyboardButton(text="➕ Add channel", callback_data="adm:chadd"))
    kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data="adm:home"))
    text = "\n".join(lines)
    if edit:
        await msg.edit_text(text, reply_markup=kb.as_markup())
    else:
        await msg.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "adm:chadd")
async def ch_add_prompt(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    await state.set_state(AddChannel.waiting_input)
    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data="adm:channels")
    await cb.message.edit_text(
        "➕ Send the channel:\n"
        "• @username, or\n"
        "• <code>t.me/username</code> invite link, or\n"
        "• numeric ID (<code>-100…</code>)\n\n"
        f"⚠️ First add @{me.username} as an <b>admin</b> in that channel, "
        "otherwise I can't check membership.",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


def _normalize_channel_input(text: str) -> str:
    text = text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return "@" + text.split("/")[0].split("?")[0] if not text.lstrip("-").isdigit() else text


@router.message(AddChannel.waiting_input)
async def ch_add_received(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    target = _normalize_channel_input(m.text or "")
    if not target:
        return await m.answer("⚠️ Send @username or channel ID. Or /admin to cancel.")
    try:
        chat = await bot.get_chat(target)
    except Exception:
        return await m.answer(f"⚠️ Can't find channel {target}. Check the username and try again.")
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            return await m.answer(
                f"⚠️ I'm not an admin in <b>{chat.title}</b>. "
                f"Add @{me.username} as admin there first, then send the channel again."
            )
    except Exception:
        return await m.answer("⚠️ Can't check my rights in that channel. Make me admin first.")
    try:
        invite = await bot.export_chat_invite_link(chat.id)
    except Exception:
        invite = chat.invite_link or ""
    if not invite and chat.username:
        invite = f"https://t.me/{chat.username}"
    if not invite:
        return await m.answer("⚠️ Couldn't get an invite link for that channel.")
    await asyncio.to_thread(db.add_channel, chat.id, chat.title or target, invite)
    await state.clear()
    await m.answer(f"✅ Added required channel: <b>{chat.title}</b>")
    await show_channels(m)


@router.callback_query(F.data.startswith("adm:chdel:"))
async def ch_delete(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔", show_alert=True)
    row_id = int(cb.data.split(":")[-1])
    await asyncio.to_thread(db.remove_channel, row_id)
    await cb.answer("🗑 Removed")
    await show_channels(cb.message, edit=True)


# ---------- force-sub check (used by bot.py) ----------

async def missing_channels(bot: Bot, user_id: int) -> list[dict]:
    channels = await asyncio.to_thread(db.get_channels)
    missing: list[dict] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            # If check fails (e.g. user never interacted), treat as missing
            missing.append(ch)
    return missing


def join_markup(missing: list[dict]) -> object:
    kb = InlineKeyboardBuilder()
    for ch in missing:
        url = ch["invite_link"] or (f"https://t.me/c/{str(ch['chat_id'])[4:]}" if str(ch["chat_id"]).startswith("-100") else None)
        if url:
            kb.row(InlineKeyboardButton(text=f"📣 Join {ch['title'] or 'channel'}", url=url))
    kb.row(InlineKeyboardButton(text="✅ I've joined — check", callback_data="sub:check"))
    return kb.as_markup()


async def gate_text(user_lang: str) -> str:
    from strings import TEXTS
    t = TEXTS.get(user_lang, TEXTS["en"])
    return t.get("sub_required", TEXTS["en"]["sub_required"])


@router.callback_query(F.data == "sub:check")
async def sub_check(cb: CallbackQuery, bot: Bot):
    """'I've joined' button: re-check membership, dismiss gate if OK."""
    from bot import lang_of  # lazy import to avoid cycle
    missing = await missing_channels(bot, cb.from_user.id)
    if missing:
        await cb.answer("⚠️ You haven't joined all channels yet.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=join_markup(missing))
        except Exception:
            pass
        return
    try:
        await cb.message.delete()
    except Exception:
        pass
    from strings import TEXTS
    lang = await lang_of(cb.from_user)
    await cb.message.answer(TEXTS[lang]["sub_ok"])
    # re-show start text so user can continue
    await cb.message.answer(TEXTS[lang]["start"])
    await cb.answer()
