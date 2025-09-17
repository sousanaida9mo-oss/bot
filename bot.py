# -*- coding: utf-8 -*-
# Bot version: v212 (inline monospace formatting like on screenshots)
# aiogram v3 only

import asyncio
import random
from io import BytesIO
from typing import List, Dict, Any, Optional, Tuple, Iterable
import imaplib
import re
import unicodedata
import math
import ssl
import time

import pandas as pd
from aiogram import Bot, Dispatcher, types, F
from aiogram import types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, File
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

from email.header import decode_header, make_header
from email import message_from_bytes
from email.utils import parseaddr
from aiogram.utils.markdown import code
from html_templates import router as html_templates_router, html_menu_kb, get_last_html


from db import (
    SessionLocal, User, Account, Preset, SmartPreset, Subject, Proxy, IncomingMessage,
    get_or_create_user, approve_user,
    list_domains, set_domains_order, add_domain, delete_domains_by_indices, clear_domains,
    add_account, update_account, delete_account, clear_accounts,
    get_setting, set_setting,
)

import config
import smtp25
import socks

def gen_numeric_html_filename() -> str:
    return f"{int(time.time())}s.html"

def _make_html_file(html: str, filename: Optional[str] = None) -> types.BufferedInputFile:
    name = filename or gen_numeric_html_filename()
    return types.BufferedInputFile((html or "").encode("utf-8"), filename=name)

VERSION = "v212"

# ====== Constants ======
READ_INTERVAL = 3  # seconds
IMAP_TIMEOUT = 20
IMAP_MAX_PARALLEL = 5  # ограничиваем одновременные IMAP-подключения
IMAP_PORT_SSL = 993
IMAP_HOST_MAP = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "gmx.de": "imap.gmx.net",
    "gmx.net": "imap.gmx.net",
    "gmx.at": "imap.gmx.net",
    "web.de": "imap.web.de",
    "yahoo.com": "imap.mail.yahoo.com",
    "yahoo.co.uk": "imap.mail.yahoo.com",
    "yandex.ru": "imap.yandex.com",
    "yandex.com": "imap.yandex.com",
    "mail.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "office365.com": "outlook.office365.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "aol.com": "imap.aol.com",
}

# ====== Access control ======
ADMIN_IDS: List[int] = []
try:
    if hasattr(config, "ADMIN_IDS") and isinstance(config.ADMIN_IDS, (list, tuple)):
        ADMIN_IDS = [int(x) for x in config.ADMIN_IDS]
    elif hasattr(config, "ADMIN_TELEGRAM_ID"):
        ADMIN_IDS = [int(config.ADMIN_TELEGRAM_ID)]
except Exception:
    ADMIN_IDS = []

def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS

async def ensure_approved(obj: types.Message | types.CallbackQuery) -> bool:
    if isinstance(obj, types.CallbackQuery):
        user = obj.from_user
        msg = obj.message
    else:
        user = obj.from_user
        msg = obj
    with SessionLocal() as s:
        u = get_or_create_user(s, user.id, user.username, user.first_name, user.last_name)
        if u.status != "approved":
            await msg.answer("Ваша заявка на доступ отправлена администратору. Ожидайте одобрения.")
            return False
    return True

# ====== FSM ======
class AddAccountFSM(StatesGroup):
    display_name = State()
    loginpass = State()

class ReplyFSM(StatesGroup):
    compose = State()
    html = State()

class EditAccountFSM(StatesGroup):
    account_id = State()
    display_name = State()
    loginpass = State()

class EmailDeleteFSM(StatesGroup):
    account_id = State()

class EmailsClearFSM(StatesGroup):
    confirm = State()

class PresetAddFSM(StatesGroup):
    title = State()
    body = State()

class PresetEditFSM(StatesGroup):
    preset_id = State()
    title = State()
    body = State()

class PresetDeleteFSM(StatesGroup):
    preset_id = State()

class PresetClearFSM(StatesGroup):
    confirm = State()

class SmartPresetAddFSM(StatesGroup):
    body = State()

class SmartPresetEditFSM(StatesGroup):
    preset_id = State()
    body = State()

class SmartPresetDeleteFSM(StatesGroup):
    preset_id = State()

class SmartPresetClearFSM(StatesGroup):
    confirm = State()

class SubjectAddFSM(StatesGroup):
    title = State()

class SubjectEditFSM(StatesGroup):
    subject_id = State()
    title = State()

class SubjectDeleteFSM(StatesGroup):
    subject_id = State()

class SubjectClearFSM(StatesGroup):
    confirm = State()

class CheckNicksFSM(StatesGroup):
    file = State()

class QuickAddFSM(StatesGroup):
    mode = State()
    name = State()
    lines = State()

class DomainsFSM(StatesGroup):
    add = State()
    reorder = State()
    delete = State()
    clear = State()

class IntervalFSM(StatesGroup):
    set = State()

class ProxiesFSM(StatesGroup):
    add = State()
    edit_pick = State()
    edit_value = State()
    delete = State()
    clear = State()

class SingleSendFSM(StatesGroup):
    to = State()
    body = State()

# +++ Admin FSM +++
class AdminFSM(StatesGroup):
    add_id = State()
    deny_id = State()

# ====== Runtime ======
tg_session = AiohttpSession(timeout=30)
bot = Bot(
    token=config.TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=tg_session,
)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(html_templates_router)    # меню генерации HTML‑шаблонов


LAST_XLSX_PER_CHAT: Dict[int, bytes] = {}
BASES_PER_CHAT: Dict[int, List[str]] = {}
VERIFIED_ROWS_PER_CHAT: Dict[int, List[Dict[str, Any]]] = {}

IMAP_TASKS: Dict[int, asyncio.Task] = {}
IMAP_STATUS: Dict[int, Dict[str, Any]] = {}
SEND_TASKS: Dict[int, asyncio.Task] = {}
SEND_STATUS: Dict[int, Dict[str, Any]] = {}
START_LOG_SENT: Dict[Tuple[int, str], bool] = {}
ERROR_LOG_SENT: Dict[Tuple[int, str], bool] = {}
QUICK_ADD_FIRST_PASS: dict[tuple[int, int], bool] = {}

def mark_quick_add_first_pass(user_id: int, account_id: int) -> None:
    """
    Пометить аккаунт как "быстро добавленный", чтобы при первом запуске
    мы тихо пометили все текущие UNSEEN как прочитанные и ничего не публиковали.
    Флаг одноразовый: снимается при первом чтении.
    """
    QUICK_ADD_FIRST_PASS[(user_id, account_id)] = True

# ====== Helpers ======
def reply_main_kb(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📖 Проверка ников"), KeyboardButton(text="🧾 HTML-шаблоны")],
        [KeyboardButton(text="Настройки⚙️")],
        [KeyboardButton(text="✉️ Отправить email"), KeyboardButton(text="➕ Быстрое добавление")],
    ]
    if admin:
        rows.append([KeyboardButton(text="👑 Админка")])
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=rows
    )

def tg(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def code(txt: str) -> str:
    return f"<code>{tg(txt)}</code>"

def join_batches(lines: Iterable[str], batch_size: int = 50) -> List[str]:
    res: List[str] = []
    buf: List[str] = []
    for ln in lines:
        buf.append(ln)
        if len(buf) >= batch_size:
            res.append("\n".join(buf)); buf = []
    if buf:
        res.append("\n".join(buf))
    return res

def nav_row(back_cb: str) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb),
             InlineKeyboardButton(text="♻️ Скрыть", callback_data="ui:hide")]]

async def delete_message_safe(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass

def _normalize_nick_local(nick: str) -> str:
    try:
        return smtp25.normalize_nick(nick)
    except Exception:
        normalized = unicodedata.normalize('NFKD', str(nick))
        ascii_nick = normalized.encode('ascii', 'ignore').decode('ascii')
        return ascii_nick.lower()

def _get_by_ordinal(items, ordinal: int):
    if not isinstance(ordinal, int):
        return None
    if ordinal < 1 or ordinal > len(items):
        return None
    return items[ordinal - 1]

_BLACKLIST_INIT_DONE = False
def _ensure_blacklist_loaded_once():
    global _BLACKLIST_INIT_DONE
    if _BLACKLIST_INIT_DONE and getattr(smtp25, "BLACKLIST_CACHE", None):
        return
    try:
        cache = smtp25.load_blacklist()
        if isinstance(cache, set):
            smtp25.BLACKLIST_CACHE = cache
        elif getattr(smtp25, "BLACKLIST_CACHE", None) is None:
            smtp25.BLACKLIST_CACHE = set()
        print(f"[v{VERSION}] Blacklist loaded: {len(getattr(smtp25, 'BLACKLIST_CACHE', set()))} entries")
    except Exception:
        if getattr(smtp25, "BLACKLIST_CACHE", None) is None:
            smtp25.BLACKLIST_CACHE = set()
    _BLACKLIST_INIT_DONE = True

def prepare_smtp25_from_db(user_id: int) -> List[str]:
    with SessionLocal() as s:
        domains = list_domains(s, user_id)
        smtp25.SEND_PROXY_LIST = [
            {"id": p.id, "host": p.host, "port": p.port, "user": p.user_login, "password": p.password}
            for p in s.query(Proxy).filter_by(user_id=user_id, type="send").all()
        ]
        smtp25.VERIFY_PROXY_LIST = [
            {"id": p.id, "host": p.host, "port": p.port, "user": p.user_login, "password": p.password}
            for p in s.query(Proxy).filter_by(user_id=user_id, type="verify").all()
        ]
        smtp25.EMAIL_ACCOUNTS = [
            {"id": a.id, "name": a.display_name, "email": a.email, "password": a.password}
            for a in s.query(Account).filter_by(user_id=user_id).all()
        ]
        smtp25.SUBJECTS = [x.title for x in s.query(Subject).filter_by(user_id=user_id).all()] or ["Ist OFFER noch verfügbar?"]
        smtp25.TEMPLATES = [x.body for x in s.query(SmartPreset).filter_by(user_id=user_id).all()] or ["Hi SELLER, ist OFFER noch verfügbar?"]
    _ensure_blacklist_loaded_once()
    return domains

async def safe_edit_message(msg: types.Message, text: str, reply_markup: InlineKeyboardMarkup | None = None, parse_mode=ParseMode.HTML):
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            try: await msg.edit_reply_markup(reply_markup=reply_markup)
            except TelegramBadRequest: pass
        else:
            raise

async def safe_cq_answer(cq: types.CallbackQuery, text: str | None = None, show_alert: bool = False, cache_time: int | None = None):
    try:
        await cq.answer(text=text, show_alert=show_alert, cache_time=cache_time)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "query is too old" in msg or "query id is invalid" in msg or "response timeout expired" in msg:
            return
        raise

def pager_row(cb_prefix: str, page: int, total_pages: int) -> list[list[InlineKeyboardButton]]:
    left_page = max(1, page - 1)
    right_page = min(total_pages, page + 1)
    return [[
        InlineKeyboardButton(text="◀️", callback_data=f"{cb_prefix}{left_page}"),
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"{cb_prefix}{right_page}")
    ]]

# ====== START / ADMIN ======
@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await delete_message_safe(m)
    with SessionLocal() as s:
        u = get_or_create_user(s, m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
        if u.status == "pending":
            for admin_id in ADMIN_IDS:
                try:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"admin:approve:{u.id}"),
                         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin:deny:{u.id}")]
                    ])
                    await bot.send_message(admin_id,
                        f"Новая заявка на доступ:\n@{u.username} ({u.first_name} {u.last_name})\nuser_id={u.id}",
                        reply_markup=kb)
                except Exception:
                    pass
            await bot.send_message(m.chat.id, "Заявка на доступ отправлена администратору. Ожидайте одобрения.")
            return
        elif u.status == "denied":
            await bot.send_message(m.chat.id, "Доступ отклонён администратором.")
            return
    await bot.send_message(m.chat.id, "Готово. Выберите действие кнопками снизу.", reply_markup=reply_main_kb(admin=is_admin(m.from_user.id)))

@dp.callback_query(F.data.startswith("admin:"))
async def admin_approve(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Недостаточно прав.", show_alert=True); return
    _, action, uid = c.data.split(":")
    user_id = int(uid)
    with SessionLocal() as s:
        approve_user(s, user_id, approved=(action == "approve"))
        u = s.query(User).filter_by(id=user_id).first()
    try:
        if u and u.tg_id:
            text = "Доступ одобрен. Добро пожаловать!" if action == "approve" else "К сожалению, доступ отклонён."
            await bot.send_message(u.tg_id, text)
    except Exception:
        pass
    await c.answer("Готово.")
    await delete_message_safe(c.message)

# ====== ADMIN UI (отдельная кнопка только для админа) ======
def admin_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Одобрить по Telegram ID", callback_data="adminui:add")],
        [InlineKeyboardButton(text="🚫 Удалить доступ по Telegram ID", callback_data="adminui:deny")],
        [InlineKeyboardButton(text="📋 Список одобренных", callback_data="adminui:list:1")],
        [InlineKeyboardButton(text="♻️ Скрыть", callback_data="ui:hide")]
    ])

def admin_list_text_and_kb(page: int = 1, per_page: int = 15) -> Tuple[str, InlineKeyboardMarkup]:
    with SessionLocal() as s:
        items = s.query(User).filter_by(status="approved").order_by(User.id.asc()).all()
    total = len(items)
    if total == 0:
        return "Одобренных пользователей пока нет.", admin_root_kb()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = min(total, start + per_page)
    slice_items = items[start:end]
    lines: List[str] = ["Одобренные пользователи:", ""]
    for i, u in enumerate(slice_items, start=start + 1):
        uname = f"@{u.username}" if u.username else ""
        fname = (u.first_name or "")
        lname = (u.last_name or "")
        name = (fname + " " + lname).strip()
        info = " ".join(x for x in [uname, name] if x).strip()
        lines.append(f"№{i}: {code(str(u.tg_id or '—'))}" + (f" {info}" if info else ""))
    rows = pager_row("adminui:list:", page, total_pages)
    rows += admin_root_kb().inline_keyboard
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(F.text == "👑 Админка")
async def admin_menu_msg(m: types.Message):
    if not is_admin(m.from_user.id):
        await m.answer("Недостаточно прав."); return
    await delete_message_safe(m)
    await bot.send_message(m.chat.id, "Админка:", reply_markup=admin_root_kb())

@dp.message(Command("admin"))
async def admin_menu_cmd(m: types.Message):
    await admin_menu_msg(m)

