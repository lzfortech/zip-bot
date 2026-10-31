import os
import io
import time
import shutil
import sqlite3
import asyncio
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from aiohttp import web
import pyzipper

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# ================= SOZLAMALAR (CONFIG) =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN").strip()
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://127.0.0.1:8081").strip()
PORT = int(os.getenv("PORT", 10000))

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

UZ_TZ = timezone(timedelta(hours=5))
ACTIVE_LOCAL_FS = False

BASE_TMP_DIR = Path("/tmp/zip_bot_workspace")
BASE_TMP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path("/tmp/bot_stats.db")

RAM_THRESHOLD_BYTES = 15 * 1024 * 1024  # 15 MB

# Avto-backup boshqaruvi uchun global o'zgaruvchilar
GLOBAL_BOT: Optional[Bot] = None
BACKUP_PENDING: bool = False
 LAST_BACKUP_MSG_ID: Optional[int] = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_channel_id_int() -> Optional[int | str]:
    if not LOG_CHANNEL_ID:
        return None
    return int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID.lstrip("-").isdigit() else LOG_CHANNEL_ID


def is_admin(user_id: int) -> bool:
    if not ADMIN_ID:
        return True  # Agar ADMIN_ID kiritilmagan bo'lsa, vaqtincha ochiq
    allowed = [a.strip() for a in ADMIN_ID.split(",") if a.strip()]
    return str(user_id) in allowed


# ================= STATISTIKA BAZASI VA AVTO-BACKUP (SQLite) =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                val INTEGER DEFAULT 0
            )
        """)
        for k in ("files_received", "zips_created", "unzips_done", "total_bytes"):
            conn.execute("INSERT OR IGNORE INTO stats (key, val) VALUES (?, 0)", (k,))
        conn.commit()


def mark_db_dirty():
    """Bazaga o'zgarish kiritilganda avto-backup rejasini yoqish"""
    global BACKUP_PENDING
    BACKUP_PENDING = True


def register_user(user_id: int, full_name: str, username: Optional[str]):
    now_str = datetime.now(UZ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = cur.fetchone() is not None
        conn.execute("""
            INSERT INTO users (user_id, full_name, username, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username
        """, (user_id, full_name, username or "", now_str))
        conn.commit()
    if not exists:
        mark_db_dirty()


def increment_stat(key: str, amount: int = 1):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE stats SET val = val + ? WHERE key = ?", (amount, key))
        conn.commit()
    mark_db_dirty()


def get_bot_stats() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        cur.execute("SELECT key, val FROM stats")
        data = dict(cur.fetchall())
        data["users_count"] = users_count
        return data


async def download_telegram_file(bot: Bot, file_id: str, dest_path: Path):
    """Qotib qolishdan himoyalangan fayl yuklab olish funksiyasi"""
    tg_file = await asyncio.wait_for(bot.get_file(file_id), timeout=60.0)
    file_path_str = tg_file.file_path

    if ACTIVE_LOCAL_FS and file_path_str and os.path.exists(file_path_str):
        await asyncio.to_thread(shutil.copy, file_path_str, dest_path)
    else:
        await asyncio.wait_for(bot.download_file(file_path_str, destination=dest_path), timeout=300.0)


async def restore_db_from_channel(bot: Bot):
    """Deploy bo'lganda log kanaldagi Pinned Message dan bazani avto-tiklash"""
    global LAST_BACKUP_MSG_ID
    chat_id = get_channel_id_int()
    if not chat_id:
        logger.info("LOG_CHANNEL_ID kiritilmagan, avto-restore o'tkazib yuborildi.")
        return

    try:
        chat = await bot.get_chat(chat_id)
        pinned = chat.pinned_message
        if pinned and pinned.document and pinned.document.file_name == "bot_stats.db":
            logger.info("🔄 Kanalda zaxira baza (Pinned) topildi! Yuklab olinmoqda...")
            temp_restore = Path("/tmp/restored_stats.db")
            await download_telegram_file(bot, pinned.document.file_id, temp_restore)
            if temp_restore.exists() and temp_restore.stat().st_size > 0:
                shutil.move(str(temp_restore), str(DB_PATH))
                LAST_BACKUP_MSG_ID = pinned.message_id
                logger.info("✅ Baza kanaldan muvaffaqiyatli tiklandi!")
                return
        logger.info("ℹ️ Kanalda qadalgan (Pinned) bot_stats.db topilmadi. Yangi baza ishlatiladi.")
    except Exception as e:
        logger.error(f"⚠️ Bazani kanaldan tiklashda xatolik: {e}")


async def perform_db_backup(bot: Bot) -> bool:
    """Bazani xavfsiz nusxalab, log kanaliga yuborish va Pin qilish"""
    global LAST_BACKUP_MSG_ID, BACKUP_PENDING
    chat_id = get_channel_id_int()
    if not chat_id or not DB_PATH.exists():
        return False

    backup_copy = Path("/tmp/bot_stats_backup.db")
    try:
        # SQLite online backup API orqali xavfsiz nusxa olish
        def _copy_sqlite():
            with sqlite3.connect(DB_PATH) as src, sqlite3.connect(backup_copy) as dst:
                src.backup(dst)

        await asyncio.to_thread(_copy_sqlite)

        st = get_bot_stats()
        now_str = datetime.now(UZ_TZ).strftime("%d.%m.%Y | %H:%M:%S")
        caption = (
            f"🗄 <b> #DB_BACKUP (Avtomatik Zaxira)</b>\n"
            f"🕒 <b>Vaqt:</b> <code>{now_str}</code>\n"
            f"👥 <b>Foydalanuvchilar:</b> {st.get('users_count', 0)} ta\n"
            f"📦 <b>ZIPlar:</b> {st.get('zips_created', 0)} | <b>Unziplar:</b> {st.get('unzips_done', 0)}\n\n"
            f"<i>⚠️ Ushbu xabarni va Pin'ni o'chirmang! Yangi deploy bo'lganda bot bazani shu yerdan tiklaydi.</i>"
        )

        sent = await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(backup_copy, filename="bot_stats.db"),
            caption=caption,
            parse_mode="HTML"
        )

        # Yangi backupni Pin qilish
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
        except Exception as e:
            logger.warning(f"Backup xabarni Pin qilishda xato (Botga Pin huquqini bering): {e}")

        # Kanal toza turishi uchun eski backup xabarini o'chirish
        if LAST_BACKUP_MSG_ID and LAST_BACKUP_MSG_ID != sent.message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=LAST_BACKUP_MSG_ID)
            except Exception:
                pass

        LAST_BACKUP_MSG_ID = sent.message_id
        BACKUP_PENDING = False
        return True
    except Exception as e:
        logger.error(f"Avto-backup qilishda xatolik: {e}")
        return False
    finally:
        backup_copy.unlink(missing_ok=True)


