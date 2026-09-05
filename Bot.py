import os
import re
import json
import math
import time
import asyncio
import logging
from pathlib import Path
from urllib.parse import quote

import aiohttp

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()

VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg05:04").strip()
TOMTOM_KEY = os.getenv("TOMTOM_KEY", "").strip()

CITY_RU = "Кривой Рог"
CITY_UA = "Кривий Ріг"
COUNTRY_RU = "Украина"
COUNTRY_UA = "Україна"

# Центр Кривого Рога
CITY_LAT = 47.9105
CITY_LON = 33.3918

# Допустимая область
KRYVYI_RIH_LAT_MIN = 47.70
KRYVYI_RIH_LAT_MAX = 48.30

KRYVYI_RIH_LON_MIN = 32.10
KRYVYI_RIH_LON_MAX = 34.00


# ============================================================
# КЭШ
# ============================================================

CACHE_FILE = Path("cache.json")

cache = {}

# Чтобы два одинаковых адреса одновременно не запускали
# несколько одинаковых запросов
inflight = {}

cache_lock = asyncio.Lock()


# ============================================================
# HTTP
# ============================================================

TIMEOUT = aiohttp.ClientTimeout(
    total=7,
    connect=3,
    sock_read=5,
)

HEADERS = {
    "User-Agent": (
        "KryvyiRihAddressBot/4.0 "
        "(Telegram address geocoder)"
    )
}


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# АЛИАСЫ
# ============================================================

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

    "дзержинского": [
        "Дзержинского",
        "Дзержинського",
    ],

    "дзержинського": [
        "Дзержинского",
        "Дзержинського",
    ],

    "фрунзе": [
        "Фрунзе",
    ],

    "карла маркса": [
        "Карла Маркса",
    ],

    "волгоградская": [
        "Волгоградская",
        "Волгоградська",
    ],

    "волгоградська": [
        "Волгоградская",
        "Волгоградська",
    ],
}


# ============================================================
# ЗАГРУЗКА КЭША
# ============================================================

def load_cache():

    global cache

    if not CACHE_FILE.exists():

        cache = {}

        logger.info(
            "CACHE: файл отсутствует"
        )

        return

    try:

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            cache = json.load(f)

        logger.info(
            "CACHE: загружено %s адресов",
            len(cache)
        )

    except Exception as e:

        logger.error(
            "CACHE LOAD ERROR: %s",
            e
        )

        cache = {}


# ============================================================
# СОХРАНЕНИЕ КЭША
# ============================================================

def save_cache():

    try:

        temp_file = CACHE_FILE.with_suffix(
            ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                cache,
                f,
                ensure_ascii=False,
                indent=2
            )

        temp_file.replace(
            CACHE_FILE
        )

    except Exception as e:

        logger.error(
            "CACHE SAVE ERROR: %s",
            e
        )


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================

