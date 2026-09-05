import os
import re
import time
import math
import asyncio
import logging
from urllib.parse import urlencode

import aiohttp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()
VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg").strip()

CITY_UA = "Кривий Ріг"
CITY_RU = "Кривой Рог"

TIMEOUT = aiohttp.ClientTimeout(total=5)

HEADERS = {
    "User-Agent": "KryvyiRihAddressBot/3.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# =========================================================
# КЭШ
# =========================================================

CACHE = {}

# сколько секунд хранить результат
CACHE_TIME = 86400 * 30


# =========================================================
# СТАРЫЕ / НОВЫЕ НАЗВАНИЯ
# =========================================================

STREET_ALIASES = {
    "одоевского": [
        "Одоевского",
        "Одоєвського",
        "Казкова",
    ],

    "одоевського": [
        "Одоевского",
        "Одоєвського",
        "Казкова",
    ],

    "казкова": [
        "Казкова",
        "Одоєвського",
        "Одоевского",
    ],
}


# =========================================================
# НОРМАЛИЗАЦИЯ
# =========================================================

def norm(text):
    text = text.lower().strip()

    text = text.replace("ё", "е")
    text = text.replace("’", "'")
    text = text.replace("`", "'")

    text = re.sub(r"\s+", " ", text)

    return text


def clean_street(street):
    street = street.strip()

    street = re.sub(
        r"^(улица|ул\.?|вулиця|вул\.?)\s+",
        "",
        street,
        flags=re.IGNORECASE
    )

    return street.strip()


def street_variants(street):

    street = clean_street(street)

    result = [street]

    key = norm(street)

    if key in STREET_ALIASES:
        result.extend(STREET_ALIASES[key])

    # Уникальные варианты
    final = []
    used = set()

    for x in result:

        k = norm(x)

        if k not in used:
            used.add(k)
            final.append(x)

    return final


# =========================================================
# РАСПОЗНАВАНИЕ АДРЕСА
# =========================================================

def parse_address(text):

    text = text.strip()

    if len(text) < 4:
        return None

    # Примеры:
    #
    # Одоевского 45
    # Одоевского, 45
    # ул. Одоевского 45
    # вул. Одоєвського 45
    # Одоевского 45а
    # Одоевского 45/1
    # Одоевского 45-1

    pattern = re.compile(
        r"""
        ^\s*
        (?P<street>.+?)
        \s*,?\s+
        (?P<number>
            \d+
            (?:[-/]\d+)?
            (?:[A-Za-zА-Яа-яІіЇїЄєҐґ])?
        )
        \s*$
        """,
        re.VERBOSE
    )

    match = pattern.match(text)

    if not match:
        return None

    street = clean_street(
        match.group("street")
    )

    number = match.group("number")

    # Защита от обычных сообщений
    if not re.search(
        r"[A-Za-zА-Яа-яІіЇїЄєҐґ]",
        street
    ):
        return None

    if len(street) > 80:
        return None

    return street, number


# =========================================================
# КООРДИНАТЫ
# =========================================================

def valid_coords(lat, lon):

    try:
        lat = float(lat)
        lon = float(lon)
    except:
        return False

    # Грубая область Украины/Кривого Рога
    return (
        47.5 < lat < 48.5
        and
        32.0 < lon < 34.2
    )


def distance_m(lat1, lon1, lat2, lon2):

    R = 6371000

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        +
        math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * R * math.asin(
        math.sqrt(a)
    )


# =========================================================
# VISICOM
# =========================================================

async def visicom(
    session,
    street,
    number
):

    if not VISICOM_KEY:
        return []

    results = []

    variants = street_variants(street)

    # Не надо ждать варианты друг за другом.
    # Запускаем все запросы одновременно.

    async def one_query(street_name):

        query = (
            f"{CITY_UA}, "
            f"{street_name}, "
            f"{number}"
        )

        url = (
            "https://api.visicom.ua/"
            "data-api/5.0/uk/geocode.json"
        )

        params = {
            "categories": "adr_address",
            "text": query,
            "country": "ua",
            "limit": "5",
            "key": VISICOM_KEY,
        }

        try:

            async with session.get(
                url,
                params=params,
                timeout=TIMEOUT
            ) as r:

                if r.status != 200:
                    return []

                data = await r.json(
                    content_type=None
                )

                items = []

                if isinstance(data, dict):
                    items = data.get(
                        "features",
                        []
                    )

                elif isinstance(data, list):
                    items = data

                output = []

                for item in items:

                    props = item.get(
                        "properties",
                        {}
                    )

                    centroid = (
                        item.get("geo_centroid")
                        or
                        props.get("geo_centroid")
                    )

                    if not centroid:
                        continue

                    lat = None
                    lon = None

                    if isinstance(
                        centroid,
                        dict
                    ):

                        coords = centroid.get(
                            "coordinates"
                        )

                        if coords:
                            lon = coords[0]
                            lat = coords[1]

                        else:
                            lon = centroid.get(
                                "lon"
                            )
                            lat = centroid.get(
                                "lat"
                            )

                    elif isinstance(
                        centroid,
                        list
                    ):

                        lon = centroid[0]
                        lat = centroid[1]

                    if not valid_coords(
                        lat,
                        lon
                    ):
                        continue

                    output.append({
                        "source": "Visicom",
                        "lat": float(lat),
                        "lon": float(lon),
                        "score": 100,
                        "name": (
                            item.get("name")
                            or
                            props.get("name")
                            or ""
                        )
                    })

                return output

        except Exception as e:

            logger.debug(
                "Visicom: %s",
                e
            )

            return []

    responses = await asyncio.gather(
        *[
            one_query(x)
            for x in variants
        ],
        return_exceptions=True
    )

    for response in responses:

        if isinstance(
            response,
            list
        ):
            results.extend(response)

    return results


# =========================================================
# MAPBOX
# =========================================================

async def mapbox(
    session,
    street,
    number
):

    if not MAPBOX_TOKEN:
        return []

    url = (
        "https://api.mapbox.com/"
        "search/geocode/v6/forward"
    )

    variants = street_variants(street)

    async def one_query(street_name):

        output = []

        # Структурированный запрос
        params = {
            "address_number": number,
            "street": street_name,
            "place": CITY_UA,
            "country": "UA",
            "types": "address",
            "limit": "5",
            "autocomplete": "false",
            "language": "uk,ru",
            "access_token": MAPBOX_TOKEN
        }

        try:

            async with session.get(
                url,
                params=params,
                timeout=TIMEOUT
            ) as r:

                if r.status != 200:
                    return []

                data = await r.json(
                    content_type=None
                )

                for feature in data.get(
                    "features",
                    []
                ):

                    item = parse_mapbox(
                        feature
                    )

                    if item:
                        output.append(item)

        except Exception as e:

            logger.debug(
                "Mapbox: %s",
                e
            )

        return output

    responses = await asyncio.gather(
        *[
            one_query(x)
            for x in variants
        ],
        return_exceptions=True
    )

    for response in responses:

        if isinstance(
            response,
            list
        ):
            results.extend(response)

    return results


def parse_mapbox(feature):

    try:

        coords = feature[
            "geometry"
        ]["coordinates"]

        lon = float(coords[0])
        lat = float(coords[1])

        if not valid_coords(
            lat,
            lon
        ):
            return None

        props = feature.get(
            "properties",
            {}
        )

        coord_info = props.get(
            "coordinates",
            {}
        )

        accuracy = coord_info.get(
            "accuracy",
            ""
        )

        match = props.get(
            "match_code",
            {}
        )

        confidence = match.get(
            "confidence",
            ""
        )

        score = 0

        # Точность точки
        if accuracy == "rooftop":
            score += 70

        elif accuracy == "parcel":
            score += 65

        elif accuracy == "point":
            score += 60

        elif accuracy == "interpolated":
            score += 40

        elif accuracy == "approximate":
            score += 10

        # Совпадение
        if confidence == "exact":
            score += 50

        elif confidence == "high":
            score += 40

        elif confidence == "medium":
            score += 20

        # Номер дома
        if match.get(
            "address_number"
        ) == "matched":
            score += 40

        # Улица
        if match.get(
            "street"
        ) == "matched":
            score += 40

        return {
            "source": "Mapbox",
            "lat": lat,
            "lon": lon,
            "score": score,
            "accuracy": accuracy,
            "confidence": confidence,
            "name": props.get(
                "full_address",
                ""
            )
        }

    except:
        return None


# =========================================================
# OPENSTREETMAP
# =========================================================

async def nominatim(
    session,
    street,
    number
):

    url = (
        "https://nominatim.openstreetmap.org/"
        "search"
    )

    # Здесь НЕ делаем 10 вариантов.
    # Это главный способ ускорить OSM.
    #
    # Используем несколько самых полезных вариантов.

    variants = street_variants(street)

    variants = variants[:3]

    async def one_query(street_name):

        query = (
            f"{street_name} {number}, "
            f"{CITY_UA}, Ukraine"
        )

        params = {
            "format": "jsonv2",
            "addressdetails": "1",
            "limit": "5",
            "countrycodes": "ua",
            "q": query
        }

        try:

            async with session.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=TIMEOUT
            ) as r:

                if r.status != 200:
                    return []

                data = await r.json(
                    content_type=None
                )

                output = []

                for item in data:

                    try:

                        lat = float(
                            item["lat"]
                        )

                        lon = float(
                            item["lon"]
                        )

                        if not valid_coords(
                            lat,
                            lon
                        ):
                            continue

                        address = item.get(
                            "address",
                            {}
                        )

                        house = address.get(
                            "house_number",
                            ""
                        )

                        road = (
                            address.get(
                                "road",
                                ""
                            )
                            or
                            address.get(
                                "street",
                                ""
                            )
                        )

                        score = 0

                        if house:
                            score += 50

                        if road:
                            score += 40

                        if item.get(
                            "type"
                        ) in (
                            "house",
                            "building"
                        ):
                            score += 30

                        output.append({
                            "source": "OpenStreetMap",
                            "lat": lat,
                            "lon": lon,
                            "score": score,
                            "name": item.get(
                                "display_name",
                                ""
                            )
                        })

                    except:
                        pass

                return output

        except Exception as e:

            logger.debug(
                "Nominatim: %s",
                e
            )

            return []

    # Параллельно
    responses = await asyncio.gather(
        *[
            one_query(x)
            for x in variants
        ],
        return_exceptions=True
    )

    results = []

    for response in responses:

        if isinstance(
            response,
            list
        ):
            results.extend(response)

    return results