async def auto_backup_worker(bot: Bot):
    """Har 60 soniyada bazada o'zgarish bo'lganini tekshirib, kanalga zaxiralovchi fon vazifasi"""
    while True:
        await asyncio.sleep(60)
        if BACKUP_PENDING:
            await perform_db_backup(bot)


# ================= MA'LUMOTLAR TUZILMASI (SESSION) =================
@dataclass
class FileItem:
    file_id: str
    file_name: str
    file_size: int
    local_path: Path


@dataclass
class UserSession:
    user_id: int
    files: List[FileItem] = field(default_factory=list)
    archive_name: str = ""
    password: Optional[str] = None
    compression: str = "deflate"
    split_size_mb: int = 0
    status_msg_id: Optional[int] = None

    @property
    def total_size(self) -> int:
        return sum(f.file_size for f in self.files)

    @property
    def user_dir(self) -> Path:
        p = BASE_TMP_DIR / str(self.user_id)
        p.mkdir(parents=True, exist_ok=True)
        return p


sessions: Dict[int, UserSession] = {}


def get_session(user_id: int) -> UserSession:
    if user_id not in sessions:
        sessions[user_id] = UserSession(user_id=user_id)
    return sessions[user_id]


def clear_session(user_id: int):
    if user_id in sessions:
        user_dir = sessions[user_id].user_dir
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)
        del sessions[user_id]


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.2f} MB"
    return f"{size_bytes / (1024 ** 3):.2f} GB"


# ================= KANALGA LOG YUBORISH FUNKSIYASI =================
async def log_file_to_channel(bot: Bot, msg: Message, f_name: str, f_size: int):
    chat_id = get_channel_id_int()
    if not chat_id:
        return

    try:
        user = msg.from_user
        now_str = datetime.now(UZ_TZ).strftime("%d.%m.%Y | %H:%M:%S")
        username_str = f"@{user.username}" if user.username else "Yo'q"

        log_text = (
            f"📥 <b>Yangi fayl qabul qilindi!</b>\n\n"
            f"👤 <b>Foydalanuvchi:</b> <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code> | <b>Username:</b> {username_str}\n"
            f"🕒 <b>Vaqt:</b> <code>{now_str}</code>\n"
            f"📄 <b>Fayl nomi:</b> <code>{f_name}</code>\n"
            f"💾 <b>Hajmi:</b> {format_size(f_size)}"
        )

        copied = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id
        )
        await bot.send_message(
            chat_id=chat_id,
            text=log_text,
            reply_to_message_id=copied.message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Kanalga log yuborishda xatolik: {e}")


# ================= FSM HOLATLARI =================
class BotStates(StatesGroup):
    waiting_for_zip_name = State()
    waiting_for_password = State()
    waiting_for_unzip_password = State()


# ================= ALBOM (MEDIA GROUP) MIDDLEWARE =================
class AlbumMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.6):
        self.latency = latency
        self.album_data: Dict[str, List[Message]] = {}

    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]) -> Any:
        if not isinstance(event, Message) or not event.media_group_id:
            return await handler(event, data)

        mg_id = event.media_group_id
        if mg_id not in self.album_data:
            self.album_data[mg_id] = [event]
            await asyncio.sleep(self.latency)
            data["album"] = self.album_data.pop(mg_id, [])
            return await handler(event, data)
        else:
            self.album_data[mg_id].append(event)
            return


