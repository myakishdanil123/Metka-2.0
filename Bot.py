import os
import re
import math
import time
import sqlite3
import asyncio
import logging
from urllib.parse import urlparse, parse_qs, unquote

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()
VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg").strip()

CITY_RU = "Кривой Рог"
CITY_UA = "Кривий Ріг"

KRYVYI_RIH_LAT_MIN = 47.75
KRYVYI_RIH_LAT_MAX = 48.25
KRYVYI_RIH_LON_MIN = 32.15
KRYVYI_RIH_LON_MAX = 33.90

# Быстрые таймауты. Медленный источник не блокирует бота надолго.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=4.5, connect=2.0)

HEADERS = {
    "User-Agent": "KryvyiRihAddressBot/4.0 (address geocoder)"
}

DB_PATH = os.getenv("ADDRESS_DB", "addresses.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# АЛИАСЫ УЛИЦ
# ============================================================

STREET_ALIASES = {
    "одоевского": ["Одоевского", "Одоєвського", "Казкова"],
    "одоевського": ["Одоевского", "Одоєвського", "Казкова"],
    "казкова": ["Казкова", "Одоєвського", "Одоевского"],
    "дзержинского": ["Дзержинского", "Дзержинського"],
    "фрунзе": ["Фрунзе"],
    "карла маркса": ["Карла Маркса"],
    "волгоградская": ["Волгоградская", "Волгоградська"],
}


# ============================================================
# БАЗА ОБУЧЕНИЯ
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS learned_addresses (
                address_key TEXT PRIMARY KEY,
                street TEXT NOT NULL,
                house TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                source TEXT NOT NULL,
                confirmations INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def learned_get(address_key: str):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM learned_addresses WHERE address_key = ?",
            (address_key,),
        ).fetchone()
        return dict(row) if row else None


def learned_save(address_key, street, house, lat, lon, source, increment=True):
    now = int(time.time())
    with db_connect() as conn:
        current = conn.execute(
            "SELECT confirmations FROM learned_addresses WHERE address_key = ?",
            (address_key,),
        ).fetchone()

        if current:
            confirmations = int(current["confirmations"]) + (1 if increment else 0)
            conn.execute(
                """
                UPDATE learned_addresses
                SET street=?, house=?, lat=?, lon=?, source=?, confirmations=?, updated_at=?
                WHERE address_key=?
                """,
                (street, house, lat, lon, source, confirmations, now, address_key),
            )
        else:
            conn.execute(
                """
                INSERT INTO learned_addresses
                (address_key, street, house, lat, lon, source, confirmations, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (address_key, street, house, lat, lon, source, 1, now),
            )
        conn.commit()


# ============================================================
# БЫСТРЫЙ RAM-КЭШ
# ============================================================

RAM_CACHE = {}
RAM_CACHE_TTL = 30 * 24 * 3600


def cache_get(key):
    item = RAM_CACHE.get(key)
    if not item:
        return None
    if time.time() - item["time"] > RAM_CACHE_TTL:
        RAM_CACHE.pop(key, None)
        return None
    return item["result"]


def cache_set(key, result):
    RAM_CACHE[key] = {"time": time.time(), "result": result}


# ============================================================
# НОРМАЛИЗАЦИЯ / ПАРСИНГ
# ============================================================

def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("ё", "е").replace("’", "'").replace("`", "'")
    text = re.sub(r"[.,;:!?()\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_street(street: str) -> str:
    s = normalize_text(street)
    s = re.sub(r"^(улица|ул\.?|вулиця|вул\.?)\s+", "", s, flags=re.I)
    return s.strip()


def address_key(street: str, house: str) -> str:
    return f"{normalize_street(street)}|{normalize_text(house).replace(' ', '')}"


def parse_address(text: str):
    original = text.strip()
    if len(original) < 4 or len(original) > 120:
        return None

    m = re.match(
        r"^\s*(?P<street>.+?)\s*,?\s+(?P<number>\d+(?:\s*[-/]\s*\d+)?(?:\s*[A-Za-zА-Яа-яІіЇїЄєҐґ])?)\s*$",
        original,
        flags=re.I,
    )
    if not m:
        return None

    street = re.sub(
        r"^(улица|ул\.?|вулиця|вул\.?)\s+",
        "",
        m.group("street").strip(),
        flags=re.I,
    ).strip()
    house = re.sub(r"\s+", "", m.group("number").strip())

    if not re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", street):
        return None

    return {"street": street, "number": house, "original": original}


def street_variants(street: str):
    clean = street.strip()
    variants = [clean]
    variants.extend(STREET_ALIASES.get(normalize_street(clean), []))
    variants.append(normalize_street(clean))

    out, seen = [], set()
    for value in variants:
        value = value.strip()
        key = normalize_text(value)
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


# ============================================================
# ГЕО
# ============================================================

def coordinates_valid(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except Exception:
        return False
    return (
        KRYVYI_RIH_LAT_MIN <= lat <= KRYVYI_RIH_LAT_MAX
        and KRYVYI_RIH_LON_MIN <= lon <= KRYVYI_RIH_LON_MAX
    )


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ============================================================
# VISICOM — варианты ПАРАЛЛЕЛЬНО
# ============================================================

async def _visicom_one(session, street_name, number):
    query = f"{CITY_UA}, {street_name}, {number}"
    url = "https://api.visicom.ua/data-api/5.0/uk/geocode.json"
    params = {
        "categories": "adr_address",
        "text": query,
        "country": "ua",
        "limit": "6",
        "key": VISICOM_KEY,
    }
    try:
        async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                return []
            data = await r.json(content_type=None)
    except Exception:
        return []

    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        for k in ("features", "items", "objects", "result"):
            if isinstance(data.get(k), list):
                items = data[k]
                break

    out = []
    for item in items:
        try:
            props = item.get("properties", {})
            centroid = item.get("geo_centroid") or props.get("geo_centroid")
            lat = lon = None
            if isinstance(centroid, dict):
                coords = centroid.get("coordinates")
                if coords and len(coords) >= 2:
                    lon, lat = float(coords[0]), float(coords[1])
                else:
                    lon, lat = centroid.get("lon"), centroid.get("lat")
            elif isinstance(centroid, list) and len(centroid) >= 2:
                lon, lat = float(centroid[0]), float(centroid[1])

            if not coordinates_valid(lat, lon):
                continue

            text = " ".join(
                str(x) for x in [item.get("name"), props.get("name"), item.get("description"), props.get("description")] if x
            )
            score = 105
            if normalize_text(number) in normalize_text(text):
                score += 25
            if normalize_street(street_name) in normalize_text(text):
                score += 20

            out.append({
                "source": "Visicom",
                "lat": float(lat),
                "lon": float(lon),
                "score": score,
                "name": text,
            })
        except Exception:
            continue
    return out


async def search_visicom(session, street, number):
    if not VISICOM_KEY:
        return []
    variants = street_variants(street)
    answers = await asyncio.gather(
        *[_visicom_one(session, v, number) for v in variants],
        return_exceptions=True,
    )
    out = []
    for x in answers:
        if isinstance(x, list):
            out.extend(x)
    return out


# ============================================================
# MAPBOX — варианты ПАРАЛЛЕЛЬНО
# ============================================================

def parse_mapbox_result(feature):
    try:
        coords = feature.get("geometry", {}).get("coordinates")
        if not coords or len(coords) < 2:
            return None
        lon, lat = float(coords[0]), float(coords[1])
        if not coordinates_valid(lat, lon):
            return None

        p = feature.get("properties", {})
        accuracy = p.get("coordinates", {}).get("accuracy", "")
        match = p.get("match_code", {})
        confidence = match.get("confidence", "")

        score = 20
        score += {"rooftop": 80, "parcel": 70, "point": 65, "interpolated": 40, "approximate": 5}.get(accuracy, 0)
        score += {"exact": 55, "high": 45, "medium": 25, "low": 5}.get(confidence, 0)
        if match.get("address_number") == "matched":
            score += 45
        if match.get("street") == "matched":
            score += 40

        return {
            "source": "Mapbox",
            "lat": lat,
            "lon": lon,
            "score": score,
            "accuracy": accuracy,
            "confidence": confidence,
            "name": p.get("full_address") or feature.get("place_name") or "",
        }
    except Exception:
        return None


async def _mapbox_one(session, street_name, number):
    url = "https://api.mapbox.com/search/geocode/v6/forward"
    queries = [
        {
            "address_number": number,
            "street": street_name,
            "place": CITY_UA,
            "country": "UA",
            "types": "address",
            "limit": "6",
            "autocomplete": "false",
            "language": "uk,ru",
            "access_token": MAPBOX_TOKEN,
        },
        {
            "q": f"{street_name} {number}, {CITY_UA}, Ukraine",
            "country": "UA",
            "types": "address",
            "limit": "6",
            "autocomplete": "false",
            "language": "uk,ru",
            "access_token": MAPBOX_TOKEN,
        },
    ]

    async def request(params):
        try:
            async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
                out = []
                for feature in data.get("features", []):
                    item = parse_mapbox_result(feature)
                    if item:
                        out.append(item)
                return out
        except Exception:
            return []

    responses = await asyncio.gather(*(request(p) for p in queries), return_exceptions=True)
    out = []
    for x in responses:
        if isinstance(x, list):
            out.extend(x)
    return out


async def search_mapbox(session, street, number):
    if not MAPBOX_TOKEN:
        return []
    variants = street_variants(street)
    responses = await asyncio.gather(
        *[_mapbox_one(session, v, number) for v in variants],
        return_exceptions=True,
    )
    out = []
    for x in responses:
        if isinstance(x, list):
            out.extend(x)
    return out


# ============================================================
# OSM / NOMINATIM
# Публичный Nominatim нельзя спамить параллельными запросами.
# Для скорости делаем один лучший запрос. Резервные варианты — только
# если основные источники ничего не дали.
# ============================================================

async def nominatim_one(session, street_name, number):
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "format": "jsonv2",
        "addressdetails": "1",
        "limit": "6",
        "countrycodes": "ua",
        "q": f"{street_name} {number}, {CITY_UA}, Ukraine",
    }
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                return []
            data = await r.json(content_type=None)
    except Exception:
        return []

    out = []
    for item in data:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
            if not coordinates_valid(lat, lon):
                continue
            addr = item.get("address", {})
            house = str(addr.get("house_number") or "")
            road = str(addr.get("road") or addr.get("street") or "")
            score = 20
            if normalize_text(house) == normalize_text(number):
                score += 65
            if road and (normalize_street(street_name) in normalize_text(road) or normalize_text(road) in normalize_street(street_name)):
                score += 50
            if item.get("type") in ("house", "building"):
                score += 25
            out.append({
                "source": "OpenStreetMap",
                "lat": lat,
                "lon": lon,
                "score": score,
                "name": item.get("display_name", ""),
            })
        except Exception:
            continue
    return out


async def search_nominatim_fast(session, street, number):
    variants = street_variants(street)
    # основной запрос — без искусственной задержки
    result = await nominatim_one(session, variants[0], number)
    if result:
        return result

    # один запасной алиас максимум, чтобы не терять секунды
    if len(variants) > 1:
        await asyncio.sleep(1.05)
        return await nominatim_one(session, variants[1], number)
    return []


# ============================================================
# ВЫБОР ЛУЧШЕЙ ТОЧКИ
# ============================================================

def cluster_results(results):
    clusters = []
    for result in results:
        placed = False
        for cluster in clusters:
            # Сравниваем с текущим центром кластера, а не только первой точкой.
            clat = sum(x["lat"] for x in cluster) / len(cluster)
            clon = sum(x["lon"] for x in cluster) / len(cluster)
            if distance_m(clat, clon, result["lat"], result["lon"]) <= 80:
                cluster.append(result)
                placed = True
                break
        if not placed:
            clusters.append([result])
    return clusters


def choose_best(results):
    if not results:
        return None

    candidates = []
    for cluster in cluster_results(results):
        sources = {x["source"] for x in cluster}
        best_item = max(cluster, key=lambda x: x.get("score", 0))

        score = max(x.get("score", 0) for x in cluster)
        # Не суммируем дубликаты одного провайдера без ограничений.
        per_source = {}
        for x in cluster:
            per_source[x["source"]] = max(per_source.get(x["source"], 0), x.get("score", 0))
        score = sum(per_source.values())

        if len(sources) >= 2:
            score += 120
        if len(sources) >= 3:
            score += 170

        # Координату берём у наиболее качественного результата,
        # а не усредняем фасад дома с центром участка.
        candidates.append({
            "lat": best_item["lat"],
            "lon": best_item["lon"],
            "score": score,
            "sources": sources,
            "best": best_item,
            "cluster": cluster,
        })

    return max(candidates, key=lambda x: x["score"])


# ============================================================
# ГЛАВНЫЙ ПОИСК — обучение > RAM > карты
# ============================================================

async def find_address(street, number):
    key = address_key(street, number)

    learned = learned_get(key)
    if learned:
        return {
            "lat": learned["lat"],
            "lon": learned["lon"],
            "score": 10000 + learned["confirmations"],
            "sources": {"Обученная база"},
            "best": {"source": "Обученная база"},
            "learned": True,
            "confirmations": learned["confirmations"],
        }

    cached = cache_get(key)
    if cached:
        return cached

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        tasks = [search_nominatim_fast(session, street, number)]
        if VISICOM_KEY:
            tasks.append(search_visicom(session, street, number))
        if MAPBOX_TOKEN:
            tasks.append(search_mapbox(session, street, number))

        responses = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for response in responses:
        if isinstance(response, list):
            results.extend(response)

    best = choose_best(results)
    if best:
        cache_set(key, best)
    return best


# ============================================================
# GOOGLE MAPS / ИЗВЛЕЧЕНИЕ КООРДИНАТ ИЗ ССЫЛКИ
# ============================================================

def google_maps_link(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat:.7f},{lon:.7f}"


def extract_coords_from_text(text: str):
    # Просто координаты: 47.123, 33.456
    m = re.search(r"(?<!\d)(-?\d{1,2}\.\d+)\s*[,; ]\s*(-?\d{1,3}\.\d+)(?!\d)", text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if coordinates_valid(lat, lon):
            return lat, lon

    decoded = unquote(text)

    # Google /@47.123,33.456,17z
    m = re.search(r"/@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)", decoded)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if coordinates_valid(lat, lon):
            return lat, lon

    # ?query=47.123,33.456 | ?q=...
    try:
        parsed = urlparse(decoded)
        qs = parse_qs(parsed.query)
        for k in ("query", "q", "ll", "center"):
            if k in qs:
                value = qs[k][0]
                m = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", value)
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if coordinates_valid(lat, lon):
                        return lat, lon
    except Exception:
        pass

    # Google data-сегменты иногда содержат !3dLAT!4dLON
    m = re.search(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)", decoded)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if coordinates_valid(lat, lon):
            return lat, lon

    return None


async def resolve_google_coords(session, text: str):
    direct = extract_coords_from_text(text)
    if direct:
        return direct

    # Короткие maps.app.goo.gl / goo.gl ссылки раскрываем редиректом.
    m = re.search(r"https?://\S+", text)
    if not m:
        return None

    url = m.group(0).rstrip(".,)>]")
    host = (urlparse(url).hostname or "").lower()
    if not any(x in host for x in ("google.", "goo.gl", "maps.app.goo.gl")):
        return None

    try:
        async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=6)) as r:
            final_url = str(r.url)
            coords = extract_coords_from_text(final_url)
            if coords:
                return coords
            # Иногда координаты есть в HTML/JS страницы.
            body = await r.text(errors="ignore")
            return extract_coords_from_text(body)
    except Exception:
        return None


# ============================================================
# INLINE-КНОПКИ
# ============================================================

def result_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Метка верна", callback_data="mark_ok"),
            InlineKeyboardButton("🎯 Уточнить", callback_data="mark_fix"),
        ]
    ])


def result_text(street, number, result, elapsed=None, corrected=False):
    lat, lon = result["lat"], result["lon"]
    sources = ", ".join(sorted(result.get("sources", []))) or result.get("source", "")
    link = google_maps_link(lat, lon)

    lines = [
        f"📍 <b>{street}, {number}</b>",
        "",
        f"🎯 <code>{lat:.7f}, {lon:.7f}</code>",
        f"🗺 {sources}",
    ]
    if corrected:
        lines.append("🧠 Точка сохранена как точная")
    elif result.get("learned"):
        lines.append(f"🧠 Из обученной базы · подтверждений: {result.get('confirmations', 1)}")
    if elapsed is not None:
        lines.append(f"⚡ {elapsed:.2f} сек.")
    lines.extend(["", f'👉 <a href="{link}">Открыть в Google Maps</a>'])
    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Поиск домов Кривого Рога\n\n"
        "Напиши, например:\n"
        "Одоевского 45\n\n"
        "После результата:\n"
        "✅ Метка верна — бот запомнит её.\n"
        "🎯 Уточнить — пришли точную ссылку Google Maps, и бот обучится новой точке."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    pending_key = f"{chat_id}:{user_id}"

    # Если пользователь нажал «Уточнить», следующее сообщение — ссылка/координаты.
    pending = context.application.bot_data.setdefault("pending_corrections", {}).get(pending_key)
    if pending:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            coords = await resolve_google_coords(session, text)

        if not coords:
            await update.message.reply_text(
                "Не смог вытащить координаты. Пришли ссылку из Google Maps на саму точку "
                "или координаты вида: 47.123456, 33.123456"
            )
            return

        lat, lon = coords
        learned_save(
            pending["address_key"],
            pending["street"],
            pending["number"],
            lat,
            lon,
            "Google Maps — уточнено пользователем",
            increment=False,
        )
        RAM_CACHE.pop(pending["address_key"], None)
        context.application.bot_data["pending_corrections"].pop(pending_key, None)

        corrected = {
            "lat": lat,
            "lon": lon,
            "sources": {"Google Maps · уточнено"},
        }
        await update.message.reply_text(
            result_text(pending["street"], pending["number"], corrected, corrected=True),
            parse_mode="HTML",
            reply_markup=result_keyboard(),
            disable_web_page_preview=True,
        )
        return

    parsed = parse_address(text)
    if not parsed:
        return

    street, number = parsed["street"], parsed["number"]
    started = time.perf_counter()

    status = await update.message.reply_text(f"🔎 {street}, {number}")
    result = await find_address(street, number)
    elapsed = time.perf_counter() - started

    if not result:
        await status.edit_text(f"❌ Не нашёл: {street}, {number}")
        return

    # Запоминаем данные результата по ID сообщения — кнопки всегда относятся
    # именно к этой конкретной метке.
    result_store = context.application.bot_data.setdefault("result_store", {})
    result_store[f"{chat_id}:{status.message_id}"] = {
        "street": street,
        "number": number,
        "address_key": address_key(street, number),
        "lat": result["lat"],
        "lon": result["lon"],
    }

    await status.edit_text(
        result_text(street, number, result, elapsed=elapsed),
        parse_mode="HTML",
        reply_markup=result_keyboard(),
        disable_web_page_preview=True,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id
    user_id = update.effective_user.id if update.effective_user else 0

    stored = context.application.bot_data.setdefault("result_store", {}).get(f"{chat_id}:{message_id}")
    if not stored:
        await query.answer("Эта метка уже устарела. Найди адрес ещё раз.", show_alert=True)
        return

    if query.data == "mark_ok":
        learned_save(
            stored["address_key"],
            stored["street"],
            stored["number"],
            stored["lat"],
            stored["lon"],
            "Подтверждено пользователем",
            increment=True,
        )
        RAM_CACHE.pop(stored["address_key"], None)
        await query.answer("✅ Запомнил. В следующий раз этот адрес будет мгновенным.", show_alert=True)
        return

    if query.data == "mark_fix":
        pending_key = f"{chat_id}:{user_id}"
        context.application.bot_data.setdefault("pending_corrections", {})[pending_key] = stored.copy()
        await query.answer()
        await query.message.reply_text(
            "🎯 Пришли ссылку на ТОЧНУЮ метку из Google Maps.\n\n"
            "Можно также просто отправить координаты:\n"
            "47.123456, 33.123456"
        )


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не указан BOT_TOKEN")

    init_db()

    logger.info("Бот запускается")
    logger.info("Visicom: %s", "ON" if VISICOM_KEY else "OFF")
    logger.info("Mapbox: %s", "ON" if MAPBOX_TOKEN else "OFF")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(mark_ok|mark_fix)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
