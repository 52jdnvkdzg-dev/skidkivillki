"""
СКИДКИ ВИЛКИ — автоматический бот для Telegram-канала.

Что делает:
1) Каждые CHECK_INTERVAL минут открывает публичную ленту новых скидок Pepper.ru.
2) Находит новые карточки скидок.
3) Открывает карточку и извлекает название, текущую/старую цену,
   процент скидки, магазин, описание, изображение и ссылку.
4) Фильтрует предложения по MIN_DISCOUNT.
5) Генерирует СОБСТВЕННЫЙ текст поста.
6) Публикует его в CHANNEL_USERNAME.
7) Не публикует одну и ту же ссылку повторно в рамках сохранённой истории.
8) Команда /scan позволяет запустить проверку вручную.

ВАЖНО:
- Бот должен быть администратором канала с правом "Публиковать сообщения".
- BOT_TOKEN хранится только в Railway Variables.
- Код не копирует посты чужих Telegram-каналов дословно.

Railway Variables:
BOT_TOKEN
CHANNEL_USERNAME=@skidkivilki
AUTO_POST=true
CHECK_INTERVAL=10
MIN_DISCOUNT=20
MAX_POSTS_PER_SCAN=3
OWNER_ID=0   # необязательно; если 0, владелец назначится при первом /start

Дополнительно:
PEPPER_URL=https://www.pepper.ru/new
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@skidkivilki").strip()

AUTO_POST = os.getenv("AUTO_POST", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

CHECK_INTERVAL = max(5, int(os.getenv("CHECK_INTERVAL", "10") or 10))
MIN_DISCOUNT = max(0, int(os.getenv("MIN_DISCOUNT", "20") or 20))
MAX_POSTS_PER_SCAN = max(1, int(os.getenv("MAX_POSTS_PER_SCAN", "3") or 3))
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

PEPPER_URL = os.getenv(
    "PEPPER_URL", "https://www.pepper.ru/new"
).strip()

SEEN_FILE = Path("seen_urls.json")
CONFIG_FILE = Path("bot_config.json")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Mobile/15E148 Safari/604.1"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ============================================================
# ХРАНЕНИЕ НАСТРОЕК / ИСТОРИИ
# ============================================================

def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


runtime_config = read_json(
    CONFIG_FILE,
    {
        "owner_id": OWNER_ID,
        "channel": CHANNEL_USERNAME,
    },
)

if not runtime_config.get("channel"):
    runtime_config["channel"] = CHANNEL_USERNAME

if OWNER_ID and not runtime_config.get("owner_id"):
    runtime_config["owner_id"] = OWNER_ID


def save_config():
    CONFIG_FILE.write_text(
        json.dumps(runtime_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_seen():
    data = read_json(SEEN_FILE, [])
    if not isinstance(data, list):
        return set()
    return set(str(x) for x in data)


seen_urls = load_seen()


def save_seen():
    # Храним достаточно большую историю, но не бесконечно.
    recent = list(seen_urls)[-3000:]
    SEEN_FILE.write_text(
        json.dumps(recent, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_channel():
    return runtime_config.get("channel") or CHANNEL_USERNAME


def get_owner_id():
    return int(runtime_config.get("owner_id") or 0)


def is_owner(message: Message):
    return bool(
        message.from_user
        and get_owner_id()
        and message.from_user.id == get_owner_id()
    )


async def require_owner(message: Message) -> bool:
    if get_owner_id() == 0 and message.from_user:
        runtime_config["owner_id"] = message.from_user.id
        save_config()
        return True

    if not is_owner(message):
        await message.answer("⛔ Эта команда доступна только владельцу бота.")
        return False

    return True


# ============================================================
# ТЕКСТ / ЦЕНЫ
# ============================================================

def clean_text(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = BeautifulSoup(value, "html.parser").get_text(
        " ", strip=True
    )
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    # Убираем query/fragment, чтобы одна скидка не считалась новой
    # из-за рекламных параметров ссылки.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def money_to_float(value):
    if value is None:
        return None

    text = str(value)
    text = text.replace("\u00a0", " ").replace("₽", "").strip()

    match = re.search(
        r"(?<!\d)(\d[\d\s]*(?:[.,]\d{1,2})?)(?!\d)",
        text,
    )
    if not match:
        return None

    number = match.group(1).replace(" ", "").replace(",", ".")

    try:
        value = float(number)
    except ValueError:
        return None

    if not (0 < value <= 100_000_000):
        return None

    return value


def money(value):
    if value is None:
        return "—"

    if abs(value - round(value)) < 0.01:
        return f"{int(round(value)):,}".replace(",", " ") + " ₽"

    return f"{value:,.2f}".replace(",", " ") + " ₽"


def calc_discount(old_price, new_price):
    if (
        old_price is None
        or new_price is None
        or old_price <= new_price
        or new_price <= 0
    ):
        return None

    return round((1 - new_price / old_price) * 100)


def extract_percent(text: str):
    match = re.search(r"(?<!\d)(\d{1,3})\s*%", text or "")
    if not match:
        return None

    value = int(match.group(1))
    return value if 1 <= value <= 100 else None


def extract_prices_from_text(text: str):
    """
    Пытается найти две цены.
    Важнее всего явные значения с ₽.
    Возвращает (old, new).
    """
    text = (text or "").replace("\u00a0", " ")

    values = []
    for match in re.finditer(
        r"(?<!\d)(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)\s*₽",
        text,
    ):
        value = money_to_float(match.group(1))
        if value is not None:
            values.append(value)

    # Убираем повторяющиеся подряд значения.
    unique = []
    for value in values:
        if not unique or abs(unique[-1] - value) > 0.001:
            unique.append(value)

    if len(unique) < 2:
        return None, unique[0] if unique else None

    a, b = unique[0], unique[1]

    # Старая цена должна быть больше текущей.
    if a > b:
        return a, b
    if b > a:
        return b, a

    return None, a


# ============================================================
# HTTP
# ============================================================

async def fetch_text(session: aiohttp.ClientSession, url: str):
    async with session.get(
        url,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=25),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")

        return await response.text(errors="ignore")


def get_meta(soup, *, name=None, prop=None):
    attrs = {}
    if name:
        attrs["name"] = name
    if prop:
        attrs["property"] = prop

    tag = soup.find("meta", attrs=attrs)
    if not tag:
        return ""

    return clean_text(tag.get("content", ""))


# ============================================================
# PEPPER: СПИСОК НОВЫХ СКИДОК
# ============================================================

async def parse_new_links(session):
    html = await fetch_text(session, PEPPER_URL)
    soup = BeautifulSoup(html, "html.parser")

    links = []
    added = set()

    # На Pepper карточки скидок ведут на /deals/...
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()

        if "/deals/" not in href:
            continue

        url = normalize_url(urljoin(PEPPER_URL, href))

        if url in added:
            continue

        added.add(url)
        links.append(url)

        # Берём с запасом, потому что часть карточек может
        # не пройти фильтр по минимальной скидке.
        if len(links) >= 60:
            break

    return links


# ============================================================
# PEPPER: КАРТОЧКА ОТДЕЛЬНОЙ СКИДКИ
# ============================================================

def extract_jsonld_prices(soup):
    current_price = None
    old_price = None

    for script in soup.find_all(
        "script", attrs={"type": "application/ld+json"}
    ):
        raw = script.string or script.get_text(" ", strip=True)

        if not raw:
            continue

        # Не пытаемся полностью доверять JSON-LD структуре:
        # Pepper может менять формат.
        current_matches = re.findall(
            r'"price"\s*:\s*"?(?P<price>\d+(?:[.,]\d+)?)',
            raw,
            flags=re.I,
        )

        for match in current_matches:
            value = money_to_float(match)
            if value is not None and value > 0:
                current_price = value
                break

        if current_price is not None:
            break

    return old_price, current_price


def extract_store(text: str):
    """
    Пытаемся найти магазин по часто встречающимся названиям.
    Если не нашли — оставляем Pepper.ru как источник.
    """
    stores = [
        "OZON",
        "Wildberries",
        "Яндекс Маркет",
        "AliExpress",
        "М.Видео",
        "Эльдорадо",
        "Ситилинк",
        "DNS",
        "Лента",
        "Пятёрочка",
        "Перекрёсток",
        "Магнит",
        "ВинЛаб",
    ]

    low = text.lower()

    for store in stores:
        if store.lower() in low:
            return store

    return None


async def parse_deal(session, url):
    html = await fetch_text(session, url)
    soup = BeautifulSoup(html, "html.parser")

    title = (
        get_meta(soup, prop="og:title")
        or get_meta(soup, name="twitter:title")
    )

    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    title = clean_text(title)
    title = re.sub(r"\s*\|\s*Pepper.*$", "", title, flags=re.I)
    title = title[:220]

    description = (
        get_meta(soup, prop="og:description")
        or get_meta(soup, name="description")
    )
    description = clean_text(description)

    image = (
        get_meta(soup, prop="og:image")
        or get_meta(soup, name="twitter:image")
    )

    body_text = clean_text(soup.get_text(" ", strip=True))

    # Сначала пытаемся взять цену из JSON-LD.
    _, jsonld_current = extract_jsonld_prices(soup)

    old_price, new_price = extract_prices_from_text(
        " ".join(
            part
            for part in [
                description,
                body_text[:12000],
            ]
            if part
        )
    )

    if jsonld_current is not None:
        new_price = jsonld_current

    percent = extract_percent(description)

    if percent is None:
        percent = extract_percent(body_text[:12000])

    # Если Pepper показывает старую и новую цену,
    # вычисляем скидку самостоятельно.
    calculated_percent = calc_discount(old_price, new_price)

    if calculated_percent is not None:
        percent = calculated_percent

    if not title or new_price is None:
        return None

    if percent is None or percent < MIN_DISCOUNT:
        return None

    store = extract_store(
        " ".join([title, description, body_text[:5000]])
    )

    return {
        "url": url,
        "title": title,
        "description": description,
        "image": image,
        "old_price": old_price,
        "new_price": new_price,
        "discount": percent,
        "store": store,
    }


async def collect_deals():
    connector = aiohttp.TCPConnector(limit=10, ssl=False)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }

    timeout = aiohttp.ClientTimeout(total=35)

    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=timeout,
    ) as session:

        links = await parse_new_links(session)

        deals = []

        for url in links:
            if url in seen_urls:
                continue

            try:
                deal = await parse_deal(session, url)

                if deal:
                    deals.append(deal)

            except Exception as exc:
                logging.warning(
                    "Не удалось обработать %s: %s",
                    url,
                    exc,
                )

            # За один проход не публикуем слишком много.
            if len(deals) >= MAX_POSTS_PER_SCAN:
                break

        return deals


# ============================================================
# ГЕНЕРАЦИЯ СОБСТВЕННОГО ПОСТА
# ============================================================

def make_post(deal):
    title = html_lib.escape(deal["title"])
    description = html_lib.escape(
        (deal.get("description") or "").strip()
    )

    # Описание делаем коротким, чтобы пост оставался читаемым.
    if len(description) > 350:
        description = description[:347].rstrip() + "..."

    lines = [
        f"🐺 <b>{title}</b>",
        "",
        f"🔥 <b>СКИДКА −{deal['discount']}%</b>",
    ]

    if deal.get("old_price") is not None:
        lines.append(
            f"💰 Было: <s>{money(deal['old_price'])}</s>"
        )

    lines.append(
        f"💸 Сейчас: <b>{money(deal['new_price'])}</b>"
    )

    if deal.get("store"):
        lines.append(f"🛍 Магазин: <b>{html_lib.escape(deal['store'])}</b>")

    if description:
        lines.extend([
            "",
            f"📝 {description}",
        ])

    lines.extend([
        "",
        "⚡️ Цена и наличие могут измениться — проверь их перед покупкой.",
        "",
        "Источник: Pepper.ru",
    ])

    return "\n".join(lines)


def buy_keyboard(url):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 ЗАБРАТЬ СКИДКУ",
                    url=url,
                )
            ]
        ]
    )


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

async def publish_deal(deal):
    channel = get_channel()

    text = make_post(deal)
    keyboard = buy_keyboard(deal["url"])

    try:
        # Фото — только если Telegram принимает URL изображения.
        # Если изображение не отправилось, делаем обычный текстовый пост.
        if deal.get("image"):
            try:
                await bot.send_photo(
                    chat_id=channel,
                    photo=deal["image"],
                    caption=text[:1024],
                    reply_markup=keyboard,
                )
            except Exception:
                logging.warning(
                    "Фото не отправилось, публикую текстом: %s",
                    deal["url"],
                )

                await bot.send_message(
                    chat_id=channel,
                    text=text,
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                )
        else:
            await bot.send_message(
                chat_id=channel,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )

        seen_urls.add(deal["url"])
        save_seen()

        logging.info("Опубликовано: %s", deal["title"])
        return True

    except Exception:
        logging.exception(
            "Не удалось опубликовать скидку: %s",
            deal["url"],
        )
        return False


async def scan_and_publish():
    deals = await collect_deals()

    published = 0

    for deal in deals:
        ok = await publish_deal(deal)

        if ok:
            published += 1

        await asyncio.sleep(2)

    return published, len(deals)


# ============================================================
# АВТОМАТИЧЕСКИЙ ЦИКЛ
# ============================================================

async def auto_worker():
    if not AUTO_POST:
        logging.info(
            "AUTO_POST=false — автоматическая публикация выключена."
        )
        return

    # Не стартуем мгновенно после деплоя.
    await asyncio.sleep(15)

    while True:
        try:
            published, found = await scan_and_publish()

            logging.info(
                "Автопроверка завершена: найдено=%s опубликовано=%s",
                found,
                published,
            )

        except Exception:
            logging.exception(
                "Ошибка автоматической проверки Pepper.ru"
            )

        await asyncio.sleep(CHECK_INTERVAL * 60)


# ============================================================
# TELEGRAM КОМАНДЫ
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not message.from_user:
        return

    # Если OWNER_ID не задан, первый /start становится владельцем.
    if get_owner_id() == 0:
        runtime_config["owner_id"] = message.from_user.id
        save_config()

    if not is_owner(message):
        await message.answer(
            "🐺 Привет! Я бот канала «Скидки Вилки»."
        )
        return

    await message.answer(
        "🐺 <b>СКИДКИ ВИЛКИ</b>\n\n"
        f"📢 Канал: <b>{html_lib.escape(get_channel())}</b>\n"
        f"🤖 Автопостинг: <b>{'ВКЛ' if AUTO_POST else 'ВЫКЛ'}</b>\n"
        f"🔥 Минимальная скидка: <b>{MIN_DISCOUNT}%</b>\n"
        f"⏱ Проверка: каждые <b>{CHECK_INTERVAL} мин.</b>\n\n"
        "Команды:\n"
        "/scan — проверить прямо сейчас\n"
        "/test — проверить публикацию в канал\n"
        "/settings — настройки\n"
        "/setchannel @канал — сменить канал\n"
        "/id — показать Telegram ID"
    )


@dp.message(Command("id"))
async def id_handler(message: Message):
    await message.answer(
        f"🆔 Твой Telegram ID: <code>{message.from_user.id}</code>"
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    if not await require_owner(message):
        return

    await message.answer(
        "🐺 <b>КОМАНДЫ БОТА</b>\n\n"
        "/scan — немедленно проверить Pepper.ru\n"
        "/test — тестовая публикация\n"
        "/settings — показать настройки\n"
        "/setchannel @skidkivilki — установить канал\n"
        "/id — показать Telegram ID\n\n"
        f"Автоматическая проверка: каждые {CHECK_INTERVAL} мин."
    )


@dp.message(Command("settings"))
async def settings_handler(message: Message):
    if not await require_owner(message):
        return

    await message.answer(
        "⚙️ <b>НАСТРОЙКИ</b>\n\n"
        f"📢 Канал: <code>{html_lib.escape(get_channel())}</code>\n"
        f"🤖 AUTO_POST: <code>{AUTO_POST}</code>\n"
        f"⏱ CHECK_INTERVAL: <code>{CHECK_INTERVAL} мин.</code>\n"
        f"🔥 MIN_DISCOUNT: <code>{MIN_DISCOUNT}%</code>\n"
        f"📦 MAX_POSTS_PER_SCAN: <code>{MAX_POSTS_PER_SCAN}</code>\n"
        f"🔎 Источник: <code>{html_lib.escape(PEPPER_URL)}</code>"
    )


@dp.message(Command("setchannel"))
async def setchannel_handler(message: Message):
    if not await require_owner(message):
        return

    args = message.text.split(maxsplit=1) if message.text else []

    if len(args) < 2:
        await message.answer(
            "Пример:\n<code>/setchannel @skidkivilki</code>"
        )
        return

    channel = args[1].strip()

    if not channel.startswith("@"):
        channel = "@" + channel

    runtime_config["channel"] = channel
    save_config()

    await message.answer(
        f"✅ Канал сохранён: <b>{html_lib.escape(channel)}</b>"
    )


@dp.message(Command("test"))
async def test_handler(message: Message):
    if not await require_owner(message):
        return

    try:
        sent = await bot.send_message(
            chat_id=get_channel(),
            text=(
                "🐺 <b>СКИДКИ ВИЛКИ</b>\n\n"
                "Канал подключён правильно ✅\n\n"
                "Автоматический сбор скидок готов."
            ),
        )

        await message.answer(
            "✅ Тестовый пост опубликован в канале.\n"
            f"ID сообщения: <code>{sent.message_id}</code>"
        )

    except Exception as exc:
        await message.answer(
            "❌ Не удалось опубликовать тест.\n\n"
            "Проверь, что @skidkivilki_bot добавлен "
            "администратором канала и имеет право "
            "«Публиковать сообщения».\n\n"
            f"<code>{html_lib.escape(str(exc)[:1200])}</code>"
        )


@dp.message(Command("scan"))
async def scan_handler(message: Message):
    if not await require_owner(message):
        return

    status = await message.answer(
        "🔎 <b>Проверяю Pepper.ru…</b>\n"
        "Это может занять немного времени."
    )

    try:
        published, found = await scan_and_publish()

        await status.edit_text(
            "🐺 <b>Проверка завершена</b>\n\n"
            f"🔎 Подходящих новых скидок: <b>{found}</b>\n"
            f"📢 Опубликовано: <b>{published}</b>"
        )

    except Exception as exc:
        logging.exception("Ошибка /scan")

        await status.edit_text(
            "❌ <b>Ошибка проверки</b>\n\n"
            f"<code>{html_lib.escape(str(exc)[:1500])}</code>"
        )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден BOT_TOKEN. Добавь его в Railway Variables."
        )

    save_config()

    # Бот работает через polling.
    await bot.delete_webhook(drop_pending_updates=True)

    logging.info(
        "Бот запущен | канал=%s | авто=%s | интервал=%s мин | минимум=%s%%",
        get_channel(),
        AUTO_POST,
        CHECK_INTERVAL,
        MIN_DISCOUNT,
    )

    worker = asyncio.create_task(auto_worker())

    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()

        try:
            await worker
        except asyncio.CancelledError:
            pass

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