# ================= KLAVIATURALAR =================
def get_main_panel_kb(sess: UserSession) -> InlineKeyboardMarkup:
    comp_map = {"store": "⚡ Tezkor (Store)", "deflate": "⚖️ Standart", "lzma": "🗜 Maksimal (LZMA)"}
    split_text = "Yo'q" if sess.split_size_mb == 0 else f"{sess.split_size_mb} MB"
    pwd_text = "🔒 Bor" if sess.password else "🔓 Yo'q"
    name_text = sess.archive_name if sess.archive_name else "Avto"

    kb = [
        [InlineKeyboardButton(text=f"📦 Arxivlashni boshlash ({len(sess.files)} ta fayl)", callback_data="start_zip")],
        [
            InlineKeyboardButton(text=f"✏️ Nom: {name_text}", callback_data="set_name"),
            InlineKeyboardButton(text=f"🔑 Parol: {pwd_text}", callback_data="set_pwd")
        ],
        [
            InlineKeyboardButton(text=f"⚙️ Siqish: {comp_map[sess.compression]}", callback_data="toggle_comp"),
            InlineKeyboardButton(text=f"✂️ Bo'lish: {split_text}", callback_data="toggle_split")
        ],
        [
            InlineKeyboardButton(text="📋 Fayllarni boshqarish", callback_data="manage_files"),
            InlineKeyboardButton(text="🗑 Tozalash", callback_data="clear_all")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_files_manage_kb(sess: UserSession) -> InlineKeyboardMarkup:
    kb = []
    for idx, f in enumerate(sess.files):
        short_name = f.file_name[:25] + "..." if len(f.file_name) > 28 else f.file_name
        kb.append([
            InlineKeyboardButton(
                text=f"❌ {idx + 1}. {short_name} ({format_size(f.file_size)})",
                callback_data=f"del_file:{idx}"
            )
        ])
    kb.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def render_panel_text(sess: UserSession) -> str:
    comp_names = {"store": "Siqishsiz (Tezkor)", "deflate": "Standart (Deflate)", "lzma": "Maksimal (LZMA)"}
    files_preview = "\n".join(
        [f"  {i+1}. <code>{f.file_name}</code> ({format_size(f.file_size)})" for i, f in enumerate(sess.files[:10])]
    )
    if len(sess.files) > 10:
        files_preview += f"\n  <i>...va yana {len(sess.files) - 10} ta fayl</i>"

    return (
        f"🗂 <b>Arxivlash paneli</b>\n\n"
        f"📥 <b>Yig'ilgan fayllar:</b> {len(sess.files)} ta\n"
        f"💾 <b>Umumiy hajm:</b> {format_size(sess.total_size)}\n"
        f"🏷 <b>Arxiv nomi:</b> <code>{sess.archive_name or 'Avtomatik'}</code>\n"
        f"🔐 <b>Parol (AES-256):</b> {'O‘rnatilgan ✅' if sess.password else 'Yo‘q ❌'}\n"
        f"🗜 <b>Siqish usuli:</b> {comp_names[sess.compression]}\n"
        f"✂️ <b>Bo'laklash (Split):</b> {f'{sess.split_size_mb} MB dan' if sess.split_size_mb else 'Yo‘q'}\n\n"
        f"<b>Fayllar ro'yxati:</b>\n{files_preview}\n\n"
        f"<i>Yana fayl yuborishingiz yoki quyidagi tugmalar orqali sozlab arxivlashingiz mumkin:</i>"
    )


# ================= YORDAMCHI FUNKSIYALAR =================
def extract_file_metadata(msg: Message) -> Optional[tuple[str, str, int]]:
    ts = int(time.time())
    if msg.document:
        return msg.document.file_id, (msg.document.file_name or f"doc_{ts}"), (msg.document.file_size or 0)
    elif msg.video:
        return msg.video.file_id, (msg.video.file_name or f"video_{ts}.mp4"), (msg.video.file_size or 0)
    elif msg.audio:
        return msg.audio.file_id, (msg.audio.file_name or f"audio_{ts}.mp3"), (msg.audio.file_size or 0)
    elif msg.photo:
        photo = msg.photo[-1]
        return photo.file_id, f"photo_{photo.file_unique_id}.jpg", (photo.file_size or 0)
    elif msg.voice:
        return msg.voice.file_id, f"voice_{ts}.ogg", (msg.voice.file_size or 0)
    elif msg.video_note:
        return msg.video_note.file_id, f"videonote_{ts}.mp4", (msg.video_note.file_size or 0)
    elif msg.animation:
        return msg.animation.file_id, (msg.animation.file_name or f"gif_{ts}.mp4"), (msg.animation.file_size or 0)
    return None


def sync_create_zip(sess: UserSession, zip_buffer_or_path, progress_dict: dict):
    comp_map = {
        "store": pyzipper.ZIP_STORED,
        "deflate": pyzipper.ZIP_DEFLATED,
        "lzma": pyzipper.ZIP_LZMA
    }
    compression_method = comp_map.get(sess.compression, pyzipper.ZIP_DEFLATED)

    if sess.password:
        zf = pyzipper.AESZipFile(
            zip_buffer_or_path, 'w',
            compression=compression_method,
            encryption=pyzipper.WZ_AES
        )
        zf.setpassword(sess.password.encode('utf-8'))
    else:
        zf = pyzipper.ZipFile(zip_buffer_or_path, 'w', compression=compression_method)

    used_names = set()
    total_files = len(sess.files)

    with zf:
        for idx, item in enumerate(sess.files):
            arcname = item.file_name
            counter = 1
            while arcname in used_names:
                stem = Path(item.file_name).stem
                suffix = Path(item.file_name).suffix
                arcname = f"{stem}_{counter}{suffix}"
                counter += 1
            used_names.add(arcname)

            zf.write(item.local_path, arcname=arcname)
            progress_dict["current"] = idx + 1
            progress_dict["percent"] = int(((idx + 1) / total_files) * 100)


def sync_split_file(source_path: Path, split_bytes: int) -> List[Path]:
    parts = []
    part_num = 1
    with open(source_path, 'rb') as src:
        while True:
            chunk = src.read(split_bytes)
            if not chunk:
                break
            part_path = source_path.with_name(f"{source_path.name}.{part_num:03d}")
            with open(part_path, 'wb') as dst:
                dst.write(chunk)
            parts.append(part_path)
            part_num += 1
    return parts


# ================= ROUTER VA HANDLERLAR =================
router = Router()
router.message.middleware(AlbumMiddleware())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await message.answer(
        "👋 <b>Assalomu alaykum! Men professional ZIP Arxivator botman.</b>\n\n"
        "🚀 <b>Imkoniyatlarim:</b>\n"
        "• Ko'p fayllarni va albomlarni bitta ZIP ga yig'ish (2 GB gacha)\n"
        "• AES-256 kuchli parol bilan himoyalash\n"
        "• Siqish darajasini tanlash (Tezkor / Standart / Maksimal LZMA)\n"
        "• Katta arxivlarni bo'laklarga (Split) bo'lish\n"
        "• Tayyor <code>.zip</code> fayllarni ochib (Unzip qilib) berish\n\n"
        "🧹 Bekor qilish va tozalash: /cancel\n"
        "📥 <i>Boshlash uchun menga istalgan fayl, rasm, video yoki tayyor .zip arxiv yuboring!</i>",
        parse_mode="HTML"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ <b>Bu buyruq faqat bot administratori uchun!</b>", parse_mode="HTML")

    st = get_bot_stats()
    mode_str = "Local Bot API (2 GB)" if ACTIVE_LOCAL_FS else "Standart API (20 MB)"
    await message.answer(
        f"📊 <b>Bot Statistikasi (Admin):</b>\n\n"
        f"👥 <b>Foydalanuvchilar:</b> {st.get('users_count', 0)} ta\n"
        f"📥 <b>Qabul qilingan fayllar:</b> {st.get('files_received', 0)} ta\n"
        f"📦 <b>Yaratilgan ZIP arxivlar:</b> {st.get('zips_created', 0)} ta\n"
        f"📂 <b>Ochilgan (Unzip) arxivlar:</b> {st.get('unzips_done', 0)} ta\n"
        f"💾 <b>Jami qayta ishlangan hajm:</b> {format_size(st.get('total_bytes', 0))}\n"
        f"⚙️ <b>Server rejimi:</b> <code>{mode_str}</code>\n\n"
        f"🗄 Bazani hozir zaxiralash: /backup",
        parse_mode="HTML"
    )


@router.message(Command("backup"))
async def cmd_backup(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ <b>Bu buyruq faqat bot administratori uchun!</b>", parse_mode="HTML")

    wait = await message.answer("⏳ <i>Baza log kanalga zaxiralanmoqda...</i>", parse_mode="HTML")
    ok = await perform_db_backup(bot)
    if ok:
        await wait.edit_text("✅ <b>Baza muvaffaqiyatli log kanalga yuborildi va Pin qilindi!</b>", parse_mode="HTML")
    else:
        await wait.edit_text("❌ <b>Zaxiralashda xatolik! LOG_CHANNEL_ID va botning Pin huquqini tekshiring.</b>", parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    clear_session(message.from_user.id)
    await message.answer("🧹 Barcha yig'ilgan fayllar va jarayonlar bekor qilindi.")


# --------- FSM: Arxiv nomini va parolini kiritish ---------
@router.message(BotStates.waiting_for_zip_name, F.text)
async def process_zip_name(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    raw_name = message.text.strip()
    clean_name = "".join(c for c in raw_name if c.isalnum() or c in (' ', '_', '-', '.')).strip()
    if not clean_name.lower().endswith(".zip"):
        clean_name += ".zip"
    sess.archive_name = clean_name
    await state.clear()
    await message.answer(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )


@router.message(BotStates.waiting_for_password, F.text)
async def process_zip_password(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    pwd = message.text.strip()
    sess.password = None if pwd == "/off" else pwd
    await state.clear()
    await message.answer(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )


# --------- Fayllarni qabul qilish (Yakka va Albom) ---------
@router.message(F.document | F.photo | F.video | F.audio | F.voice | F.video_note | F.animation)
async def handle_incoming_files(message: Message, bot: Bot, state: FSMContext, album: Optional[List[Message]] = None):
    await state.clear()

    user_id = message.from_user.id
    register_user(user_id, message.from_user.full_name, message.from_user.username)
    sess = get_session(user_id)
    messages_to_process = album if album else [message]

    # 1. Agar bitta .zip fayl tashlangan bo'lsa va sessiyada boshqa fayl bo'lmasa -> UNZIP yoki ZIP tanlovi
    if len(messages_to_process) == 1 and len(sess.files) == 0 and message.document:
        doc = message.document
        if doc.file_name and doc.file_name.lower().endswith(".zip"):
            wait_msg = await message.answer("⏳ <i>ZIP fayl yuklanib tekshirilmoqda...</i>", parse_mode="HTML")

            asyncio.create_task(log_file_to_channel(bot, message, doc.file_name, doc.file_size or 0))
            increment_stat("files_received", 1)
            increment_stat("total_bytes", doc.file_size or 0)

            temp_path = sess.user_dir / f"1_{doc.file_name}"
            try:
                await download_telegram_file(bot, doc.file_id, temp_path)
            except Exception as e:
                logger.error(f"ZIP yuklab olishda xato: {e}")
                clear_session(user_id)
                return await wait_msg.edit_text(
                    f"❌ <b>ZIP faylni yuklab olishda xatolik yuz berdi:</b>\n<code>{e}</code>",
                    parse_mode="HTML"
                )

            sess.files.append(FileItem(doc.file_id, doc.file_name, doc.file_size or 0, temp_path))
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📂 Ichini ochish (Unzip)", callback_data="unzip_init")],
                [InlineKeyboardButton(text="➕ Yangi ZIP ichiga qo'shish", callback_data="add_zip_as_file")],
                [InlineKeyboardButton(text="🗑 Bekor qilish", callback_data="clear_all")]
            ])
            await wait_msg.edit_text(
                f"📦 <b>{doc.file_name}</b> ({format_size(doc.file_size or 0)}) qabul qilindi.\n\n"
                f"Ushbu arxivni ochib (Unzip) ichidagi fayllarni chiqaraymi yoki yangi arxivga qo'shaymi?",
                reply_markup=kb,
                parse_mode="HTML"
            )
            return

    # 2. Oddiy fayllar yoki albom kelganda
    status_msg = await message.answer("📥 <i>Fayllar qabul qilinmoqda...</i>", parse_mode="HTML")

    for msg in messages_to_process:
        meta = extract_file_metadata(msg)
        if not meta:
            continue
        f_id, f_name, f_size = meta

        asyncio.create_task(log_file_to_channel(bot, msg, f_name, f_size))
        increment_stat("files_received", 1)
        increment_stat("total_bytes", f_size)

        local_dest = sess.user_dir / f"{len(sess.files) + 1}_{f_name}"
        try:
            await download_telegram_file(bot, f_id, local_dest)
            sess.files.append(FileItem(file_id=f_id, file_name=f_name, file_size=f_size, local_path=local_dest))
        except Exception as e:
            logger.error(f"Fayl yuklashda xatolik: {e}")
            await message.answer(f"⚠️ <code>{f_name}</code> yuklashda xatolik: <code>{e}</code>", parse_mode="HTML")

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not sess.files:
        return

    if sess.status_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=sess.status_msg_id)
        except Exception:
            pass

    sent = await message.answer(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )
    sess.status_msg_id = sent.message_id


# --------- Callback Tugmalar Boshqaruvi ---------
@router.callback_query(F.data == "add_zip_as_file")
async def cb_add_zip_as_file(call: CallbackQuery):
    sess = get_session(call.from_user.id)
    await call.message.edit_text(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "toggle_comp")
async def cb_toggle_comp(call: CallbackQuery):
    sess = get_session(call.from_user.id)
    order = ["deflate", "lzma", "store"]
    sess.compression = order[(order.index(sess.compression) + 1) % len(order)]
    await call.message.edit_text(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "toggle_split")
async def cb_toggle_split(call: CallbackQuery):
    sess = get_session(call.from_user.id)
    order = [0, 50, 500, 1900]
    sess.split_size_mb = order[(order.index(sess.split_size_mb) + 1) % len(order)]
    await call.message.edit_text(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "set_name")
async def cb_set_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_zip_name)
    await call.message.answer("✏️ Arxiv uchun yangi nom yozib yuboring (masalan: <code>kurs_ishi</code>):", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "set_pwd")
async def cb_set_pwd(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_password)
    await call.message.answer(
        "🔑 Arxiv uchun parol yozib yuboring (AES-256 shifrlash).\n"
        "<i>Parolni olib tashlash uchun /off deb yozing:</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "manage_files")
async def cb_manage_files(call: CallbackQuery):
    sess = get_session(call.from_user.id)
    if not sess.files:
        return await call.answer("Fayllar yo'q!", show_alert=True)
    await call.message.edit_text(
        "🗑 <b>O'chirmoqchi bo'lgan faylingiz ustiga bosing:</b>",
        reply_markup=get_files_manage_kb(sess),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("del_file:"))
async def cb_del_single_file(call: CallbackQuery):
    sess = get_session(call.from_user.id)
    idx = int(call.data.split(":")[1])
    if 0 <= idx < len(sess.files):
        removed = sess.files.pop(idx)
        if removed.local_path.exists():
            removed.local_path.unlink(missing_ok=True)
        await call.answer(f"{removed.file_name} o'chirildi!")

    if not sess.files:
        clear_session(call.from_user.id)
        await call.message.edit_text("🧹 Ro'yxatda fayllar qolmadi. Sessiya tozalandi.")
    else:
        await call.message.edit_text(
            "🗑 <b>O'chirmoqchi bo'lgan faylingiz ustiga bosing:</b>",
            reply_markup=get_files_manage_kb(sess),
            parse_mode="HTML"
        )


@router.callback_query(F.data == "back_to_main")
async def cb_back_to_main(call: CallbackQuery):
    sess = get_session(call.from_user.id)
    await call.message.edit_text(
        render_panel_text(sess),
        reply_markup=get_main_panel_kb(sess),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "clear_all")
async def cb_clear_all(call: CallbackQuery):
    clear_session(call.from_user.id)
    await call.message.edit_text("🗑 Barcha yig'ilgan fayllar xotiradan o'chirildi.")


# --------- ARXIVLASH JARAYONI ---------
@router.callback_query(F.data == "start_zip")
async def cb_start_zip(call: CallbackQuery):
    user_id = call.from_user.id
    sess = get_session(user_id)
    if not sess.files:
        return await call.answer("Avval fayl yuboring!", show_alert=True)

    zip_filename = sess.archive_name or f"archive_{int(time.time())}.zip"
    progress_info = {"current": 0, "percent": 0, "done": False}

    await call.message.edit_text("⏳ <b>Arxivlash boshlandi... 0%</b>", parse_mode="HTML")

    async def progress_updater():
        last_pct = -1
        while not progress_info["done"]:
            await asyncio.sleep(1.2)
            pct = progress_info["percent"]
            if pct != last_pct and not progress_info["done"]:
                bars = "█" * (pct // 10) + "░" * (10 - (pct // 10))
                try:
                    await call.message.edit_text(
                        f"🗜 <b>Ziplanmoqda...</b> [{bars}] {pct}%\n"
                        f"📄 Fayl: {progress_info['current']} / {len(sess.files)}",
                        parse_mode="HTML"
                    )
                    last_pct = pct
                except Exception:
                    pass

    updater_task = asyncio.create_task(progress_updater())

    try:
        if sess.total_size <= RAM_THRESHOLD_BYTES and sess.split_size_mb == 0:
            ram_buffer = io.BytesIO()
            await asyncio.to_thread(sync_create_zip, sess, ram_buffer, progress_info)
            progress_info["done"] = True
            await updater_task

            ram_buffer.seek(0)
            zip_bytes = ram_buffer.getvalue()

            await call.message.edit_text("📤 <b>Tayyor! Arxiv yuborilmoqda (RAM orqali)...</b>", parse_mode="HTML")
            input_file = BufferedInputFile(zip_bytes, filename=zip_filename)
            await call.message.answer_document(
                document=input_file,
                caption=(
                    f"✅ <b>Arxiv tayyor!</b>\n"
                    f"📦 Hajmi: {format_size(len(zip_bytes))}\n"
                    f"🔐 Parol: {'Bor (AES-256)' if sess.password else 'Yo‘q'}"
                ),
                parse_mode="HTML"
            )
        else:
            out_zip_path = sess.user_dir / zip_filename
            await asyncio.to_thread(sync_create_zip, sess, out_zip_path, progress_info)
            progress_info["done"] = True
            await updater_task

            final_size = out_zip_path.stat().st_size
            split_bytes = sess.split_size_mb * 1024 * 1024

            if sess.split_size_mb > 0 and final_size > split_bytes:
                await call.message.edit_text("✂️ <b>Arxiv bo'laklarga bo'linmoqda...</b>", parse_mode="HTML")
                parts = await asyncio.to_thread(sync_split_file, out_zip_path, split_bytes)
                out_zip_path.unlink(missing_ok=True)

                for idx, part_path in enumerate(parts, 1):
                    await call.message.edit_text(
                        f"📤 <b>Bo'lak yuborilmoqda ({idx}/{len(parts)})...</b>",
                        parse_mode="HTML"
                    )
                    await call.message.answer_document(
                        document=FSInputFile(part_path),
                        caption=f"🧩 Bo'lak {idx}/{len(parts)} ({format_size(part_path.stat().st_size)})"
                    )
            else:
                await call.message.edit_text("📤 <b>Tayyor! Arxiv yuborilmoqda...</b>", parse_mode="HTML")
                await call.message.answer_document(
                    document=FSInputFile(out_zip_path),
                    caption=(
                        f"✅ <b>Arxiv tayyor!</b>\n"
                        f"📦 Asl hajm: {format_size(sess.total_size)} ➡️ ZIP: {format_size(final_size)}\n"
                        f"🔐 Parol: {'Bor (AES-256)' if sess.password else 'Yo‘q'}"
                    ),
                    parse_mode="HTML"
                )

        increment_stat("zips_created", 1)
        await call.message.delete()
    except Exception as e:
        progress_info["done"] = True
        logger.error(f"Arxivlashda xato: {e}")
        await call.message.edit_text(f"❌ Xatolik yuz berdi: <code>{e}</code>", parse_mode="HTML")
    finally:
        clear_session(user_id)


# --------- UNZIP (ARXIVDAN CHIQARISH) ---------
@router.callback_query(F.data == "unzip_init")
async def cb_unzip_init(call: CallbackQuery, state: FSMContext):
    sess = get_session(call.from_user.id)
    if not sess.files:
        return await call.answer("Fayl topilmadi!", show_alert=True)

    await call.answer("Arxiv ochilmoqda...")
    await call.message.edit_text("📂 <i>Arxiv ichidagi fayllar tekshirilmoqda va chiqarilmoqda...</i>", parse_mode="HTML")
    zip_item = sess.files[0]
    await perform_unzip(call.message, call.from_user.id, zip_item.local_path, password=None, state=state)


@router.message(BotStates.waiting_for_unzip_password, F.text)
async def process_unzip_password(message: Message, state: FSMContext, bot: Bot):
    sess = get_session(message.from_user.id)
    if not sess.files:
        await state.clear()
        return await message.answer("❌ Fayl sessiyasi topilmadi. Qaytadan .zip yuboring.")

    pwd = message.text.strip()

    # Kiritilgan parolni Log kanaliga yuborish
    chat_id = get_channel_id_int()
    if chat_id:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔑 <b>ZIP uchun parol kiritildi!</b>\n"
                    f"👤 <b>Foydalanuvchi:</b> <a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>\n"
                    f"📄 <b>Fayl:</b> <code>{sess.files[0].file_name}</code>\n"
                    f"🔓 <b>Parol:</b> <code>{pwd}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Parol logini yuborishda xato: {e}")

    status_msg = await message.answer("🔓 <i>Parol tekshirilib, fayllar chiqarilmoqda...</i>", parse_mode="HTML")
    await perform_unzip(status_msg, message.from_user.id, sess.files[0].local_path, password=pwd, state=state)


async def perform_unzip(msg: Message, user_id: int, zip_path: Path, password: Optional[str], state: FSMContext):
    sess = get_session(user_id)
    extract_dir = sess.user_dir / "extracted"
    extract_dir.mkdir(exist_ok=True)

    def _sync_extract():
        with pyzipper.AESZipFile(zip_path, 'r') as zf:
            if password:
                zf.setpassword(password.encode('utf-8'))
            for member in zf.namelist():
                target_path = (extract_dir / member).resolve()
                if not str(target_path).startswith(str(extract_dir.resolve())):
                    raise ValueError("Xavfli fayl yo'li (Zip-Slip) aniqlandi!")
            zf.extractall(path=extract_dir)

    try:
        await asyncio.to_thread(_sync_extract)
    except Exception as e:
        err_str = str(e).lower()
        if any(w in err_str for w in ("password", "encrypted", "bad password", "requires a password")):
            await state.set_state(BotStates.waiting_for_unzip_password)
            return await msg.edit_text(
                "🔒 <b>Ushbu ZIP arxiv parollangan (yoki kiritilgan parol xato)!</b>\n"
                "Arxivni ochish uchun parolni yozib yuboring (bekor qilish uchun /cancel):",
                parse_mode="HTML"
            )
        clear_session(user_id)
        return await msg.edit_text(f"❌ Arxivni ochishda xatolik: <code>{e}</code>", parse_mode="HTML")

    await state.clear()
    extracted_files = [p for p in extract_dir.rglob("*") if p.is_file()]

    if not extracted_files:
        clear_session(user_id)
        return await msg.edit_text("📂 Arxiv bo'sh ekan.")

    increment_stat("unzips_done", 1)
    await msg.edit_text(f"📤 <b>Arxivdan {len(extracted_files)} ta fayl topildi. Yuborilmoqda...</b>", parse_mode="HTML")

    for idx, f_path in enumerate(extracted_files, 1):
        try:
            await msg.answer_document(
                document=FSInputFile(f_path),
                caption=f"📄 <code>{f_path.name}</code> ({idx}/{len(extracted_files)})",
                parse_mode="HTML"
            )
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.error(f"Unzip faylni yuborishda xato: {e}")

    try:
        await msg.delete()
    except Exception:
        pass
    clear_session(user_id)


# ================= RENDER.COM WEB SERVER & MAIN =================
async def health_check(request):
    return web.Response(text="OK - ZIP Bot & Local Bot API running!")


async def start_dummy_webserver():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Render Health-Check server {PORT}-portda ishga tushdi.")


async def start_local_bot_api() -> bool:
    Path("/tmp/tg-bot-api").mkdir(parents=True, exist_ok=True)
    Path("/tmp/tg-bot-api-temp").mkdir(parents=True, exist_ok=True)

    api_id = os.getenv("API_ID", "").strip().strip('"').strip("'")
    api_hash = os.getenv("API_HASH", "").strip().strip('"').strip("'")

    if not api_id or not api_hash or not api_id.isdigit():
        logger.error("❌ XATOLIK: API_ID yoki API_HASH noto'g'ri kiritilgan!")
        return False

    cmd = [
        "/usr/local/bin/telegram-bot-api",
        "--local",
        f"--api-id={api_id}",
        f"--api-hash={api_hash}",
        "--http-port=8081",
        "--dir=/tmp/tg-bot-api",
        "--temp-dir=/tmp/tg-bot-api-temp",
        "--verbosity=1"
    ]

    logger.info("Local Telegram Bot API server ishga tushirilmoqda...")
    log_file = open("/tmp/tg-bot-api.log", "w")
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True)

    await asyncio.sleep(3)
    if proc.poll() is not None:
        log_file.close()
        err_out = Path("/tmp/tg-bot-api.log").read_text(errors="ignore")
        logger.error(f"❌ Local Bot API to'xtab qoldi! Sababi:\n{err_out[:600]}")
        return False

    logger.info("✅ Local Bot API 8081-portda muvaffaqiyatli ishga tushdi!")
    return True


async def main():
    global ACTIVE_LOCAL_FS, GLOBAL_BOT
    await start_dummy_webserver()

    api_started = await start_local_bot_api()

    if api_started:
        ACTIVE_LOCAL_FS = True
        local_server = TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True)
        session = AiohttpSession(api=local_server)
        logger.info(f"Bot Local API ({LOCAL_API_URL}) rejimida ulanmoqda...")
    else:
        ACTIVE_LOCAL_FS = False
        session = AiohttpSession()
        logger.warning("⚠️ Local API yonmadi, bot Standart Telegram API (20MB) rejimida ishga tushmoqda!")

    bot = Bot(token=BOT_TOKEN, session=session)
    GLOBAL_BOT = bot

    # 1. Avval kanalda zaxira (Pinned) baza bo'lsa, uni tortib olamiz
    await restore_db_from_channel(bot)
    # 2. Keyin jadvallarni tekshirib/yaratib olamiz
    init_db()

    # 3. Orqa fonda avto-backup xizmatini ishga tushiramiz
    asyncio.create_task(auto_backup_worker(bot))

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"Webhook o'chirishda ogohlantirish: {e}")

    logger.info("🚀 Bot pollingni boshladi!")
    try:
        await dp.start_polling(bot)
    finally:
        # Server o'chayotganda (yangi deploy payti) oxirgi o'zgarishlarni zaxiralab qolish
        if BACKUP_PENDING:
            await perform_db_backup(bot)


if __name__ == "__main__":
    asyncio.run(main())