# =========================================================
# КЛАСТЕРИЗАЦИЯ
# =========================================================

def choose_best(results):

    if not results:
        return None

    clusters = []

    for item in results:

        placed = False

        for cluster in clusters:

            first = cluster[0]

            d = distance_m(
                first["lat"],
                first["lon"],
                item["lat"],
                item["lon"]
            )

            # Если карты показывают точки
            # в радиусе 100 метров,
            # считаем их одним домом.
            if d <= 100:

                cluster.append(item)
                placed = True
                break

        if not placed:
            clusters.append([item])

    candidates = []

    for cluster in clusters:

        sources = set(
            x["source"]
            for x in cluster
        )

        score = sum(
            x.get("score", 0)
            for x in cluster
        )

        # Совпадение разных карт
        if len(sources) >= 2:
            score += 100

        if len(sources) >= 3:
            score += 150

        # Координаты центра
        lat = sum(
            x["lat"]
            for x in cluster
        ) / len(cluster)

        lon = sum(
            x["lon"]
            for x in cluster
        ) / len(cluster)

        best = max(
            cluster,
            key=lambda x:
            x.get("score", 0)
        )

        candidates.append({
            "lat": lat,
            "lon": lon,
            "score": score,
            "sources": sources,
            "best": best
        })

    candidates.sort(
        key=lambda x:
        x["score"],
        reverse=True
    )

    return candidates[0]


