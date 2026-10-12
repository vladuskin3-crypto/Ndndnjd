import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime

import aiohttp
import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ───────────────────────── НАСТРОЙКИ (.env) ─────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "8851216228:AAF-xVEBNbvjKbtAybAir-aFPTvJtxoqaVA")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "8759747461").replace(" ", "").split(",") if x.isdigit()]
SHOP_NAME = os.getenv("SHOP_NAME", "Sm Hosting")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "Mormonsq").lstrip("@")
BANNER = os.getenv("BANNER", "banner.jpg")          # путь к файлу или URL картинки
CURRENCY = os.getenv("CURRENCY", "$")
MIN_TOPUP = float(os.getenv("MIN_TOPUP", "50"))
LOW_STOCK = int(os.getenv("LOW_STOCK", "3"))         # уведомление админам при малом остатке
DB_PATH = os.getenv("DB_PATH", "shop.db")

# CryptoBot (Crypto Pay API): токен берётся в @CryptoBot → Crypto Pay → Create App
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "http://t.me/send?start=IVXbX0mcEeBt").strip()
CRYPTO_TESTNET = os.getenv("CRYPTO_TESTNET", "0") == "1"          # 1 = тестовая сеть (@CryptoTestnetBot)
CRYPTO_FIAT = os.getenv("CRYPTO_FIAT", "Dollars").upper()             # валюта счёта, должна совпадать с валютой магазина
CRYPTO_ASSETS = os.getenv("CRYPTO_ASSETS", "USDT,TON,BTC,ETH,LTC,TRX,USDC").replace(" ", "")  # пусто = любые
CRYPTO_TTL_MIN = int(os.getenv("CRYPTO_TTL_MIN", "60"))           # срок жизни счёта, минут
CRYPTO_POLL = int(os.getenv("CRYPTO_POLL", "10"))                 # как часто проверять оплату, секунд

# Курс: сколько единиц валюты магазина стоит $1 (например 90 для ₽). 0 = брать из CryptoBot / не показывать $
USD_RATE = float(os.getenv("USD_RATE", "0") or 0)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")

bot = Bot(token=TOKEN or "0:placeholder", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
db: aiosqlite.Connection
BUY_LOCK = asyncio.Lock()

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT DEFAULT 'ru',
    balance REAL DEFAULT 0, banned INTEGER DEFAULT 0, purchases INTEGER DEFAULT 0, joined TEXT);
CREATE TABLE IF NOT EXISTS sections(
    id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER DEFAULT 0, name TEXT);
CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY AUTOINCREMENT, section_id INTEGER DEFAULT 0, name TEXT,
    description TEXT DEFAULT '', price REAL, stock INTEGER DEFAULT 0, content TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT, rating INTEGER, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
    product_name TEXT, price REAL, created TEXT);
CREATE TABLE IF NOT EXISTS topups(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
    status TEXT DEFAULT 'pending', created TEXT);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, amount REAL, uses INTEGER);
