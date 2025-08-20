from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

router = Router(name="reply_send_html")

# ================= Интеграция (email и адресат) =================
_EmailSender = Callable[[int, str, str, str], bool]
_email_sender: Optional[_EmailSender] = None

def set_email_sender(sender: _EmailSender) -> None:
    """
    sender(user_id, to_email, subject, html_body) -> bool
    """
    global _email_sender
    _email_sender = sender

# (chat_id, incoming_message_id) -> email
_REPLY_CTX: Dict[tuple[int, int], str] = {}

def set_reply_context(chat_id: int, incoming_message_id: int, to_email: str) -> None:
    _REPLY_CTX[(chat_id, incoming_message_id)] = to_email

# ================= Клавиатуры и FSM =================
def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Отмена", callback_data="reply:cancel")]
    ])

class ReplyHTMLFSM(StatesGroup):
    wait_html = State()

@dataclass
class Flow:
    src_mid: int = 0
    to_email: str = ""

# ================= Helpers =================
def _files_from_html(html: str, base: str) -> tuple[types.BufferedInputFile, types.BufferedInputFile]:
    ts = int(time.time())
    txt = types.BufferedInputFile((html or "").encode("utf-8"), filename=f"{base}_{ts}.txt")
    htm = types.BufferedInputFile((html or "").encode("utf-8"), filename=f"{base}_{ts}.html")
    return txt, htm

async def _read_txt_document_as_text(m: types.Message) -> Optional[str]:
    if not m.document:
        return None
    name = (m.document.file_name or "").lower()
    if not (name.endswith(".txt") or (m.document.mime_type or "").startswith("text/plain")):
        return None
    buf = io.BytesIO()
    await m.bot.download(m.document, destination=buf)
    raw = buf.getvalue()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")

# ================= Handlers =================
# Кнопка “📩 Отправить HTML” должна иметь callback_data = f"reply:html:start:{incoming_mid}"
@router.callback_query(F.data.startswith("reply:html:start:"))
async def start_html_reply(c: types.CallbackQuery, state: FSMContext):
    src_mid = int(c.data.split(":")[3])
    to_email = _REPLY_CTX.get((c.message.chat.id, src_mid), "")
    await state.update_data(flow=Flow(src_mid=src_mid, to_email=to_email).__dict__)
    await c.message.answer("Отправьте HTML-разметку текстом или .txt файлом:", reply_markup=cancel_kb())
    await state.set_state(ReplyHTMLFSM.wait_html)
    await c.answer()

@router.callback_query(F.data == "reply:cancel")
async def cancel_reply(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer("Отменено.")
    await c.answer()

# Принимаем .txt документ
@router.message(ReplyHTMLFSM.wait_html, F.document)
async def got_html_file(m: types.Message, state: FSMContext):
    text = await _read_txt_document_as_text(m)
    if text is None:
        await m.answer("Нужен .txt файл с HTML-разметкой (text/plain). Пришлите текстом или .txt.", reply_markup=cancel_kb())
        return
    await _finalize_send(m, state, html=text)

# Или принимаем текстом
@router.message(ReplyHTMLFSM.wait_html, F.text)
async def got_html_text(m: types.Message, state: FSMContext):
    html = (m.text or "").strip()
    if not html:
        await m.answer("Пустой текст. Пришлите HTML-разметку или .txt файл.", reply_markup=cancel_kb())
        return
    await _finalize_send(m, state, html=html)

async def _finalize_send(m: types.Message, state: FSMContext, html: str):
    data = await state.get_data()
    flow = Flow(**data.get("flow", {}))
    src_mid = flow.src_mid
    to_email = flow.to_email

    # 1) Публикуем шаблон в чат в виде .txt (как “переслали шаблон в чат для отправки”)
    txtf, htmlf = _files_from_html(html, "template")
    await m.answer_document(txtf, caption="Шаблон (TXT)")

    # 2) Отправляем email (если подключено)
    ok = False
    if _email_sender and to_email:
        try:
            ok = bool(_email_sender(m.from_user.id, to_email, "HTML-Template", html))
        except Exception:
            ok = False

    # 3) Лог ответом на входящее + прикладываем .html
    caption = (
        f"Ответ с HTML-вложением успешно отправлен пользователю {to_email}"
        if ok and to_email
        else (f"Не удалось отправить HTML пользователю {to_email}" if to_email else "HTML отправлен (email не указан)")
    )
    try:
        await m.bot.send_document(
            chat_id=m.chat.id,
            document=htmlf,
            caption=caption,
            reply_to_message_id=src_mid
        )
    except Exception:
        _, htmlf2 = _files_from_html(html, "template")
        await m.bot.send_document(
            chat_id=m.chat.id,
            document=htmlf2,
            caption=caption,
            reply_to_message_id=src_mid
        )

    await state.clear()