@dp.callback_query(F.data == "adminui:add")
async def admin_add_open(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Недостаточно прав.", show_alert=True); return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите Telegram ID пользователя, которого нужно одобрить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("ui:hide")))
    await state.set_state(AdminFSM.add_id); await safe_cq_answer(c)

@dp.message(AdminFSM.add_id)
async def admin_add_id_input(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await m.answer("Недостаточно прав."); return
    await delete_message_safe(m)
    text = (m.text or "").strip()
    if not text.isdigit():
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Нужен числовой Telegram ID.", reply_markup=admin_root_kb()); return
    tg_id = int(text)
    with SessionLocal() as s:
        u = s.query(User).filter_by(tg_id=tg_id).first()
        if not u:
            u = get_or_create_user(s, tg_id, None, None, None)
        approve_user(s, u.id, approved=True)
        u = s.query(User).filter_by(id=u.id).first()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, f"Одобрено. Пользователь {code(str(tg_id))}.", reply_markup=admin_root_kb())
    try: await bot.send_message(tg_id, "Доступ одобрен. Добро пожаловать!")
    except Exception: pass
    await state.clear()

@dp.callback_query(F.data == "adminui:deny")
async def admin_deny_open(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Недостаточно прав.", show_alert=True); return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите Telegram ID пользователя, у которого нужно удалить доступ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("ui:hide")))
    await state.set_state(AdminFSM.deny_id); await safe_cq_answer(c)

@dp.message(AdminFSM.deny_id)
async def admin_deny_id_input(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await m.answer("Недостаточно прав."); return
    await delete_message_safe(m)
    text = (m.text or "").strip()
    if not text.isdigit():
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Нужен числовой Telegram ID.", reply_markup=admin_root_kb()); return
    tg_id = int(text)
    with SessionLocal() as s:
        u = s.query(User).filter_by(tg_id=tg_id).first()
        if not u:
            u = get_or_create_user(s, tg_id, None, None, None)
        approve_user(s, u.id, approved=False)
        u = s.query(User).filter_by(id=u.id).first()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, f"Доступ удалён. Пользователь {code(str(tg_id))}.", reply_markup=admin_root_kb())
    try: await bot.send_message(tg_id, "Доступ отклонён администратором.")
    except Exception: pass
    await state.clear()

@dp.callback_query(F.data.startswith("adminui:list:"))
async def admin_list_show(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Недостаточно прав.", show_alert=True); return
    parts = c.data.split(":")
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    text, kb = admin_list_text_and_kb(page=page, per_page=15)
    await safe_edit_message(c.message, text, reply_markup=kb); await safe_cq_answer(c)

# ====== UI generic ======
@dp.callback_query(F.data == "ui:hide")
async def ui_hide(c: types.CallbackQuery, state: FSMContext):
    # Удаляем текущее сообщение и, если это была подсказка из FSM, чистим трекинг
    await delete_message_safe(c.message)
    try:
        data = await state.get_data()
        ui_msgs = data.get("_ui_msgs", [])
        # если это сообщение было среди трекаемых — забудем его
        ui_msgs = [(ch, mid) for (ch, mid) in ui_msgs if mid != c.message.message_id]
        await state.update_data(_ui_msgs=ui_msgs)
    except Exception:
        pass
    await safe_cq_answer(c)
    
# ====== helpers: трекинг и удаление подсказок внутри FSM ======
async def _ui_msgs_add(state: FSMContext, chat_id: int, message_id: int):
    data = await state.get_data()
    lst = list(data.get("_ui_msgs", []))
    lst.append((chat_id, message_id))
    await state.update_data(_ui_msgs=lst)

async def ui_prompt(state: FSMContext, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    await _ui_msgs_add(state, chat_id, msg.message_id)
    return msg

async def ui_clear_prompts(state: FSMContext):
    data = await state.get_data()
    lst = list(data.get("_ui_msgs", []))
    if lst:
        for chat_id, mid in lst:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
        await state.update_data(_ui_msgs=[])

@dp.callback_query(F.data == "settings:back")
async def settings_back(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await c.message.edit_text("Настройки:", reply_markup=settings_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data == "noop")
async def noop_cb(c: types.CallbackQuery):
    await safe_cq_answer(c)

@dp.message(F.text == "Настройки⚙️")
async def btn_settings(m: types.Message):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await bot.send_message(m.chat.id, "Настройки:", reply_markup=settings_kb())

@dp.message(Command("settings"))
async def cmd_settings(m: types.Message):
    await btn_settings(m)

# ====== Settings root ======
def settings_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📑 Домены", callback_data="domains:open"),
         InlineKeyboardButton(text="📚 Пресеты (IMAP)", callback_data="presets:open")],
        [InlineKeyboardButton(text="📌 Темы", callback_data="subjects:open"),
         InlineKeyboardButton(text="📗 Умные пресеты", callback_data="smart:open")],
        [InlineKeyboardButton(text="📧 E‑mail", callback_data="emails:open"),
         InlineKeyboardButton(text="🌐 Прокси", callback_data="proxies:root")],
        [InlineKeyboardButton(text="⏱ Интервал", callback_data="interval:open")],
        [InlineKeyboardButton(text="♻️ Скрыть", callback_data="ui:hide")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ====== Domains ======
def domains_text_for_user(user_id: int) -> str:
    with SessionLocal() as s:
        doms = list_domains(s, user_id)
    if not doms:
        return code("Текущие домены: список пуст.")
    lines = ["Текущие домены (по приоритету):", ""]
    for i, d in enumerate(doms, start=1):
        lines.append(f"Домен №{i}: {code(d)}")
    return "\n".join(lines)

def domains_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="domains:add"),
         InlineKeyboardButton(text="🔁 Изменить порядок", callback_data="domains:reorder")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="domains:delete"),
         InlineKeyboardButton(text="🧹 Удалить все", callback_data="domains:clear")],
        *nav_row("settings:back")
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "domains:open")
async def domains_open(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_edit_message(c.message, domains_text_for_user(c.from_user.id), reply_markup=domains_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data == "domains:add")
async def domains_add(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    txt = domains_text_for_user(c.from_user.id) + "\n\nВведите домен. Можно позицию: «gmail.com 1»."
    await ui_prompt(state, c.message.chat.id, txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open")))
    await state.set_state(DomainsFSM.add); await safe_cq_answer(c)

@dp.message(DomainsFSM.add)
async def domains_add_input(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    parts = (m.text or "").strip().split()
    if not parts:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Пустой ввод.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open"))); return
    name = parts[0]
    pos = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    with SessionLocal() as s:
        add_domain(s, m.from_user.id, name, pos)
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, domains_text_for_user(m.from_user.id), reply_markup=domains_kb()); await state.clear()

@dp.callback_query(F.data == "domains:reorder")
async def domains_reorder(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    txt = domains_text_for_user(c.from_user.id) + "\n\nВведите новый порядок номеров (например: 3 1 2 4)"
    await ui_prompt(state, c.message.chat.id, txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open")))
    await state.set_state(DomainsFSM.reorder); await safe_cq_answer(c)

@dp.message(DomainsFSM.reorder)
async def domains_reorder_input(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    with SessionLocal() as s:
        names = list_domains(s, m.from_user.id)
    try:
        order = [int(x) for x in (m.text or "").replace(",", " ").split()]
        if sorted(order) != list(range(1, len(names) + 1)):
            raise ValueError
        new_names = [names[i - 1] for i in order]
        with SessionLocal() as s:
            set_domains_order(s, m.from_user.id, new_names)
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, domains_text_for_user(m.from_user.id), reply_markup=domains_kb()); await state.clear()
    except Exception:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Неверный формат. Пример: 2 1 3", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open")))

@dp.callback_query(F.data == "domains:delete")
async def domains_delete(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    txt = domains_text_for_user(c.from_user.id) + "\n\nВведите номера доменов для удаления (например: 1 4 6)."
    await ui_prompt(state, c.message.chat.id, txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open")))
    await state.set_state(DomainsFSM.delete); await safe_cq_answer(c)

@dp.message(DomainsFSM.delete)
async def domains_delete_input(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    try:
        nums = sorted({int(x) for x in (m.text or "").replace(",", " ").split()}, reverse=True)
        with SessionLocal() as s:
            delete_domains_by_indices(s, m.from_user.id, list(nums))
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, domains_text_for_user(m.from_user.id), reply_markup=domains_kb()); await state.clear()
    except Exception:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Неверный ввод. Пример: 2 5 6", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open")))

@dp.callback_query(F.data == "domains:clear")
async def domains_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Подтвердите удаление всех доменов: ДА", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("domains:open")))
    await state.set_state(DomainsFSM.clear); await safe_cq_answer(c)

@dp.message(DomainsFSM.clear)
async def domains_clear_input(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if (m.text or "").strip().upper() == "ДА":
        with SessionLocal() as s:
            clear_domains(s, m.from_user.id)
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Все домены удалены.\n\n" + domains_text_for_user(m.from_user.id), reply_markup=domains_kb())
    else:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=domains_kb())
    await state.clear()

# ====== INTERVAL ======
def interval_text(user_id: int) -> str:
    vmin = get_setting(user_id, "send_delay_min", str(smtp25.MIN_SEND_DELAY))
    vmax = get_setting(user_id, "send_delay_max", str(smtp25.MAX_SEND_DELAY))
    return f"Текущий интервал:\n\n{code(f'[{vmin}, {vmax}]')}"

def interval_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Изменить интервал", callback_data="interval:change"),
         InlineKeyboardButton(text="🔄 Сбросить интервал", callback_data="interval:reset")],
        *nav_row("settings:back")
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "interval:open")
async def interval_open(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_edit_message(c.message, interval_text(c.from_user.id), reply_markup=interval_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data == "interval:change")
async def interval_change(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    txt = interval_text(c.from_user.id) + "\n\nВведите два числа: MIN MAX (например: 3 6)"
    await ui_prompt(state, c.message.chat.id, txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("interval:open")))
    await state.set_state(IntervalFSM.set); await safe_cq_answer(c)

@dp.message(IntervalFSM.set)
async def interval_set_value(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    try:
        parts = [int(x) for x in (m.text or "").replace(",", " ").split()]
        if len(parts) != 2:
            raise ValueError
        minv, maxv = parts
        if minv < 0 or maxv < 0 or minv >= maxv:
            raise ValueError
        set_setting(m.from_user.id, "send_delay_min", str(minv))
        set_setting(m.from_user.id, "send_delay_max", str(maxv))
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, interval_text(m.from_user.id), reply_markup=interval_kb())
        await state.clear()
    except Exception:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Неверный ввод. Пример: 3 6", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("interval:open")))

@dp.callback_query(F.data == "interval:reset")
async def interval_reset(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    set_setting(c.from_user.id, "send_delay_min", str(smtp25.MIN_SEND_DELAY))
    set_setting(c.from_user.id, "send_delay_max", str(smtp25.MAX_SEND_DELAY))
    await safe_edit_message(c.message, interval_text(c.from_user.id), reply_markup=interval_kb())
    await c.answer("Сброшено")

# ====== PROXIES ======
def proxies_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Verify прокси", callback_data="proxies:open:verify")],
        [InlineKeyboardButton(text="🚀 Send прокси", callback_data="proxies:open:send")],
        *nav_row("settings:back")
    ])
    
def _probe_target_for_kind(kind: str) -> Tuple[str, int]:
    # Куда коннектимся для проверки работоспособности
    if kind == "verify":
        return ("imap.gmail.com", 993)  # IMAP SSL
    return ("smtp.gmail.com", 587)      # SMTP STARTTLS порт

def _test_proxy_sync(host: str, port: int, user: str, pwd: str, target_host: str, target_port: int, timeout: int = 6) -> Tuple[bool, str]:
    try:
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, host, int(port), True, user or None, pwd or None)
        s.settimeout(timeout)
        s.connect((target_host, int(target_port)))
        try:
            s.close()
        except Exception:
            pass
        return True, "OK"
    except Exception as e:
        return False, str(e)

async def _test_proxy_async(host: str, port: int, user: str, pwd: str, target_host: str, target_port: int, timeout: int = 6) -> Tuple[bool, str]:
    return await asyncio.to_thread(_test_proxy_sync, host, port, user, pwd, target_host, target_port, timeout)

def proxies_section_kb(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Показать прокси", callback_data=f"proxies:list:{kind}:1")],
        [InlineKeyboardButton(text="➕ Добавить прокси", callback_data=f"proxies:add:{kind}"),
         InlineKeyboardButton(text="✏️ Изменить прокси", callback_data=f"proxies:edit:{kind}")],
        [InlineKeyboardButton(text="🗑 Удалить прокси", callback_data=f"proxies:delete:{kind}"),
         InlineKeyboardButton(text="🧹 Удалить все", callback_data=f"proxies:clear:{kind}")],
        *nav_row("proxies:root")
    ])

def render_proxies_text_page(user_id: int, kind: str, page: int, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    with SessionLocal() as s:
        items = s.query(Proxy).filter_by(user_id=user_id, type=kind).order_by(Proxy.id.asc()).all()
    title = "Verify прокси" if kind == "verify" else "Send прокси"
    total = len(items)
    if not total:
        return f"{title}:\n(список пуст)", proxies_section_kb(kind)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = min(total, start + per_page)
    slice_items = items[start:end]
    lines = [f"{title}:", ""]
    for i, p in enumerate(slice_items, start=start + 1):
        host = p.host or ""
        login = p.user_login or ""
        pwd = p.password or ""
        lines.append(f"Прокси №{i}: {code(f'{host}:{p.port}:{login}:{pwd}')}")
    lines.append("")
    lines.append("Для редактирования/удаления указывайте номера по списку (например: 1 3 5).")
    rows = pager_row(f"proxies:list:{kind}:", page, total_pages)
    rows += proxies_section_kb(kind).inline_keyboard
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "proxies:root")
async def proxies_root(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_edit_message(c.message, "Настройки прокси:", reply_markup=proxies_root_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("proxies:open:"))
async def proxies_open_section(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    kind = c.data.split(":")[2]
    title = "Verify прокси" if kind == "verify" else "Send прокси"
    await safe_edit_message(c.message, f"Настройки {title}:", reply_markup=proxies_section_kb(kind)); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("proxies:list:"))
async def proxies_list(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    parts = c.data.split(":")
    kind = parts[2]
    page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 1
    text, kb = render_proxies_text_page(c.from_user.id, kind, page, per_page=10)
    await safe_edit_message(c.message, text, reply_markup=kb); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("proxies:add:"))
async def proxies_add(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    kind = c.data.split(":")[2]
    await state.update_data(proxy_kind=kind)
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите прокси в формате host:port:log:pass✍️\nМожно по одному на строку.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
    await state.set_state(ProxiesFSM.add); await safe_cq_answer(c)

@dp.message(ProxiesFSM.add)
async def proxies_add_save(m: types.Message, state: FSMContext):
    if not await ensure_approved(m):
        return
    await delete_message_safe(m)
    data = await state.get_data()
    kind = data.get("proxy_kind", "send")
    parsed = parse_proxy_lines(m.text or "")

    if not parsed:
        await ui_clear_prompts(state)
        await bot.send_message(
            m.chat.id,
            "Не распознано ни одной строки. Ожидается host:port:login:password",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}"))
        )
        return

    target_host, target_port = _probe_target_for_kind(kind)
    lines: List[str] = [f"Проверка {('Verify' if kind=='verify' else 'Send')} прокси:"]
    ok_cnt = 0
    fail_cnt = 0

    with SessionLocal() as s:
        for host, port, user, pwd in parsed:
            ok, err = await _test_proxy_async(host, port, user, pwd, target_host, target_port, timeout=6)
            s.add(Proxy(
                user_id=m.from_user.id,
                host=host,
                port=port,
                user_login=user,
                password=pwd,
                type=kind,
                active=bool(ok),
            ))
            status = "✅ OK" if ok else f"❌ Ошибка: {err}"
            masked_pwd = "*" * len(pwd) if pwd else ""
            lines.append(f"{host}:{port}:{user}:{masked_pwd} — {status}")
            if ok:
                ok_cnt += 1
            else:
                fail_cnt += 1
        s.commit()

    lines.append("")
    lines.append(f"Итог: OK={ok_cnt}, Ошибок={fail_cnt}")
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "\n".join(lines), reply_markup=proxies_section_kb(kind))
    await state.clear()