CREATE TABLE IF NOT EXISTS promo_used(code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS crypto_invoices(
    id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER UNIQUE, user_id INTEGER, amount REAL,
    kind TEXT, product_id INTEGER DEFAULT 0, status TEXT DEFAULT 'active', url TEXT,
    created TEXT, paid_at TEXT);
"""


async def q(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchall()


async def q1(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchone()


async def ex(sql, args=()):
    c = await db.execute(sql, args)
    await db.commit()
    return c.lastrowid


async def get_setting(k, default=None):
    r = await q1("SELECT value FROM settings WHERE key=?", (k,))
    return r["value"] if r else default


async def set_setting(k, v):
    await ex("INSERT INTO settings(key,value) VALUES(?,?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


async def del_setting(k):
    await ex("DELETE FROM settings WHERE key=?", (k,))


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def fm(x):
    s = f"{float(x):.2f}"
    return s.rstrip("0").rstrip(".")


SHORT_USD = " / ${}"


def usd(x, tpl=" (≈ ${})"):
    """Доллары по текущему курсу: usd(100) -> ' (≈ $1.11)'. Пусто, если курс не задан."""
    if USD_RATE <= 0 or CURRENCY.strip() in ("$", "USD"):
        return ""
    return tpl.format(f"{float(x) / USD_RATE:,.2f}")


def cu(x):
    """Валюта магазина + доллары: подставляется вместо {cur} рядом с одной суммой."""
    return CURRENCY + usd(x)


def stock_s(s):
    return "∞" if s == -1 else str(s)


def esc(s):
    return html.escape(str(s or ""))


def parse_num(s):
    try:
        return float((s or "").replace(",", ".").strip())
    except ValueError:
        return None


# ───────────────────────── ЦВЕТА КНОПОК ─────────────────────────
# Цвет кнопок — поле `style` Bot API (primary=синий, success=зелёный, danger=красный).
GROUPS = {"menu": "Главное меню", "nav": "Навигация («Назад»)",
          "buy": "Покупки и оплата", "admin": "Админ-панель"}
COLOR_NAMES = {"primary": "🔵 Синий", "success": "🟢 Зелёный",
               "danger": "🔴 Красный", "default": "⚪ По умолчанию"}
COLORS = {"menu": "primary", "nav": "default", "buy": "success", "admin": "primary"}


async def load_colors():
    for g in GROUPS:
        v = await get_setting("color_" + g)
        if v:
            COLORS[g] = v


def B(text, cb, group="nav"):
    kw = {}
    if COLORS.get(group) in ("primary", "success", "danger"):
        kw["style"] = COLORS[group]
    return InlineKeyboardButton(text=text, callback_data=cb, **kw)


def U(text, url, group="buy"):
    kw = {}
    if COLORS.get(group) in ("primary", "success", "danger"):
        kw["style"] = COLORS[group]
    return InlineKeyboardButton(text=text, url=url, **kw)


def KB(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────────────────── ТЕКСТЫ / ЯЗЫКИ ─────────────────────────
TX = {
    "ru": {
        "welcome": "✨ <b>Добро пожаловать в {shop}!</b> ✨\n\nПривет, <b>{name}</b> 👋\n"
                   "Лучшие товары, мгновенная выдача, честные цены и поддержка 24/7.\n\n🛍 Выберите раздел ниже 👇",
        "rules": "📜 <b>Правила и пользовательское соглашение</b>\n\n"
                 "1. Покупая товар, вы соглашаетесь с этими правилами.\n"
                 "2. Товары цифровые — возврат возможен только при браке, подтверждённом поддержкой.\n"
                 "3. Запрещены мошенничество, перепродажа и попытки обмана системы.\n"
                 "4. Администрация вправе отказать в обслуживании при нарушении правил.\n"
                 "5. Пополнение баланса возврату не подлежит.",
        "faq": "❓ <b>FAQ</b>\n\n<b>Как купить?</b>\nПополните баланс в профиле → выберите товар → подтвердите покупку.\n\n"
               "<b>Как пополнить баланс?</b>\nПрофиль → «Пополнить», оплатите по реквизитам и отправьте чек.\n\n"
               "<b>Товар не пришёл?</b>\nНапишите в поддержку, указав номер покупки.",
        "support": "💬 <b>Поддержка</b>\n\nВозникли вопросы? Напишите нам — отвечаем быстро!",
        "pay_info": "Реквизиты ещё не настроены администратором.",
        "btn_profile": "👤 Профиль", "btn_products": "🛍 Товары", "btn_rules": "📜 Правила / Соглашение",
        "btn_faq": "❓ FAQ", "btn_support": "💬 Поддержка", "btn_lang": "🌐 Язык",
        "back": "⬅️ Назад", "menu_btn": "🏠 Меню", "cancel_btn": "❌ Отмена",
        "profile": "👤 <b>Ваш профиль</b>\n\n🆔 ID: <code>{id}</code>\n📛 Имя: {name}\n"
                   "💰 Баланс: <b>{balance}</b> {cur}\n🛒 Покупок: <b>{purchases}</b>\n📅 Регистрация: {joined}",
        "topup_btn": "➕ Пополнить", "promo_btn": "🎟 Промокод", "orders_btn": "🧾 Мои покупки",
        "choose_lang": "🌐 <b>Выберите язык</b>", "banned": "🚫 Вы заблокированы.",
        "catalog_title": "🛍 <b>Каталог</b>", "empty": "Здесь пока пусто.",
        "reviews_btn": "⭐ Отзывы", "reviews_title": "⭐ <b>Отзывы клиентов</b>", "no_reviews": "Отзывов пока нет.",
        "product": "🛒 <b>{name}</b>\n\n{desc}\n\n💵 Цена: <b>{price}</b> {cur}\n📦 В наличии: <b>{stock}</b>",
        "buy_btn": "💳 Купить", "confirm_btn": "✅ Подтвердить",
        "confirm_buy": "🛒 <b>Подтверждение покупки</b>\n\nТовар: <b>{name}</b>\nЦена: <b>{price}</b> {cur}{price_usd}\n"
                       "Ваш баланс: <b>{balance}</b> {cur}{balance_usd}",
        "no_stock": "❌ Товар закончился.", "product_gone": "❌ Товар недоступен.",
        "no_money": "❌ Недостаточно средств. Не хватает <b>{need}</b> {cur}.",
        "bought": "✅ <b>Покупка успешна!</b>\n\nТовар: <b>{name}</b>\nСписано: {price} {cur}\nЗаказ №{oid}\n\nТовар отправлен сообщением ниже 👇",
        "bought_manual": "✅ <b>Покупка успешна!</b>\n\nТовар: <b>{name}</b>\nСписано: {price} {cur}\nЗаказ №{oid}\n\n"
                         "Администратор свяжется с вами. Если нет — напишите в поддержку.",
        "topup_ask": "💳 <b>Пополнение баланса</b>\n\n{pay}\n\nВведите сумму пополнения (мин. {min} {cur}):",
        "topup_bad": "❌ Введите число не меньше {min} {cur}.",
        "topup_proof": "📸 Теперь отправьте скриншот чека или комментарий к оплате:",
        "topup_sent": "✅ Заявка №{id} на {amount} {cur} отправлена. Ожидайте подтверждения.",
        "topup_ok": "✅ Ваш баланс пополнен на <b>{amount}</b> {cur}. Заявка №{id}.",
        "topup_no": "❌ Заявка №{id} отклонена. Свяжитесь с поддержкой.",
        "promo_ask": "🎟 Введите промокод:", "promo_bad": "❌ Промокод не найден или закончился.",
        "promo_used": "❌ Вы уже использовали этот промокод.", "promo_ok": "✅ Промокод активирован! +{amount} {cur}",
        "no_orders": "У вас пока нет покупок.", "orders_title": "🧾 <b>Последние покупки</b>\n\n",
        "write_support": "✉️ Написать в поддержку",
    },
    "en": {
        "welcome": "✨ <b>Welcome to {shop}!</b> ✨\n\nHi, <b>{name}</b> 👋\n"
                   "Top products, instant delivery, fair prices and 24/7 support.\n\n🛍 Choose a section below 👇",
        "rules": "📜 <b>Rules & Terms of Service</b>\n\n"
                 "1. By buying a product you accept these rules.\n"
                 "2. Products are digital — refunds only for defects confirmed by support.\n"
                 "3. Fraud, resale and abuse are prohibited.\n"
                 "4. Administration may refuse service for violations.\n"
                 "5. Balance top-ups are non-refundable.",
        "faq": "❓ <b>FAQ</b>\n\n<b>How to buy?</b>\nTop up in Profile → choose a product → confirm.\n\n"
               "<b>How to top up?</b>\nProfile → “Top up”, pay using the details and send the receipt.\n\n"
               "<b>Product not received?</b>\nContact support with your order number.",
        "support": "💬 <b>Support</b>\n\nHave questions? Write to us — we reply fast!",
        "pay_info": "Payment details are not configured yet.",
        "btn_profile": "👤 Profile", "btn_products": "🛍 Products", "btn_rules": "📜 Rules / Terms",
        "btn_faq": "❓ FAQ", "btn_support": "💬 Support", "btn_lang": "🌐 Language",
        "back": "⬅️ Back", "menu_btn": "🏠 Menu", "cancel_btn": "❌ Cancel",
        "profile": "👤 <b>Your profile</b>\n\n🆔 ID: <code>{id}</code>\n📛 Name: {name}\n"
                   "💰 Balance: <b>{balance}</b> {cur}\n🛒 Purchases: <b>{purchases}</b>\n📅 Joined: {joined}",
        "topup_btn": "➕ Top up", "promo_btn": "🎟 Promo code", "orders_btn": "🧾 My purchases",
        "choose_lang": "🌐 <b>Choose language</b>", "banned": "🚫 You are banned.",
        "catalog_title": "🛍 <b>Catalog</b>", "empty": "Nothing here yet.",
        "reviews_btn": "⭐ Reviews", "reviews_title": "⭐ <b>Customer reviews</b>", "no_reviews": "No reviews yet.",
        "product": "🛒 <b>{name}</b>\n\n{desc}\n\n💵 Price: <b>{price}</b> {cur}\n📦 In stock: <b>{stock}</b>",
        "buy_btn": "💳 Buy", "confirm_btn": "✅ Confirm",
        "confirm_buy": "🛒 <b>Confirm purchase</b>\n\nProduct: <b>{name}</b>\nPrice: <b>{price}</b> {cur}{price_usd}\n"
                       "Your balance: <b>{balance}</b> {cur}{balance_usd}",
        "no_stock": "❌ Out of stock.", "product_gone": "❌ Product unavailable.",
        "no_money": "❌ Not enough funds. You need <b>{need}</b> {cur} more.",
        "bought": "✅ <b>Purchase successful!</b>\n\nProduct: <b>{name}</b>\nCharged: {price} {cur}\nOrder #{oid}\n\nYour product is in the message below 👇",
        "bought_manual": "✅ <b>Purchase successful!</b>\n\nProduct: <b>{name}</b>\nCharged: {price} {cur}\nOrder #{oid}\n\n"
                         "An administrator will contact you. If not — write to support.",
        "topup_ask": "💳 <b>Top up balance</b>\n\n{pay}\n\nEnter the amount (min. {min} {cur}):",
        "topup_bad": "❌ Enter a number not less than {min} {cur}.",
        "topup_proof": "📸 Now send a receipt screenshot or a payment comment:",
        "topup_sent": "✅ Request #{id} for {amount} {cur} sent. Please wait for confirmation.",
        "topup_ok": "✅ Your balance was topped up by <b>{amount}</b> {cur}. Request #{id}.",
        "topup_no": "❌ Request #{id} was declined. Contact support.",
        "promo_ask": "🎟 Enter promo code:", "promo_bad": "❌ Promo code not found or exhausted.",
        "promo_used": "❌ You have already used this code.", "promo_ok": "✅ Promo activated! +{amount} {cur}",
        "no_orders": "You have no purchases yet.", "orders_title": "🧾 <b>Recent purchases</b>\n\n",
        "write_support": "✉️ Contact support",
    },
}


TX["ru"].update({
    "topup_methods": "💳 <b>Пополнение баланса</b>\n\nВыберите способ оплаты:",
    "crypto_method": "🪙 CryptoBot (автоматически)", "manual_method": "💳 По реквизитам (вручную)",
    "crypto_ask": "🪙 <b>Пополнение через CryptoBot</b>\n\nВведите сумму в {cur} (мин. {min}).\n"
                  "Оплатить можно любой криптовалютой: USDT, TON, BTC и другими:",
    "crypto_invoice_topup": "🪙 <b>Счёт №{id}</b>\n\nПополнение баланса на <b>{amount}</b> {cur}\n"
                            "⏳ Действителен {mins} мин.\n\nНажмите «Оплатить», выберите монету и оплатите в @CryptoBot. "
                            "Баланс пополнится автоматически.",
    "crypto_invoice_buy": "🪙 <b>Счёт №{id}</b>\n\nТовар: <b>{name}</b>\nК оплате: <b>{amount}</b> {cur}\n"
                          "⏳ Действителен {mins} мин.\n\nПосле оплаты товар придёт сюда автоматически.",
    "crypto_pay_btn": "🪙 Оплатить через CryptoBot", "crypto_open_btn": "🪙 Оплатить",
    "crypto_check_btn": "🔄 Проверить оплату",
    "crypto_wait": "⏳ Оплата пока не поступила. Если вы уже оплатили — подождите минуту и проверьте снова.",
    "crypto_expired": "⌛ Счёт истёк. Создайте новый.", "crypto_already": "✅ Этот счёт уже оплачен.",
    "crypto_err": "❌ Не удалось создать счёт. Попробуйте позже или выберите другой способ.",
    "crypto_topup_ok": "✅ Оплата получена! Баланс пополнен на <b>{amount}</b> {cur}.",
    "crypto_gone": "⚠️ Оплата получена, но товар <b>{name}</b> закончился. "
                   "Сумма <b>{amount}</b> {cur} зачислена на ваш баланс.",
})
TX["en"].update({
    "topup_methods": "💳 <b>Top up balance</b>\n\nChoose a payment method:",
    "crypto_method": "🪙 CryptoBot (automatic)", "manual_method": "💳 By details (manual)",
    "crypto_ask": "🪙 <b>Top up via CryptoBot</b>\n\nEnter the amount in {cur} (min. {min}).\n"
                  "You can pay with any crypto: USDT, TON, BTC and more:",
    "crypto_invoice_topup": "🪙 <b>Invoice #{id}</b>\n\nBalance top-up: <b>{amount}</b> {cur}\n"
                            "⏳ Valid for {mins} min.\n\nTap “Pay”, choose a coin and pay in @CryptoBot. "
                            "Your balance will be topped up automatically.",
    "crypto_invoice_buy": "🪙 <b>Invoice #{id}</b>\n\nProduct: <b>{name}</b>\nTo pay: <b>{amount}</b> {cur}\n"
                          "⏳ Valid for {mins} min.\n\nThe product will arrive here automatically after payment.",
    "crypto_pay_btn": "🪙 Pay with CryptoBot", "crypto_open_btn": "🪙 Pay",
    "crypto_check_btn": "🔄 Check payment",
    "crypto_wait": "⏳ Payment not received yet. If you already paid, wait a minute and check again.",
    "crypto_expired": "⌛ Invoice expired. Create a new one.", "crypto_already": "✅ This invoice is already paid.",
    "crypto_err": "❌ Could not create the invoice. Try later or choose another method.",
    "crypto_topup_ok": "✅ Payment received! Balance topped up by <b>{amount}</b> {cur}.",
    "crypto_gone": "⚠️ Payment received, but <b>{name}</b> is out of stock. "
                   "<b>{amount}</b> {cur} was added to your balance.",
})


TX["ru"].update({
    "sub_text": "📢 <b>Подпишитесь на наш канал, чтобы продолжить</b>\n\n"
                "Нажмите «Подписаться», затем вернитесь сюда и нажмите «Я подписался».",
    "sub_btn": "Подписаться", "sub_check": "✅ Я подписался",
    "sub_fail": "❌ Вы ещё не подписаны на все каналы. Подпишитесь и нажмите кнопку снова.",
})
TX["en"].update({
    "sub_text": "📢 <b>Subscribe to our channel to continue</b>\n\n"
                "Tap “Subscribe”, then come back and tap “I subscribed”.",
    "sub_btn": "Subscribe", "sub_check": "✅ I subscribed",
    "sub_fail": "❌ You are not subscribed to all channels yet. Subscribe and tap the button again.",
})


def tx(lang, key, **kw):
    s = TX.get(lang, TX["ru"]).get(key) or TX["ru"][key]
    return s.format(**kw) if kw else s


async def get_text(key, lang):
    """Текст с учётом правок админа (правила, FAQ, поддержка, реквизиты)."""
    if key == "pay_info":
        return await get_setting("pay_info") or tx(lang, "pay_info")
    return await get_setting(f"{key}_{lang}") or tx(lang, key)


async def welcome_text(lang, tg_user):
    raw = await get_setting(f"welcome_{lang}") or tx(lang, "welcome")
    try:
        return raw.format(shop=esc(SHOP_NAME), name=esc(tg_user.first_name))
    except Exception:
        return raw


# ───────────────────────── ОТПРАВКА ЭКРАНОВ (с баннером) ─────────────────────────
async def get_banner():
    fid = await get_setting("banner_file_id")
    if fid:
        return fid
    if BANNER.startswith("http"):
        return BANNER
    if os.path.exists(BANNER):
        return FSInputFile(BANNER)
    return None


async def send_screen(chat_id, text, kb):
    banner = await get_banner()
    if banner and len(text) <= 1024:
        try:
            m = await bot.send_photo(chat_id, banner, caption=text, reply_markup=kb)
            if not isinstance(banner, str) or banner.startswith("http"):
                await set_setting("banner_file_id", m.photo[-1].file_id)
            return m
        except Exception as e:
            log.warning("banner send failed: %s", e)
    return await bot.send_message(chat_id, text, reply_markup=kb)


async def show(target, text, kb):
    """Редактирует текущий экран; при невозможности — пересоздаёт."""
    msg = target.message if isinstance(target, CallbackQuery) else target
    short = len(text) <= 1024
    try:
        if msg.photo and short:
            await msg.edit_caption(caption=text, reply_markup=kb)
            return
        if not msg.photo and not (short and await get_banner()):
            await msg.edit_text(text, reply_markup=kb)
            return
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("edit failed: %s", e)
    try:
        await msg.delete()
    except Exception:
        pass
    await send_screen(msg.chat.id, text, kb)


# ───────────────────────── ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ─────────────────────────
SUB_OK = {}          # user_id -> до какого момента (unix) подписка считается проверенной
SUB_CACHE_SEC = 60


async def get_channels():
    """Список (chat, ссылка). Формат записи: @channel, -1001234567890 или -1001234567890|https://t.me/+invite"""
    raw = await get_setting("sub_channels")
    if raw is None:
        raw = os.getenv("REQUIRED_CHANNELS", "@Smdix")
    out = []
    for part in raw.replace("\n", ",").split(","):
        part, link = part.strip(), None
        if not part:
            continue
        if "|" in part:
            part, link = [x.strip() for x in part.split("|", 1)]
        if part.startswith(("https://t.me/", "http://t.me/", "t.me/")):
            part = "@" + part.rstrip("/").split("/")[-1]
        if not link and part.startswith("@"):
            link = "https://t.me/" + part[1:]
        out.append((part, link))
    return out


async def not_subscribed(user_id):
    """Каналы, на которые пользователь ещё не подписан. Если бот не может проверить канал — пропускаем его."""
    missing = []
    for chat, link in await get_channels():
        cid = int(chat) if chat.lstrip("-").isdigit() else chat
        try:
            m = await bot.get_chat_member(cid, user_id)
            ok = m.status in ("member", "administrator", "creator") or \
                 (m.status == "restricted" and getattr(m, "is_member", False))
        except Exception as e:
            log.warning("Не удалось проверить подписку на %s (бот должен быть админом канала): %s", chat, e)
            ok = True
        if not ok:
            missing.append((chat, link))
    return missing


async def sub_gate(event, user_row):
    """True — можно пускать дальше. Иначе показывает экран «Подпишитесь» и возвращает False."""
    uid = user_row["id"]
    if uid in ADMIN_IDS or SUB_OK.get(uid, 0) > asyncio.get_event_loop().time():
        return True
    missing = await not_subscribed(uid)
    if not missing:
        SUB_OK[uid] = asyncio.get_event_loop().time() + SUB_CACHE_SEC
        return True
    lang = user_row["lang"]
    rows = [[U(f"📢 {tx(lang, 'sub_btn')}" + (f" {chat}" if chat.startswith("@") else ""), link, "buy")]
            for chat, link in missing if link]
    rows.append([B(tx(lang, "sub_check"), "checksub", "buy")])
    if isinstance(event, CallbackQuery):
        await show(event, tx(lang, "sub_text"), KB(rows))
        await event.answer()
    else:
        await send_screen(event.chat.id, tx(lang, "sub_text"), KB(rows))
    return False


# ───────────────────────── MIDDLEWARE ─────────────────────────
class UserMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            row = await q1("SELECT * FROM users WHERE id=?", (u.id,))
            if not row:
                lang = "ru" if (u.language_code or "").split("-")[0] in ("ru", "uk", "be", "kk") else "en"
                await ex("INSERT INTO users(id,username,first_name,lang,joined) VALUES(?,?,?,?,?)",
                         (u.id, u.username, u.first_name, lang, now()))
                row = await q1("SELECT * FROM users WHERE id=?", (u.id,))
            elif row["username"] != u.username or row["first_name"] != u.first_name:
                await ex("UPDATE users SET username=?, first_name=? WHERE id=?", (u.username, u.first_name, u.id))
            if row["banned"] and u.id not in ADMIN_IDS:
                if isinstance(event, CallbackQuery):
                    await event.answer(tx(row["lang"], "banned"), show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(tx(row["lang"], "banned"))
                return
            data["user"] = row
            is_checksub = isinstance(event, CallbackQuery) and event.data == "checksub"
            if isinstance(event, (Message, CallbackQuery)) and not is_checksub:
                if not await sub_gate(event, row):
                    return
        return await handler(event, data)


class ClearStateMW(BaseMiddleware):
    """Нажатие кнопки в пользовательской части отменяет незавершённый ввод."""
    async def __call__(self, handler, event, data):
        st = data.get("state")
        if st:
            await st.clear()
        return await handler(event, data)


# ───────────────────────── СОСТОЯНИЯ ─────────────────────────
class Topup(StatesGroup):
    amount = State()
    proof = State()
    crypto_amount = State()


class PromoS(StatesGroup):
    code = State()


class Adm(StatesGroup):
    price = State()
    stock = State()
    desc = State()
    p_name = State()
    p_price = State()
    p_stock = State()
    p_desc = State()
    p_content = State()
    sec_name = State()
    ann = State()
    rev_author = State()
    rev_text = State()
    user_q = State()
    u_bal = State()
    u_msg = State()
    txt = State()
    rate = State()
    subs = State()
    banner = State()
    promo = State()


user_router = Router()
adm = Router()
user_router.callback_query.outer_middleware(ClearStateMW())
adm.message.filter(F.from_user.id.in_(ADMIN_IDS))
adm.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


# ───────────────────────── ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ─────────────────────────
def main_kb(lang):
    return KB([
        [B(tx(lang, "btn_products"), "cat:0", "menu"), B(tx(lang, "btn_profile"), "profile", "menu")],
        [B(tx(lang, "btn_rules"), "rules", "menu")],
        [B(tx(lang, "btn_faq"), "faq", "menu"), B(tx(lang, "btn_support"), "support", "menu")],
        [B(tx(lang, "btn_lang"), "lang", "menu")],
    ])


def back_menu(lang):
    return KB([[B(tx(lang, "menu_btn"), "menu", "nav")]])


@user_router.message(CommandStart())
async def start(m: Message, state: FSMContext, user):
    await state.clear()
    lang = user["lang"]
    await send_screen(m.chat.id, await welcome_text(lang, m.from_user), main_kb(lang))


@user_router.callback_query(F.data == "checksub")
async def check_sub(cb: CallbackQuery, user):
    lang = user["lang"]
    SUB_OK.pop(user["id"], None)
    if user["id"] not in ADMIN_IDS and await not_subscribed(user["id"]):
        await cb.answer(tx(lang, "sub_fail"), show_alert=True)
        return
    SUB_OK[user["id"]] = asyncio.get_event_loop().time() + SUB_CACHE_SEC
    await show(cb, await welcome_text(lang, cb.from_user), main_kb(lang))
    await cb.answer("✅")


@user_router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, user):
    lang = user["lang"]
    await show(cb, await welcome_text(lang, cb.from_user), main_kb(lang))
    await cb.answer()


@user_router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery, user):
    lang = user["lang"]
    text = tx(lang, "profile", id=user["id"], name=esc(user["first_name"]), balance=fm(user["balance"]),
              cur=cu(user["balance"]), purchases=user["purchases"], joined=user["joined"])
    kb = KB([
        [B(tx(lang, "topup_btn"), "topup", "buy"), B(tx(lang, "promo_btn"), "promo", "buy")],
        [B(tx(lang, "orders_btn"), "myorders", "menu")],
        [B(tx(lang, "menu_btn"), "menu", "nav")],
    ])
    await show(cb, text, kb)
    await cb.answer()