# =========================================================
# ГЛАВНЫЙ ПОИСК
# =========================================================

async def find_address(
    street,
    number
):

    cache_key = (
        norm(street),
        norm(number)
    )

    # =====================================================
    # КЭШ
    # =====================================================

    cached = CACHE.get(
        cache_key
    )

    if cached:

        if time.time() - cached["time"] < CACHE_TIME:

            logger.info(
                "CACHE HIT: %s %s",
                street,
                number
            )

            return cached["result"]

    # =====================================================
    # ВСЕ ИСТОЧНИКИ ОДНОВРЕМЕННО
    # =====================================================

    async with aiohttp.ClientSession(
        headers=HEADERS
    ) as session:

        tasks = [
            nominatim(
                session,
                street,
                number
            )
        ]

        if VISICOM_KEY:

            tasks.append(
                visicom(
                    session,
                    street,
                    number
                )
            )

        if MAPBOX_TOKEN:

            tasks.append(
                mapbox(
                    session,
                    street,
                    number
                )
            )

        responses = await asyncio.gather(
            *tasks,
            return_exceptions=True
        )

    results = []

    for response in responses:

        if isinstance(
            response,
            list
        ):
            results.extend(response)

    best = choose_best(
        results
    )

    # Сохраняем
    CACHE[cache_key] = {
        "time": time.time(),
        "result": best
    }

    logger.info(
        "FOUND %s %s | results=%d",
        street,
        number,
        len(results)
    )

    return best


