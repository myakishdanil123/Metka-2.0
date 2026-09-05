import os
import re
import math
import asyncio
import logging
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

# Visicom API key
VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()

# Mapbox access token
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg").strip()

# Кривой Рог
CITY_RU = "Кривой Рог"
CITY_UA = "Кривий Ріг"
COUNTRY = "Украина"

# Ограничиваем область поиска приблизительно Кривым Рогом
# Это НЕ обязательное условие для API, а дополнительная проверка.
KRYVYI_RIH_LAT_MIN = 47.75
KRYVYI_RIH_LAT_MAX = 48.25
KRYVYI_RIH_LON_MIN = 32.15
KRYVYI_RIH_LON_MAX = 33.90

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ============================================================
# АЛИАСЫ УЛИЦ
# ============================================================
#
# Сюда можно постепенно добавлять старые/новые названия.
#
# Ключ = вариант, который может написать человек
# Значение = варианты, которые отправляем геокодерам
#
# Например:
# Одоевского -> Одоєвського / Казкова
#
# ВАЖНО:
# Бот работает и без этого словаря.
# Он всё равно отправляет исходное название в API.
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

    # Примеры старых/новых вариантов.
    # При необходимости сюда можно добавить весь список.
    "дзержинского": [
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
}


# ============================================================
# HTTP
# ============================================================

TIMEOUT = aiohttp.ClientTimeout(total=12)

HEADERS = {
    "User-Agent": (
        "KryvyiRihAddressBot/2.0 "
        "(Telegram address geocoder)"
    )
}


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================

def normalize_text(text: str) -> str:
    text = text.lower().strip()

    text = text.replace("ё", "е")
    text = text.replace("’", "'")
    text = text.replace("`", "'")

    # Убираем лишние символы
    text = re.sub(r"[.,;:!?()\[\]{}]", " ", text)

    # Пробелы
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_street(street: str) -> str:
    s = normalize_text(street)

    prefixes = [
        r"^улица\s+",
        r"^ул\s+",
        r"^ул\s*\.\s*",
        r"^вулиця\s+",
        r"^вул\s+",
        r"^вул\s*\.\s*",
    ]

    for pattern in prefixes:
        s = re.sub(pattern, "", s)

    return s.strip()


# ============================================================
# РАСПОЗНАВАНИЕ АДРЕСА
# ============================================================

def parse_address(text: str):
    """
    Поддерживает:

    Одоевского 45
    Одоевского, 45
    ул. Одоевского 45
    вул. Одоєвського 45
    Одоевского 45А
    Одоевского 45а
    Одоевского 45/1
    Одоевского 45-1
    """

    original = text.strip()

    if len(original) < 4:
        return None

    # Номер дома обязательно должен присутствовать.
    #
    # 45
    # 45а
    # 45А
    # 45/1
    # 45-1
    #
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
        re.VERBOSE,
    )

    match = pattern.match(original)

    if not match:
        return None

    street = match.group("street").strip()
    number = match.group("number").strip()

    street = re.sub(
        r"^(улица|ул\.?|вулиця|вул\.?)\s+",
        "",
        street,
        flags=re.IGNORECASE,
    )

    street = street.strip()

    # Улица должна содержать буквы
    if not re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", street):
        return None

    # Не принимаем слишком длинные сообщения
    if len(street) > 80:
        return None

    return {
        "original": original,
        "street": street,
        "number": number,
    }


# ============================================================
# ВАРИАНТЫ УЛИЦЫ
# ============================================================

def street_variants(street: str):
    result = []

    clean = street.strip()
    key = normalize_street(clean)

    # Исходный вариант
    result.append(clean)

    # Алиасы
    if key in STREET_ALIASES:
        result.extend(STREET_ALIASES[key])

    # Нормализованный
    result.append(normalize_street(clean))

    # Уникальные
    final = []

    for x in result:
        x = x.strip()

        if not x:
            continue

        if normalize_text(x) not in [
            normalize_text(v) for v in final
        ]:
            final.append(x)

    return final