@user_router.callback_query(F.data == "myorders")
async def myorders(cb: CallbackQuery, user):
    lang = user["lang"]
    rows = await q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],))
    if rows:
        text = tx(lang, "orders_title") + "\n".join(
            f"№{r['id']} • {esc(r['product_name'])} • {fm(r['price'])} {CURRENCY} • {r['created']}" for r in rows)
    else:
        text = tx(lang, "no_orders")
    await show(cb, text, KB([[B(tx(lang, "back"), "profile", "nav")]]))
    await cb.answer()


@user_router.callback_query(F.data.in_({"rules", "faq"}))
async def rules_faq(cb: CallbackQuery, user):
    lang = user["lang"]
    await show(cb, await get_text(cb.data, lang), back_menu(lang))
    await cb.answer()


@user_router.callback_query(F.data == "support")
async def support(cb: CallbackQuery, user):
    lang = user["lang"]
    rows = []
    if SUPPORT_USERNAME:
        rows.append([InlineKeyboardButton(text=tx(lang, "write_support"), url=f"https://t.me/{SUPPORT_USERNAME}")])
    rows.append([B(tx(lang, "menu_btn"), "menu", "nav")])
    await show(cb, await get_text("support", lang), KB(rows))
    await cb.answer()


@user_router.callback_query(F.data == "lang")
async def lang_menu(cb: CallbackQuery, user):
    lang = user["lang"]
    kb = KB([[B("🇷🇺 Русский", "setlang:ru", "menu"), B("🇬🇧 English", "setlang:en", "menu")],
             [B(tx(lang, "menu_btn"), "menu", "nav")]])
    await show(cb, tx(lang, "choose_lang"), kb)
    await cb.answer()


@user_router.callback_query(F.data.startswith("setlang:"))
async def set_lang(cb: CallbackQuery):
    lang = cb.data.split(":")[1]
    await ex("UPDATE users SET lang=? WHERE id=?", (lang, cb.from_user.id))
    await show(cb, await welcome_text(lang, cb.from_user), main_kb(lang))
    await cb.answer("✅")


# ── каталог ──
async def section_path(sid):
    parts = []
    while sid:
        r = await q1("SELECT * FROM sections WHERE id=?", (sid,))
        if not r:
            break
        parts.append(r["name"])
        sid = r["parent_id"]
    return " › ".join(reversed(parts))


@user_router.callback_query(F.data.startswith("cat:"))
async def catalog(cb: CallbackQuery, user):
    lang = user["lang"]
    sid = int(cb.data.split(":")[1])
    subs = await q("SELECT * FROM sections WHERE parent_id=? ORDER BY id", (sid,))
    prods = await q("SELECT * FROM products WHERE section_id=? ORDER BY id", (sid,))
    rows = [[B(f"📁 {s['name']}", f"cat:{s['id']}", "menu")] for s in subs]
    for p in prods:
        mark = " ❌" if p["stock"] == 0 else ""
        label = p["name"] + " • " + fm(p["price"]) + CURRENCY + usd(p["price"], SHORT_USD) + mark
        rows.append([B(label, f"prod:{p['id']}", "buy")])
    if sid == 0:
        rows.append([B(tx(lang, "reviews_btn"), "rev:0", "menu")])
        rows.append([B(tx(lang, "menu_btn"), "menu", "nav")])
        title = tx(lang, "catalog_title")
    else:
        cur = await q1("SELECT parent_id FROM sections WHERE id=?", (sid,))
        parent = cur["parent_id"] if cur else 0
        rows.append([B(tx(lang, "back"), f"cat:{parent}", "nav"), B(tx(lang, "menu_btn"), "menu", "nav")])
        title = f"📂 <b>{esc(await section_path(sid))}</b>"
    if not subs and not prods:
        title += "\n\n" + tx(lang, "empty")
    await show(cb, title, KB(rows))
    await cb.answer()


@user_router.callback_query(F.data.startswith("prod:"))
async def product(cb: CallbackQuery, user):
    lang = user["lang"]
    p = await q1("SELECT * FROM products WHERE id=?", (int(cb.data.split(":")[1]),))
    if not p:
        await cb.answer(tx(lang, "product_gone"), show_alert=True)
        return
    text = tx(lang, "product", name=esc(p["name"]), desc=p["description"] or "—",
              price=fm(p["price"]), cur=cu(p["price"]), stock=stock_s(p["stock"]))
    rows = []
    if p["stock"] != 0:
        rows.append([B(tx(lang, "buy_btn"), f"buy:{p['id']}", "buy")])
    rows.append([B(tx(lang, "back"), f"cat:{p['section_id']}", "nav")])
    await show(cb, text, KB(rows))
    await cb.answer()


