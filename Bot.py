import os
import re
import math
import time
import csv
import io
import sqlite3
import asyncio
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote

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

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=4.5, connect=2.0)

HEADERS = {
    "User-Agent": "KryvyiRihAddressBot/5.1 (address geocoder)"
}

DB_PATH = os.getenv("ADDRESS_DB", "addresses.db")

# База людей после импорта из novakom.mdb
PEOPLE_DB_PATH = os.getenv("PEOPLE_DB", "people.db")

# Путь к исходной Access-базе. Можно положить novakom.mdb рядом с Bot.py
NOVAKOM_MDB = os.getenv("NOVAKOM_MDB", "novakom.mdb")

# Кто имеет право нажимать "Кто тут".
# Пусто = разрешено всем (не рекомендуется).
# Пример: ALLOWED_USER_IDS="123456789,987654321"
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# АЛИАСЫ УЛИЦ
# ============================================================

# ВАЖНО:
# Все варианты в одной группе считаются одной улицей.
# Добавляй сюда старые/новые названия.
STREET_ALIAS_GROUPS = [
    ["Одоевского", "Одоєвського", "Казкова"],
    ["Дзержинского", "Дзержинського"],
    ["Волгоградская", "Волгоградська"],
    ["Фрунзе"],
    ["Карла Маркса"],
]

# Автоматически строим двусторонние алиасы.
STREET_ALIASES = {}

def _build_aliases():
    STREET_ALIASES.clear()
    for group in STREET_ALIAS_GROUPS:
        normalized_group = []
        for name in group:
            n = normalize_text_basic(name)
            if n and n not in normalized_group:
                normalized_group.append(n)
        for name in group:
            STREET_ALIASES[normalize_text_basic(name)] = list(group)