@dp.callback_query(F.data.startswith("proxies:edit:"))
async def proxies_edit_pick(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    kind = c.data.split(":")[2]
    await state.update_data(proxy_kind=kind)
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номер прокси по списку (например: 2):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
    await state.set_state(ProxiesFSM.edit_pick); await safe_cq_answer(c)

@dp.message(ProxiesFSM.edit_pick)
async def proxies_edit_id(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    kind = data.get("proxy_kind", "send")
    if not (m.text or "").strip().isdigit():
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Нужен номер (например: 2).", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
        return
    ordinal = int((m.text or "").strip())
    with SessionLocal() as s:
        items = s.query(Proxy).filter_by(user_id=m.from_user.id, type=kind).order_by(Proxy.id.asc()).all()
    chosen = _get_by_ordinal(items, ordinal)
    if not chosen:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный номер.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
        return
    await state.update_data(proxy_id=int(chosen.id))
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Введите новые данные в формате host:port:log:pass:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
    await state.set_state(ProxiesFSM.edit_value)

@dp.message(ProxiesFSM.edit_value)
async def proxies_edit_save(m: types.Message, state: FSMContext):
    if not await ensure_approved(m):
        return
    await delete_message_safe(m)
    data = await state.get_data()
    kind = data.get("proxy_kind", "send")
    proxy_id = int(data.get("proxy_id"))

    parsed = parse_proxy_lines(m.text or "")
    if len(parsed) != 1:
        await ui_clear_prompts(state)
        await ui_prompt(
            state, m.chat.id,
            "Ожидается одна строка формата host:port:login:password.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}"))
        )
        return

    host, port, user, pwd = parsed[0]
    target_host, target_port = _probe_target_for_kind(kind)
    ok, err = await _test_proxy_async(host, port, user, pwd, target_host, target_port, timeout=6)

    with SessionLocal() as s:
        pr = s.query(Proxy).filter_by(user_id=m.from_user.id, id=proxy_id, type=kind).first()
        if not pr:
            await ui_clear_prompts(state)
            await bot.send_message(m.chat.id, "Прокси не найден.", reply_markup=proxies_section_kb(kind))
        else:
            pr.host = host
            pr.port = port
            pr.user_login = user
            pr.password = pwd
            pr.active = bool(ok)
            s.commit()

            status = "✅ OK" if ok else f"❌ Ошибка: {err}"
            await ui_clear_prompts(state)
            await bot.send_message(m.chat.id, f"Прокси обновлён.\nРезультат проверки: {status}", reply_markup=proxies_section_kb(kind))
    await state.clear()

@dp.callback_query(F.data.startswith("proxies:delete:"))
async def proxies_delete(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    kind = c.data.split(":")[2]
    await state.update_data(proxy_kind=kind)
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номера прокси для удаления (например: 1 3 5):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
    await state.set_state(ProxiesFSM.delete); await safe_cq_answer(c)

@dp.message(ProxiesFSM.delete)
async def proxies_delete_do(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    kind = data.get("proxy_kind", "send")
    try:
        ordinals = [int(x) for x in (m.text or "").replace(",", " ").split()]
    except Exception:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный ввод. Пример: 1 2 3", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}"))); return

    with SessionLocal() as s:
        items = s.query(Proxy).filter_by(user_id=m.from_user.id, type=kind).order_by(Proxy.id.asc()).all()
        ids_to_delete = []
        for o in ordinals:
            item = _get_by_ordinal(items, o)
            if item: ids_to_delete.append(item.id)
        if ids_to_delete:
            s.query(Proxy).filter(Proxy.user_id == m.from_user.id, Proxy.type == kind, Proxy.id.in_(ids_to_delete)).delete(synchronize_session=False)
            s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Удаление выполнено.", reply_markup=proxies_section_kb(kind))
    await state.clear()

@dp.callback_query(F.data.startswith("proxies:clear:"))
async def proxies_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    kind = c.data.split(":")[2]
    await state.update_data(proxy_kind=kind)
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Подтвердите удаление всех прокси: ДА", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row(f"proxies:open:{kind}")))
    await state.set_state(ProxiesFSM.clear); await safe_cq_answer(c)

@dp.message(ProxiesFSM.clear)
async def proxies_clear_confirm(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    kind = data.get("proxy_kind", "send")
    if (m.text or "").strip().upper() == "ДА":
        with SessionLocal() as s:
            s.query(Proxy).filter_by(user_id=m.from_user.id, type=kind).delete()
            s.commit()
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Все прокси удалены.", reply_markup=proxies_section_kb(kind))
    else:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=proxies_section_kb(kind))
    await state.clear()

# ====== EMAIL ACCOUNTS ======
def emails_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📬 Показать E‑mail", callback_data="emails:list:1")],
        [InlineKeyboardButton(text="➕ Добавить E‑mail", callback_data="emails:add"),
         InlineKeyboardButton(text="✏️ Изменить E‑mail", callback_data="emails:edit")],
        [InlineKeyboardButton(text="🗑 Удалить E‑mail", callback_data="emails:delete"),
         InlineKeyboardButton(text="🧹 Удалить все", callback_data="emails:clear")],
        *nav_row("settings:back")
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_emails_text_and_kb(user_id: int, page: int = 1, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    with SessionLocal() as s:
        items = s.query(Account).filter_by(user_id=user_id).order_by(Account.id.asc()).all()
    if not items:
        return "Пока аккаунтов нет.", emails_menu_kb()
    total = len(items)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = min(total, start + per_page)
    slice_items = items[start:end]
    lines = []
    for i, acc in enumerate(slice_items, start=start + 1):
        lines.append(f"E‑mail №{i}")
        lines.append(code(acc.display_name or ""))
        lines.append(code(f"{acc.email}:{acc.password}"))
        lines.append("")
    rows = pager_row("emails:list:", page, total_pages)
    rows += emails_menu_kb().inline_keyboard
    return "\n".join(lines).strip(), InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "emails:open")
async def emails_open(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_edit_message(c.message, "Настройки E‑mail:", reply_markup=emails_menu_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("emails:list"))
async def emails_list(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    parts = c.data.split(":")
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    text, kb = build_emails_text_and_kb(c.from_user.id, page=page, per_page=10)
    await safe_edit_message(c.message, text, reply_markup=kb); await safe_cq_answer(c)

async def _ensure_imap_started_for_user(uid: int, chat_id: int):
    await asyncio.to_thread(prepare_smtp25_from_db, uid)
    IMAP_STATUS.setdefault(uid, {})["chat_id"] = chat_id  # <- добавили
    if uid not in IMAP_TASKS or IMAP_TASKS[uid].done():
        IMAP_TASKS[uid] = asyncio.create_task(imap_loop(uid, chat_id))

@dp.callback_query(F.data == "emails:add")
async def emails_add(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите отображаемое имя и фамилию. Например: Jessy Jackson ✍️", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open")))
    await state.set_state(AddAccountFSM.display_name); await safe_cq_answer(c)

@dp.message(AddAccountFSM.display_name)
async def emails_add_name(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await state.update_data(display_name=(m.text or "").strip())
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Введите E‑mail в формате login:pass ✍️", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open")))
    await state.set_state(AddAccountFSM.loginpass)

@dp.message(AddAccountFSM.loginpass)
async def emails_add_loginpass(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    disp = data.get("display_name", "").strip()
    if ":" not in (m.text or ""):
        await bot.send_message(m.chat.id, "Ожидаю формат login:pass. Попробуйте ещё раз.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))); return
    login, password = [x.strip() for x in m.text.split(":", 1)]
    with SessionLocal() as s:
        add_account(s, m.from_user.id, disp, login, password, auto_bind_proxy=True)
        acc = s.query(Account).filter_by(user_id=m.from_user.id, email=login).order_by(Account.id.desc()).first()
        if acc:
            acc.active = True
        s.commit()

    # Сброс одноразовых логов для нового email (иначе “запущен⚡” может не прийти, если этот e‑mail уже использовался ранее)
    key = (m.from_user.id, login)
    START_LOG_SENT.pop(key, None)
    ERROR_LOG_SENT.pop(key, None)

    # Инициализируем runtime‑статус как неактивный до первого успешного IMAP‑логина
    IMAP_STATUS.setdefault(m.from_user.id, {}).setdefault("accounts", {}).setdefault(login, {})
    IMAP_STATUS[m.from_user.id]["accounts"][login].update({"active": False})

    # Запускаем/обновляем луп и актуальный chat_id
    await _ensure_imap_started_for_user(m.from_user.id, m.chat.id)

    # Финальный лаконичный лог для раздела (подсказки уже удалены самим delete_message_safe)
    await bot.send_message(m.chat.id, "Аккаунт добавлен.", reply_markup=emails_menu_kb())
    await state.clear()

@dp.callback_query(F.data == "emails:edit")
async def emails_edit(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номер аккаунта для изменения (например: 1):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open")))
    await state.set_state(EditAccountFSM.account_id); await safe_cq_answer(c)

@dp.message(EditAccountFSM.account_id)
async def emails_edit_pick(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if not (m.text or "").strip().isdigit():
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Нужен номер аккаунта.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))); return
    ordinal = int((m.text or "").strip())
    with SessionLocal() as s:
        items = s.query(Account).filter_by(user_id=m.from_user.id).order_by(Account.id.asc()).all()
    chosen = _get_by_ordinal(items, ordinal)
    if not chosen:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный номер.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))); return
    await state.update_data(account_id=int(chosen.id))
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Новое отображаемое имя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open")))
    await state.set_state(EditAccountFSM.display_name)

@dp.message(EditAccountFSM.display_name)
async def emails_edit_name(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await state.update_data(display_name=(m.text or "").strip())
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Новый login:pass:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open")))
    await state.set_state(EditAccountFSM.loginpass)

@dp.message(EditAccountFSM.loginpass)
async def emails_edit_save(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    acc_id = int(data["account_id"])
    if ":" not in (m.text or ""):
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Ожидаю формат login:pass.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))); return
    login, password = [x.strip() for x in (m.text or "").split(":", 1)]
    with SessionLocal() as s:
        update_account(s, m.from_user.id, acc_id, display_name=data["display_name"], email=login, password=password)
        s.commit()
    # сброс одноразовых флагов логов для нового логина
    key = (m.from_user.id, login)
    START_LOG_SENT.pop(key, None)
    ERROR_LOG_SENT.pop(key, None)

    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Аккаунт обновлён.", reply_markup=emails_menu_kb())
    await _ensure_imap_started_for_user(m.from_user.id, m.chat.id)
    await state.clear()

@dp.callback_query(F.data == "emails:delete")
async def emails_delete(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номер аккаунта для удаления (например: 1):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open")))
    await state.set_state(EmailDeleteFSM.account_id); await safe_cq_answer(c)

@dp.message(EmailDeleteFSM.account_id)
async def emails_delete_do(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if not (m.text or "").strip().isdigit():
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Нужен номер аккаунта.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))); return
    ordinal = int((m.text or "").strip())
    with SessionLocal() as s:
        items = s.query(Account).filter_by(user_id=m.from_user.id).order_by(Account.id.asc()).all()
        chosen = _get_by_ordinal(items, ordinal)
        if not chosen:
            await ui_clear_prompts(state)
            await ui_prompt(state, m.chat.id, "Неверный номер.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))); return
        s.query(Account).filter_by(user_id=m.from_user.id, id=chosen.id).delete()
        s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Аккаунт удалён.", reply_markup=emails_menu_kb())
    await state.clear()

@dp.callback_query(F.data == "emails:clear")
async def emails_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c):
        return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    uid = c.from_user.id

    # Собираем кандидатов на удаление: неактивные по БД ИЛИ неактивные по runtime
    with SessionLocal() as s:
        accounts = s.query(Account).filter_by(user_id=uid).order_by(Account.id.asc()).all()

    st_accounts = IMAP_STATUS.get(uid, {}).get("accounts", {}) if IMAP_STATUS.get(uid) else {}
    def is_runtime_active(email: str) -> Optional[bool]:
        v = st_accounts.get(email, {}).get("active")
        return v  # True/False/None

    to_delete = []
    for acc in accounts:
        ra = is_runtime_active(acc.email)
        if (acc.active is False) or (ra is False):
            to_delete.append({"id": acc.id, "email": acc.email})

    if not to_delete:
        await bot.send_message(c.message.chat.id, "Нет неактивных аккаунтов для удаления.", reply_markup=emails_menu_kb())
        await safe_cq_answer(c)
        return

    await state.update_data(emails_clear_ids=[x["id"] for x in to_delete],
                            emails_clear_emails=[x["email"] for x in to_delete])

    await ui_prompt(
        state,
        c.message.chat.id,
        f"Будут удалены только неактивные аккаунты: {len(to_delete)} шт.\nПодтвердите удаление: ДА",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))
    )
    await state.set_state(EmailsClearFSM.confirm)
    await safe_cq_answer(c)

@dp.message(EmailsClearFSM.confirm)
async def emails_clear_confirm(m: types.Message, state: FSMContext):
    if not await ensure_approved(m):
        return
    await delete_message_safe(m)

    if (m.text or "").strip().upper() != "ДА":
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=emails_menu_kb())
        await state.clear()
        return

    data = await state.get_data()
    uid = m.from_user.id
    ids: list[int] = data.get("emails_clear_ids", []) or []
    emails: list[str] = data.get("emails_clear_emails", []) or []

    deleted_cnt = 0
    if ids:
        with SessionLocal() as s:
            deleted_cnt = (
                s.query(Account)
                 .filter(Account.user_id == uid, Account.id.in_(ids))
                 .delete(synchronize_session=False)
            )
            s.commit()

    if emails:
        IMAP_STATUS.setdefault(uid, {}).setdefault("accounts", {})
        for em in emails:
            IMAP_STATUS[uid]["accounts"].pop(em, None)

    await ui_clear_prompts(state)
    if deleted_cnt == 0:
        await bot.send_message(m.chat.id, "Нет неактивных аккаунтов для удаления.", reply_markup=emails_menu_kb())
    else:
        await bot.send_message(m.chat.id, f"Удалено неактивных аккаунтов: {deleted_cnt}", reply_markup=emails_menu_kb())
    await state.clear()

@dp.callback_query(F.data == "emails:clear")
async def emails_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c):
        return
    uid = c.from_user.id

    # Собираем кандидатов на удаление: неактивные по БД ИЛИ неактивные по runtime
    with SessionLocal() as s:
        accounts = s.query(Account).filter_by(user_id=uid).order_by(Account.id.asc()).all()

    st_accounts = IMAP_STATUS.get(uid, {}).get("accounts", {}) if IMAP_STATUS.get(uid) else {}
    def is_runtime_active(email: str) -> Optional[bool]:
        v = st_accounts.get(email, {}).get("active")
        return v  # может быть True/False/None

    to_delete = []
    for acc in accounts:
        ra = is_runtime_active(acc.email)
        # Удаляем, если аккаунт неактивен в БД или явно неактивен в runtime
        if (acc.active is False) or (ra is False):
            to_delete.append({"id": acc.id, "email": acc.email})

    if not to_delete:
        await safe_edit_message(
            c.message,
            "Нет неактивных аккаунтов для удаления.",
            reply_markup=emails_menu_kb()
        )
        await safe_cq_answer(c)
        return

    await state.update_data(emails_clear_ids=[x["id"] for x in to_delete],
                            emails_clear_emails=[x["email"] for x in to_delete])

    cnt = len(to_delete)
    await safe_edit_message(
        c.message,
        f"Будут удалены только неактивные аккаунты: {cnt} шт.\nПодтвердите удаление: ДА",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("emails:open"))
    )
    await state.set_state(EmailsClearFSM.confirm)
    await safe_cq_answer(c)