@user_router.callback_query(F.data.startswith("buy:"))
async def buy_confirm(cb: CallbackQuery, user):
    lang = user["lang"]
    p = await q1("SELECT * FROM products WHERE id=?", (int(cb.data.split(":")[1]),))
    if not p:
        await cb.answer(tx(lang, "product_gone"), show_alert=True)
        return
    if p["stock"] == 0:
        await cb.answer(tx(lang, "no_stock"), show_alert=True)
        return
    text = tx(lang, "confirm_buy", name=esc(p["name"]), price=fm(p["price"]),
              balance=fm(user["balance"]), cur=CURRENCY, price_usd=usd(p["price"]),
              balance_usd=usd(user["balance"]))
    rows = [[B(tx(lang, "confirm_btn"), f"buyok:{p['id']}", "buy"), B(tx(lang, "cancel_btn"), f"prod:{p['id']}", "nav")]]
    if CRYPTO_TOKEN:
        rows.insert(1, [B(tx(lang, "crypto_pay_btn"), f"cpbuy:{p['id']}", "buy")])
    await show(cb, text, KB(rows))
    await cb.answer()


@user_router.callback_query(F.data.startswith("buyok:"))
async def buy_do(cb: CallbackQuery, user):
    lang = user["lang"]
    pid = int(cb.data.split(":")[1])
    async with BUY_LOCK:
        p = await q1("SELECT * FROM products WHERE id=?", (pid,))
        u = await q1("SELECT * FROM users WHERE id=?", (cb.from_user.id,))
        if not p:
            await cb.answer(tx(lang, "product_gone"), show_alert=True)
            return
        if p["stock"] == 0:
            await cb.answer(tx(lang, "no_stock"), show_alert=True)
            return
        if u["balance"] < p["price"]:
            need = fm(p["price"] - u["balance"])
            rows = [[B(tx(lang, "topup_btn"), "topup", "buy")], [B(tx(lang, "back"), f"prod:{pid}", "nav")]]
            if CRYPTO_TOKEN:
                rows.insert(1, [B(tx(lang, "crypto_pay_btn"), f"cpbuy:{pid}", "buy")])
            await show(cb, tx(lang, "no_money", need=need, cur=cu(p["price"] - u["balance"])), KB(rows))
            await cb.answer()
            return
        await db.execute("UPDATE users SET balance=balance-?, purchases=purchases+1 WHERE id=?", (p["price"], u["id"]))
        if p["stock"] > 0:
            await db.execute("UPDATE products SET stock=stock-1 WHERE id=?", (pid,))
        c = await db.execute("INSERT INTO orders(user_id,product_id,product_name,price,created) VALUES(?,?,?,?,?)",
                             (u["id"], pid, p["name"], p["price"], now()))
        await db.commit()
        oid = c.lastrowid
    key = "bought" if p["content"] else "bought_manual"
    await show(cb, tx(lang, key, name=esc(p["name"]), price=fm(p["price"]), cur=CURRENCY, oid=oid), back_menu(lang))
    await cb.answer("✅")
    await after_purchase(u, p, oid)


async def after_purchase(u, p, oid, via=""):
    """Выдача товара покупателю и уведомление админов. p — строка товара ДО списания остатка."""
    if p["content"]:
        await bot.send_message(u["id"], f"📦 <b>{esc(p['name'])}</b>\n\n<code>{esc(p['content'])}</code>")
    left = p["stock"] - 1 if p["stock"] > 0 else p["stock"]
    note = f"🛒 Новая покупка №{oid}{via}\n{esc(p['name'])} — {fm(p['price'])} {CURRENCY}\n" \
           f"Покупатель: {esc(u['first_name'])} (<code>{u['id']}</code>)"
    if not p["content"]:
        note += "\n⚠️ Выдача вручную (контент не задан)!"
    if 0 <= left <= LOW_STOCK:
        note += f"\n📉 Остаток: {left}"
    await notify_admins(note)


async def notify_admins(text):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


@user_router.callback_query(F.data.startswith("rev:"))
async def reviews(cb: CallbackQuery, user):
    lang = user["lang"]
    page, per = int(cb.data.split(":")[1]), 5
    total = (await q1("SELECT COUNT(*) c FROM reviews"))["c"]
    rows = await q("SELECT * FROM reviews ORDER BY id DESC LIMIT ? OFFSET ?", (per, page * per))
    if not rows:
        text = tx(lang, "reviews_title") + "\n\n" + tx(lang, "no_reviews")
    else:
        text = tx(lang, "reviews_title") + "\n\n" + "\n\n".join(
            f"{'⭐' * r['rating']} <b>{esc(r['author'])}</b> • {r['created'][:10]}\n{esc(r['text'])}" for r in rows)
    nav = pager("rev", page, total, per)
    kb = KB(([nav] if nav else []) + [[B(tx(lang, "back"), "cat:0", "nav")]])
    await show(cb, text, kb)
    await cb.answer()


def pager(prefix, page, total, per):
    row = []
    if page > 0:
        row.append(B("◀️", f"{prefix}:{page - 1}", "nav"))
    if (page + 1) * per < total:
        row.append(B("▶️", f"{prefix}:{page + 1}", "nav"))
    return row


# ───────────────────────── CRYPTOBOT (Crypto Pay API) ─────────────────────────
CRYPTO_API = "https://testnet-pay.crypt.bot/api/" if CRYPTO_TESTNET else "https://pay.crypt.bot/api/"
_http = None


class CryptoError(Exception):
    pass


async def crypto_call(method, **params):
    """Вызов метода Crypto Pay API. Возвращает поле result или бросает CryptoError."""
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    params = {k: v for k, v in params.items() if v is not None}
    try:
        async with _http.post(CRYPTO_API + method, json=params,
                              headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN}) as r:
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise CryptoError(f"network: {e}")
    if not isinstance(data, dict) or not data.get("ok"):
        raise CryptoError(str(data.get("error") if isinstance(data, dict) else data))
    return data["result"]


async def crypto_create(user_id, amount, kind, product_id=0, title=""):
    """Создаёт счёт в CryptoBot и запись в БД. Возвращает (id записи, ссылка на оплату)."""
    res = await crypto_call(
        "createInvoice", currency_type="fiat", fiat=CRYPTO_FIAT, accepted_assets=CRYPTO_ASSETS or None,
        amount=fm(amount), description=(title or SHOP_NAME)[:200], payload=f"{kind}:{user_id}",
        expires_in=CRYPTO_TTL_MIN * 60, allow_comments=False, allow_anonymous=False)
    url = res.get("bot_invoice_url") or res.get("pay_url")
    if not url:
        raise CryptoError("no invoice url in response")
    rid = await ex("INSERT INTO crypto_invoices(invoice_id,user_id,amount,kind,product_id,url,created) "
                   "VALUES(?,?,?,?,?,?,?)", (int(res["invoice_id"]), user_id, amount, kind, product_id, url, now()))
    return rid, url


async def crypto_settle(row_id):
    """Идемпотентно обрабатывает оплаченный счёт: пополнение баланса или выдача товара."""
    async with BUY_LOCK:
        inv = await q1("SELECT * FROM crypto_invoices WHERE id=?", (row_id,))
        if not inv or inv["status"] != "active":
            return False
        u = await q1("SELECT * FROM users WHERE id=?", (inv["user_id"],))
        await db.execute("UPDATE crypto_invoices SET status='paid', paid_at=? WHERE id=?", (now(), row_id))
        p, oid, to_balance = None, None, True
        if u and inv["kind"] == "buy":
            p = await q1("SELECT * FROM products WHERE id=?", (inv["product_id"],))
            if p and p["stock"] != 0:
                to_balance = False
                if p["stock"] > 0:
                    await db.execute("UPDATE products SET stock=stock-1 WHERE id=?", (p["id"],))
                await db.execute("UPDATE users SET purchases=purchases+1 WHERE id=?", (u["id"],))
                c = await db.execute("INSERT INTO orders(user_id,product_id,product_name,price,created) "
                                     "VALUES(?,?,?,?,?)", (u["id"], p["id"], p["name"], inv["amount"], now()))
                oid = c.lastrowid
        if u and to_balance:
            await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (inv["amount"], u["id"]))
        await db.commit()
    if not u:
        return True
    lang, amount = u["lang"], fm(inv["amount"])
    try:
        if oid:
            key = "bought" if p["content"] else "bought_manual"
            await bot.send_message(u["id"], tx(lang, key, name=esc(p["name"]), price=amount, cur=CURRENCY, oid=oid),
                                   reply_markup=back_menu(lang))
            await after_purchase(u, dict(p, price=inv["amount"]), oid, via=" (CryptoBot)")
        elif inv["kind"] == "buy":
            await bot.send_message(u["id"], tx(lang, "crypto_gone", name=esc(p["name"] if p else "—"),
                                               amount=amount, cur=CURRENCY), reply_markup=back_menu(lang))
        else:
            await bot.send_message(u["id"], tx(lang, "crypto_topup_ok", amount=amount, cur=CURRENCY),
                                   reply_markup=back_menu(lang))
    except Exception as e:
        log.warning("crypto notify failed: %s", e)
    if not oid:
        await notify_admins(f"🪙 CryptoBot: пополнение +{amount} {CURRENCY}\n"
                            f"{esc(u['first_name'])} (<code>{u['id']}</code>)")
    return True


async def crypto_sync(only_row=None):
    """Спрашивает у CryptoBot статус активных счетов и обрабатывает оплаченные."""
    if only_row:
        rows = await q("SELECT * FROM crypto_invoices WHERE status='active' AND id=?", (only_row,))
    else:
        rows = await q("SELECT * FROM crypto_invoices WHERE status='active'")
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        res = await crypto_call("getInvoices", invoice_ids=",".join(str(r["invoice_id"]) for r in chunk), count=100)
        items = res.get("items", []) if isinstance(res, dict) else res
        statuses = {int(x["invoice_id"]): x.get("status") for x in items}
        for r in chunk:
            s = statuses.get(r["invoice_id"])
            if s == "paid":
                await crypto_settle(r["id"])
            elif s == "expired":
                await ex("UPDATE crypto_invoices SET status='expired' WHERE id=? AND status='active'", (r["id"],))


async def crypto_watcher():
    while True:
        await asyncio.sleep(CRYPTO_POLL)
        try:
            await crypto_sync()
        except Exception as e:
            log.warning("crypto watcher: %s", e)


async def fetch_auto_rate():
    """Курс 1 USDT в валюте счёта из CryptoBot. Возвращает число или None."""
    for r in await crypto_call("getExchangeRates"):
        if r.get("source") == "USDT" and r.get("target") == CRYPTO_FIAT and r.get("is_valid", True):
            return float(r["rate"])
    return None


async def load_rate():
    global USD_RATE
    v = await get_setting("usd_rate")
    if v:
        try:
            USD_RATE = float(v)
        except ValueError:
            pass


async def rate_updater():
    """Пока курс не задан вручную в админке, раз в час берём его из CryptoBot."""
    global USD_RATE
    while True:
        if CRYPTO_TOKEN and not await get_setting("usd_rate"):
            try:
                rate = await fetch_auto_rate()
                if rate:
                    USD_RATE = rate
            except Exception as e:
                log.warning("rate update failed: %s", e)
        await asyncio.sleep(3600)


