"""Avito monitor Telegram bot. Run with: python bot.py"""
import asyncio, hashlib, os, re, logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avito-bot")
TOKEN = os.environ["BOT_TOKEN"]
API_ROOT = os.getenv("TELEGRAM_API_ROOT", "https://api.telegram.org").rstrip("/")
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CHECKS", "8"))
FRESH_HOURS = float(os.getenv("FRESHNESS_HOURS", "2"))
DB_PATH = os.getenv("DATABASE_PATH", "avito_bot.sqlite3")
PROXY = os.getenv("AVITO_PROXY") or None
ROTATE_URL, ROTATE_TOKEN = os.getenv("PROXY_ROTATE_URL"), os.getenv("PROXY_ROTATE_TOKEN")

TEXT = {
    "welcome": "Пришлите ссылку поиска Avito. Бот сохранит все фильтры из ссылки и будет искать новые объявления.",
    "saved": "Ссылка добавлена. Проверка выполняется каждые {seconds} секунд.",
    "bad_url": "Нужна ссылка avito.ru или m.avito.ru.",
    "list_empty": "У вас пока нет ссылок.",
    "stopped": "Мониторинг остановлен.",
    "help": "Добавьте ссылку на поиск Avito (ПК или мобильную). Фильтры применяются самой страницей Avito.",
}
router = Router()
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="➕ Добавить ссылку"), KeyboardButton(text="📋 Мои ссылки")], [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="🛑 Остановить")]], resize_keyboard=True)

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, UNIQUE(chat_id,url));
        CREATE TABLE IF NOT EXISTS seen(search_id INTEGER NOT NULL, item_id TEXT NOT NULL, first_seen TEXT NOT NULL, PRIMARY KEY(search_id,item_id));
        """)
        await db.commit()

def valid_url(value: str):
    p = urlparse(value.strip())
    return p.scheme in ("http", "https") and p.hostname and (p.hostname.lower() == "avito.ru" or p.hostname.lower().endswith(".avito.ru"))

@router.message(CommandStart())
async def start(m: Message): await m.answer(TEXT["welcome"], reply_markup=MENU)

@router.message(F.text == "➕ Добавить ссылку")
async def add_prompt(m: Message): await m.answer("Отправьте ссылку поиска Avito.")

@router.message(F.text == "📖 Инструкция")
async def help_(m: Message): await m.answer(TEXT["help"], reply_markup=MENU)

@router.message(F.text == "📋 Мои ссылки")
async def list_(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT id,url,active FROM searches WHERE chat_id=? ORDER BY id", (m.chat.id,))).fetchall()
    await m.answer(TEXT["list_empty"] if not rows else "\n".join(f"{i}. {'✅' if a else '⏸️'} {u}" for i,u,a in rows))

@router.message(F.text == "🛑 Остановить")
async def stop(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE searches SET active=0 WHERE chat_id=?", (m.chat.id,)); await db.commit()
    await m.answer(TEXT["stopped"], reply_markup=MENU)

@router.message(F.text)
async def add_url(m: Message):
    url = m.text.strip()
    if not valid_url(url): return await m.answer(TEXT["bad_url"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO searches(chat_id,url,created_at) VALUES(?,?,?)", (m.chat.id,url,datetime.now(timezone.utc).isoformat())); await db.commit()
    await m.answer(TEXT["saved"].format(seconds=INTERVAL), reply_markup=MENU)

async def rotate_ip():
    if not ROTATE_URL: return False
    headers = {"Authorization": f"Bearer {ROTATE_TOKEN}"} if ROTATE_TOKEN else {}
    async with aiohttp.ClientSession() as s:
        async with s.post(ROTATE_URL, headers=headers, timeout=20) as r: return r.status < 300

async def scrape(page, url):
    log.info("Открываю Avito: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=45000); await page.wait_for_timeout(2500)
    cards = await page.locator('[data-marker="item"] , [data-marker="item-link"]').all()
    out = []
    for card in cards[:100]:
        href = await card.get_attribute("href") or await card.locator("a").first.get_attribute("href")
        text = (await card.inner_text()).strip() if await card.count() else ""
        if not href: continue
        if href.startswith("/"): href = "https://www.avito.ru" + href
        item_id = hashlib.sha256(href.encode()).hexdigest()[:32]
        out.append((item_id, text[:500], href))
    log.info("Avito вернуло карточек: %d", len(out))
    return out

async def check_one(browser, row, bot, sem):
    async with sem:
        sid, chat, url = row
        for attempt in range(2):
            try:
                context = await browser.new_context(proxy={"server": PROXY} if PROXY else None)
                page = await context.new_page(); items = await scrape(page, url); await context.close(); break
            except Exception as exc:
                log.exception("Ошибка проверки ссылки %s: %s", url, exc)
                if attempt == 0 and await rotate_ip(): await asyncio.sleep(5)
                else: return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
        async with aiosqlite.connect(DB_PATH) as db:
            for iid, title, href in items:
                exists = await (await db.execute("SELECT 1 FROM seen WHERE search_id=? AND item_id=?", (sid,iid))).fetchone()
                await db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?)", (sid,iid,datetime.now(timezone.utc).isoformat()))
                # First-seen is the only reliable freshness signal without opening every item.
                if not exists:
                    await bot.send_message(chat, f"{title}\n{href}")
                    log.info("Отправлено новое объявление в чат %s", chat)
            await db.commit()

async def monitor(bot):
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            while True:
                async with aiosqlite.connect(DB_PATH) as db: rows = await (await db.execute("SELECT id,chat_id,url FROM searches WHERE active=1")).fetchall()
                log.info("Активных ссылок: %d", len(rows))
                await asyncio.gather(*(check_one(browser, r, bot, sem) for r in rows)); await asyncio.sleep(INTERVAL)
        finally: await browser.close()

async def main():
    await db_init(); log.info("Бот запускается; интервал проверки %s секунд", INTERVAL)
    server = TelegramAPIServer.from_base(API_ROOT) if API_ROOT != "https://api.telegram.org" else None
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), server=server)
    dp = Dispatcher(); dp.include_router(router)
    await asyncio.gather(dp.start_polling(bot), monitor(bot))

if __name__ == "__main__": asyncio.run(main())