@dp.message(EmailsClearFSM.confirm)
async def emails_clear_confirm(m: types.Message, state: FSMContext):
    if not await ensure_approved(m):
        return
    await delete_message_safe(m)

    if (m.text or "").strip().upper() != "ДА":
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=emails_menu_kb())
        await state.clear()
        return

    data = await state.get_data()
    uid = m.from_user.id
    ids: list[int] = data.get("emails_clear_ids", []) or []
    emails: list[str] = data.get("emails_clear_emails", []) or []

    deleted_cnt = 0
    if ids:
        with SessionLocal() as s:
            deleted_cnt = (
                s.query(Account)
                 .filter(Account.user_id == uid, Account.id.in_(ids))
                 .delete(synchronize_session=False)
            )
            s.commit()

    # Чистим runtime-статус для удалённых e‑mail
    if emails:
        IMAP_STATUS.setdefault(uid, {}).setdefault("accounts", {})
        for em in emails:
            IMAP_STATUS[uid]["accounts"].pop(em, None)

    if deleted_cnt == 0:
        await bot.send_message(m.chat.id, "Нет неактивных аккаунтов для удаления.", reply_markup=emails_menu_kb())
    else:
        await bot.send_message(m.chat.id, f"Удалено неактивных аккаунтов: {deleted_cnt}", reply_markup=emails_menu_kb())

    await state.clear()

# ====== PRESETS (IMAP) ======
def presets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Показать", callback_data="presets:show:1")],
        [InlineKeyboardButton(text="➕ Добавить", callback_data="presets:add"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data="presets:edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="presets:delete"),
         InlineKeyboardButton(text="🧹 Очистить", callback_data="presets:clear")],
        *nav_row("presets:open")
    ])

def presets_pager_kb(page: int, total_pages: int) -> list[list[InlineKeyboardButton]]:
    return pager_row("presets:show:", page, total_pages)

def presets_manage_kb() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="presets:add"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data="presets:edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="presets:delete"),
         InlineKeyboardButton(text="🧹 Очистить", callback_data="presets:clear")],
        *nav_row("presets:open")
    ]