def invoice_kb(lang, rid, url):
    return KB([[U(tx(lang, "crypto_open_btn"), url, "buy")],
               [B(tx(lang, "crypto_check_btn"), f"cpchk:{rid}", "buy")],
               [B(tx(lang, "menu_btn"), "menu", "nav")]])


@user_router.callback_query(F.data.startswith("cpchk:"))
async def cp_check(cb: CallbackQuery, user):
    lang = user["lang"]
    inv = await q1("SELECT * FROM crypto_invoices WHERE id=? AND user_id=?", (int(cb.data.split(":")[1]), user["id"]))
    if not inv:
        await cb.answer("❌", show_alert=True)
        return
    if inv["status"] == "active":
        try:
            await crypto_sync(inv["id"])
        except CryptoError as e:
            log.warning("crypto check failed: %s", e)
        inv = await q1("SELECT * FROM crypto_invoices WHERE id=?", (inv["id"],))
    if inv["status"] == "paid":
        await cb.answer(tx(lang, "crypto_already"), show_alert=True)
    elif inv["status"] == "expired":
        await cb.answer(tx(lang, "crypto_expired"), show_alert=True)
    else:
        await cb.answer(tx(lang, "crypto_wait"), show_alert=True)


@user_router.callback_query(F.data.startswith("cpbuy:"))
async def cp_buy(cb: CallbackQuery, user):
    lang = user["lang"]
    if not CRYPTO_TOKEN:
        await cb.answer()
        return
    p = await q1("SELECT * FROM products WHERE id=?", (int(cb.data.split(":")[1]),))
    if not p:
        await cb.answer(tx(lang, "product_gone"), show_alert=True)
        return
    if p["stock"] == 0:
        await cb.answer(tx(lang, "no_stock"), show_alert=True)
        return
    try:
        rid, url = await crypto_create(user["id"], p["price"], "buy", p["id"], p["name"])
    except CryptoError as e:
        log.warning("createInvoice failed: %s", e)
        await cb.answer(tx(lang, "crypto_err"), show_alert=True)
        return
    text = tx(lang, "crypto_invoice_buy", id=rid, name=esc(p["name"]), amount=fm(p["price"]),
              cur=cu(p["price"]), mins=CRYPTO_TTL_MIN)
    await show(cb, text, invoice_kb(lang, rid, url))
    await cb.answer()


@user_router.callback_query(F.data == "topup:crypto")
async def topup_crypto(cb: CallbackQuery, state: FSMContext, user):
    lang = user["lang"]
    await state.set_state(Topup.crypto_amount)
    await show(cb, tx(lang, "crypto_ask", min=fm(MIN_TOPUP), cur=CURRENCY),
               KB([[B(tx(lang, "cancel_btn"), "topup", "nav")]]))
    await cb.answer()


@user_router.message(Topup.crypto_amount)
async def topup_crypto_amount(m: Message, state: FSMContext, user):
    lang = user["lang"]
    v = parse_num(m.text)
    if v is None or v < MIN_TOPUP:
        await m.answer(tx(lang, "topup_bad", min=fm(MIN_TOPUP), cur=CURRENCY))
        return
    try:
        rid, url = await crypto_create(user["id"], v, "topup", title=f"{SHOP_NAME}: top-up")
    except CryptoError as e:
        log.warning("createInvoice failed: %s", e)
        await m.answer(tx(lang, "crypto_err"), reply_markup=back_menu(lang))
        await state.clear()
        return
    await state.clear()
    await m.answer(tx(lang, "crypto_invoice_topup", id=rid, amount=fm(v), cur=cu(v), mins=CRYPTO_TTL_MIN),
                   reply_markup=invoice_kb(lang, rid, url))


# ── пополнение ──
@user_router.callback_query(F.data == "topup")
async def topup(cb: CallbackQuery, state: FSMContext, user):
    lang = user["lang"]
    if not CRYPTO_TOKEN:
        await topup_manual(cb, state, user)
        return
    kb = KB([[B(tx(lang, "crypto_method"), "topup:crypto", "buy")],
             [B(tx(lang, "manual_method"), "topup:manual", "buy")],
             [B(tx(lang, "back"), "profile", "nav")]])
    await show(cb, tx(lang, "topup_methods"), kb)
    await cb.answer()


@user_router.callback_query(F.data == "topup:manual")
async def topup_manual(cb: CallbackQuery, state: FSMContext, user):
    lang = user["lang"]
    await state.set_state(Topup.amount)
    text = tx(lang, "topup_ask", pay=await get_text("pay_info", lang), min=fm(MIN_TOPUP), cur=CURRENCY)
    await show(cb, text, KB([[B(tx(lang, "cancel_btn"), "profile", "nav")]]))
    await cb.answer()


@user_router.message(Topup.amount)
async def topup_amount(m: Message, state: FSMContext, user):
    lang = user["lang"]
    v = parse_num(m.text)
    if v is None or v < MIN_TOPUP:
        await m.answer(tx(lang, "topup_bad", min=fm(MIN_TOPUP), cur=CURRENCY))
        return
    await state.update_data(amount=v)
    await state.set_state(Topup.proof)
    await m.answer(tx(lang, "topup_proof"))


@user_router.message(Topup.proof)
async def topup_proof(m: Message, state: FSMContext, user):
    lang = user["lang"]
    amount = (await state.get_data())["amount"]
    await state.clear()
    tid = await ex("INSERT INTO topups(user_id,amount,created) VALUES(?,?,?)", (user["id"], amount, now()))
    await m.answer(tx(lang, "topup_sent", id=tid, amount=fm(amount), cur=CURRENCY), reply_markup=back_menu(lang))
    info = (f"💳 <b>Заявка на пополнение №{tid}</b>\nОт: {esc(user['first_name'])} "
            f"(<code>{user['id']}</code>) @{esc(user['username'])}\nСумма: <b>{fm(amount)}</b> {CURRENCY}")
    kb = KB([[B(f"✅ Принять №{tid}", f"adm:tp:ok:{tid}", "buy"), B(f"❌ Отклонить №{tid}", f"adm:tp:no:{tid}", "buy")]])
    for aid in ADMIN_IDS:
        try:
            await m.copy_to(aid)
            await bot.send_message(aid, info, reply_markup=kb)
        except Exception:
            pass


# ── промокод ──
@user_router.callback_query(F.data == "promo")
async def promo(cb: CallbackQuery, state: FSMContext, user):
    lang = user["lang"]
    await state.set_state(PromoS.code)
    await show(cb, tx(lang, "promo_ask"), KB([[B(tx(lang, "cancel_btn"), "profile", "nav")]]))
    await cb.answer()


@user_router.message(PromoS.code)
async def promo_code(m: Message, state: FSMContext, user):
    lang = user["lang"]
    code = (m.text or "").strip().upper()
    await state.clear()
    p = await q1("SELECT * FROM promos WHERE code=?", (code,))
    if not p or p["uses"] <= 0:
        await m.answer(tx(lang, "promo_bad"), reply_markup=back_menu(lang))
        return
    if await q1("SELECT 1 FROM promo_used WHERE code=? AND user_id=?", (code, user["id"])):
        await m.answer(tx(lang, "promo_used"), reply_markup=back_menu(lang))
        return
    await db.execute("UPDATE promos SET uses=uses-1 WHERE code=?", (code,))
    await db.execute("INSERT INTO promo_used(code,user_id) VALUES(?,?)", (code, user["id"]))
    await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (p["amount"], user["id"]))
    await db.commit()
    await m.answer(tx(lang, "promo_ok", amount=fm(p["amount"]), cur=CURRENCY), reply_markup=back_menu(lang))


# ───────────────────────── АДМИН-ПАНЕЛЬ (/adm) ─────────────────────────
ADM_BTNS = [
    ("💰 Изменить цену", "adm:pick:price:0"), ("➕ Добавить товар", "adm:sec:addp:0"),
    ("📁 Добавить раздел", "adm:newsec"), ("📢 Объявления", "adm:ann"),
    ("⭐ Добавить отзыв", "adm:addrev"), ("👤 Пользователь", "adm:user"),
    ("💾 Бэкап базы", "adm:backup"), ("📦 Изменить остаток", "adm:pick:stock:0"),
    ("🗑 Удалить товар", "adm:pick:del:0"), ("🎨 Цвета кнопок", "adm:colors"),
    ("📊 Статистика", "adm:stats"), ("🧹 Удалить отзыв", "adm:delrev:0"),
    ("💳 Заявки на пополнение", "adm:tps"), ("✏️ Изменить описание", "adm:pick:desc:0"),
    ("🗂 Раздел в раздел", "adm:sec:subsec:0"),
    # дополнительные функции
    ("🗑 Удалить раздел", "adm:sec:delsec:0"), ("📝 Тексты бота", "adm:txt"),
    ("🖼 Баннер", "adm:banner"), ("🎟 Промокоды", "adm:promos"), ("🧾 Последние заказы", "adm:orders"),
    ("💱 Курс доллара", "adm:rate"), ("📣 Обязат. подписка", "adm:subs"),
]
PER = 8


def adm_kb():
    rows = []
    for i in range(0, len(ADM_BTNS), 2):
        rows.append([B(t, c, "admin") for t, c in ADM_BTNS[i:i + 2]])
    return KB(rows)


def to_adm():
    return KB([[B("⬅️ В админ-панель", "adm:menu", "admin")]])


CANCEL = KB([[B("❌ Отмена", "adm:menu", "admin")]])
ADM_TEXT = "🛠 <b>Админ-панель</b>\n\nВыберите действие:"


@adm.message(Command("adm"))
async def adm_cmd(m: Message, state: FSMContext):
    await state.clear()
    await send_screen(m.chat.id, ADM_TEXT, adm_kb())


@adm.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, ADM_TEXT, adm_kb())
    await cb.answer()


# ── выбор товара: цена / остаток / описание / удаление ──
PICK_TITLES = {"price": "💰 <b>Изменить цену</b>", "stock": "📦 <b>Изменить остаток</b>",
               "desc": "✏️ <b>Изменить описание</b>", "del": "🗑 <b>Удалить товар</b>"}