# ============================================================
# ПРОВЕРКА КООРДИНАТ
# ============================================================

def coordinates_valid(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return False

    return (
        KRYVYI_RIH_LAT_MIN <= lat <= KRYVYI_RIH_LAT_MAX
        and
        KRYVYI_RIH_LON_MIN <= lon <= KRYVYI_RIH_LON_MAX
    )


# ============================================================
# РАССТОЯНИЕ
# ============================================================

def distance_m(lat1, lon1, lat2, lon2):
    """
    Расстояние между двумя координатами.
    """

    R = 6371000

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * R * math.asin(math.sqrt(a))


# ============================================================
# VISICOM
# ============================================================

async def search_visicom(
    session,
    street,
    number,
):
    if not VISICOM_KEY:
        logger.warning("VISICOM_KEY не установлен")
        return []

    results = []

    variants = street_variants(street)

    for street_name in variants:

        query = f"{CITY_UA}, {street_name}, {number}"

        url = (
            "https://api.visicom.ua/"
            "data-api/5.0/uk/geocode.json"
        )

        params = {
            "categories": "adr_address",
            "text": query,
            "country": "ua",
            "limit": "10",
            "key": VISICOM_KEY,
        }

        try:
            async with session.get(
                url,
                params=params,
                timeout=TIMEOUT,
            ) as response:

                if response.status != 200:
                    logger.warning(
                        "Visicom HTTP %s",
                        response.status,
                    )
                    continue

                data = await response.json(
                    content_type=None
                )

        except Exception as e:
            logger.warning(
                "Visicom error: %s",
                e,
            )
            continue

        # Возможные структуры ответа
        items = []

        if isinstance(data, list):
            items = data

        elif isinstance(data, dict):
            for key in (
                "features",
                "items",
                "objects",
                "result",
            ):
                value = data.get(key)

                if isinstance(value, list):
                    items = value
                    break

        for item in items:

            try:
                properties = item.get(
                    "properties",
                    {}
                )

                centroid = (
                    item.get("geo_centroid")
                    or properties.get("geo_centroid")
                )

                if not centroid:
                    continue

                lat = None
                lon = None

                if isinstance(centroid, dict):
                    if "coordinates" in centroid:
                        coords = centroid["coordinates"]

                        if len(coords) >= 2:
                            lon = float(coords[0])
                            lat = float(coords[1])

                    else:
                        lon = centroid.get("lon")
                        lat = centroid.get("lat")

                elif isinstance(centroid, list):
                    if len(centroid) >= 2:
                        lon = float(centroid[0])
                        lat = float(centroid[1])

                if lat is None or lon is None:
                    continue

                if not coordinates_valid(lat, lon):
                    continue

                name = (
                    item.get("name")
                    or properties.get("name")
                    or ""
                )

                description = (
                    item.get("description")
                    or properties.get("description")
                    or ""
                )

                results.append({
                    "source": "Visicom",
                    "lat": lat,
                    "lon": lon,
                    "name": str(name),
                    "description": str(description),
                    "street": street_name,
                    "query": query,
                    "score": 100,
                })

            except Exception as e:
                logger.debug(
                    "Ошибка обработки Visicom: %s",
                    e,
                )

    return results


# ============================================================
# MAPBOX
# ============================================================

async def search_mapbox(
    session,
    street,
    number,
):
    if not MAPBOX_TOKEN:
        logger.warning("MAPBOX_TOKEN не установлен")
        return []

    results = []

    variants = street_variants(street)

    url = (
        "https://api.mapbox.com/"
        "search/geocode/v6/forward"
    )

    for street_name in variants:

        # ----------------------------------------------------
        # 1. Структурированный запрос
        # ----------------------------------------------------

        params = {
            "address_number": number,
            "street": street_name,
            "place": CITY_UA,
            "country": "UA",
            "types": "address",
            "limit": "10",
            "autocomplete": "false",
            "language": "uk,ru",
            "access_token": MAPBOX_TOKEN,
        }

        try:
            async with session.get(
                url,
                params=params,
                timeout=TIMEOUT,
            ) as response:

                if response.status == 200:
                    data = await response.json(
                        content_type=None
                    )

                    features = data.get(
                        "features",
                        []
                    )

                    for feature in features:

                        candidate = parse_mapbox_result(
                            feature,
                            street_name,
                            number,
                        )

                        if candidate:
                            results.append(candidate)

        except Exception as e:
            logger.warning(
                "Mapbox structured error: %s",
                e,
            )

        # ----------------------------------------------------
        # 2. Обычный текстовый запрос
        # ----------------------------------------------------

        queries = [
            f"{street_name} {number}, {CITY_UA}, Ukraine",
            f"{street_name}, {number}, {CITY_RU}, Ukraine",
        ]

        for q in queries:

            params = {
                "q": q,
                "country": "UA",
                "types": "address",
                "limit": "10",
                "autocomplete": "false",
                "language": "uk,ru",
                "access_token": MAPBOX_TOKEN,
            }

            try:
                async with session.get(
                    url,
                    params=params,
                    timeout=TIMEOUT,
                ) as response:

                    if response.status != 200:
                        continue

                    data = await response.json(
                        content_type=None
                    )

                    for feature in data.get(
                        "features",
                        []
                    ):

                        candidate = parse_mapbox_result(
                            feature,
                            street_name,
                            number,
                        )

                        if candidate:
                            results.append(candidate)

            except Exception as e:
                logger.warning(
                    "Mapbox text error: %s",
                    e,
                )

    return results


def parse_mapbox_result(
    feature,
    street,
    number,
):
    try:
        geometry = feature.get(
            "geometry",
            {}
        )

        coordinates = geometry.get(
            "coordinates"
        )

        if not coordinates or len(coordinates) < 2:
            return None

        lon = float(coordinates[0])
        lat = float(coordinates[1])

        if not coordinates_valid(lat, lon):
            return None

        properties = feature.get(
            "properties",
            {}
        )

        accuracy = properties.get(
            "coordinates",
            {}
        ).get(
            "accuracy",
            ""
        )

        match_code = properties.get(
            "match_code",
            {}
        )

        confidence = match_code.get(
            "confidence",
            ""
        )

        feature_type = (
            properties.get("feature_type")
            or feature.get("feature_type")
            or ""
        )

        full_address = (
            properties.get("full_address")
            or feature.get("place_name")
            or ""
        )

        # ----------------------------------------------------
        # Оценка
        # ----------------------------------------------------

        score = 0

        if feature_type == "address":
            score += 40

        if accuracy == "rooftop":
            score += 50
        elif accuracy == "parcel":
            score += 45
        elif accuracy == "point":
            score += 40
        elif accuracy == "interpolated":
            score += 25
        elif accuracy == "approximate":
            score += 5

        if confidence == "exact":
            score += 30
        elif confidence == "high":
            score += 25
        elif confidence == "medium":
            score += 15
        elif confidence == "low":
            score += 5

        # Совпадение компонентов
        if match_code.get("address_number") == "matched":
            score += 30

        if match_code.get("street") == "matched":
            score += 30

        return {
            "source": "Mapbox",
            "lat": lat,
            "lon": lon,
            "name": full_address,
            "description": full_address,
            "street": street,
            "number": number,
            "accuracy": accuracy,
            "confidence": confidence,
            "score": score,
        }

    except Exception:
        return None


# ============================================================
# OPENSTREETMAP / NOMINATIM
# ============================================================

async def search_nominatim(
    session,
    street,
    number,
):
    results = []

    variants = street_variants(street)

    url = (
        "https://nominatim.openstreetmap.org/"
        "search"
    )

    # Nominatim публичный сервер нельзя долбить десятками
    # запросов одновременно.
    for street_name in variants:

        queries = [
            f"{street_name} {number}, {CITY_UA}, Ukraine",
            f"{street_name} {number}, {CITY_RU}, Ukraine",
        ]

        for q in queries:

            params = {
                "format": "jsonv2",
                "addressdetails": "1",
                "namedetails": "1",
                "extratags": "1",
                "limit": "10",
                "countrycodes": "ua",
                "q": q,
            }

            try:
                async with session.get(
                    url,
                    params=params,
                    headers=HEADERS,
                    timeout=TIMEOUT,
                ) as response:

                    if response.status != 200:
                        continue

                    data = await response.json(
                        content_type=None
                    )

            except Exception as e:
                logger.warning(
                    "Nominatim error: %s",
                    e,
                )
                continue

            for item in data:

                try:
                    lat = float(item["lat"])
                    lon = float(item["lon"])

                    if not coordinates_valid(
                        lat,
                        lon
                    ):
                        continue

                    address = item.get(
                        "address",
                        {}
                    )

                    road = (
                        address.get("road")
                        or address.get("street")
                        or ""
                    )

                    house = (
                        address.get("house_number")
                        or ""
                    )

                    display_name = item.get(
                        "display_name",
                        ""
                    )

                    score = 0

                    # Если есть номер дома
                    if house:
                        score += 50

                    # Если улица похожа
                    if normalize_text(
                        street_name
                    ) in normalize_text(
                        road
                    ) or normalize_text(
                        road
                    ) in normalize_text(
                        street_name
                    ):
                        score += 40

                    # Адресный объект
                    if item.get(
                        "type"
                    ) in (
                        "house",
                        "building",
                    ):
                        score += 30

                    results.append({
                        "source": "OpenStreetMap",
                        "lat": lat,
                        "lon": lon,
                        "name": display_name,
                        "description": display_name,
                        "street": road,
                        "number": house,
                        "score": score,
                    })

                except Exception:
                    continue

            # Чтобы не нарушать ограничения публичного
            # Nominatim-сервера.
            await asyncio.sleep(1.05)

    return results


# ============================================================
# ОБЪЕДИНЕНИЕ РЕЗУЛЬТАТОВ
# ============================================================

def cluster_results(results):
    """
    Если разные источники дали практически одну и ту же
    точку, объединяем их.

    Это позволяет понять:
        Visicom -> точка A
        Mapbox  -> точка A + 8 метров
        OSM     -> точка A + 12 метров

    => скорее всего это настоящий дом.
    """

    clusters = []

    for result in results:

        added = False

        for cluster in clusters:

            first = cluster[0]

            d = distance_m(
                first["lat"],
                first["lon"],
                result["lat"],
                result["lon"],
            )

            if d <= 100:
                cluster.append(result)
                added = True
                break

        if not added:
            clusters.append([result])

    return clusters


def choose_best(results):
    if not results:
        return None

    clusters = cluster_results(results)

    candidates = []

    for cluster in clusters:

        total_score = 0

        sources = set()

        for item in cluster:

            total_score += item.get(
                "score",
                0
            )

            sources.add(
                item.get("source")
            )

        # Огромный плюс, если несколько карт
        # согласны по координатам.
        if len(sources) >= 2:
            total_score += 80

        if len(sources) >= 3:
            total_score += 100

        # Средние координаты
        lat = sum(
            x["lat"] for x in cluster
        ) / len(cluster)

        lon = sum(
            x["lon"] for x in cluster
        ) / len(cluster)

        # Лучший результат внутри кластера
        best_item = max(
            cluster,
            key=lambda x: x.get(
                "score",
                0
            )
        )

        candidates.append({
            "lat": lat,
            "lon": lon,
            "score": total_score,
            "sources": sources,
            "best": best_item,
            "cluster": cluster,
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    return candidates[0]


# ============================================================
# GOOGLE MAPS
# ============================================================

def google_maps_link(lat, lon):
    return (
        "https://www.google.com/maps/"
        f"search/?api=1&query={lat},{lon}"
    )


# ============================================================
# ПОИСК ВО ВСЕХ ИСТОЧНИКАХ
# ============================================================

async def find_address(
    street,
    number,
):
    async with aiohttp.ClientSession(
        headers=HEADERS
    ) as session:

        tasks = []

        # Visicom
        if VISICOM_KEY:
            tasks.append(
                search_visicom(
                    session,
                    street,
                    number,
                )
            )

        # Mapbox
        if MAPBOX_TOKEN:
            tasks.append(
                search_mapbox(
                    session,
                    street,
                    number,
                )
            )

        # OSM
        tasks.append(
            search_nominatim(
                session,
                street,
                number,
            )
        )

        results = []

        if tasks:
            responses = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            for response in responses:

                if isinstance(
                    response,
                    Exception
                ):
                    logger.warning(
                        "Provider error: %s",
                        response,
                    )
                    continue

                if isinstance(
                    response,
                    list
                ):
                    results.extend(
                        response
                    )

        return results


# ============================================================
# TELEGRAM
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "📍 Бот поиска адресов Кривого Рога\n\n"
        "Напиши, например:\n"
        "Одоевского 45\n"
        "Одоєвського 45\n"
        "ул. Одоевского 45\n\n"
        "Обычные сообщения бот игнорирует."
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = update.message.text

    if not text:
        return

    # --------------------------------------------------------
    # РАСПОЗНАЁМ ТОЛЬКО АДРЕС
    # --------------------------------------------------------

    parsed = parse_address(text)

    if not parsed:
        return

    street = parsed["street"]
    number = parsed["number"]

    logger.info(
        "Ищем адрес: %s %s",
        street,
        number,
    )

    # --------------------------------------------------------
    # Сообщение пользователю
    # --------------------------------------------------------

    status_message = await update.message.reply_text(
        f"🔎 Ищу:\n"
        f"📍 {street}, {number}\n\n"
        f"Проверяю карты..."
    )

    try:

        results = await find_address(
            street,
            number,
        )

        if not results:

            await status_message.edit_text(
                f"❌ Не удалось найти:\n"
                f"{street}, {number}\n\n"
                f"Попробуйте написать:\n"
                f"улица + номер дома."
            )

            return

        best = choose_best(results)

        if not best:

            await status_message.edit_text(
                "❌ Подходящая координата не найдена."
            )

            return

        lat = best["lat"]
        lon = best["lon"]

        sources = best["sources"]

        link = google_maps_link(
            lat,
            lon,
        )

        # ----------------------------------------------------
        # Формируем информацию
        # ----------------------------------------------------

        source_text = ", ".join(
            sorted(sources)
        )

        best_item = best["best"]

        accuracy = best_item.get(
            "accuracy",
            ""
        )

        confidence = best_item.get(
            "confidence",
            ""
        )

        extra = ""

        if accuracy:
            extra += f"\n🎯 Точность Mapbox: {accuracy}"

        if confidence:
            extra += f"\n🔎 Совпадение: {confidence}"

        # ----------------------------------------------------
        # РЕЗУЛЬТАТ
        # ----------------------------------------------------

        await status_message.edit_text(
            f"📍 <b>{street}, {number}</b>\n\n"
            f"🗺 Координаты:\n"
            f"<code>{lat:.7f}, {lon:.7f}</code>\n\n"
            f"🌐 Найдено через: {source_text}"
            f"{extra}\n\n"
            f"👉 <a href=\"{link}\">Открыть точку в Google Maps</a>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )

    except Exception as e:

        logger.exception(
            "Ошибка поиска"
        )

        await status_message.edit_text(
            "⚠️ Ошибка при поиске адреса.\n"
            "Попробуйте ещё раз."
        )


# ============================================================
# ЗАПУСК
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не указан BOT_TOKEN"
        )

    logger.info(
        "Запуск бота..."
    )

    logger.info(
        "Visicom: %s",
        "ON" if VISICOM_KEY else "OFF",
    )

    logger.info(
        "Mapbox: %s",
        "ON" if MAPBOX_TOKEN else "OFF",
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Команда /start
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # Только текстовые сообщения.
    # Фото, видео, стикеры и т.д. бот игнорирует.
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info(
        "Бот запущен."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