def build_imap_presets_text_and_kb(user_id: int, page: int = 1, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    with SessionLocal() as s:
        items = s.query(Preset).filter_by(user_id=user_id).order_by(Preset.id.asc()).all()
    total = len(items)
    if total == 0:
        return "Пресетов пока нет.", presets_kb()

    def compose_page(pp: int) -> Tuple[str, int]:
        total_pages = max(1, math.ceil(total / pp))
        page_clamped = max(1, min(page, total_pages))
        start = (page_clamped - 1) * pp
        end = min(total, start + pp)
        slice_items = items[start:end]
        lines: list[str] = []
        for idx, p in enumerate(slice_items, start=start + 1):
            title = (p.title or "").strip()
            body = (p.body or "").strip()
            lines.append(f"Пресет №{idx}" + (f" — {title}" if title else ""))
            if body:
                lines.append(code(body))
            lines.append("")
        return "\n".join(lines).strip(), total_pages

    text, total_pages = compose_page(per_page)
    while len(text) > 3800 and per_page > 3:
        per_page -= 1
        text, total_pages = compose_page(per_page)

    ik = presets_pager_kb(page, total_pages)
    ik += presets_manage_kb()
    return text, InlineKeyboardMarkup(inline_keyboard=ik)

def presets_inline_kb(user_id: int, back_cb: str) -> InlineKeyboardMarkup:
    with SessionLocal() as s:
        items = s.query(Preset).filter_by(user_id=user_id).order_by(Preset.id.asc()).all()
    rows = []
    for i, p in enumerate(items, start=1):
        title = (p.title or "").strip() or f"Пресет №{i}"
        if len(title) > 60:
            title = title[:57] + "..."
        rows.append([InlineKeyboardButton(text=f"📜 {title}", callback_data=f"presets:view:{p.id}:{back_cb}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "presets:open")
async def presets_open(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await c.message.edit_text("Пресеты (IMAP):", reply_markup=presets_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("presets:show"))
async def presets_show(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    parts = c.data.split(":")
    page = 1
    if len(parts) == 3 and parts[2].isdigit():
        page = int(parts[2])
    text, kb = build_imap_presets_text_and_kb(c.from_user.id, page=page, per_page=10)
    await safe_edit_message(c.message, text, reply_markup=kb); await safe_cq_answer(c)

@dp.callback_query(F.data == "presets:noop")
async def presets_noop(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_cq_answer(c)

@dp.callback_query(F.data == "presets:add")
async def presets_add(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): 
        return
    await ui_clear_prompts(state)
    await delete_message_safe(c.message)
    await ui_prompt(
        state,
        c.message.chat.id,
        "Введите заголовок пресета:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetAddFSM.title)
    await safe_cq_answer(c)

@dp.message(PresetAddFSM.title)
async def presets_add_title(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    await state.update_data(title=(m.text or "").strip())
    await ui_clear_prompts(state)
    await ui_prompt(
        state,
        m.chat.id,
        "Введите текст пресета:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetAddFSM.body)

@dp.message(PresetAddFSM.body)
async def presets_add_body(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    data = await state.get_data()
    with SessionLocal() as s:
        s.add(Preset(user_id=m.from_user.id, title=data["title"], body=m.text or ""))
        s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Пресет добавлен.", reply_markup=presets_kb())
    await state.clear()

@dp.callback_query(F.data == "presets:edit")
async def presets_edit(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): 
        return
    await ui_clear_prompts(state)
    await delete_message_safe(c.message)
    await ui_prompt(
        state,
        c.message.chat.id,
        "Введите номер пресета по списку (например: 1):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetEditFSM.preset_id)
    await safe_cq_answer(c)

@dp.message(PresetEditFSM.preset_id)
async def presets_edit_pick(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    if not (m.text or "").strip().isdigit():
        await ui_clear_prompts(state)
        await ui_prompt(
            state,
            m.chat.id,
            "Нужен номер пресета (например: 1).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
        )
        return
    ordinal = int((m.text or "").strip())
    with SessionLocal() as s:
        items = s.query(Preset).filter_by(user_id=m.from_user.id).order_by(Preset.id.asc()).all()
    chosen = (items[ordinal-1] if 1 <= ordinal <= len(items) else None)
    if not chosen:
        await ui_clear_prompts(state)
        await ui_prompt(
            state, m.chat.id,
            "Неверный номер.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
        )
        return
    await state.update_data(preset_id=chosen.id)
    await ui_clear_prompts(state)
    await ui_prompt(
        state,
        m.chat.id,
        "Новый заголовок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetEditFSM.title)

@dp.message(PresetEditFSM.title)
async def presets_edit_title(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    await state.update_data(title=(m.text or "").strip())
    await ui_clear_prompts(state)
    await ui_prompt(
        state,
        m.chat.id,
        "Новый текст:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetEditFSM.body)

@dp.message(PresetEditFSM.body)
async def presets_edit_save(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    data = await state.get_data()
    with SessionLocal() as s:
        p = s.query(Preset).filter_by(user_id=m.from_user.id, id=data["preset_id"]).first()
        if not p:
            await ui_clear_prompts(state)
            await bot.send_message(m.chat.id, "Пресет не найден.", reply_markup=presets_kb())
            await state.clear()
            return
        p.title = data["title"]
        p.body = (m.text or "")
        s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Пресет обновлён.", reply_markup=presets_kb())
    await state.clear()

@dp.callback_query(F.data == "presets:delete")
async def presets_delete(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): 
        return
    await ui_clear_prompts(state)
    await delete_message_safe(c.message)
    await ui_prompt(
        state, c.message.chat.id,
        "Введите номера пресетов для удаления (например: 1 3 4):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetDeleteFSM.preset_id)
    await safe_cq_answer(c)

@dp.message(PresetDeleteFSM.preset_id)
async def presets_delete_do(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    try:
        ordinals = [int(x) for x in (m.text or "").replace(",", " ").split()]
    except Exception:
        await ui_clear_prompts(state)
        await ui_prompt(
            state, m.chat.id,
            "Неверный ввод. Пример: 1 2 3",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
        )
        return
    with SessionLocal() as s:
        items = s.query(Preset).filter_by(user_id=m.from_user.id).order_by(Preset.id.asc()).all()
        ids_to_delete = []
        for o in ordinals:
            if 1 <= o <= len(items):
                ids_to_delete.append(items[o-1].id)
        if ids_to_delete:
            s.query(Preset).filter(Preset.user_id == m.from_user.id, Preset.id.in_(ids_to_delete)).delete(synchronize_session=False)
            s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Удалено.", reply_markup=presets_kb())
    await state.clear()

@dp.callback_query(F.data == "presets:clear")
async def presets_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): 
        return
    await ui_clear_prompts(state)
    await delete_message_safe(c.message)
    await ui_prompt(
        state,
        c.message.chat.id,
        "Подтвердите очистку пресетов: ДА",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("presets:open"))
    )
    await state.set_state(PresetClearFSM.confirm)
    await safe_cq_answer(c)

@dp.message(PresetClearFSM.confirm)
async def presets_clear_confirm(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    if (m.text or "").strip().upper() == "ДА":
        with SessionLocal() as s:
            s.query(Preset).filter_by(user_id=m.from_user.id).delete()
            s.commit()
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Все пресеты удалены.", reply_markup=presets_kb())
    else:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=presets_kb())
    await state.clear()

# ====== SMART PRESETS ======
def smart_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Показать пресеты", callback_data="smart:show:1")],
        [InlineKeyboardButton(text="➕ Добавить", callback_data="smart:add"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data="smart:edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="smart:delete"),
         InlineKeyboardButton(text="🧹 Очистить", callback_data="smart:clear")],
        *nav_row("settings:back")
    ])

def smart_pager_kb(page: int, total_pages: int) -> list[list[InlineKeyboardButton]]:
    return pager_row("smart:show:", page, total_pages)

def smart_manage_kb() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="smart:add"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data="smart:edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="smart:delete"),
         InlineKeyboardButton(text="🧹 Очистить", callback_data="smart:clear")],
        *nav_row("smart:open")
    ]

def build_smart_text_and_kb(user_id: int, page: int = 1, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    with SessionLocal() as s:
        items = s.query(SmartPreset).filter_by(user_id=user_id).order_by(SmartPreset.id.asc()).all()

    total = len(items)
    if total == 0:
        return "Пресетов пока нет.", smart_settings_kb()

    def compose_page(pp: int) -> Tuple[str, int]:
        total_pages = max(1, math.ceil(total / pp))
        page_clamped = max(1, min(page, total_pages))
        start = (page_clamped - 1) * pp
        end = min(total, start + pp)
        slice_items = items[start:end]
        lines: list[str] = []
        for i, p in enumerate(slice_items, start=start + 1):
            lines.append(f"Пресет №{i}")
            lines.append(code((p.body or "").strip()))
            lines.append("")
        return "\n".join(lines).strip(), total_pages

    text, total_pages = compose_page(per_page)
    while len(text) > 3800 and per_page > 3:
        per_page -= 1
        text, total_pages = compose_page(per_page)

    ik = smart_pager_kb(page, total_pages)
    ik += smart_manage_kb()
    return text, InlineKeyboardMarkup(inline_keyboard=ik)

@dp.callback_query(F.data == "smart:open")
async def smart_open(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_edit_message(c.message, "Настройки умных пресетов:", reply_markup=smart_settings_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("smart:show"))
async def smart_show(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    parts = c.data.split(":")
    page = 1
    if len(parts) == 3 and parts[2].isdigit():
        page = int(parts[2])
    text, kb = build_smart_text_and_kb(c.from_user.id, page=page, per_page=10)
    await safe_edit_message(c.message, text, reply_markup=kb); await safe_cq_answer(c)

@dp.callback_query(F.data == "smart:noop")
async def smart_noop(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await safe_cq_answer(c)

@dp.callback_query(F.data == "smart:add")
async def smart_add(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите текст пресета✍️", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
    await state.set_state(SmartPresetAddFSM.body); await safe_cq_answer(c)

@dp.message(SmartPresetAddFSM.body)
async def smart_add_body(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    with SessionLocal() as s:
        s.add(SmartPreset(user_id=m.from_user.id, body=(m.text or "").strip())); s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Пресет добавлен.", reply_markup=smart_settings_kb())
    await state.clear()

@dp.callback_query(F.data == "smart:edit")
async def smart_edit(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номер умного пресета для изменения (например: 1):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
    await state.set_state(SmartPresetEditFSM.preset_id); await safe_cq_answer(c)

@dp.message(SmartPresetEditFSM.preset_id)
async def smart_edit_pick(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if not (m.text or "").strip().isdigit():
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Нужен номер пресета (например: 1).", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
        return
    ordinal = int((m.text or "").strip())
    with SessionLocal() as s:
        items = s.query(SmartPreset).filter_by(user_id=m.from_user.id).order_by(SmartPreset.id.asc()).all()
    chosen = (items[ordinal-1] if 1 <= ordinal <= len(items) else None)
    if not chosen:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный номер.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
        return
    await state.update_data(preset_id=int(chosen.id))
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Введите новый текст пресета:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
    await state.set_state(SmartPresetEditFSM.body)

@dp.message(SmartPresetEditFSM.body)
async def smart_edit_save(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    body = (m.text or "").strip()
    with SessionLocal() as s:
        p = s.query(SmartPreset).filter_by(user_id=m.from_user.id, id=int(data.get("preset_id", 0))).first()
        if not p:
            await ui_clear_prompts(state)
            await bot.send_message(m.chat.id, "Пресет не найден.", reply_markup=smart_settings_kb())
            await state.clear(); return
        p.body = body; s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Пресет обновлён.", reply_markup=smart_settings_kb())
    await state.clear()

@dp.callback_query(F.data == "smart:delete")
async def smart_delete(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номера умных пресетов для удаления (например: 1 3 4):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
    await state.set_state(SmartPresetDeleteFSM.preset_id); await safe_cq_answer(c)

@dp.message(SmartPresetDeleteFSM.preset_id)
async def smart_delete_do(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    try:
        ordinals = [int(x) for x in (m.text or "").replace(",", " ").split()]
    except Exception:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный ввод. Пример: 1 2 3", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
        return
    with SessionLocal() as s:
        items = s.query(SmartPreset).filter_by(user_id=m.from_user.id).order_by(SmartPreset.id.asc()).all()
        ids_to_delete: list[int] = []
        for o in ordinals:
            if 1 <= o <= len(items):
                ids_to_delete.append(int(items[o-1].id))
        if ids_to_delete:
            s.query(SmartPreset).filter(SmartPreset.user_id == m.from_user.id, SmartPreset.id.in_(ids_to_delete)).delete(synchronize_session=False)
            s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Удалено.", reply_markup=smart_settings_kb())
    await state.clear()

@dp.callback_query(F.data == "smart:clear")
async def smart_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Подтвердите очистку всех умных пресетов: ДА", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("smart:open")))
    await state.set_state(SmartPresetClearFSM.confirm); await safe_cq_answer(c)

@dp.message(SmartPresetClearFSM.confirm)
async def smart_clear_confirm(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if (m.text or "").strip().upper() == "ДА":
        with SessionLocal() as s:
            s.query(SmartPreset).filter_by(user_id=m.from_user.id).delete(); s.commit()
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Все умные пресеты удалены.", reply_markup=smart_settings_kb())
    else:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=smart_settings_kb())
    await state.clear()

# ====== SUBJECTS ======
def subjects_text_page(user_id: int, page: int = 1, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    with SessionLocal() as s:
        items = s.query(Subject).filter_by(user_id=user_id).order_by(Subject.id.asc()).all()
    if not items:
        return "Тем пока нет.", subjects_kb()
    total = len(items)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = min(total, start + per_page)
    slice_items = items[start:end]
    lines = ["Ваши темы:", ""]
    for i, x in enumerate(slice_items, start=start + 1):
        lines.append(f"Тема №{i} {code(x.title)}")
    rows = pager_row("subjects:show:", page, total_pages)
    rows += subjects_kb().inline_keyboard
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

def subjects_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Показать", callback_data="subjects:show:1")],
        [InlineKeyboardButton(text="➕ Добавить", callback_data="subjects:add"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data="subjects:edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="subjects:delete"),
         InlineKeyboardButton(text="🧹 Очистить", callback_data="subjects:clear")],
        *nav_row("settings:back")
    ])

@dp.callback_query(F.data == "subjects:open")
async def subjects_open(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    await c.message.edit_text("Темы:", reply_markup=subjects_kb()); await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("subjects:show"))
async def subjects_list(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    parts = c.data.split(":")
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    text, kb = subjects_text_page(c.from_user.id, page=page, per_page=10)
    await safe_edit_message(c.message, text, reply_markup=kb); await safe_cq_answer(c)

@dp.callback_query(F.data == "subjects:add")
async def subjects_add(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите название темы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
    await state.set_state(SubjectAddFSM.title); await safe_cq_answer(c)

@dp.message(SubjectAddFSM.title)
async def subjects_add_title(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    with SessionLocal() as s:
        s.add(Subject(user_id=m.from_user.id, title=(m.text or "").strip())); s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Тема добавлена.", reply_markup=subjects_kb()); await state.clear()

@dp.callback_query(F.data == "subjects:edit")
async def subjects_edit(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номер темы (например: 1):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
    await state.set_state(SubjectEditFSM.subject_id); await safe_cq_answer(c)

@dp.message(SubjectEditFSM.subject_id)
async def subjects_edit_pick(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if not (m.text or "").strip().isdigit():
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Нужен номер темы.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
        return
    ordinal = int((m.text or "").strip())
    with SessionLocal() as s:
        items = s.query(Subject).filter_by(user_id=m.from_user.id).order_by(Subject.id.asc()).all()
    chosen = (items[ordinal-1] if 1 <= ordinal <= len(items) else None)
    if not chosen:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный номер.", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
        return
    await state.update_data(subject_id=int(chosen.id))
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Новое название темы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
    await state.set_state(SubjectEditFSM.title)

@dp.message(SubjectEditFSM.title)
async def subjects_edit_save(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    with SessionLocal() as s:
        subj = s.query(Subject).filter_by(user_id=m.from_user.id, id=data["subject_id"]).first()
        if not subj:
            await ui_clear_prompts(state)
            await bot.send_message(m.chat.id, "Тема не найдена.", reply_markup=subjects_kb()); await state.clear(); return
        subj.title = (m.text or "").strip(); s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Тема обновлена.", reply_markup=subjects_kb()); await state.clear()

@dp.callback_query(F.data == "subjects:delete")
async def subjects_delete(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите номера тем для удаления (например: 2 4 5):", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
    await state.set_state(SubjectDeleteFSM.subject_id); await safe_cq_answer(c)

@dp.message(SubjectDeleteFSM.subject_id)
async def subjects_delete_do(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    try:
        ordinals = [int(x) for x in (m.text or "").replace(",", " ").split()]
    except Exception:
        await ui_clear_prompts(state)
        await ui_prompt(state, m.chat.id, "Неверный ввод. Пример: 1 2 3", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
        return
    with SessionLocal() as s:
        items = s.query(Subject).filter_by(user_id=m.from_user.id).order_by(Subject.id.asc()).all()
        ids_to_delete = []
        for o in ordinals:
            if 1 <= o <= len(items):
                ids_to_delete.append(items[o-1].id)
        if ids_to_delete:
            s.query(Subject).filter(Subject.user_id == m.from_user.id, Subject.id.in_(ids_to_delete)).delete(synchronize_session=False)
            s.commit()
    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, "Удалено.", reply_markup=subjects_kb()); await state.clear()

@dp.callback_query(F.data == "subjects:clear")
async def subjects_clear(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Подтвердите очистку тем: ДА", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("subjects:open")))
    await state.set_state(SubjectClearFSM.confirm); await safe_cq_answer(c)

@dp.message(SubjectClearFSM.confirm)
async def subjects_clear_confirm(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    if (m.text or "").strip().upper() == "ДА":
        with SessionLocal() as s: s.query(Subject).filter_by(user_id=m.from_user.id).delete(); s.commit()
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Все темы удалены.", reply_markup=subjects_kb())
    else:
        await ui_clear_prompts(state)
        await bot.send_message(m.chat.id, "Отменено.", reply_markup=subjects_kb())
    await state.clear()

# ====== CHECK NICKS (XLSX) ======
def after_xlsx_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 Выполнить проверку email", callback_data="check:verify_emails")],
        [InlineKeyboardButton(text="♻️ Скрыть", callback_data="ui:hide")]
    ])

def after_verify_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Запустить сендинг", callback_data="send:start")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="send:status"),
         InlineKeyboardButton(text="🛑 Стоп", callback_data="send:stop")],
        [InlineKeyboardButton(text="♻️ Скрыть", callback_data="ui:hide")]
    ])

@dp.message(F.text.in_(["📖 Проверка ников", "Проверка ников"]))
async def btn_check(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    await state.set_state(CheckNicksFSM.file)
    await ui_clear_prompts(state)  # на всякий случай чистим предыдущие подсказки
    await ui_prompt(
        state,
        m.chat.id,
        "Пришлите .xlsx файл для проверки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_row("ui:hide"))
    )

@dp.message(Command("check"))
async def cmd_check(m: types.Message, state: FSMContext):
    await btn_check(m, state)

@dp.message(F.text.regexp(r"(?i)проверка\s*ников"))
async def btn_check_regex(m: types.Message, state: FSMContext):
    await btn_check(m, state)

def pick_columns_via_smtp25(df: pd.DataFrame) -> Tuple[pd.DataFrame, Optional[str], Optional[str]]:
    seller_col: Optional[str] = None
    title_col: Optional[str] = None
    try:
        col_map = smtp25.detect_columns(df) or {}
        seller_col = col_map.get("seller_nick")
        title_col = col_map.get("title")
    except Exception:
        pass
    if not seller_col:
        for cand in ("seller_nick", "Имя продавца"):
            if cand in df.columns:
                seller_col = cand; break
    if not title_col:
        for cand in ("title", "Название", "Название товара"):
            if cand in df.columns:
                title_col = cand; break
    rename = {}
    if seller_col: rename[seller_col] = "seller_nick"
    if title_col: rename[title_col] = "title"
    return df.rename(columns=rename).copy(), seller_col, title_col

@dp.message(CheckNicksFSM.file, F.document)
async def on_xlsx_received(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): 
        return

    filename = (m.document.file_name or "").lower()
    if not filename.endswith(".xlsx"):
        await bot.send_message(m.chat.id, "Ожидается .xlsx файл.")
        return

    buf = BytesIO()
    f = await bot.get_file(m.document.file_id)
    await bot.download(f, destination=buf)
    LAST_XLSX_PER_CHAT[m.chat.id] = buf.getvalue()

    try:
        df = await asyncio.to_thread(pd.read_excel, BytesIO(LAST_XLSX_PER_CHAT[m.chat.id]))
        df, seller_col, _ = pick_columns_via_smtp25(df)

        if not seller_col and "seller_nick" not in df.columns:
            cols = ", ".join([str(c) for c in df.columns])
            await bot.send_message(
                m.chat.id,
                "Не удалось определить колонку с никами продавцов.\n"
                "Переименуйте столбец в «Имя продавца» или «seller_nick».\n\n"
                f"Найденные столбцы: {cols}"
            )
            return

        await bot.send_message(m.chat.id, f"Колонка ников: “{seller_col or 'seller_nick'}”")
        await asyncio.to_thread(prepare_smtp25_from_db, m.from_user.id)

        bases: List[str] = []
        seen: set[str] = set()
        bl = getattr(smtp25, "BLACKLIST_CACHE", set())

        for row in df.itertuples(index=False):
            nick = str(getattr(row, "seller_nick", "")).strip()
            if not nick:
                continue
            normalized = _normalize_nick_local(nick)
            if normalized in bl:
                continue
            parts = smtp25.extract_name_parts(nick)
            if not parts:
                continue
            first, last = parts
            if len(first) < 3 or (last and len(last) < 3):
                continue
            base = smtp25.generate_email(first, last)
            if base and base not in seen:
                seen.add(base)
                bases.append(base)

        BASES_PER_CHAT[m.chat.id] = bases
        if bases:
            for chunk in join_batches([code(b) for b in bases], 50):
                await bot.send_message(m.chat.id, chunk)
            await bot.send_message(m.chat.id, "Выполнено успешно✅", reply_markup=after_xlsx_kb())
        else:
            await bot.send_message(m.chat.id, "Не удалось распознать ни одного валидного ника.")
    except Exception as e:
        await bot.send_message(m.chat.id, f"Ошибка обработки XLSX: {e}")
    finally:
        # Важно: удаляем подсказку, показанную через ui_prompt, и выходим из состояния
        await ui_clear_prompts(state)
        await state.clear()

@dp.message(CheckNicksFSM.file)
async def ignore_non_xlsx(m: types.Message):
    pass

def verify_emails_from_df_for_user(user_id: int, df: pd.DataFrame) -> List[Dict[str, Any]]:
    domains = prepare_smtp25_from_db(user_id)
    _ensure_blacklist_loaded_once()
    try:
        smtp25.PROCESSED_NICKS_CACHE.clear()
    except Exception:
        smtp25.PROCESSED_NICKS_CACHE = set()

    df2, _, _ = pick_columns_via_smtp25(df)
    keep = [c for c in ["seller_nick", "title"] if c in df2.columns]
    if not keep:
        return []
    df2 = df2[keep].copy()

    results: List[Dict[str, Any]] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = max(1, getattr(smtp25, "THREADS", 10))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(smtp25.process_row, row, domains): idx for idx, row in df2.iterrows()}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                res = future.result()
                if res:
                    email_addr, seller_name = res
                    title = df2.at[idx, "title"] if "title" in df2.columns else ""
                    results.append({"email": email_addr, "seller_name": seller_name, "title": title})
            except Exception:
                continue
    return results

@dp.callback_query(F.data == "check:verify_emails")
async def verify_emails_btn(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    chat_id = c.message.chat.id
    xls = LAST_XLSX_PER_CHAT.get(chat_id)
    if not xls:
        await c.answer("Сначала загрузите XLSX через «Проверка ников».", show_alert=True); return

    status_msg = await bot.send_message(chat_id, "Проверка email выполняется…")
    try:
        df = await asyncio.to_thread(pd.read_excel, BytesIO(xls))
        results = await asyncio.to_thread(verify_emails_from_df_for_user, c.from_user.id, df)
        VERIFIED_ROWS_PER_CHAT[chat_id] = results or []

        if not results:
            await delete_message_safe(status_msg)
            hint = (
                "Не найдено ни одного валидного email.\n"
                "Проверьте:\n"
                "• колонку с никами (seller_nick/«Имя продавца»)\n"
                "• корректность Verify‑прокси в Настройках\n"
                "• список доменов"
            )
            await bot.send_message(chat_id, hint)
            return

        emails = [r["email"] for r in results]
        for chunk in join_batches([f"№{i} {code(e)}" for i, e in enumerate(emails, start=1)], 50):
            await bot.send_message(chat_id, chunk)

        await delete_message_safe(status_msg)
        await bot.send_message(chat_id, "Выполнено успешно ✅", reply_markup=after_verify_kb())
    except Exception as e:
        await delete_message_safe(status_msg)
        await bot.send_message(chat_id, f"Ошибка проверки email: {e}")

# ====== SEND (batch) ======
async def _quick_check_send_proxies(uid: int) -> str:
    await asyncio.to_thread(prepare_smtp25_from_db, uid)
    if not smtp25.SEND_PROXY_LIST:
        return "Нет send‑прокси."
    bad: List[str] = []
    for p in smtp25.SEND_PROXY_LIST:
        try:
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, p["host"], int(p["port"]), True, p.get("user"), p.get("password"))
            s.settimeout(5)
            s.connect(("smtp.gmail.com", 587))
            s.close()
        except Exception:
            bad.append(f"{p['host']}:{p['port']} (ID={p.get('id','?')})")
    if bad:
        return "Неработающие прокси:\n" + "\n".join(bad)
    return "✅ Все прокси валидны"

def _render_message(subject: str, template: str, seller_name: str, title: str) -> Tuple[str, str, str]:
    """
    Возвращает:
    - subject: тема после подстановок
    - body: тело с подстановками (для отправки письма)
    - body_for_log: тело для логов (без первой смысловой строки, если в шаблоне первой строкой была OFFER)
    """
    subj_in = subject or smtp25.get_random_subject()
    tmpl_in = template or smtp25.get_random_template()

    def repl(txt: str) -> str:
        if seller_name:
            txt = txt.replace("{SELLER}", seller_name).replace("SELLER", seller_name)
        else:
            txt = txt.replace("{SELLER}", "").replace("SELLER", "")
        return (txt
                .replace("{ITEM}", title or "")
                .replace("{OFFER}", title or "")
                .replace("OFFER", title or ""))

    subject_out = repl(subj_in).strip()
    body_out = repl(tmpl_in)

    # Определяем: была ли первая НЕПУСТАЯ строка шаблона про OFFER
    import re as _re
    tmpl_lines = (tmpl_in or "").splitlines()
    offer_first = False
    for ln in tmpl_lines:
        s = (ln or "").strip()
        if not s:
            continue
        # считаем, что это OFFER-строка, если в ней встречается токен OFFER/{OFFER}
        if _re.search(r'\{?OFFER\}?', s, flags=_re.I):
            offer_first = True
        break

    body_for_log = body_out
    if offer_first:
        # Удаляем первую НЕПУСТУЮ строку из уже сгенерированного тела
        body_lines = (body_out or "").splitlines()
        idx = next((i for i, l in enumerate(body_lines) if (l or "").strip()), None)
        if idx is not None:
            body_for_log = "\n".join(body_lines[:idx] + body_lines[idx + 1:]).lstrip("\n")

    return subject_out, body_out, body_for_log

async def _send_one(uid: int, to_email: str, subject: str, body: str) -> bool:
    await asyncio.to_thread(prepare_smtp25_from_db, uid)
    acc = smtp25.get_random_account()
    proxy = smtp25.get_next_proxy("send")
    if not acc or not proxy:
        await bot.send_message(uid, "Нет аккаунтов или send‑прокси. Добавьте их в Настройках.")
        return False
    def _sync() -> bool:
        try:
            smtp = smtp25.initialize_smtp(acc, proxy)
            if not smtp: return False
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            msg = MIMEMultipart()
            msg['From'] = f"{acc.get('name') or acc['email']} <{acc['email']}>"
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))
            smtp.sendmail(acc["email"], to_email, msg.as_string())
            try: smtp.quit()
            except Exception: pass
            return True
        except Exception:
            return False
    ok = await asyncio.to_thread(_sync)
    if not ok:
        await bot.send_message(uid, "Ошибка подключения при отправке (аккаунт/прокси).")
    return ok

async def log_send_ok(chat_id: int, subject: str, body: str, to_email: str, reply_to_message_id: Optional[int] = None):
    import re as _re

    def _clean(s: str) -> str:
        return _re.sub(r'^(re|fw|fwd)\s*:\s*', '', (s or '').strip(), flags=_re.I)

    subj_clean = _clean(subject or "")
    body_lines = (body or "").splitlines()
    body_for_log = ""
    if body_lines:
        first_clean = _clean(body_lines[0])
        if first_clean.lower() == subj_clean.lower():
            body_for_log = "\n".join(body_lines[1:]).lstrip()
        else:
            body_for_log = body or ""

    # В логе тема показывается, а из текста — первая строка убирается, если равна теме
    text = "Сообщение " + code(subject or "")
    if body_for_log:
        text += "\n" + code(body_for_log)
    text += f"\nуспешно отправлено пользователю {code(to_email)} ⚡"

    if reply_to_message_id:
        try:
            await bot.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text)

async def send_loop(uid: int, chat_id: int):
    SEND_STATUS[uid] = {"running": True, "sent": 0, "failed": 0, "total": 0, "cancel": False, "last_err": None}
    results = VERIFIED_ROWS_PER_CHAT.get(chat_id, [])
    SEND_STATUS[uid]["total"] = len(results)
    proxy_report = await _quick_check_send_proxies(uid)
    await bot.send_message(chat_id, proxy_report)
    vmin = int(get_setting(uid, "send_delay_min", str(smtp25.MIN_SEND_DELAY)))
    vmax = int(get_setting(uid, "send_delay_max", str(smtp25.MAX_SEND_DELAY)))
    for r in results:
        if SEND_STATUS[uid].get("cancel"): break
        email = r["email"]; seller_name = r.get("seller_name", ""); title = r.get("title", "")
        subject, body, body_for_log = _render_message(
            smtp25.get_random_subject(),
            smtp25.get_random_template(),
            seller_name or "",
            title or ""
        )
        ok = await _send_one(uid, email, subject, body)
        if ok:
            SEND_STATUS[uid]["sent"] += 1
            await log_send_ok(chat_id, subject, body_for_log, email)
        else:
            SEND_STATUS[uid]["failed"] += 1
            await bot.send_message(chat_id, f"Не удалось отправить пользователю {code(email)}")
        await asyncio.sleep(random.uniform(vmin, vmax))
    SEND_STATUS[uid]["running"] = False
    await bot.send_message(chat_id, "Сендинг остановлен ⏹" if SEND_STATUS[uid].get("cancel") else "Сендинг завершён ✅")
    
async def log_html_reply_ok(chat_id: int, to_email: str, html_str: str, reply_to_message_id: int):
    # Один лог: документ + подпись в одном сообщении
    caption = f"Ответ с HTML‑вложением успешно отправлен пользователю {code(to_email)} ⚡"
    try:
        # Правильный способ ответить на сообщение в aiogram v3/Telegram Bot API 7+
        reply_params = types.ReplyParameters(message_id=reply_to_message_id)
        await bot.send_document(
            chat_id=chat_id,
            document=_make_html_file(html_str),  # имя вида 1692549600s.html
            caption=caption,
            reply_parameters=reply_params
        )
        return
    except Exception:
        # Фолбэк: отправим как отдельное сообщение, но всё равно одним сообщением (документ + caption)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=_make_html_file(html_str),
                caption=caption
            )
            return
        except Exception:
            # Совсем аварийный фолбэк — короткая строка (без документа)
            try:
                await bot.send_message(chat_id, caption, reply_to_message_id=reply_to_message_id)
            except Exception:
                pass

async def log_text_reply_ok(chat_id: int, body: str, to_email: str, reply_to_message_id: int):
    # В логе только текст который отправили (без темы)
    text = code(body or "") + f"\nуспешно отправлено пользователю {code(to_email)} ⚡"
    try:
        await bot.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
    except Exception:
        await bot.send_message(chat_id, text)

@dp.callback_query(F.data == "send:start")
async def send_start_cb(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    uid = c.from_user.id; chat_id = c.message.chat.id
    if chat_id not in VERIFIED_ROWS_PER_CHAT or not VERIFIED_ROWS_PER_CHAT[chat_id]:
        await c.answer("Сначала выполните проверку email.", show_alert=True); return
    if uid in SEND_TASKS and not SEND_TASKS[uid].done():
        await c.answer("Сендинг уже запущен.", show_alert=True); return
    SEND_STATUS[uid] = {"running": True, "sent": 0, "failed": 0, "total": len(VERIFIED_ROWS_PER_CHAT[chat_id]), "cancel": False}
    SEND_TASKS[uid] = asyncio.create_task(send_loop(uid, chat_id))
    await c.message.answer("Сендинг запущен 🚀"); await safe_cq_answer(c)

@dp.callback_query(F.data == "send:status")
async def send_status_cb(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    st = SEND_STATUS.get(c.from_user.id)
    if not st:
        await c.answer("Сендинг не запускался.", show_alert=True); return
    await c.message.answer(
        "Статус: " + ("идёт" if st.get("running") else "остановлен") + "\n"
        f"Отправлено: {st.get('sent',0)}\n"
        f"Не отправлено: {st.get('failed',0)}\n"
        f"Всего: {st.get('total',0)}"
    )
    await safe_cq_answer(c)

@dp.callback_query(F.data == "send:stop")
async def send_stop_cb(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    uid = c.from_user.id
    t = SEND_TASKS.get(uid)
    if t and not t.done():
        SEND_STATUS[uid]["cancel"] = True
        await c.answer("Останавливаю…")
    else:
        await c.answer("Сендинг не запущен.", show_alert=True)

# ====== ONE‑OFF SEND ======
def onesend_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="onesend:cancel")]])

@dp.message(F.text.regexp(r"(?i)отправить\s*e-?mail"))
async def onesend_entry_btn(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await state.set_state(SingleSendFSM.to)
    await bot.send_message(m.chat.id, "Введите email получателя✍️", reply_markup=onesend_kb())

@dp.message(F.text == "✉️ Отправить email")
async def onesend_entry_exact(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await state.set_state(SingleSendFSM.to)
    await bot.send_message(m.chat.id, "Введите email получателя✍️", reply_markup=onesend_kb())

@dp.message(Command("send"))
async def cmd_send(m: types.Message, state: FSMContext):
    await onesend_entry_btn(m, state)

@dp.message(SingleSendFSM.to)
async def onesend_got_to(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    to = (m.text or "").strip()
    await delete_message_safe(m)
    if "@" not in to:
        await bot.send_message(m.chat.id, "Некорректный email.")
        await state.clear()
        return
    await state.update_data(to=to)
    await state.set_state(SingleSendFSM.body)
    await bot.send_message(m.chat.id, "Введите текст письма✍️", reply_markup=onesend_kb())

@dp.message(SingleSendFSM.body)
async def onesend_got_text(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    data = await state.get_data()
    await delete_message_safe(m)
    subject, body, body_for_log = _render_message(
        smtp25.get_random_subject(),
        (m.text or smtp25.get_random_template()),
        "",
        ""
    )
    ok = await _send_one(m.from_user.id, data.get("to"), subject, body)
    if ok:
        await log_send_ok(m.chat.id, subject, body_for_log, data.get("to"))
    else:
        await bot.send_message(m.chat.id, "Ошибка отправки.")
    await state.clear()

@dp.callback_query(F.data == "onesend:cancel")
async def onesend_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_message(c.message, "Отменено."); await safe_cq_answer(c)

# ====== Reply на входящее ======
async def send_email_via_account(uid: int, acc_id: int, to_email: str, subject: str, body: str, html: bool = False, photo_bytes: Optional[bytes] = None, photo_name: Optional[str] = None, sender_name_override: Optional[str] = None) -> bool:
    await asyncio.to_thread(prepare_smtp25_from_db, uid)
    with SessionLocal() as s:
        acc = s.query(Account).filter_by(user_id=uid, id=acc_id).first()
    if not acc:
        await bot.send_message(uid, "Аккаунт не найден."); return False
    proxy = smtp25.get_next_proxy("send")
    if not proxy:
        await bot.send_message(uid, "Нет send‑прокси."); return False

    def _sync() -> bool:
        try:
            smtp = smtp25.initialize_smtp({"email": acc.email, "password": acc.password, "name": acc.display_name}, proxy)
            if not smtp: return False
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            msg = MIMEMultipart()
            display_name = (sender_name_override or acc.display_name or acc.email).strip()
            msg['From'] = f"{display_name} <{acc.email}>"
            msg['To'] = to_email
            msg['Subject'] = subject or ""
            subtype = 'html' if html else 'plain'
            msg.attach(MIMEText(body or "", subtype))
            if photo_bytes:
                from email.mime.image import MIMEImage
                img = MIMEImage(photo_bytes, name=photo_name or "image.jpg")
                img.add_header('Content-Disposition', 'attachment', filename=photo_name or "image.jpg")
                msg.attach(img)
            smtp.sendmail(acc.email, to_email, msg.as_string())
            try: smtp.quit()
            except Exception: pass
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_sync)
    
def reply_button_kb(caption: str = "✉️ Ответить") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=caption, callback_data="reply:msg")]])

async def _mark_replied(chat_id: int, src_tg_mid: int):
    # Меняем кнопку под исходным логом на “✍️ Написать ещё”
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=src_tg_mid,
            reply_markup=reply_button_kb("✍️ Написать ещё")
        )
    except Exception:
        pass


@dp.callback_query(F.data == "reply:msg")
async def reply_msg_cb(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    tg_mid = c.message.message_id
    with SessionLocal() as s:
        row = (
            s.query(IncomingMessage)
            .filter_by(user_id=c.from_user.id, tg_message_id=tg_mid)
            .order_by(IncomingMessage.id.desc())
            .first()
        )
    if not row:
        await c.answer("Не нашёл данные письма", show_alert=True); return

    await state.set_state(ReplyFSM.compose)
    await state.update_data(
        acc_id=int(row.account_id),
        to=row.from_email,
        subject=f"Re: {row.subject or ''}",
        src_tg_mid=int(tg_mid)
    )

    # Показываем подсказку через ui_prompt, чтобы потом её можно было убрать
    await ui_prompt(
        state,
        c.message.chat.id,
        "Введите сообщение✍️",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📬 Отправить пресет", callback_data="reply:use_preset"),
             InlineKeyboardButton(text="5️⃣ Отправить HTML", callback_data="reply:use_html")],
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="reply:cancel")]
        ])
    )
    await safe_cq_answer(c)

@dp.callback_query(F.data.startswith("presets:view:"))
async def presets_view_cb(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    _, _, pid, back_cb = c.data.split(":", 3)
    with SessionLocal() as s:
        p = s.query(Preset).filter_by(user_id=c.from_user.id, id=int(pid)).first()
    if not p:
        await c.answer("Не найдено", show_alert=True); return

    cur_state = await state.get_state()
    if cur_state in {ReplyFSM.compose, ReplyFSM.html}:
        data = await state.get_data()
        acc_id = int(data["acc_id"])
        to_email = data["to"]
        subj = data.get("subject") or "Re:"
        body = (p.body or "").strip()
        is_html = (cur_state == ReplyFSM.html)

        ok = await send_email_via_account(
            c.from_user.id, acc_id, to_email, subj, body, html=is_html
        )
        src_mid = int(data.get("src_tg_mid", 0)) if data else 0

        if ok and src_mid:
            await _mark_replied(c.message.chat.id, src_mid)
            # логирование ответов:
            if is_html:
                await log_html_reply_ok(c.message.chat.id, to_email, body, reply_to_message_id=src_mid)
            else:
                await log_text_reply_ok(c.message.chat.id, body, to_email, reply_to_message_id=src_mid)
        elif not ok:
            if src_mid:
                await bot.send_message(c.message.chat.id, "Ошибка отправки ❌", reply_to_message_id=src_mid)
            else:
                await bot.send_message(c.message.chat.id, "Ошибка отправки ❌")

        try:
            await delete_message_safe(c.message)
        except Exception:
            pass
        await state.clear()
        await safe_cq_answer(c)
        return

    await safe_edit_message(
        c.message,
        code((p.body or "").strip()),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)]])
    )
    await safe_cq_answer(c)

@dp.callback_query(F.data == "reply:use_preset")
async def reply_use_preset(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await safe_edit_message(c.message, "Выберите пресет:", reply_markup=presets_inline_kb(c.from_user.id, back_cb="reply:back")); await safe_cq_answer(c)


@dp.callback_query(F.data == "reply:use_html")
async def reply_use_html(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c):
        return

    data = await state.get_data()
    if not data:
        await c.answer("Нет контекста ответа.", show_alert=True)
        return

    html = get_last_html(c.message.chat.id)
    if not html:
        await safe_edit_message(
            c.message,
            "Нет сгенерированного HTML. Откройте «🧾 HTML‑шаблоны», создайте шаблон и повторите.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="reply:cancel")]])
        )
        await safe_cq_answer(c)
        return

    acc_id = int(data["acc_id"])
    to_email = data["to"]
    subj = (data.get("subject") or "").strip()
    src_mid = int(data.get("src_tg_mid", 0) or 0)

    # Имя отправителя для шаблонов — из config
    sender_name = getattr(config, "SENDER_DISPLAY_NAME_FOR_TEMPLATES", "Willhaben Transaktion")

    ok = await send_email_via_account(
        c.from_user.id, acc_id, to_email, subj, html, html=True,
        sender_name_override=sender_name
    )

    if ok:
        if src_mid:
            await _mark_replied(c.message.chat.id, src_mid)
            # Один лог: файл + подпись внутри одного reply к входящему
            await log_html_reply_ok(c.message.chat.id, to_email, html, reply_to_message_id=src_mid)
        # Удаляем меню с кнопками; сообщение "Отправлено ✅" не показываем
        try:
            await delete_message_safe(c.message)
        except Exception:
            pass
    else:
        if src_mid:
            await c.message.answer("Ошибка отправки ❌", reply_to_message_id=src_mid)
        else:
            await safe_edit_message(c.message, "Ошибка отправки ❌")

    await state.clear()
    await safe_cq_answer(c)

@dp.callback_query(F.data == "reply:back")
async def reply_back(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await state.set_state(ReplyFSM.compose)
    await safe_edit_message(c.message, "Введите сообщение✍️", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📬 Отправить пресет", callback_data="reply:use_preset"),
         InlineKeyboardButton(text="5️⃣ Отправить HTML", callback_data="reply:use_html")],
        [InlineKeyboardButton(text="🚫 Отмена", callback_data="reply:cancel")]
    ])); await safe_cq_answer(c)

@dp.callback_query(F.data == "reply:cancel")
async def reply_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_message(c.message, "Отменено.", reply_markup=None); await safe_cq_answer(c)

@dp.message(ReplyFSM.compose)
async def reply_compose_text_or_photo(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)

    # Чистим все служебные подсказки перед обработкой ввода
    await ui_clear_prompts(state)

    data = await state.get_data()
    photo_bytes = None; photo_name = None
    body = ""
    if m.photo:
        ph = m.photo[-1]
        f: File = await bot.get_file(ph.file_id)
        buf = BytesIO()
        await bot.download(f, destination=buf)
        photo_bytes = buf.getvalue()
        photo_name = "image.jpg"
        body = m.caption or ""
    else:
        body = m.text or ""
    subj = data.get("subject") or "Re:"
    src_mid = int(data.get("src_tg_mid", 0)) if data else 0
    ok = await send_email_via_account(m.from_user.id, int(data["acc_id"]), data["to"], subj, body, html=False, photo_bytes=photo_bytes, photo_name=photo_name)
    if ok:
        if src_mid:
            await _mark_replied(m.chat.id, src_mid)
            await log_text_reply_ok(m.chat.id, body, data.get("to"), reply_to_message_id=src_mid)
    else:
        if src_mid:
            await bot.send_message(m.chat.id, "Ошибка отправки ❌", reply_to_message_id=src_mid)
        else:
            await bot.send_message(m.chat.id, "Ошибка отправки ❌")
    await state.clear()



# ====== QUICK ADD ======
def quickadd_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Одно имя", callback_data="quickadd:one"),
         InlineKeyboardButton(text="1️⃣2️⃣3️⃣4️⃣ Разные имена", callback_data="quickadd:many")],
        *nav_row("ui:hide")
    ])

def quickadd_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="quickadd:cancel")]])

@dp.message(F.text == "➕ Быстрое добавление")
async def quickadd_start(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await state.set_state(QuickAddFSM.mode)
    await bot.send_message(m.chat.id, "Выберите опцию:", reply_markup=quickadd_menu_kb())

@dp.message(Command("quickadd"))
async def cmd_quickadd(m: types.Message, state: FSMContext):
    await quickadd_start(m, state)

@dp.callback_query(F.data == "quickadd:one")
async def quickadd_one(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await state.update_data(mode="one")
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Введите отображаемое имя и фамилию. Например: Jessy Jackson ✍️", reply_markup=quickadd_cancel_kb()); await state.set_state(QuickAddFSM.name); await safe_cq_answer(c)

@dp.callback_query(F.data == "quickadd:many")
async def quickadd_many(c: types.CallbackQuery, state: FSMContext):
    if not await ensure_approved(c): return
    await state.update_data(mode="many")
    await ui_clear_prompts(state); await delete_message_safe(c.message)
    await ui_prompt(state, c.message.chat.id, "Отправьте данные текстом:\n\nemail1:password1:Имя Фамилия\nemail2:password2:Имя Фамилия", reply_markup=quickadd_cancel_kb()); await state.set_state(QuickAddFSM.lines); await safe_cq_answer(c)

@dp.message(QuickAddFSM.name)
async def quickadd_got_name(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await state.update_data(name=(m.text or "").strip())
    await ui_clear_prompts(state)
    await ui_prompt(state, m.chat.id, "Теперь отправьте строки вида:\nemail:password", reply_markup=quickadd_cancel_kb())
    await state.set_state(QuickAddFSM.lines)
    
def parse_lines_one(text: str) -> List[Tuple[str, str]]:
    """
    Формат:
      email:password
    По одной паре на строку.
    """
    rows: List[Tuple[str, str]] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ":" not in ln:
            continue
        email, password = ln.split(":", 1)
        email = email.strip()
        password = password.strip()
        if email and password:
            rows.append((email, password))
    return rows

def parse_lines_many(text: str) -> List[Tuple[str, str, str]]:
    """
    Формат:
      email:password:Имя Фамилия
    По одной запись на строку.
    """
    rows: List[Tuple[str, str, str]] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(":", 2)
        if len(parts) != 3:
            continue
        email, password, name = (p.strip() for p in parts)
        if email and password:
            rows.append((email, password, name))
    return rows

def parse_proxy_lines(text: str) -> List[Tuple[str, int, str, str]]:
    """
    Формат:
      host:port:login:password
    Пароль может содержать двоеточия (склеиваем хвост).
    Возвращает список (host, port, login, password).
    Невалидные строки пропускаются.
    """
    out: List[Tuple[str, int, str, str]] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(":")
        if len(parts) < 4:
            continue
        host = parts[0].strip()
        port_str = parts[1].strip()
        user = parts[2].strip()
        pwd = ":".join(parts[3:]).strip()
        if not host or not port_str.isdigit() or not user or not pwd:
            continue
        out.append((host, int(port_str), user, pwd))
    return out

@dp.message(QuickAddFSM.lines)
async def quickadd_lines_text(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    data = await state.get_data()
    mode = data.get("mode")
    added = 0; total = 0
    if mode == "one":
        name = data.get("name", "") or ""
        pairs = parse_lines_one(m.text or "")
        total = len(pairs)
        with SessionLocal() as s:
            for email_addr, password in pairs:
                try:
                    add_account(s, m.from_user.id, name or email_addr.split("@")[0], email_addr, password, auto_bind_proxy=True)
                    acc = s.query(Account).filter_by(user_id=m.from_user.id, email=email_addr).order_by(Account.id.desc()).first()
                    if acc:
                        acc.active = True
                        mark_quick_add_first_pass(m.from_user.id, acc.id)  # NEW: помечаем как "быстрое добавление"
                    # сбрасываем одноразовые флаги логов
                    key = (m.from_user.id, email_addr)
                    START_LOG_SENT.pop(key, None)
                    ERROR_LOG_SENT.pop(key, None)
                    added += 1
                except Exception:
                    pass
            s.commit()
        await _ensure_imap_started_for_user(m.from_user.id, m.chat.id)
    else:
        triples = parse_lines_many(m.text or "")
        total = len(triples)
        with SessionLocal() as s:
            for email_addr, password, name in triples:
                try:
                    add_account(s, m.from_user.id, name or email_addr.split("@")[0], email_addr, password, auto_bind_proxy=True)
                    acc = s.query(Account).filter_by(user_id=m.from_user.id, email=email_addr).order_by(Account.id.desc()).first()
                    if acc:
                        acc.active = True
                        mark_quick_add_first_pass(m.from_user.id, acc.id)  # NEW: помечаем как "быстрое добавление"
                    # сбрасываем одноразовые флаги логов
                    key = (m.from_user.id, email_addr)
                    START_LOG_SENT.pop(key, None)
                    ERROR_LOG_SENT.pop(key, None)
                    added += 1
                except Exception:
                    pass
            s.commit()
        await _ensure_imap_started_for_user(m.from_user.id, m.chat.id)

    await ui_clear_prompts(state)
    await bot.send_message(m.chat.id, f"Добавлено аккаунтов: {added} из {total}", reply_markup=emails_menu_kb())
    await state.clear()

# ====== FALLBACK кнопки (текст) ======
@dp.message(F.text.regexp(r"(?i)\bпроверка\s+ников\b"))
async def fallback_btn_check(m: types.Message, state: FSMContext):
    if not await ensure_approved(m): return
    await btn_check(m, state)

@dp.message(F.text == "🧾 HTML-шаблоны")
async def open_html_templates_menu(m: types.Message):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    await bot.send_message(m.chat.id, "Выберите шаблон:", reply_markup=html_menu_kb())

# ====== IMAP helpers ======
def resolve_imap_host(email_addr: str) -> str:
    domain = (email_addr.split("@", 1)[1] if "@" in email_addr else "").lower()
    if domain in IMAP_HOST_MAP:
        return IMAP_HOST_MAP[domain]
    return f"imap.{domain}" if domain else "imap.gmail.com"

def _decode_header(s: Optional[str]) -> str:
    if not s: return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s

def _extract_body(msg) -> str:
    text_parts = []
    html_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                text_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(re.sub(r"<[^>]+>", " ", text))
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/plain":
                text_parts.append(text)
            else:
                html_parts.append(re.sub(r"<[^>]+>", " ", text))
        except Exception:
            pass
    body = "\n".join(text_parts) if text_parts else "\n".join(html_parts)
    body = re.sub(r"\s+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:3500]

class SocksIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int = IMAP_PORT_SSL, proxy: dict | None = None, timeout: int = IMAP_TIMEOUT, ssl_context: Optional[ssl.SSLContext] = None):
        self._proxy = proxy or {}
        self._timeout = timeout
        self._ssl_context = ssl_context or ssl.create_default_context()
        imaplib.IMAP4.__init__(self, host="", port=port, timeout=timeout)
        self.open(host, port)
    def open(self, host: str, port: int):
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, self._proxy["host"], int(self._proxy["port"]), True, self._proxy.get("user"), self._proxy.get("password"))
        s.settimeout(self._timeout)
        s.connect((host, port))
        ssock = self._ssl_context.wrap_socket(s, server_hostname=host)
        self.sock = ssock
        self.file = self.sock.makefile("rb")
        
def _connect_verify_with_retries(host: str, timeout: int, attempts: int = 3) -> tuple[imaplib.IMAP4_SSL | None, str]:
    """
    Для rotating-прокси: пробуем несколько быстрых подключений подряд,
    чтобы получить новый выходной IP и обойти случайные SSL-обрывы.
    """
    last_err = None
    for i in range(max(1, attempts)):
        try:
            proxy = smtp25.get_next_proxy("verify")
            imap = SocksIMAP4SSL(host, IMAP_PORT_SSL, proxy=proxy, timeout=timeout)
            return imap, f"via verify {proxy.get('host')}:{proxy.get('port')} (try {i+1})"
        except Exception as e:
            last_err = e
            # небольшая пауза, чтобы прокси успел "провернуть" IP
            time.sleep(0.25 + 0.25 * i)
    return None, f"via direct (verify failed: {type(last_err).__name__ if last_err else 'unknown'})"

# === СИНХРОННЫЙ IMAP-ФЕТЧ В ПОТОКЕ (verify/direct), UNSEEN + авто‑прочитывание ===
def _sync_imap_fetch(user_id: int, acc, timeout: int) -> tuple[list[dict], str, bool]:
    host = resolve_imap_host(acc.email)

    # быстрые ретраи на verify-прокси
    imap, via_descr = _connect_verify_with_retries(host, timeout, attempts=3)

    # строгий режим (опционально): чтобы вообще не уходить на прямое
    strict_verify = (get_setting(user_id, "verify_strict", "0") == "1")

    if imap is None:
        if strict_verify:
            raise RuntimeError("verify proxy required but failed")
        # фоллбэк на прямое подключение
        imap = imaplib.IMAP4_SSL(host, timeout=timeout)
        via_descr = "via direct"

    # login/select: если SSL сорвётся именно тут — один раз попробуем ещё раз через verify
    try:
        imap.login(acc.email, acc.password)
        typ, _ = imap.select("INBOX")
    except ssl.SSLError:
        try:
            try: imap.logout()
            except Exception: pass
        except Exception:
            pass
        # повторная попытка: ещё одно verify-подключение (вдруг новый IP)
        imap2, via2 = _connect_verify_with_retries(host, timeout, attempts=2)
        if imap2 is not None:
            imap = imap2
            via_descr = via2
            imap.login(acc.email, acc.password)
            typ, _ = imap.select("INBOX")
        else:
            if strict_verify:
                raise
            imap = imaplib.IMAP4_SSL(host, timeout=timeout)
            via_descr = "via direct (verify SSL failed)"
            imap.login(acc.email, acc.password)
            typ, _ = imap.select("INBOX")

    if typ != "OK":
        raise RuntimeError("IMAP select INBOX failed")

    def _connect_via_verify():
        proxy = smtp25.get_next_proxy("verify")
        if not proxy:
            return None, "via direct (no verify proxy)"
        imap = SocksIMAP4SSL(host, IMAP_PORT_SSL, proxy=proxy, timeout=timeout)
        return imap, f"via verify {proxy.get('host')}:{proxy.get('port')}"

    imap, via_descr = None, ""
    try:
        imap, via_descr = _connect_via_verify()
    except Exception:
        imap = None
        via_descr = "via direct (verify failed)"

    if imap is None:
        # прямое подключение — обязательно с таймаутом
        imap = imaplib.IMAP4_SSL(host, timeout=timeout)
        via_descr = "via direct"

    # login/select
    imap.login(acc.email, acc.password)
    typ, _ = imap.select("INBOX")
    if typ != "OK":
        raise RuntimeError("IMAP select INBOX failed")

    # Ищем все непрочитанные
    typ, data = imap.uid("search", None, "UNSEEN")
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ}")

    uid_bytes = (data[0] or b"")
    unseen_uids = [u for u in uid_bytes.split() if u]

    # Решаем, нужен ли "тихий первый проход"
    do_silent_first = QUICK_ADD_FIRST_PASS.pop((user_id, acc.id), False)
    if do_silent_first:
        # На всякий случай проверим, что это действительно первый наш запуск (нет сохранённых логов входящих для этого аккаунта)
        with SessionLocal() as s:
            has_any_saved = s.query(IncomingMessage.id).filter_by(account_id=acc.id).first() is not None
        if not has_any_saved and unseen_uids:
            for u in unseen_uids:
                try:
                    imap.uid("store", u, "+FLAGS", r"(\Seen)")
                except Exception:
                    pass
            try:
                imap.logout()
            except Exception:
                pass
            # Ничего не публикуем — тихий пропуск только для "быстрого добавления"
            return [], via_descr, True
        # Если письма уже когда-то сохранялись — ведём себя как обычно (публикуем)

    # Обычный режим: получить письма и пометить Seen
    messages: list[dict] = []
    for u in unseen_uids:
        typ, msg_data = imap.uid("fetch", u, "(RFC822)")
        if typ != "OK" or not msg_data:
            continue
        part = next((x for x in msg_data if isinstance(x, tuple) and x and isinstance(x[1], (bytes, bytearray))), None)
        if not part:
            continue
        # После получения — пометить прочитанным, чтобы не повторялось
        try:
            imap.uid("store", u, "+FLAGS", r"(\Seen)")
        except Exception:
            pass

        msg = message_from_bytes(part[1])
        from_raw = msg.get("From", "")
        from_name, from_email = parseaddr(from_raw)
        subject = _decode_header(msg.get("Subject"))
        body = _extract_body(msg)
        messages.append({
            "uid": u.decode(),
            "from_name": from_name or "",
            "from_email": from_email or "",
            "subject": subject,
            "body": body,
        })

    try:
        imap.logout()
    except Exception:
        pass

    return messages, via_descr, True

# === Обёртка IMAP: лог старта только после реального подключения, лог ошибки при неудаче ===
async def fetch_and_post_new_mails(user_id: int, acc: Account, chat_id: int) -> int:
    key = (user_id, acc.email)

    def _make_incoming_html(from_name: str, from_email: str, subject: str, body: str) -> bytes:
        def esc(s: str) -> str:
            return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{esc(subject)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 16px; line-height: 1.45; }}
  .hdr {{ margin-bottom: 16px; }}
  .lbl {{ color: #666; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; }}
</style>
</head>
<body>
  <div class="hdr"><span class="lbl">От:</span> {esc(from_name)} &lt;{esc(from_email)}&gt;</div>
  <div class="hdr"><span class="lbl">Тема:</span> {esc(subject)}</div>
  <hr>
  <pre>{esc(body)}</pre>
</body>
</html>"""
        return html.encode("utf-8")

    try:
        await asyncio.to_thread(prepare_smtp25_from_db, user_id)
        messages, via_descr, connected = await asyncio.to_thread(_sync_imap_fetch, user_id, acc, IMAP_TIMEOUT)

        if connected and not START_LOG_SENT.get(key):
            START_LOG_SENT[key] = True
            # После первого успешного коннекта разрешим снова показать ошибку при будущих фейлах
            ERROR_LOG_SENT.pop(key, None)
            try:
                await bot.send_message(chat_id, f"Поток для {code(acc.email)} запущен⚡")
            except Exception:
                pass

        new_count = 0
        for m in messages:
            text = (
                f"⚡️ Получено сообщение на {code(acc.email)} от {code(m['from_email'])}\n"
                f"({code(m['from_name'])} &lt;{code(m['from_email'])}&gt;)\n\n"
                f"Тема:\n{code(m['subject'])}\n\n"
                f"Текст:\n{code(m['body'])}"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✉️ Ответить", callback_data="reply:msg")]]
            )
            tg_msg = await bot.send_message(chat_id, text, reply_markup=kb)

            # HTML-вложение как документ, ответом на лог входящего
            try:
                html_bytes = _make_incoming_html(m["from_name"], m["from_email"], m["subject"], m["body"])
                fname = gen_numeric_html_filename()
                buf_file = types.BufferedInputFile(html_bytes, filename=fname)
                await bot.send_document(chat_id, buf_file, reply_to_message_id=tg_msg.message_id)
            except Exception:
                pass

            with SessionLocal() as s:
                s.add(IncomingMessage(
                    user_id=user_id,
                    account_id=acc.id,
                    uid=m["uid"],
                    from_name=m["from_name"],
                    from_email=m["from_email"],
                    subject=m["subject"],
                    body=m["body"],
                    tg_message_id=tg_msg.message_id
                ))
                s.commit()

            try:
                await bot.pin_chat_message(chat_id, tg_msg.message_id, disable_notification=True)
            except Exception:
                pass

            new_count += 1

        # Успех: активен, ошибки нет, сбрасываем backoff
        IMAP_STATUS.setdefault(user_id, {}).setdefault("accounts", {}).setdefault(acc.email, {})
        IMAP_STATUS[user_id]["accounts"][acc.email].update({
            "active": True,
            "last_ok": str(int(time.time())),
            "last_err": None,
            "retries": 0,
            "retry_at": 0,
        })
        return new_count

    except Exception as e:
        # Ошибка: снимаем active, сохраняем причину и назначаем backoff
        err_type = type(e).__name__
        err_msg = str(e)
        IMAP_STATUS.setdefault(user_id, {}).setdefault("accounts", {}).setdefault(acc.email, {})
        st = IMAP_STATUS[user_id]["accounts"][acc.email]
        retries = int(st.get("retries", 0)) + 1
        backoff = min(600, 2 ** min(retries, 6)) + random.uniform(0, 1)  # до 10 минут
        retry_at = time.time() + backoff
        st.update({
            "active": False,
            "last_ok": "",
            "last_err": f"{err_type}: {err_msg}"[:300],
            "retries": retries,
            "retry_at": retry_at,
        })

        if not ERROR_LOG_SENT.get(key):
            ERROR_LOG_SENT[key] = True
            try:
                await bot.send_message(chat_id, f"Ошибка подключения потока для {code(acc.email)}: {code(err_type)}: {code(err_msg[:180])}")
            except Exception:
                pass
        return 0

# ====== IMAP loop + UI (/read, /stop, /status) ======
async def imap_loop(user_id: int, chat_id: int):
    # переносим прошлый стейт accounts/chat_id, отмечаем как running
    IMAP_STATUS[user_id] = {
        "running": True,
        "last_ok": IMAP_STATUS.get(user_id, {}).get("last_ok"),
        "last_err": IMAP_STATUS.get(user_id, {}).get("last_err"),
        "accounts": IMAP_STATUS.get(user_id, {}).get("accounts", {}),
        "chat_id": IMAP_STATUS.get(user_id, {}).get("chat_id", chat_id),
    }

    sem = asyncio.Semaphore(IMAP_MAX_PARALLEL)

    while True:
        try:
            chat_id = IMAP_STATUS.get(user_id, {}).get("chat_id", chat_id)

            # Активные в БД
            with SessionLocal() as s:
                accounts = s.query(Account).filter_by(user_id=user_id, active=True).all()

            # Пропускаем те, кто на backoff
            now = time.time()
            to_poll: list[Account] = []
            acc_state = IMAP_STATUS.setdefault(user_id, {}).setdefault("accounts", {})
            for a in accounts:
                st = acc_state.get(a.email, {})
                retry_at = float(st.get("retry_at", 0) or 0)
                if retry_at and retry_at > now:
                    continue  # ещё рано пробовать
                to_poll.append(a)

            if not to_poll:
                await asyncio.sleep(READ_INTERVAL)
                continue

            async def _run_one(a: Account):
                async with sem:
                    return await fetch_and_post_new_mails(user_id, a, chat_id)

            tasks = [asyncio.create_task(_run_one(a)) for a in to_poll]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            total_new = 0
            for r in results:
                if isinstance(r, Exception):
                    continue
                total_new += int(r or 0)

            IMAP_STATUS[user_id]["last_ok"] = f"+{total_new} new" if total_new else "нет новых"
            await asyncio.sleep(READ_INTERVAL)

        except asyncio.CancelledError:
            IMAP_STATUS[user_id]["running"] = False
            raise
        except Exception as e:
            IMAP_STATUS[user_id]["last_err"] = str(e)
            await asyncio.sleep(READ_INTERVAL)

def _get_user_accounts(uid: int) -> List[Account]:
    with SessionLocal() as s:
        return list(s.query(Account).filter_by(user_id=uid).order_by(Account.id.asc()).all())

def _split_active_inactive(accounts: List[Account]) -> Tuple[List[Account], List[Account]]:
    act = [a for a in accounts if a.active]
    ina = [a for a in accounts if not a.active]
    return act, ina
    
def _runtime_is_active(uid: int, email: str) -> bool:
    """
    Истина только если:
    - общий IMAP‑луп запущен (running == True)
    - и конкретный email помечен active == True.
    """
    root = IMAP_STATUS.get(uid, {}) or {}
    if root.get("running") is not True:
        return False
    st_all = root.get("accounts", {})
    return st_all.get(email, {}).get("active") is True

def _kb_read_menu(uid: int) -> InlineKeyboardMarkup:
    """
    Меню запуска: показываем только те аккаунты, которые НЕ активны по runtime.
    """
    accounts = _get_user_accounts(uid)
    need_start = [a for a in accounts if not _runtime_is_active(uid, a.email)]
    rows: list[list[InlineKeyboardButton]] = []
    for i, a in enumerate(need_start, start=1):
        rows.append([InlineKeyboardButton(text=f"E‑mail №{i}: {a.email}", callback_data=f"imap:start:{a.id}")])
    rows.append([InlineKeyboardButton(text="Запустить все потоки", callback_data="imap:start_all")])
    rows.append([InlineKeyboardButton(text="Скрыть", callback_data="ui:hide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _kb_stop_menu(uid: int) -> InlineKeyboardMarkup:
    """
    Меню остановки: показываем только те аккаунты, которые активны по runtime.
    """
    accounts = _get_user_accounts(uid)
    act_runtime = [a for a in accounts if _runtime_is_active(uid, a.email)]
    rows: list[list[InlineKeyboardButton]] = []
    for i, a in enumerate(act_runtime, start=1):
        rows.append([InlineKeyboardButton(text=f"E‑mail №{i}: {a.email}", callback_data=f"imap:stop:{a.id}")])
    rows.append([InlineKeyboardButton(text="Остановить все потоки", callback_data="imap:stop_all")])
    rows.append([InlineKeyboardButton(text="Скрыть", callback_data="ui:hide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _ensure_imap_stopped_for_user(uid: int):
    t = IMAP_TASKS.get(uid)
    if t and not t.done():
        t.cancel()
        try:
            await t
        except Exception:
            pass
    # Глобально луп остановлен
    IMAP_STATUS.setdefault(uid, {})
    IMAP_STATUS[uid]["running"] = False
    # И ВСЕМ аккаунтам в runtime ставим active=False
    IMAP_STATUS[uid].setdefault("accounts", {})
    for em, st in list(IMAP_STATUS[uid]["accounts"].items()):
        try:
            st["active"] = False
        except Exception:
            IMAP_STATUS[uid]["accounts"][em] = {"active": False}

@dp.message(Command("read"))
async def cmd_read(m: types.Message):
    if not await ensure_approved(m): 
        return
    await delete_message_safe(m)
    uid = m.from_user.id
    accounts = _get_user_accounts(uid)
    if not accounts:
        await bot.send_message(m.chat.id, "Аккаунтов не найдено.")
        return

    # Если есть хоть один аккаунт, который ещё не активен по runtime — надо предлагать запуск
    need_start_exists = any(not _runtime_is_active(uid, a.email) for a in accounts)
    text = "Нажмите на E‑mail для запуска потока чтения:" if need_start_exists else "Все потоки уже запущены."
    await bot.send_message(m.chat.id, text, reply_markup=_kb_read_menu(uid))

@dp.message(Command("stop"))
async def cmd_stop(m: types.Message):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    uid = m.from_user.id
    accounts = _get_user_accounts(uid)
    act, _ = _split_active_inactive(accounts)
    if not accounts:
        await bot.send_message(m.chat.id, "Аккаунтов не найдено."); return
    text = "Нажмите на E‑mail для остановки потока чтения:" if act else "Нет активных потоков."
    await bot.send_message(m.chat.id, text, reply_markup=_kb_stop_menu(uid))

def _status_text(uid: int) -> str:
    accounts = _get_user_accounts(uid)
    if not accounts:
        return "Аккаунтов не найдено."

    root = IMAP_STATUS.get(uid, {}) or {}
    running = root.get("running") is True
    st_all = root.get("accounts", {}) if root else {}

    lines: list[str] = []
    for a in accounts:
        acc_st = st_all.get(a.email, {}) if st_all else {}
        is_active = running and (acc_st.get("active") is True)
        lines.append(f"{a.email} {'активен ✅' if is_active else 'неактивен ❌'}")
    return "\n".join(lines)

@dp.message(Command("status"))
async def cmd_status(m: types.Message):
    if not await ensure_approved(m): return
    await delete_message_safe(m)
    await bot.send_message(m.chat.id, _status_text(m.from_user.id), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Скрыть", callback_data="ui:hide")]]))

@dp.callback_query(F.data.startswith("imap:start:"))
async def imap_start_one(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    uid = c.from_user.id
    acc_id = int(c.data.split(":")[2])
    with SessionLocal() as s:
        acc = s.query(Account).filter_by(user_id=uid, id=acc_id).first()
        if not acc:
            await c.answer("Аккаунт не найден", show_alert=True); return
        acc.active = True
        s.commit()
        chat_id = c.message.chat.id
    await _ensure_imap_started_for_user(uid, chat_id)
    # Больше не логируем "Поток ... запущен" тут — лог пойдёт только после реального подключения в fetch_and_post_new_mails
    await safe_edit_message(c.message, "Нажмите на E‑mail для запуска потока чтения:", reply_markup=_kb_read_menu(uid)); await safe_cq_answer(c, "Запущено")

@dp.callback_query(F.data == "imap:start_all")
async def imap_start_all(c: types.CallbackQuery):
    if not await ensure_approved(c): return
    uid = c.from_user.id
    with SessionLocal() as s:
        s.query(Account).filter_by(user_id=uid, active=False).update({"active": True})
        s.commit()
        chat_id = c.message.chat.id
    await _ensure_imap_started_for_user(uid, chat_id)
    await safe_edit_message(c.message, "Все потоки запущены.", reply_markup=_kb_stop_menu(uid)); await safe_cq_answer(c, "OK")

@dp.callback_query(F.data.startswith("imap:stop:"))
async def imap_stop_one(c: types.CallbackQuery):
    if not await ensure_approved(c):
        return
    uid = c.from_user.id
    acc_id = int(c.data.split(":")[2])

    # 1) Снимаем активность в БД
    with SessionLocal() as s:
        acc = s.query(Account).filter_by(user_id=uid, id=acc_id).first()
        if not acc:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        acc.active = False
        email = acc.email
        s.commit()

    # 2) Снимаем runtime-активность (для /status)
    IMAP_STATUS.setdefault(uid, {}).setdefault("accounts", {}).setdefault(email, {})
    IMAP_STATUS[uid]["accounts"][email].update({"active": False})

    # 3) Лог остановки
    await c.message.answer(f"Поток для {code(email)} остановлен⚡")

    # 4) Если активных не осталось — останавливаем фон
    with SessionLocal() as s:
        has_active = s.query(Account).filter_by(user_id=uid, active=True).first() is not None
    if not has_active:
        await _ensure_imap_stopped_for_user(uid)

    # 5) Обновляем меню остановки
    text = "Нажмите на E‑mail для остановки потока чтения:" if has_active else "Нет активных потоков."
    await safe_edit_message(c.message, text, reply_markup=_kb_stop_menu(uid))
    await safe_cq_answer(c, "Остановлено")
    
@dp.callback_query(F.data == "imap:stop_all")
async def imap_stop_all(c: types.CallbackQuery):
    if not await ensure_approved(c):
        return
    uid = c.from_user.id

    # 1) Собираем текущие активные, затем гасим в БД
    with SessionLocal() as s:
        act = s.query(Account).filter_by(user_id=uid, active=True).all()
        emails = [a.email for a in act]
        if emails:
            s.query(Account).filter_by(user_id=uid, active=True).update({"active": False})
            s.commit()

    # 2) Гасим runtime-активность и логируем для каждого
    for email in emails:
        IMAP_STATUS.setdefault(uid, {}).setdefault("accounts", {}).setdefault(email, {})
        IMAP_STATUS[uid]["accounts"][email].update({"active": False})
        try:
            await c.message.answer(f"Поток для {code(email)} остановлен⚡")
        except Exception:
            pass

    # 3) Останавливаем фон и обновляем меню
    await _ensure_imap_stopped_for_user(uid)
    await safe_edit_message(c.message, "Нет активных потоков.", reply_markup=_kb_stop_menu(uid))
    await safe_cq_answer(c, "Остановлено")
    
# ====== MAIN ======
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="check", description="Проверка ников (XLSX)"),
        BotCommand(command="send", description="Отправить email"),
        BotCommand(command="quickadd", description="Быстрое добавление"),
        BotCommand(command="read", description="IMAP: меню запуска потоков"),
        BotCommand(command="status", description="IMAP: статус"),
        BotCommand(command="stop", description="IMAP: меню остановки"),
        BotCommand(command="admin", description="Админка"),
    ]
    await bot.set_my_commands(commands)

async def main() -> None:
    await set_bot_commands(bot)
    print(f"Starting aiogram bot {VERSION}…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user")