def normalize_text_basic(text: str) -> str:
    text = str(text or "").lower().strip()
    text = text.replace("ё", "е").replace("’", "'").replace("`", "'")
    text = re.sub(r"[;:!?()\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

_build_aliases()


# ============================================================
# БАЗА ОБУЧЕНИЯ ТОЧЕК
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
                (
                    street, house, lat, lon, source,
                    confirmations, now, address_key
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO learned_addresses
                (address_key, street, house, lat, lon, source, confirmations, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address_key, street, house, lat, lon,
                    source, 1, now
                ),
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
# НОРМАЛИЗАЦИЯ АДРЕСА
# ============================================================

def normalize_text(text: str) -> str:
    text = str(text or "").lower().strip()
    text = text.replace("ё", "е").replace("’", "'").replace("`", "'")
    text = re.sub(r"[;:!?()\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_street(street: str) -> str:
    s = normalize_text(street)
    s = re.sub(
        r"^(улица|ул\.?|вулиця|вул\.?|проспект|просп\.?|"
        r"провулок|пров\.?|переулок|пер\.?|бульвар|бул\.?)\s+",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_house(house: str) -> str:
    s = normalize_text(house)
    s = re.sub(r"\s+", "", s)
    return s


def normalize_apartment(apartment: str) -> str:
    s = normalize_text(apartment)
    s = re.sub(r"^(кв\.?|квартира|apt\.?|apartment)\s*", "", s, flags=re.I)
    s = re.sub(r"\s+", "", s)
    return s


def address_key(street: str, house: str) -> str:
    # КВАРТИРА НАМЕРЕННО НЕ ВХОДИТ В КЛЮЧ.
    # Лермонтова 25/11 и Лермонтова 25/99 = один и тот же дом.
    return f"{normalize_street(street)}|{normalize_house(house)}"


def street_variants(street: str):
    clean = street.strip()
    key = normalize_street(clean)

    variants = [clean]

    # Сначала пытаемся найти группу по точному нормализованному ключу.
    for alias_key, group in STREET_ALIASES.items():
        if normalize_street(alias_key) == key:
            variants.extend(group)

    # Также ищем по нормализованным названиям внутри групп.
    for group in STREET_ALIAS_GROUPS:
        normalized = {normalize_street(x) for x in group}
        if key in normalized:
            variants.extend(group)

    variants.append(key)

    out, seen = [], set()
    for value in variants:
        value = str(value or "").strip()
        k = normalize_street(value)
        if value and k not in seen:
            seen.add(k)
            out.append(value)
    return out


def parse_address(text: str):
    """
    Примеры:

    Лермонтова 25           -> house=25, apartment=""
    Лермонтова 25А         -> house=25А
    Лермонтова 25/11       -> house=25, apartment=11
    Лермонтова 25.11       -> house=25, apartment=11
    Лермонтова 25-11       -> house=25, apartment=11
    Лермонтова 25,11       -> house=25, apartment=11
    Лермонтова 25 11       -> house=25, apartment=11
    Лермонтова 25 кв 11    -> house=25, apartment=11
    Лермонтова 25 кв. 11   -> house=25, apartment=11
    Лермонтова 25 квартира 11 -> house=25, apartment=11
    Лермонтова 25А/11      -> house=25А, apartment=11

    По твоему правилу всё после / . - , или отдельного хвоста после
    номера дома считается квартирой.
    """
    original = str(text or "").strip()
    if len(original) < 4 or len(original) > 140:
        return None

    # Убираем тип улицы в самом конце после выделения street.
    # Основная идея: street + house (+ apartment)
    #
    # house = цифры + необязательная литера.
    # После house допускаем:
    #   /11 .11 -11 ,11
    #   кв 11 / кв.11 / квартира 11
    #   просто пробел + 11
    pattern = re.compile(
        r"""
        ^\s*
        (?P<street>.+?)
        [,\s]+
        (?P<house>\d+\s*[A-Za-zА-Яа-яІіЇїЄєҐґ]?)
        (?:
            \s*
            (?:
                [/.,\-]\s*
                |
                \s+(?:кв(?:артира)?\.?\s*)?
            )
            (?P<apartment>\d+[A-Za-zА-Яа-яІіЇїЄєҐґ]?)
        )?
        \s*$
        """,
        re.VERBOSE | re.I,
    )

    m = pattern.match(original)
    if not m:
        return None

    street = m.group("street").strip()
    street = re.sub(
        r"^(улица|ул\.?|вулиця|вул\.?|проспект|просп\.?|"
        r"провулок|пров\.?|переулок|пер\.?|бульвар|бул\.?)\s+",
        "",
        street,
        flags=re.I,
    ).strip()

    house = re.sub(r"\s+", "", m.group("house") or "")
    apartment = re.sub(r"\s+", "", m.group("apartment") or "")

    if not re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", street):
        return None

    # Не даём "кв" случайно прилипнуть к house как литера.
    # Например "Лермонтова 25 кв 11".
    if house.lower().endswith("к") and re.search(r"\bкв", original.lower()):
        house = house[:-1]

    return {
        "street": street,
        "number": house,
        "house": house,
        "apartment": apartment,
        "original": original,
    }


# ============================================================
# БАЗА ЛЮДЕЙ people.db
# ============================================================

def people_db_connect():
    conn = sqlite3.connect(PEOPLE_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_people_db():
    with people_db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT,
                street_raw TEXT,
                street_norm TEXT NOT NULL,
                house_raw TEXT,
                house_norm TEXT NOT NULL,
                apartment_raw TEXT,
                apartment_norm TEXT,
                source_table TEXT,
                source_row INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_people_addr "
            "ON people(street_norm, house_norm, apartment_norm)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_people_house "
            "ON people(street_norm, house_norm)"
        )
        conn.commit()


def _candidate_street_norms(street: str):
    return sorted({
        normalize_street(x)
        for x in street_variants(street)
        if normalize_street(x)
    })


def people_lookup(street: str, house: str, apartment: str = "", limit=100):
    """
    Если apartment указан — ищем именно квартиру.
    Если apartment пуст — показываем всех по дому.
    """
    streets = _candidate_street_norms(street)
    house_n = normalize_house(house)
    apt_n = normalize_apartment(apartment)

    if not streets or not house_n:
        return []

    placeholders = ",".join("?" for _ in streets)

    with people_db_connect() as conn:
        if apt_n:
            rows = conn.execute(
                f"""
                SELECT *
                FROM people
                WHERE street_norm IN ({placeholders})
                  AND house_norm = ?
                  AND apartment_norm = ?
                ORDER BY full_name
                LIMIT ?
                """,
                (*streets, house_n, apt_n, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT *
                FROM people
                WHERE street_norm IN ({placeholders})
                  AND house_norm = ?
                ORDER BY
                    CASE
                        WHEN apartment_norm GLOB '[0-9]*' THEN CAST(apartment_norm AS INTEGER)
                        ELSE 999999
                    END,
                    apartment_norm,
                    full_name
                LIMIT ?
                """,
                (*streets, house_n, limit),
            ).fetchall()

    return [dict(x) for x in rows]


# ============================================================
# ИМПОРТ MDB -> PEOPLE.DB
# ============================================================

STREET_COLUMN_HINTS = [
    "улица", "вулиця", "street", "ул", "вул",
    "адрес улица", "адрес_улица",
]
HOUSE_COLUMN_HINTS = [
    "дом", "будинок", "house", "буд", "№ дома", "номер дома",
]
APARTMENT_COLUMN_HINTS = [
    "квартира", "кв", "apartment", "flat", "помещение",
]
NAME_COLUMN_HINTS = [
    "фио", "піб", "фамилия имя отчество", "full_name",
    "name", "фамилия", "прізвище",
]
ADDRESS_COLUMN_HINTS = [
    "адрес", "address", "адреса",
]


def _norm_col(name):
    s = normalize_text(str(name or ""))
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _find_column(fieldnames, hints):
    norm_map = {_norm_col(x): x for x in fieldnames if x}
    # точное совпадение
    for hint in hints:
        hn = _norm_col(hint)
        if hn in norm_map:
            return norm_map[hn]
    # частичное
    for norm, original in norm_map.items():
        for hint in hints:
            hn = _norm_col(hint)
            if hn and (hn in norm or norm in hn):
                return original
    return None


def _parse_full_address(raw):
    """
    Пытается вытащить street/house/apartment из общего поля "Адрес".
    """
    raw = str(raw or "").strip()
    if not raw:
        return None

    # Убираем город из начала, если он присутствует.
    s = re.sub(
        r"^\s*(?:г\.?\s*)?(?:кривой\s+рог|кривий\s+ріг)\s*,?\s*",
        "",
        raw,
        flags=re.I,
    )

    # Иногда адрес вида "ул. Лермонтова, д. 25, кв. 11"
    s = re.sub(r"\bд(?:ом)?\.?\s*", "", s, flags=re.I)
    s = re.sub(r"\bбуд(?:инок)?\.?\s*", "", s, flags=re.I)

    s = re.sub(r"\bкв(?:артира)?\.?\s*", " кв ", s, flags=re.I)
    s = re.sub(r"\s*,\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    return parse_address(s)


def _mdb_tables(mdb_path):
    cmd = ["mdb-tables", "-1", mdb_path]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    if p.returncode != 0:
        raise RuntimeError(
            "mdb-tables не сработал. Установи: sudo apt install mdbtools\n"
            + (p.stderr or "")
        )
    return [x.strip() for x in p.stdout.splitlines() if x.strip()]


def _mdb_export_table(mdb_path, table):
    cmd = ["mdb-export", mdb_path, table]
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
        bufsize=1024 * 1024,
    )
    return p


def import_novakom_mdb(mdb_path=NOVAKOM_MDB):
    """
    Одноразовый импорт Access MDB в people.db.

    Нужен пакет mdbtools:
        sudo apt update
        sudo apt install -y mdbtools

    Функция сама пытается понять названия колонок:
    ФИО / улица / дом / квартира.
    Если в таблице только один общий "Адрес", пытается разобрать его.
    """
    mdb_path = str(mdb_path)
    if not Path(mdb_path).exists():
        raise FileNotFoundError(f"Не найден файл: {mdb_path}")

    init_people_db()
    tables = _mdb_tables(mdb_path)

    inserted_total = 0

    with people_db_connect() as conn:
        # Переиндексация с нуля
        conn.execute("DELETE FROM people")
        conn.commit()

        for table in tables:
            logger.info("MDB: пробую таблицу %s", table)
            p = _mdb_export_table(mdb_path, table)

            if not p.stdout:
                continue

            reader = csv.DictReader(p.stdout)
            fields = reader.fieldnames or []
            if not fields:
                continue

            street_col = _find_column(fields, STREET_COLUMN_HINTS)
            house_col = _find_column(fields, HOUSE_COLUMN_HINTS)
            apt_col = _find_column(fields, APARTMENT_COLUMN_HINTS)
            name_col = _find_column(fields, NAME_COLUMN_HINTS)
            address_col = _find_column(fields, ADDRESS_COLUMN_HINTS)

            # Таблица нам интересна, если:
            # 1) есть улица+дом
            # 2) или есть общий адрес
            if not ((street_col and house_col) or address_col):
                if p.stdout:
                    p.stdout.close()
                p.terminate()
                continue

            logger.info(
                "MDB %s -> name=%s street=%s house=%s apt=%s address=%s",
                table, name_col, street_col, house_col, apt_col, address_col
            )

            batch = []
            row_no = 0

            for row in reader:
                row_no += 1

                full_name = str(row.get(name_col, "") if name_col else "").strip()
                street = ""
                house = ""
                apartment = ""

                if street_col and house_col:
                    street = str(row.get(street_col, "") or "").strip()
                    house = str(row.get(house_col, "") or "").strip()
                    apartment = str(row.get(apt_col, "") or "").strip() if apt_col else ""
                elif address_col:
                    parsed = _parse_full_address(row.get(address_col, ""))
                    if parsed:
                        street = parsed["street"]
                        house = parsed["house"]
                        apartment = parsed["apartment"]

                if not street or not house:
                    continue

                batch.append((
                    full_name,
                    street,
                    normalize_street(street),
                    house,
                    normalize_house(house),
                    apartment,
                    normalize_apartment(apartment),
                    table,
                    row_no,
                ))

                if len(batch) >= 5000:
                    conn.executemany(
                        """
                        INSERT INTO people
                        (full_name, street_raw, street_norm,
                         house_raw, house_norm,
                         apartment_raw, apartment_norm,
                         source_table, source_row)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    inserted_total += len(batch)
                    batch.clear()
                    conn.commit()
                    logger.info("MDB: импортировано %s", inserted_total)

            if batch:
                conn.executemany(
                    """
                    INSERT INTO people
                    (full_name, street_raw, street_norm,
                     house_raw, house_norm,
                     apartment_raw, apartment_norm,
                     source_table, source_row)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted_total += len(batch)
                conn.commit()

            if p.stdout:
                p.stdout.close()
            try:
                p.wait(timeout=3)
            except Exception:
                p.terminate()

    logger.info("MDB IMPORT DONE: %s строк", inserted_total)
    return inserted_total


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
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


# ============================================================
# VISICOM
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
                str(x)
                for x in [
                    item.get("name"),
                    props.get("name"),
                    item.get("description"),
                    props.get("description"),
                ]
                if x
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
# MAPBOX
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
        score += {
            "rooftop": 80,
            "parcel": 70,
            "point": 65,
            "interpolated": 40,
            "approximate": 5,
        }.get(accuracy, 0)

        score += {
            "exact": 55,
            "high": 45,
            "medium": 25,
            "low": 5,
        }.get(confidence, 0)

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

    responses = await asyncio.gather(
        *(request(p) for p in queries),
        return_exceptions=True,
    )

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
        async with session.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        ) as r:
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

            if road and (
                normalize_street(street_name) in normalize_text(road)
                or normalize_text(road) in normalize_street(street_name)
            ):
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

    result = await nominatim_one(session, variants[0], number)
    if result:
        return result

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
            clat = sum(x["lat"] for x in cluster) / len(cluster)
            clon = sum(x["lon"] for x in cluster) / len(cluster)

            if distance_m(
                clat,
                clon,
                result["lat"],
                result["lon"],
            ) <= 80:
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

        per_source = {}

        for x in cluster:
            per_source[x["source"]] = max(
                per_source.get(x["source"], 0),
                x.get("score", 0),
            )

        score = sum(per_source.values())

        if len(sources) >= 2:
            score += 120

        if len(sources) >= 3:
            score += 170

        candidates.append({
            "lat": best_item["lat"],
            "lon": best_item["lon"],
            "score": score,
            "sources": sources,
            "best": best_item,
            "cluster": cluster,
        })

    return max(
        candidates,
        key=lambda x: x["score"],
    )


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
        tasks = [
            search_nominatim_fast(session, street, number)
        ]

        if VISICOM_KEY:
            tasks.append(
                search_visicom(session, street, number)
            )

        if MAPBOX_TOKEN:
            tasks.append(
                search_mapbox(session, street, number)
            )

        responses = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    results = []

    for response in responses:
        if isinstance(response, list):
            results.extend(response)

    best = choose_best(results)

    if best:
        cache_set(key, best)

    return best


# ============================================================
# GOOGLE MAPS / МАРШРУТ
# ============================================================

def google_maps_link(lat, lon):
    return (
        "https://www.google.com/maps/search/"
        f"?api=1&query={lat:.7f},{lon:.7f}"
    )


def google_route_link(lat, lon):
    # Это именно кнопка "В путь".
    # Google Maps сам использует текущее местоположение как точку старта.
    return (
        "https://www.google.com/maps/dir/"
        f"?api=1&destination={lat:.7f},{lon:.7f}&travelmode=driving"
    )


def extract_coords_from_text(text: str):
    m = re.search(
        r"(?<!\d)(-?\d{1,2}\.\d+)\s*[,; ]\s*(-?\d{1,3}\.\d+)(?!\d)",
        text,
    )

    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if coordinates_valid(lat, lon):
            return lat, lon

    decoded = unquote(text)

    m = re.search(
        r"/@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)",
        decoded,
    )

    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if coordinates_valid(lat, lon):
            return lat, lon

    try:
        parsed = urlparse(decoded)
        qs = parse_qs(parsed.query)

        for k in ("query", "q", "ll", "center"):
            if k in qs:
                value = qs[k][0]
                m = re.search(
                    r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)",
                    value,
                )

                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if coordinates_valid(lat, lon):
                        return lat, lon

    except Exception:
        pass

    m = re.search(
        r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)",
        decoded,
    )

    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if coordinates_valid(lat, lon):
            return lat, lon

    return None


async def resolve_google_coords(session, text: str):
    direct = extract_coords_from_text(text)

    if direct:
        return direct

    m = re.search(r"https?://\S+", text)

    if not m:
        return None

    url = m.group(0).rstrip(".,)>]")
    host = (urlparse(url).hostname or "").lower()

    if not any(
        x in host
        for x in (
            "google.",
            "goo.gl",
            "maps.app.goo.gl",
        )
    ):
        return None

    try:
        async with session.get(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            final_url = str(r.url)

            coords = extract_coords_from_text(final_url)

            if coords:
                return coords

            body = await r.text(errors="ignore")

            return extract_coords_from_text(body)

    except Exception:
        return None


# ============================================================
# INLINE-КНОПКИ
# ============================================================

def result_keyboard(lat, lon):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Метка верна",
                callback_data="mark_ok",
            ),
            InlineKeyboardButton(
                "🎯 Уточнить",
                callback_data="mark_fix",
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Кто тут",
                callback_data="who_here",
            ),
            InlineKeyboardButton(
                "🚗 В путь",
                url=google_route_link(lat, lon),
            ),
        ],
    ])


def result_text(street, house, apartment, result, elapsed=None, corrected=False):
    lat, lon = result["lat"], result["lon"]

    sources = (
        ", ".join(sorted(result.get("sources", [])))
        or result.get("source", "")
    )

    address_line = f"{street}, {house}"

    if apartment:
        address_line += f" · кв. {apartment}"

    lines = [
        f"📍 <b>{address_line}</b>",
        "",
        f"🎯 <code>{lat:.7f}, {lon:.7f}</code>",
        f"🗺 {sources}",
    ]

    if corrected:
        lines.append(
            "🧠 Точка сохранена как точная"
        )

    elif result.get("learned"):
        lines.append(
            f"🧠 Из обученной базы · подтверждений: "
            f"{result.get('confirmations', 1)}"
        )

    if elapsed is not None:
        lines.append(
            f"⚡ {elapsed:.2f} сек."
        )

    # Больше НЕ выводим текстовую ссылку.
    # Открытие маршрута только кнопкой "🚗 В путь".

    return "\n".join(lines)


def format_people(street, house, apartment, rows):
    title = f"👥 <b>{street}, {house}</b>"

    if apartment:
        title += f" · кв. {apartment}"

    if not rows:
        return (
            title
            + "\n\n"
            + "Никого по этому адресу в базе не нашёл."
        )

    lines = [title, ""]

    for i, row in enumerate(rows[:50], 1):
        name = str(row.get("full_name") or "").strip() or "Без ФИО"
        apt = str(row.get("apartment_raw") or "").strip()

        if apartment:
            lines.append(
                f"{i}. {name}"
            )
        else:
            if apt:
                lines.append(
                    f"{i}. <b>кв. {apt}</b> — {name}"
                )
            else:
                lines.append(
                    f"{i}. {name}"
                )

    if len(rows) > 50:
        lines.append("")
        lines.append(
            "Показаны первые 50 записей."
        )

    lines.append("")
    lines.append(
        f"Найдено: {len(rows)}"
    )

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Поиск домов Кривого Рога\n\n"
        "Примеры:\n"
        "Лермонтова 25\n"
        "Лермонтова 25/11\n"
        "Лермонтова 25 кв 11\n\n"
        "✅ Метка верна — сохраняет точку дома.\n"
        "🎯 Уточнить — можно прислать точную метку Google Maps.\n"
        "👥 Кто тут — ищет людей в базе по дому/квартире.\n"
        "🚗 В путь — открывает маршрут."
    )


async def import_people_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if not Path(NOVAKOM_MDB).exists():
        await update.message.reply_text(
            f"❌ Не найден {NOVAKOM_MDB}"
        )
        return

    status = await update.message.reply_text(
        "📦 Импортирую novakom.mdb в people.db..."
    )

    try:
        count = await asyncio.to_thread(
            import_novakom_mdb,
            NOVAKOM_MDB,
        )

        await status.edit_text(
            f"✅ Готово. Импортировано записей: {count}"
        )

    except Exception as e:
        logger.exception("IMPORT ERROR")

        await status.edit_text(
            f"❌ Ошибка импорта:\n{e}"
        )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    chat_id = (
        update.effective_chat.id
        if update.effective_chat
        else 0
    )

    pending_key = f"{chat_id}:{user_id}"

    # --------------------------------------------------------
    # РЕЖИМ УТОЧНЕНИЯ
    # --------------------------------------------------------

    pending = (
        context.application.bot_data
        .setdefault(
            "pending_corrections",
            {},
        )
        .get(pending_key)
    )

    if pending:
        async with aiohttp.ClientSession(
            headers=HEADERS
        ) as session:

            coords = await resolve_google_coords(
                session,
                text,
            )

        if not coords:
            await update.message.reply_text(
                "Не смог вытащить координаты.\n"
                "Пришли ссылку Google Maps на саму точку "
                "или координаты вида:\n"
                "47.123456, 33.123456"
            )
            return

        lat, lon = coords

        learned_save(
            pending["address_key"],
            pending["street"],
            pending["house"],
            lat,
            lon,
            "Google Maps — уточнено пользователем",
            increment=False,
        )

        RAM_CACHE.pop(
            pending["address_key"],
            None,
        )

        context.application.bot_data[
            "pending_corrections"
        ].pop(
            pending_key,
            None,
        )

        corrected = {
            "lat": lat,
            "lon": lon,
            "sources": {
                "Google Maps · уточнено"
            },
        }

        await update.message.reply_text(
            result_text(
                pending["street"],
                pending["house"],
                pending.get("apartment", ""),
                corrected,
                corrected=True,
            ),
            parse_mode="HTML",
            reply_markup=result_keyboard(lat, lon),
            disable_web_page_preview=True,
        )

        return

    # --------------------------------------------------------
    # ОБЫЧНЫЙ АДРЕС
    # --------------------------------------------------------

    parsed = parse_address(text)

    if not parsed:
        return

    street = parsed["street"]
    house = parsed["house"]
    apartment = parsed["apartment"]

    started = time.perf_counter()

    # В поиске координат квартира НЕ участвует.
    status = await update.message.reply_text(
        f"🔎 {street}, {house}"
    )

    result = await find_address(
        street,
        house,
    )

    elapsed = time.perf_counter() - started

    if not result:
        await status.edit_text(
            f"❌ Не нашёл: {street}, {house}"
        )
        return

    result_store = (
        context.application.bot_data
        .setdefault(
            "result_store",
            {},
        )
    )

    result_store[
        f"{chat_id}:{status.message_id}"
    ] = {
        "street": street,
        "house": house,
        "number": house,
        "apartment": apartment,
        "address_key": address_key(
            street,
            house,
        ),
        "lat": result["lat"],
        "lon": result["lon"],
    }

    await status.edit_text(
        result_text(
            street,
            house,
            apartment,
            result,
            elapsed=elapsed,
        ),
        parse_mode="HTML",
        reply_markup=result_keyboard(
            result["lat"],
            result["lon"],
        ),
        disable_web_page_preview=True,
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not query.message:
        return

    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    stored = (
        context.application.bot_data
        .setdefault(
            "result_store",
            {},
        )
        .get(
            f"{chat_id}:{message_id}"
        )
    )

    if not stored:
        await query.answer(
            "Эта метка уже устарела. Найди адрес ещё раз.",
            show_alert=True,
        )
        return

    # --------------------------------------------------------
    # МЕТКА ВЕРНА
    # --------------------------------------------------------

    if query.data == "mark_ok":
        learned_save(
            stored["address_key"],
            stored["street"],
            stored["house"],
            stored["lat"],
            stored["lon"],
            "Подтверждено пользователем",
            increment=True,
        )

        RAM_CACHE.pop(
            stored["address_key"],
            None,
        )

        await query.answer(
            "✅ Запомнил. В следующий раз этот дом будет мгновенным.",
            show_alert=True,
        )
        return

    # --------------------------------------------------------
    # УТОЧНИТЬ
    # --------------------------------------------------------

    if query.data == "mark_fix":
        pending_key = (
            f"{chat_id}:{user_id}"
        )

        context.application.bot_data.setdefault(
            "pending_corrections",
            {},
        )[pending_key] = stored.copy()

        await query.message.reply_text(
            "🎯 Пришли ссылку на ТОЧНУЮ метку из Google Maps.\n\n"
            "Можно также просто отправить координаты:\n"
            "47.123456, 33.123456"
        )
        return

    # --------------------------------------------------------
    # КТО ТУТ
    # --------------------------------------------------------

    if query.data == "who_here":
        if (
            ALLOWED_USER_IDS
            and user_id not in ALLOWED_USER_IDS
        ):
            await query.answer(
                "⛔ Нет доступа к базе.",
                show_alert=True,
            )
            return

        try:
            rows = await asyncio.to_thread(
                people_lookup,
                stored["street"],
                stored["house"],
                stored.get("apartment", ""),
                100,
            )

        except Exception as e:
            logger.exception("PEOPLE LOOKUP ERROR")

            await query.message.reply_text(
                f"❌ Ошибка базы людей: {e}"
            )
            return

        await query.message.reply_text(
            format_people(
                stored["street"],
                stored["house"],
                stored.get("apartment", ""),
                rows,
            ),
            parse_mode="HTML",
        )
        return


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не указан BOT_TOKEN"
        )

    init_db()
    init_people_db()

    logger.info("Бот запускается")
    logger.info(
        "Visicom: %s",
        "ON" if VISICOM_KEY else "OFF",
    )
    logger.info(
        "Mapbox: %s",
        "ON" if MAPBOX_TOKEN else "OFF",
    )
    logger.info(
        "People DB: %s",
        PEOPLE_DB_PATH,
    )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "import_people",
            import_people_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=r"^(mark_ok|mark_fix|who_here)$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