# =========================================================
# GOOGLE MAPS
# =========================================================

def google_maps(lat, lon):

    params = urlencode({
        "api": "1",
        "query": f"{lat},{lon}"
    })

    return (
        "https://www.google.com/maps/search/"
        f"?{params}"
    )


# =========================================================
# TELEGRAM
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📍 Бот поиска домов Кривого Рога\n\n"
        "Напиши:\n"
        "Одоевского 45\n"
        "Одоєвського 45\n"
        "ул. Одоевского 45\n\n"
        "Обычные сообщения игнорируются."
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = update.message.text

    if not text:
        return

    parsed = parse_address(
        text
    )

    # Не адрес — вообще ничего не делаем
    if not parsed:
        return

    street, number = parsed

    logger.info(
        "SEARCH: %s %s",
        street,
        number
    )

    message = await update.message.reply_text(
        f"🔎 Ищу {street} {number}..."
    )

    start_time = time.time()

    try:

        result = await find_address(
            street,
            number
        )

        elapsed = (
            time.time() - start_time
        )

        if not result:

            await message.edit_text(
                f"❌ Не нашёл:\n"
                f"{street}, {number}\n\n"
                f"Попробуйте другое написание улицы."
            )

            return

        lat = result["lat"]
        lon = result["lon"]

        sources = ", ".join(
            sorted(
                result["sources"]
            )
        )

        link = google_maps(
            lat,
            lon
        )

        await message.edit_text(
            f"📍 <b>{street}, {number}</b>\n\n"
            f"🎯 <code>{lat:.7f}, {lon:.7f}</code>\n\n"
            f"🗺 Источники: {sources}\n"
            f"⚡ Поиск: {elapsed:.2f} сек.\n\n"
            f"👉 <a href=\"{link}\">Открыть метку в Google Maps</a>",
            parse_mode="HTML"
        )

    except Exception:

        logger.exception(
            "SEARCH ERROR"
        )

        await message.edit_text(
            "⚠️ Ошибка поиска."
        )


# =========================================================
# START
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN не указан"
        )

    logger.info(
        "Starting bot..."
    )

    logger.info(
        "Visicom: %s",
        "ON" if VISICOM_KEY else "OFF"
    )

    logger.info(
        "Mapbox: %s",
        "ON" if MAPBOX_TOKEN else "OFF"
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