def normalize_text(text: str) -> str:

    text = text.lower().strip()

    text = text.replace(
        "ё",
        "е"
    )

    text = text.replace(
        "’",
        "'"
    )

    text = text.replace(
        "`",
        "'"
    )

    text = re.sub(
        r"[.,;:!?(){}\[\]]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def normalize_street(street: str) -> str:

    s = normalize_text(
        street
    )

    prefixes = [
        r"^улица\s+",
        r"^ул\s+",
        r"^ул\s*\.\s*",
        r"^вулиця\s+",
        r"^вул\s+",
        r"^вул\s*\.\s*",
        r"^проспект\s+",
        r"^просп\.\s*",
        r"^провулок\s+",
        r"^пров\.\s*",
        r"^бульвар\s+",
        r"^бул\.\s*",
    ]

    for pattern in prefixes:

        s = re.sub(
            pattern,
            "",
            s
        )

    return s.strip()


# ============================================================
# РАЗБОР АДРЕСА
# ============================================================

def parse_address(text: str):

    original = text.strip()

    if len(original) < 4:
        return None

    pattern = re.compile(
        r"""
        ^\s*

        (?P<street>.+?)

        \s+

        (?P<number>
            \d+
            (?:\s*[-/]\s*\d+)?
            (?:\s*[A-Za-zА-Яа-яІіЇїЄєҐґ])?
        )

        \s*$

        """,
        re.VERBOSE
    )

    match = pattern.match(
        original
    )

    if not match:
        return None

    street = match.group(
        "street"
    ).strip()

    number = match.group(
        "number"
    ).strip()

    street = re.sub(
        r"^(улица|ул\.?|вулиця|вул\.?|"
        r"проспект|просп\.?|"
        r"провулок|пров\.?)\s+",
        "",
        street,
        flags=re.IGNORECASE
    )

    street = street.strip()

    if not re.search(
        r"[A-Za-zА-Яа-яІіЇїЄєҐґ]",
        street
    ):
        return None

    if len(street) > 80:
        return None

    return {
        "original": original,
        "street": street,
        "number": number,
    }


# ============================================================
# ПРОВЕРКА: ЭТО АДРЕСА ИЛИ НЕТ
# ============================================================

def is_address(text: str) -> bool:

    return parse_address(
        text
    ) is not None


# ============================================================
# ВАРИАНТЫ УЛИЦЫ
# ============================================================

def street_variants(
    street: str
):

    result = []

    clean = street.strip()

    key = normalize_street(
        clean
    )

    result.append(
        clean
    )

    if key in STREET_ALIASES:

        result.extend(
            STREET_ALIASES[key]
        )

    result.append(
        normalize_street(clean)
    )

    final = []

    existing = set()

    for value in result:

        value = value.strip()

        normalized = normalize_text(
            value
        )

        if not value:
            continue

        if normalized in existing:
            continue

        existing.add(
            normalized
        )

        final.append(
            value
        )

    return final


# ============================================================
# КЛЮЧ КЭША
# ============================================================

def cache_key(
    street: str,
    number: str
):

    return normalize_text(
        f"{street} {number}"
    )


# ============================================================
# КООРДИНАТЫ
# ============================================================

def coordinates_valid(
    lat,
    lon
):

    try:

        lat = float(lat)
        lon = float(lon)

    except Exception:

        return False

    return (
        KRYVYI_RIH_LAT_MIN
        <= lat
        <= KRYVYI_RIH_LAT_MAX

        and

        KRYVYI_RIH_LON_MIN
        <= lon
        <= KRYVYI_RIH_LON_MAX
    )


# ============================================================
# РАССТОЯНИЕ
# ============================================================

def distance_m(
    lat1,
    lon1,
    lat2,
    lon2
):

    R = 6371000

    p1 = math.radians(
        lat1
    )

    p2 = math.radians(
        lat2
    )

    dp = math.radians(
        lat2 - lat1
    )

    dl = math.radians(
        lon2 - lon1
    )

    a = (
        math.sin(dp / 2) ** 2
        +
        math.cos(p1)
        *
        math.cos(p2)
        *
        math.sin(dl / 2) ** 2
    )

    return (
        2
        *
        R
        *
        math.asin(
            math.sqrt(a)
        )
    )


# ============================================================
# ОЦЕНКА РЕЗУЛЬТАТА
# ============================================================

def score_result(
    result,
    street,
    number
):

    if not result:
        return -999

    lat = result.get(
        "lat"
    )

    lon = result.get(
        "lon"
    )

    if not coordinates_valid(
        lat,
        lon
    ):
        return -999

    score = 0.0

    # --------------------------------------------------------
    # Источник
    # --------------------------------------------------------

    source = result.get(
        "source",
        ""
    )

    if source == "Visicom":
        score += 0.30

    elif source == "TomTom":
        score += 0.30

    elif source == "Mapbox":
        score += 0.25

    # --------------------------------------------------------
    # Точность
    # --------------------------------------------------------

    accuracy = normalize_text(
        str(
            result.get(
                "accuracy",
                ""
            )
        )
    )

    if accuracy in (
        "rooftop",
        "building",
        "address"
    ):
        score += 0.35

    elif accuracy in (
        "interpolated",
        "interpolation"
    ):
        score += 0.20

    # --------------------------------------------------------
    # Совпадение номера
    # --------------------------------------------------------

    result_number = normalize_text(
        str(
            result.get(
                "house",
                ""
            )
        )
    )

    wanted_number = normalize_text(
        number
    )

    if result_number:

        if result_number == wanted_number:

            score += 0.35

        elif wanted_number in result_number:

            score += 0.15

    # --------------------------------------------------------
    # Улица
    # --------------------------------------------------------

    result_street = normalize_street(
        result.get(
            "street",
            ""
        )
    )

    wanted_street = normalize_street(
        street
    )

    if result_street:

        if (
            normalize_text(
                result_street
            )
            ==
            normalize_text(
                wanted_street
            )
        ):

            score += 0.25

        elif (
            normalize_text(
                wanted_street
            )
            in
            normalize_text(
                result_street
            )
        ):

            score += 0.10

    # --------------------------------------------------------
    # Близость к центру
    # --------------------------------------------------------

    dist = distance_m(
        float(lat),
        float(lon),
        CITY_LAT,
        CITY_LON
    )

    if dist < 30000:
        score += 0.10

    elif dist < 60000:
        score += 0.03

    return score


# ============================================================
# VISICOM
# ============================================================

async def search_visicom(
    session,
    street,
    number
):

    if not VISICOM_KEY:

        return []

    query = (
        f"{CITY_UA}, "
        f"{street}, "
        f"{number}"
    )

    url = (
        "https://api.visicom.ua/"
        "data-api/5.0/uk/geocode.json"
    )

    params = {
        "text": query,
        "key": VISICOM_KEY,
        "country": "ua",
        "limit": 10,
    }

    try:

        async with session.get(
            url,
            params=params
        ) as response:

            if response.status != 200:

                logger.warning(
                    "VISICOM HTTP %s",
                    response.status
                )

                return []

            data = await response.json()

            results = []

            # API может отдавать список
            items = data.get(
                "features",
                data.get(
                    "results",
                    []
                )
            )

            for item in items:

                properties = item.get(
                    "properties",
                    {}
                )

                geometry = item.get(
                    "geometry",
                    {}
                )

                coordinates = geometry.get(
                    "coordinates"
                )

                if not coordinates:

                    coordinates = (
                        item
                        .get(
                            "geo_centroid",
                            {}
                        )
                        .get(
                            "coordinates"
                        )
                    )

                if not coordinates:
                    continue

                try:

                    lon = float(
                        coordinates[0]
                    )

                    lat = float(
                        coordinates[1]
                    )

                except Exception:

                    continue

                if not coordinates_valid(
                    lat,
                    lon
                ):
                    continue

                results.append({
                    "lat": lat,
                    "lon": lon,
                    "address": (
                        properties.get(
                            "label"
                        )
                        or
                        properties.get(
                            "name"
                        )
                        or
                        f"{street} {number}"
                    ),
                    "street": (
                        properties.get(
                            "street"
                        )
                        or ""
                    ),
                    "house": (
                        properties.get(
                            "number"
                        )
                        or properties.get(
                            "street_number"
                        )
                        or ""
                    ),
                    "accuracy": "address",
                    "source": "Visicom",
                })

            return results

    except Exception as e:

        logger.warning(
            "VISICOM ERROR: %s",
            e
        )

        return []


# ============================================================
# TOMTOM
# ============================================================

async def search_tomtom(
    session,
    street,
    number
):

    if not TOMTOM_KEY:

        return []

    query = (
        f"{street} {number}, "
        f"{CITY_UA}, "
        f"{COUNTRY_UA}"
    )

    url = (
        "https://api.tomtom.com/"
        "search/2/geocode/"
        + quote(query)
        + ".json"
    )

    params = {
        "key": TOMTOM_KEY,
        "limit": 5,
        "language": "uk-UA",
        "countrySet": "UA",
        "lat": CITY_LAT,
        "lon": CITY_LON,
        "radius": 50000,
    }

    try:

        async with session.get(
            url,
            params=params
        ) as response:

            if response.status != 200:

                return []

            data = await response.json()

            output = []

            for item in data.get(
                "results",
                []
            ):

                position = item.get(
                    "position"
                )

                if not position:
                    continue

                lat = position.get(
                    "lat"
                )

                lon = position.get(
                    "lon"
                )

                if not coordinates_valid(
                    lat,
                    lon
                ):
                    continue

                address = item.get(
                    "address",
                    {}
                )

                output.append({
                    "lat": float(lat),
                    "lon": float(lon),

                    "address": (
                        address.get(
                            "freeformAddress"
                        )
                        or
                        f"{street} {number}"
                    ),

                    "street": (
                        address.get(
                            "streetName"
                        )
                        or ""
                    ),

                    "house": (
                        address.get(
                            "streetNumber"
                        )
                        or ""
                    ),

                    "accuracy": (
                        item.get(
                            "type",
                            ""
                        )
                    ),

                    "match_confidence": (
                        item.get(
                            "matchConfidence",
                            {}
                        )
                    ),

                    "source": "TomTom",
                })

            return output

    except Exception as e:

        logger.warning(
            "TOMTOM ERROR: %s",
            e
        )

        return []


# ============================================================
# MAPBOX
# ============================================================

async def search_mapbox(
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

    params = {
        "street": street,
        "address_number": number,
        "place": CITY_UA,
        "country": "UA",

        "access_token": MAPBOX_TOKEN,

        "autocomplete": "false",
        "limit": 5,

        "proximity": (
            f"{CITY_LON},"
            f"{CITY_LAT}"
        ),

        "types": "address",
    }

    try:

        async with session.get(
            url,
            params=params
        ) as response:

            if response.status != 200:

                return []

            data = await response.json()

            output = []

            for feature in data.get(
                "features",
                []
            ):

                coordinates = (
                    feature
                    .get(
                        "geometry",
                        {}
                    )
                    .get(
                        "coordinates"
                    )
                )

                if not coordinates:
                    continue

                lon = coordinates[0]
                lat = coordinates[1]

                if not coordinates_valid(
                    lat,
                    lon
                ):
                    continue

                props = feature.get(
                    "properties",
                    {}
                )

                context = props.get(
                    "context",
                    {}
                )

                address_context = (
                    context.get(
                        "address",
                        {}
                    )
                )

                match_code = (
                    props.get(
                        "match_code",
                        {}
                    )
                )

                accuracy = (
                    props
                    .get(
                        "coordinates",
                        {}
                    )
                    .get(
                        "accuracy",
                        ""
                    )
                )

                output.append({
                    "lat": float(lat),
                    "lon": float(lon),

                    "address": (
                        props.get(
                            "full_address"
                        )
                        or
                        props.get(
                            "name"
                        )
                        or
                        f"{street} {number}"
                    ),

                    "street": (
                        address_context.get(
                            "street_name"
                        )
                        or ""
                    ),

                    "house": (
                        address_context.get(
                            "address_number"
                        )
                        or ""
                    ),

                    "accuracy": accuracy,

                    "match_code": match_code,

                    "source": "Mapbox",
                })

            return output

    except Exception as e:

        logger.warning(
            "MAPBOX ERROR: %s",
            e
        )

        return []


# ============================================================
# ОДИН ВАРИАНТ УЛИЦЫ
# ============================================================

async def search_variant(
    session,
    street,
    number
):

    tasks = []

    if VISICOM_KEY:

        tasks.append(
            search_visicom(
                session,
                street,
                number
            )
        )

    if TOMTOM_KEY:

        tasks.append(
            search_tomtom(
                session,
                street,
                number
            )
        )

    if MAPBOX_TOKEN:

        tasks.append(
            search_mapbox(
                session,
                street,
                number
            )
        )

    if not tasks:

        return []

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

            results.extend(
                response
            )

    return results


# ============================================================
# ОСНОВНОЙ ПОИСК
# ============================================================

async def geocode_address(
    street,
    number
):

    key = cache_key(
        street,
        number
    )

    # --------------------------------------------------------
    # КЭШ
    # --------------------------------------------------------

    if key in cache:

        result = cache[key].copy()

        result["cached"] = True

        logger.info(
            "CACHE HIT: %s %s",
            street,
            number
        )

        return result

    logger.info(
        "CACHE MISS: %s %s",
        street,
        number
    )

    # --------------------------------------------------------
    # Защита от параллельных одинаковых запросов
    # --------------------------------------------------------

    if key in inflight:

        logger.info(
            "WAITING FOR EXISTING SEARCH: %s",
            key
        )

        return await inflight[key]

    async def do_search():

        async with aiohttp.ClientSession(
            timeout=TIMEOUT,
            headers=HEADERS
        ) as session:

            variants = street_variants(
                street
            )

            all_results = []

            # ------------------------------------------------
            # Сначала все варианты параллельно
            # ------------------------------------------------

            tasks = []

            for variant in variants:

                tasks.append(
                    search_variant(
                        session,
                        variant,
                        number
                    )
                )

            responses = await asyncio.gather(
                *tasks,
                return_exceptions=True
            )

            for response in responses:

                if isinstance(
                    response,
                    list
                ):

                    all_results.extend(
                        response
                    )

            # ------------------------------------------------
            # Оцениваем
            # ------------------------------------------------

            scored = []

            for result in all_results:

                score = score_result(
                    result,
                    street,
                    number
                )

                if score > 0:

                    scored.append(
                        (
                            score,
                            result
                        )
                    )

            if not scored:

                return None

            scored.sort(
                key=lambda x: x[0],
                reverse=True
            )

            best_score, best = scored[0]

            best = best.copy()

            best["confidence"] = round(
                min(
                    best_score,
                    1.0
                ),
                3
            )

            # ------------------------------------------------
            # Сохраняем только хороший результат
            # ------------------------------------------------

            if best_score >= 0.65:

                cache[key] = best

                save_cache()

                logger.info(
                    "CACHE SAVE: %s %s -> %.3f",
                    street,
                    number,
                    best_score
                )

            return best

    task = asyncio.create_task(
        do_search()
    )

    inflight[key] = task

    try:

        return await task

    finally:

        inflight.pop(
            key,
            None
        )


# ============================================================
# GOOGLE MAPS
# ============================================================

def google_maps_url(
    lat,
    lon
):

    return (
        "https://www.google.com/maps/"
        "search/?api=1&query="
        f"{lat},{lon}"
    )


# ============================================================
# КОМАНДА /CACHE
# ============================================================

async def cache_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        f"💾 В кэше: {len(cache)} адресов"
    )


# ============================================================
# TELEGRAM
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:

        return

    text = update.message.text

    if not text:

        return

    text = text.strip()

    # Команды не обрабатываем
    if text.startswith("/"):

        return

    # --------------------------------------------------------
    # Только полноценный адрес
    # --------------------------------------------------------

    parsed = parse_address(
        text
    )

    if not parsed:

        # Обычные сообщения игнорируются
        return

    street = parsed[
        "street"
    ]

    number = parsed[
        "number"
    ]

    logger.info(
        "ADDRESS: %s %s",
        street,
        number
    )

    # --------------------------------------------------------
    # Быстро проверяем кэш
    # --------------------------------------------------------

    key = cache_key(
        street,
        number
    )

    if key in cache:

        result = cache[key].copy()

        result["cached"] = True

    else:

        try:

            status = await update.message.reply_text(
                "🔎 Ищу адрес..."
            )

        except Exception:

            status = None

        start = time.monotonic()

        result = await geocode_address(
            street,
            number
        )

        elapsed = (
            time.monotonic()
            - start
        )

        logger.info(
            "SEARCH TIME: %.2f sec",
            elapsed
        )

        if status:

            try:

                await status.delete()

            except Exception:

                pass

    # --------------------------------------------------------
    # Не найден
    # --------------------------------------------------------

    if not result:

        await update.message.reply_text(
            "❌ Дом не найден.\n"
            "Попробуй написать, например:\n"
            "Одоевского 45"
        )

        return

    # --------------------------------------------------------
    # Данные
    # --------------------------------------------------------

    lat = result.get(
        "lat"
    )

    lon = result.get(
        "lon"
    )

    address = result.get(
        "address",
        f"{street} {number}"
    )

    source = result.get(
        "source",
        "unknown"
    )

    confidence = result.get(
        "confidence",
        0
    )

    cached = result.get(
        "cached",
        False
    )

    # --------------------------------------------------------
    # Геолокация
    # --------------------------------------------------------

    try:

        await update.message.reply_location(
            latitude=float(lat),
            longitude=float(lon)
        )

    except Exception as e:

        logger.error(
            "LOCATION ERROR: %s",
            e
        )

    # --------------------------------------------------------
    # Ссылка
    # --------------------------------------------------------

    cache_text = (
        " ⚡ КЭШ"
        if cached
        else ""
    )

    percent = int(
        confidence * 100
    )

    message = (
        f"📍 <b>{address}</b>\n\n"
        f"🎯 Точность: примерно {percent}%\n"
        f"🔎 Источник: {source}{cache_text}\n\n"
        f"🗺 <a href=\""
        f"{google_maps_url(lat, lon)}"
        f"\">Открыть Google Maps</a>"
    )

    try:

        await update.message.reply_text(
            message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:

        logger.error(
            "MESSAGE ERROR: %s",
            e
        )


# ============================================================
# ОШИБКИ
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "BOT ERROR: %s",
        context.error
    )


# ============================================================
# ЗАПУСК
# ============================================================

def main():

    if not BOT_TOKEN:

        print(
            "ОШИБКА: BOT_TOKEN не установлен"
        )

        return

    load_cache()

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # /cache
    application.add_handler(
        CommandHandler(
            "cache",
            cache_command
        )
    )

    # Только текст
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "================================="
    )

    logger.info(
        "KRYVYI RIH ADDRESS BOT STARTED"
    )

    logger.info(
        "CACHE: %s",
        len(cache)
    )

    logger.info(
        "VISICOM: %s",
        bool(VISICOM_KEY)
    )

    logger.info(
        "TOMTOM: %s",
        bool(TOMTOM_KEY)
    )

    logger.info(
        "MAPBOX: %s",
        bool(MAPBOX_TOKEN)
    )

    logger.info(
        "================================="
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()