@adm.callback_query(F.data.startswith("adm:pick:"))
async def pick_product(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, action, page = cb.data.split(":")
    page = int(page)
    total = (await q1("SELECT COUNT(*) c FROM products"))["c"]
    if not total:
        await cb.answer("Товаров пока нет", show_alert=True)
        return
    rows = await q("SELECT * FROM products ORDER BY id LIMIT ? OFFSET ?", (PER, page * PER))
    kb = [[B(f"#{r['id']} {r['name']} • {fm(r['price'])}{CURRENCY} • {stock_s(r['stock'])} шт",
             f"adm:p:{action}:{r['id']}", "admin")] for r in rows]
    nav = pager(f"adm:pick:{action}", page, total, PER)
    if nav:
        kb.append(nav)
    kb.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    await show(cb, PICK_TITLES[action] + "\n\nВыберите товар:", KB(kb))
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:p:"))
async def picked_product(cb: CallbackQuery, state: FSMContext):
    _, _, action, pid = cb.data.split(":")
    pid = int(pid)
    p = await q1("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        await cb.answer("Товар не найден", show_alert=True)
        return
    await state.update_data(pid=pid)
    name = esc(p["name"])
    if action == "price":
        await state.set_state(Adm.price)
        await show(cb, f"💰 <b>{name}</b>\nТекущая цена: {fm(p['price'])} {CURRENCY}\n\nВведите новую цену:", CANCEL)
    elif action == "stock":
        await state.set_state(Adm.stock)
        await show(cb, f"📦 <b>{name}</b>\nТекущий остаток: {stock_s(p['stock'])}\n\n"
                       f"Введите новый остаток (число, <code>-1</code> = безлимит):", CANCEL)
    elif action == "desc":
        await state.set_state(Adm.desc)
        await show(cb, f"✏️ <b>{name}</b>\n\nТекущее описание:\n{p['description'] or '—'}\n\n"
                       f"Отправьте новое описание (поддерживается HTML):", CANCEL)
    elif action == "del":
        kb = KB([[B("✅ Да, удалить", f"adm:delok:{pid}", "buy"), B("❌ Нет", "adm:menu", "admin")]])
        await show(cb, f"🗑 Удалить товар <b>{name}</b>?", kb)
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:delok:"))
async def del_product(cb: CallbackQuery):
    await ex("DELETE FROM products WHERE id=?", (int(cb.data.split(":")[2]),))
    await show(cb, "✅ Товар удалён.", to_adm())
    await cb.answer()


@adm.message(Adm.price)
async def st_price(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None or v < 0:
        await m.answer("❌ Введите число, например <code>199.9</code>")
        return
    await ex("UPDATE products SET price=? WHERE id=?", (v, (await state.get_data())["pid"]))
    await state.clear()
    await m.answer(f"✅ Цена обновлена: {fm(v)} {CURRENCY}", reply_markup=to_adm())


@adm.message(Adm.stock)
async def st_stock(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None or v < -1 or v != int(v):
        await m.answer("❌ Введите целое число (≥ 0) или -1 для безлимита")
        return
    await ex("UPDATE products SET stock=? WHERE id=?", (int(v), (await state.get_data())["pid"]))
    await state.clear()
    await m.answer(f"✅ Остаток обновлён: {stock_s(int(v))}", reply_markup=to_adm())


@adm.message(Adm.desc)
async def st_desc(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Отправьте текст")
        return
    await ex("UPDATE products SET description=? WHERE id=?", (m.html_text, (await state.get_data())["pid"]))
    await state.clear()
    await m.answer("✅ Описание обновлено", reply_markup=to_adm())


# ── выбор раздела: добавить товар / раздел в раздел / удалить раздел ──
async def all_section_paths():
    rows = await q("SELECT * FROM sections ORDER BY id")
    by = {r["id"]: r for r in rows}

    def path(i):
        parts = []
        while i and i in by:
            parts.append(by[i]["name"])
            i = by[i]["parent_id"]
        return " › ".join(reversed(parts))
    items = [(r["id"], path(r["id"])) for r in rows]
    items.sort(key=lambda x: x[1])
    return items


SEC_TITLES = {"addp": "➕ <b>Добавить товар</b>\n\nВыберите раздел:",
              "subsec": "🗂 <b>Добавить раздел в раздел</b>\n\nВыберите родительский раздел:",
              "delsec": "🗑 <b>Удалить раздел</b>\n\nВыберите раздел (удалятся и вложенные разделы с товарами):"}


@adm.callback_query(F.data.startswith("adm:sec:"))
async def pick_section(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, action, page = cb.data.split(":")
    page = int(page)
    items = await all_section_paths()
    if not items and action != "addp":
        await cb.answer("Сначала создайте раздел", show_alert=True)
        return
    kb = []
    if action == "addp" and page == 0:
        kb.append([B("🏠 Корень каталога", "adm:s:addp:0", "admin")])
    for sid, path in items[page * PER:(page + 1) * PER]:
        kb.append([B(f"📁 {path}", f"adm:s:{action}:{sid}", "admin")])
    nav = pager(f"adm:sec:{action}", page, len(items), PER)
    if nav:
        kb.append(nav)
    kb.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    await show(cb, SEC_TITLES[action], KB(kb))
    await cb.answer()


@adm.callback_query(F.data == "adm:newsec")
async def new_root_section(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.sec_name)
    await state.update_data(parent=0)
    await show(cb, "📁 <b>Новый раздел</b>\n\nВведите название раздела:", CANCEL)
    await cb.answer()


async def descendants(sid):
    ids, stack = [sid], [sid]
    while stack:
        cur = stack.pop()
        for r in await q("SELECT id FROM sections WHERE parent_id=?", (cur,)):
            ids.append(r["id"])
            stack.append(r["id"])
    return ids


@adm.callback_query(F.data.startswith("adm:s:"))
async def picked_section(cb: CallbackQuery, state: FSMContext):
    _, _, action, sid = cb.data.split(":")
    sid = int(sid)
    if action == "addp":
        await state.set_state(Adm.p_name)
        await state.update_data(section_id=sid)
        await show(cb, "➕ <b>Новый товар</b>\n\nВведите название товара:", CANCEL)
    elif action == "subsec":
        await state.set_state(Adm.sec_name)
        await state.update_data(parent=sid)
        await show(cb, "🗂 Введите название нового вложенного раздела:", CANCEL)
    elif action == "delsec":
        kb = KB([[B("✅ Да, удалить", f"adm:delsecok:{sid}", "buy"), B("❌ Нет", "adm:menu", "admin")]])
        await show(cb, f"🗑 Удалить раздел <b>{esc(await section_path(sid))}</b> со всем содержимым?", kb)
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:delsecok:"))
async def del_section(cb: CallbackQuery):
    ids = await descendants(int(cb.data.split(":")[2]))
    ph = ",".join("?" * len(ids))
    await db.execute(f"DELETE FROM products WHERE section_id IN ({ph})", ids)
    await db.execute(f"DELETE FROM sections WHERE id IN ({ph})", ids)
    await db.commit()
    await show(cb, "✅ Раздел удалён.", to_adm())
    await cb.answer()


@adm.message(Adm.sec_name)
async def st_sec_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        await m.answer("❌ Введите название текстом")
        return
    parent = (await state.get_data())["parent"]
    sid = await ex("INSERT INTO sections(parent_id,name) VALUES(?,?)", (parent, name))
    await state.clear()
    await m.answer(f"✅ Раздел создан (#{sid}): <b>{esc(name)}</b>", reply_markup=to_adm())


# ── добавление товара ──
@adm.message(Adm.p_name)
async def st_p_name(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Введите название текстом")
        return
    await state.update_data(name=m.text.strip())
    await state.set_state(Adm.p_price)
    await m.answer(f"💵 Введите цену ({CURRENCY}):", reply_markup=CANCEL)


@adm.message(Adm.p_price)
async def st_p_price(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None or v < 0:
        await m.answer("❌ Введите число, например <code>199.9</code>")
        return
    await state.update_data(price=v)
    await state.set_state(Adm.p_stock)
    await m.answer("📦 Введите остаток (число, <code>-1</code> = безлимит):", reply_markup=CANCEL)


@adm.message(Adm.p_stock)
async def st_p_stock(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None or v < -1 or v != int(v):
        await m.answer("❌ Введите целое число (≥ 0) или -1")
        return
    await state.update_data(stock=int(v))
    await state.set_state(Adm.p_desc)
    await m.answer("📝 Введите описание товара (HTML разрешён) или <code>-</code> чтобы пропустить:", reply_markup=CANCEL)


@adm.message(Adm.p_desc)
async def st_p_desc(m: Message, state: FSMContext):
    desc = "" if (m.text or "").strip() == "-" else (m.html_text or "")
    await state.update_data(desc=desc)
    await state.set_state(Adm.p_content)
    await m.answer("🎁 Введите, что получит покупатель после оплаты (ключ, ссылка, текст) "
                   "или <code>-</code> — если выдаёте вручную:", reply_markup=CANCEL)


@adm.message(Adm.p_content)
async def st_p_content(m: Message, state: FSMContext):
    content = "" if (m.text or "").strip() == "-" else (m.text or "")
    d = await state.get_data()
    pid = await ex("INSERT INTO products(section_id,name,description,price,stock,content) VALUES(?,?,?,?,?,?)",
                   (d["section_id"], d["name"], d["desc"], d["price"], d["stock"], content))
    await state.clear()
    await m.answer(f"✅ Товар добавлен (#{pid}): <b>{esc(d['name'])}</b> — {fm(d['price'])} {CURRENCY}",
                   reply_markup=to_adm())


# ── объявления (рассылка) ──
@adm.callback_query(F.data == "adm:ann")
async def ann(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.ann)
    await show(cb, "📢 <b>Объявление</b>\n\nОтправьте сообщение для рассылки (текст, фото, видео — как есть):", CANCEL)
    await cb.answer()


@adm.message(Adm.ann)
async def st_ann(m: Message, state: FSMContext):
    n = (await q1("SELECT COUNT(*) c FROM users WHERE banned=0"))["c"]
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    kb = KB([[B(f"✅ Отправить ({n})", "adm:annok", "buy"), B("❌ Отмена", "adm:menu", "admin")]])
    await m.answer(f"Разослать это сообщение <b>{n}</b> пользователям?", reply_markup=kb)


async def broadcast(admin_id, chat_id, msg_id):
    ok = fail = 0
    for u in await q("SELECT id FROM users WHERE banned=0"):
        for _ in range(2):
            try:
                await bot.copy_message(u["id"], chat_id, msg_id)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    await bot.send_message(admin_id, f"📢 Рассылка завершена\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")


@adm.callback_query(F.data == "adm:annok")
async def ann_ok(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    if not d.get("msg_id"):
        await cb.answer("Нечего отправлять", show_alert=True)
        return
    asyncio.create_task(broadcast(cb.from_user.id, d["chat_id"], d["msg_id"]))
    await show(cb, "🚀 Рассылка запущена. Пришлю отчёт по завершении.", to_adm())
    await cb.answer()


# ── отзывы ──
@adm.callback_query(F.data == "adm:addrev")
async def add_review(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rev_author)
    await show(cb, "⭐ <b>Новый отзыв</b>\n\nВведите имя автора:", CANCEL)
    await cb.answer()


@adm.message(Adm.rev_author)
async def st_rev_author(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Введите имя текстом")
        return
    await state.update_data(author=m.text.strip())
    kb = KB([[B("⭐" * i, f"adm:rate:{i}", "admin") for i in (1, 2, 3)],
             [B("⭐" * i, f"adm:rate:{i}", "admin") for i in (4, 5)],
             [B("❌ Отмена", "adm:menu", "admin")]])
    await m.answer("Выберите оценку:", reply_markup=kb)


@adm.callback_query(F.data.startswith("adm:rate:"))
async def rev_rate(cb: CallbackQuery, state: FSMContext):
    await state.update_data(rating=int(cb.data.split(":")[2]))
    await state.set_state(Adm.rev_text)
    await show(cb, "💬 Введите текст отзыва:", CANCEL)
    await cb.answer()


@adm.message(Adm.rev_text)
async def st_rev_text(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Введите текст отзыва")
        return
    d = await state.get_data()
    rid = await ex("INSERT INTO reviews(author,rating,text,created) VALUES(?,?,?,?)",
                   (d["author"], d["rating"], m.text, now()))
    await state.clear()
    await m.answer(f"✅ Отзыв добавлен (#{rid})", reply_markup=to_adm())


@adm.callback_query(F.data.startswith("adm:delrev:"))
async def del_review_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(cb.data.split(":")[2])
    total = (await q1("SELECT COUNT(*) c FROM reviews"))["c"]
    if not total:
        await cb.answer("Отзывов нет", show_alert=True)
        return
    rows = await q("SELECT * FROM reviews ORDER BY id DESC LIMIT ? OFFSET ?", (PER, page * PER))
    kb = [[B(f"🗑 #{r['id']} {r['author']}: {r['text'][:25]}", f"adm:rdel:{r['id']}:{page}", "admin")] for r in rows]
    nav = pager("adm:delrev", page, total, PER)
    if nav:
        kb.append(nav)
    kb.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    await show(cb, "🧹 <b>Удалить отзыв</b>\n\nНажмите на отзыв, чтобы удалить:", KB(kb))
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:rdel:"))
async def del_review(cb: CallbackQuery, state: FSMContext):
    _, _, rid, page = cb.data.split(":")
    await ex("DELETE FROM reviews WHERE id=?", (int(rid),))
    left = (await q1("SELECT COUNT(*) c FROM reviews"))["c"]
    if not left:
        await show(cb, "✅ Отзыв удалён. Отзывов больше нет.", to_adm())
        await cb.answer()
        return
    cb.data = f"adm:delrev:{min(int(page), (left - 1) // PER)}"
    await del_review_list(cb, state)


# ── пользователь ──
async def user_card(uid):
    u = await q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return None, None
    text = (f"👤 <b>{esc(u['first_name'])}</b> @{esc(u['username'])}\n🆔 <code>{u['id']}</code>\n"
            f"💰 Баланс: <b>{fm(u['balance'])}</b> {CURRENCY}\n🛒 Покупок: {u['purchases']}\n"
            f"📅 Регистрация: {u['joined']}\n🌐 Язык: {u['lang']}\n"
            f"Статус: {'🚫 заблокирован' if u['banned'] else '✅ активен'}")
    kb = KB([
        [B("💰 Изменить баланс", f"adm:u:bal:{uid}", "admin"), B("✉️ Написать", f"adm:u:msg:{uid}", "admin")],
        [B("✅ Разбанить" if u["banned"] else "🚫 Забанить", f"adm:u:ban:{uid}", "admin")],
        [B("⬅️ В админ-панель", "adm:menu", "admin")],
    ])
    return text, kb


@adm.callback_query(F.data == "adm:user")
async def user_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.user_q)
    await show(cb, "👤 <b>Пользователь</b>\n\nОтправьте ID или @username:", CANCEL)
    await cb.answer()


@adm.message(Adm.user_q)
async def st_user_q(m: Message, state: FSMContext):
    s = (m.text or "").strip()
    if s.startswith("@"):
        u = await q1("SELECT id FROM users WHERE LOWER(username)=?", (s[1:].lower(),))
    elif s.isdigit():
        u = await q1("SELECT id FROM users WHERE id=?", (int(s),))
    else:
        u = None
    if not u:
        await m.answer("❌ Пользователь не найден. Попробуйте ещё раз:", reply_markup=CANCEL)
        return
    await state.clear()
    text, kb = await user_card(u["id"])
    await m.answer(text, reply_markup=kb)


@adm.callback_query(F.data.startswith("adm:u:"))
async def user_action(cb: CallbackQuery, state: FSMContext):
    _, _, action, uid = cb.data.split(":")
    uid = int(uid)
    if action == "ban":
        u = await q1("SELECT banned FROM users WHERE id=?", (uid,))
        await ex("UPDATE users SET banned=? WHERE id=?", (0 if u["banned"] else 1, uid))
        text, kb = await user_card(uid)
        await show(cb, text, kb)
    elif action == "bal":
        await state.set_state(Adm.u_bal)
        await state.update_data(uid=uid)
        await show(cb, "💰 Введите сумму: <code>100</code> — начислить, <code>-100</code> — списать:", CANCEL)
    elif action == "msg":
        await state.set_state(Adm.u_msg)
        await state.update_data(uid=uid)
        await show(cb, "✉️ Отправьте сообщение пользователю:", CANCEL)
    await cb.answer()


@adm.message(Adm.u_bal)
async def st_u_bal(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None:
        await m.answer("❌ Введите число, например <code>100</code> или <code>-50</code>")
        return
    uid = (await state.get_data())["uid"]
    await ex("UPDATE users SET balance=balance+? WHERE id=?", (v, uid))
    await state.clear()
    text, kb = await user_card(uid)
    await m.answer("✅ Баланс изменён\n\n" + text, reply_markup=kb)


@adm.message(Adm.u_msg)
async def st_u_msg(m: Message, state: FSMContext):
    uid = (await state.get_data())["uid"]
    await state.clear()
    try:
        await m.copy_to(uid)
        await m.answer("✅ Отправлено", reply_markup=to_adm())
    except Exception as e:
        await m.answer(f"❌ Не удалось отправить: {esc(e)}", reply_markup=to_adm())


# ── бэкап ──
def _backup_sync(path):
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


async def send_backup(chat_id, caption="💾 Бэкап базы данных"):
    tmp = f"backup_{datetime.now():%Y%m%d_%H%M%S}.db"
    await asyncio.to_thread(_backup_sync, tmp)
    try:
        await bot.send_document(chat_id, FSInputFile(tmp), caption=caption)
    finally:
        os.remove(tmp)


@adm.callback_query(F.data == "adm:backup")
async def backup(cb: CallbackQuery):
    await cb.answer("Готовлю бэкап…")
    await send_backup(cb.from_user.id)


async def auto_backup():
    while True:
        await asyncio.sleep(24 * 3600)
        if ADMIN_IDS:
            try:
                await send_backup(ADMIN_IDS[0], "💾 Автоматический бэкап (раз в сутки)")
            except Exception as e:
                log.warning("auto backup failed: %s", e)


# ── цвета кнопок ──
def colors_screen():
    rows = [[B(f"{name}: {COLOR_NAMES[COLORS[g]]}", f"adm:cg:{g}", g)] for g, name in GROUPS.items()]
    rows.append([B("🌈 Все кнопки сразу", "adm:cg:all", "menu")])
    rows.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    return ("🎨 <b>Цвета кнопок</b>\n\nВыберите группу кнопок. Цвет применяется сразу "
            "(виден в новых и обновлённых сообщениях)."), KB(rows)


@adm.callback_query(F.data == "adm:colors")
async def colors(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = colors_screen()
    await show(cb, text, kb)
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:cg:"))
async def color_group(cb: CallbackQuery):
    g = cb.data.split(":")[2]
    name = "все кнопки" if g == "all" else GROUPS[g]
    rows = [[InlineKeyboardButton(text=COLOR_NAMES[c], callback_data=f"adm:cs:{g}:{c}",
                                  **({"style": c} if c != "default" else {}))] for c in COLOR_NAMES]
    rows.append([B("⬅️ Назад", "adm:colors", "admin")])
    await show(cb, f"🎨 Выберите цвет для: <b>{name}</b>", KB(rows))
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:cs:"))
async def color_set(cb: CallbackQuery):
    _, _, g, c = cb.data.split(":")
    for grp in (GROUPS if g == "all" else [g]):
        COLORS[grp] = c
        await set_setting("color_" + grp, c)
    text, kb = colors_screen()
    await show(cb, text, kb)
    await cb.answer("✅ Цвет применён")


# ── статистика ──
@adm.callback_query(F.data == "adm:stats")
async def stats(cb: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")

    async def one(sql, args=()):
        return (await q1(sql, args))[0] or 0

    SQL_TOPUPS_OK = "SELECT SUM(amount) FROM topups WHERE status='ok'"
    SQL_TOPUPS_PENDING = "SELECT COUNT(*) FROM topups WHERE status='pending'"
    SQL_CRYPTO_SUM = "SELECT SUM(amount) FROM crypto_invoices WHERE status='paid'"
    SQL_CRYPTO_CNT = "SELECT COUNT(*) FROM crypto_invoices WHERE status='paid'"
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{await one('SELECT COUNT(*) FROM users')}</b> "
        f"(новых сегодня: {await one('SELECT COUNT(*) FROM users WHERE joined LIKE ?', (today + '%',))})\n"
        f"🚫 Забанено: {await one('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
        f"💰 Сумма балансов: {fm(await one('SELECT SUM(balance) FROM users'))} {CURRENCY}\n\n"
        f"🛒 Покупок всего: <b>{await one('SELECT COUNT(*) FROM orders')}</b> "
        f"(сегодня: {await one('SELECT COUNT(*) FROM orders WHERE created LIKE ?', (today + '%',))})\n"
        f"💵 Выручка: <b>{fm(await one('SELECT SUM(price) FROM orders'))}</b> {CURRENCY} "
        f"(сегодня: {fm(await one('SELECT SUM(price) FROM orders WHERE created LIKE ?', (today + '%',)))})\n"
        f"✅ Пополнений: {fm(await one(SQL_TOPUPS_OK))} {CURRENCY}\n"
        f"⏳ Заявок в ожидании: {await one(SQL_TOPUPS_PENDING)}\n"
        f"🪙 CryptoBot: {fm(await one(SQL_CRYPTO_SUM))} {CURRENCY} ({await one(SQL_CRYPTO_CNT)} счетов)\n\n"
        f"📦 Товаров: {await one('SELECT COUNT(*) FROM products')} • "
        f"📁 Разделов: {await one('SELECT COUNT(*) FROM sections')} • "
        f"⭐ Отзывов: {await one('SELECT COUNT(*) FROM reviews')}"
    )
    await show(cb, text, to_adm())
    await cb.answer()


# ── заявки на пополнение ──
async def topups_screen():
    rows = await q("SELECT t.*, u.first_name FROM topups t LEFT JOIN users u ON u.id=t.user_id "
                   "WHERE t.status='pending' ORDER BY t.id LIMIT 10")
    if not rows:
        return "💳 <b>Заявки на пополнение</b>\n\nОжидающих заявок нет.", to_adm()
    text = "💳 <b>Заявки на пополнение</b>\n\n" + "\n".join(
        f"№{r['id']} • {esc(r['first_name'])} (<code>{r['user_id']}</code>) • <b>{fm(r['amount'])}</b> {CURRENCY} • {r['created']}"
        for r in rows)
    kb = [[B(f"✅ №{r['id']}", f"adm:tp:ok:{r['id']}", "buy"), B(f"❌ №{r['id']}", f"adm:tp:no:{r['id']}", "buy")]
          for r in rows]
    kb.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    return text, KB(kb)


@adm.callback_query(F.data == "adm:tps")
async def tps(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await topups_screen()
    await show(cb, text, kb)
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:tp:"))
async def tp_decide(cb: CallbackQuery):
    _, _, action, tid = cb.data.split(":")
    t = await q1("SELECT * FROM topups WHERE id=?", (int(tid),))
    if not t or t["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return
    u = await q1("SELECT lang FROM users WHERE id=?", (t["user_id"],))
    lang = u["lang"] if u else "ru"
    if action == "ok":
        await db.execute("UPDATE topups SET status='ok' WHERE id=?", (t["id"],))
        await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (t["amount"], t["user_id"]))
        await db.commit()
        msg = tx(lang, "topup_ok", amount=fm(t["amount"]), cur=CURRENCY, id=t["id"])
    else:
        await ex("UPDATE topups SET status='no' WHERE id=?", (t["id"],))
        msg = tx(lang, "topup_no", id=t["id"])
    try:
        await bot.send_message(t["user_id"], msg)
    except Exception:
        pass
    text, kb = await topups_screen()
    await show(cb, text, kb)
    await cb.answer("✅ Принято" if action == "ok" else "❌ Отклонено")


# ── тексты бота ──
TEXT_KEYS = {"welcome": "👋 Приветствие", "rules": "📜 Правила", "faq": "❓ FAQ",
             "support": "💬 Поддержка", "pay_info": "💳 Реквизиты оплаты"}


@adm.callback_query(F.data == "adm:txt")
async def txt_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = [[B(v, f"adm:t:{k}", "admin")] for k, v in TEXT_KEYS.items()]
    kb.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    await show(cb, "📝 <b>Тексты бота</b>\n\nЧто изменить?", KB(kb))
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:t:"))
async def txt_pick(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    if key == "pay_info":
        await state.set_state(Adm.txt)
        await state.update_data(skey="pay_info")
        await show(cb, "💳 Отправьте реквизиты оплаты (HTML разрешён):", CANCEL)
    else:
        kb = KB([[B("🇷🇺 Русский", f"adm:tl:{key}:ru", "admin"), B("🇬🇧 English", f"adm:tl:{key}:en", "admin")],
                 [B("⬅️ Назад", "adm:txt", "admin")]])
        await show(cb, "Выберите язык:", kb)
    await cb.answer()


@adm.callback_query(F.data.startswith("adm:tl:"))
async def txt_lang(cb: CallbackQuery, state: FSMContext):
    _, _, key, lang = cb.data.split(":")
    await state.set_state(Adm.txt)
    await state.update_data(skey=f"{key}_{lang}")
    extra = "\nДоступны подстановки: <code>{shop}</code> и <code>{name}</code>." if key == "welcome" else ""
    await show(cb, f"📝 Отправьте новый текст (HTML разрешён).{extra}\n"
                   f"Отправьте <code>-</code>, чтобы вернуть текст по умолчанию.", CANCEL)
    await cb.answer()


@adm.message(Adm.txt)
async def st_txt(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Отправьте текст")
        return
    skey = (await state.get_data())["skey"]
    if m.text.strip() == "-":
        await del_setting(skey)
        await m.answer("✅ Возвращён текст по умолчанию", reply_markup=to_adm())
    else:
        await set_setting(skey, m.html_text)
        await m.answer("✅ Текст обновлён", reply_markup=to_adm())
    await state.clear()


# ── баннер ──
@adm.callback_query(F.data == "adm:banner")
async def banner_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.banner)
    await show(cb, "🖼 Отправьте новое фото для баннера (приветственного изображения):", CANCEL)
    await cb.answer()


@adm.message(Adm.banner, F.photo)
async def st_banner(m: Message, state: FSMContext):
    await set_setting("banner_file_id", m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Баннер обновлён. Проверьте: /start", reply_markup=to_adm())


@adm.message(Adm.banner)
async def st_banner_bad(m: Message):
    await m.answer("❌ Нужно отправить именно фото (не файлом).")


# ── промокоды ──
async def promos_screen():
    rows = await q("SELECT * FROM promos ORDER BY code")
    text = "🎟 <b>Промокоды</b>\n\n" + ("\n".join(
        f"<code>{esc(r['code'])}</code> • +{fm(r['amount'])} {CURRENCY} • осталось: {r['uses']}" for r in rows)
        if rows else "Промокодов пока нет.")
    kb = [[B("➕ Создать промокод", "adm:promonew", "buy")]]
    kb += [[B(f"🗑 {r['code']}", f"adm:promodel:{r['code']}", "admin")] for r in rows]
    kb.append([B("⬅️ В админ-панель", "adm:menu", "admin")])
    return text, KB(kb)


@adm.callback_query(F.data == "adm:promos")
async def promos(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await promos_screen()
    await show(cb, text, kb)
    await cb.answer()


@adm.callback_query(F.data == "adm:promonew")
async def promo_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.promo)
    await show(cb, "🎟 Введите: <code>КОД СУММА КОЛ-ВО_АКТИВАЦИЙ</code>\nНапример: <code>SALE 100 50</code>", CANCEL)
    await cb.answer()


@adm.message(Adm.promo)
async def st_promo(m: Message, state: FSMContext):
    parts = (m.text or "").split()
    amount = parse_num(parts[1]) if len(parts) == 3 else None
    uses = parse_num(parts[2]) if len(parts) == 3 else None
    if amount is None or uses is None or amount <= 0 or uses < 1 or len(parts[0]) > 30:
        await m.answer("❌ Формат: <code>КОД СУММА КОЛ-ВО</code>, например <code>SALE 100 50</code>")
        return
    await ex("INSERT OR REPLACE INTO promos(code,amount,uses) VALUES(?,?,?)", (parts[0].upper(), amount, int(uses)))
    await state.clear()
    await m.answer(f"✅ Промокод <code>{esc(parts[0].upper())}</code> создан", reply_markup=to_adm())


@adm.callback_query(F.data.startswith("adm:promodel:"))
async def promo_del(cb: CallbackQuery):
    await ex("DELETE FROM promos WHERE code=?", (cb.data.split(":", 2)[2],))
    text, kb = await promos_screen()
    await show(cb, text, kb)
    await cb.answer("✅ Удалён")


# ── обязательная подписка ──
@adm.callback_query(F.data == "adm:subs")
async def subs_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.subs)
    chans = await get_channels()
    cur = "\n".join(f"• <code>{esc(c)}</code>" for c, _ in chans) if chans else "выключена"
    await show(cb, f"📣 <b>Обязательная подписка</b>\n\nСейчас: {cur}\n\n"
                   "Отправьте каналы через запятую или с новой строки:\n"
                   "<code>@mychannel</code>\n"
                   "<code>-1001234567890|https://t.me/+invite</code> — приватный канал (ID и ссылка-приглашение)\n\n"
                   "<code>-</code> — выключить подписку.\n"
                   "⚠️ Бот должен быть администратором канала, иначе проверить подписку нельзя.", CANCEL)
    await cb.answer()


@adm.message(Adm.subs)
async def st_subs(m: Message, state: FSMContext):
    s = (m.text or "").strip()
    if not s:
        await m.answer("❌ Отправьте текст")
        return
    await set_setting("sub_channels", "" if s == "-" else s)
    SUB_OK.clear()
    await state.clear()
    chans = await get_channels()
    await m.answer("✅ Подписка выключена" if not chans else
                   "✅ Каналы сохранены: " + ", ".join(esc(c) for c, _ in chans), reply_markup=to_adm())


# ── курс доллара ──
@adm.callback_query(F.data == "adm:rate")
async def rate_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rate)
    mode = "вручную" if await get_setting("usd_rate") else ("авто (CryptoBot)" if CRYPTO_TOKEN else "не задан")
    cur = f"1 $ = {fm(USD_RATE)} {CURRENCY}" if USD_RATE > 0 else "доллары не показываются"
    hint = "\n<code>auto</code> — брать курс из CryptoBot" if CRYPTO_TOKEN else ""
    await show(cb, f"💱 <b>Курс доллара</b>\n\nСейчас: {cur} ({mode})\n\n"
                   f"Введите, сколько {CURRENCY} стоит 1 $ (например <code>90</code>).\n"
                   f"<code>0</code> — не показывать доллары{hint}", CANCEL)
    await cb.answer()


@adm.message(Adm.rate)
async def st_rate(m: Message, state: FSMContext):
    global USD_RATE
    s = (m.text or "").strip().lower()
    if s == "auto" and CRYPTO_TOKEN:
        try:
            rate = await fetch_auto_rate()
        except CryptoError as e:
            await m.answer(f"❌ Не удалось получить курс из CryptoBot: {esc(e)}")
            return
        if not rate:
            await m.answer(f"❌ CryptoBot не вернул курс USDT → {CRYPTO_FIAT}. Введите курс вручную.")
            return
        await del_setting("usd_rate")
        USD_RATE = rate
        await state.clear()
        await m.answer(f"✅ Курс из CryptoBot: 1 $ = {fm(rate)} {CURRENCY} (обновляется раз в час)", reply_markup=to_adm())
        return
    v = parse_num(s)
    if v is None or v < 0:
        await m.answer("❌ Введите число, например <code>90</code>")
        return
    await set_setting("usd_rate", fm(v))
    USD_RATE = v
    await state.clear()
    await m.answer(f"✅ Курс: 1 $ = {fm(v)} {CURRENCY}" if v > 0 else "✅ Доллары отключены", reply_markup=to_adm())


# ── последние заказы ──
@adm.callback_query(F.data == "adm:orders")
async def last_orders(cb: CallbackQuery):
    rows = await q("SELECT * FROM orders ORDER BY id DESC LIMIT 15")
    text = "🧾 <b>Последние заказы</b>\n\n" + ("\n".join(
        f"№{r['id']} • {esc(r['product_name'])} • {fm(r['price'])} {CURRENCY} • <code>{r['user_id']}</code> • {r['created']}"
        for r in rows) if rows else "Заказов пока нет.")
    await show(cb, text, to_adm())
    await cb.answer()


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    global db
    if not TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — админ-панель /adm будет недоступна!")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    await db.executescript(SCHEMA)
    await db.commit()
    await load_colors()
    await load_rate()
    asyncio.create_task(rate_updater())

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMW())
    dp.callback_query.outer_middleware(UserMW())
    dp.include_router(adm)
    dp.include_router(user_router)

    asyncio.create_task(auto_backup())
    if CRYPTO_TOKEN:
        try:
            app = await crypto_call("getMe")
            log.info("CryptoBot подключён: %s%s", app.get("name", "?"), " (testnet)" if CRYPTO_TESTNET else "")
        except CryptoError as e:
            log.warning("CryptoBot: проверьте CRYPTO_TOKEN / CRYPTO_TESTNET (%s)", e)
        asyncio.create_task(crypto_watcher())
    else:
        log.info("CRYPTO_TOKEN не задан — оплата через CryptoBot отключена")
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        if _http and not _http.closed:
            await _http.close()


if __name__ == "__main__":
    asyncio.run(main())
