# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import difflib
import logging
import math
import os
import re
import sqlite3
import time
import unicodedata
import uuid

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote

import aiohttp

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "AIzaSyDrf2qAL0FQJJ2_TrKWkz5IVedU-yok-uc").strip()
VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg05:04").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()

CITY_UA = "Кривий Ріг"
CITY_RU = "Кривой Рог"

COUNTRY_UA = "Україна"
COUNTRY_RU = "Украина"


# ============================================================
# CENTRAL DISTRICT
# ============================================================

DISTRICT_NAME_UA = "Центрально-Міський район"

#
# Используется только для предварительного ограничения.
# После загрузки OSM polygon каждая точка проверяется
# непосредственно по границе района.
#
DISTRICT_LAT_MIN = 47.78
DISTRICT_LAT_MAX = 48.02

DISTRICT_LON_MIN = 33.20
DISTRICT_LON_MAX = 33.39


# Общий sanity-check Кривого Рога.
CITY_LAT_MIN = 47.60
CITY_LAT_MAX = 48.25

CITY_LON_MIN = 32.70
CITY_LON_MAX = 33.90


# ============================================================
# SPEED / ACCURACY
# ============================================================

HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=8.0,
    connect=2.5,
    sock_read=6.5,
)

CACHE_TTL = 24 * 3600
PENDING_TTL = 2 * 3600

#
# Если разные источники находятся не дальше 60 м —
# считаем, что они подтверждают друг друга.
#
CLUSTER_METERS = 60.0

#
# Если OSM дальше этого расстояния,
# а минимум 2 других независимых источника
# согласны между собой — OSM можно исправить.
#
OSM_CONFLICT_METERS = 120.0


if Path("/app/data").exists():

    DEFAULT_DB = (
        "/app/data/"
        "metka_osm_primary.sqlite3"
    )

else:

    DEFAULT_DB = (
        "metka_osm_primary.sqlite3"
    )


DB_PATH = os.getenv(
    "DB_PATH",
    DEFAULT_DB,
).strip()


logging.basicConfig(

    level=getattr(
        logging,
        os.getenv(
            "LOG_LEVEL",
            "INFO",
        ).upper(),
        logging.INFO,
    ),

    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)


log = logging.getLogger(
    "metka-osm-primary"
)


# ============================================================
# DATA
# ============================================================

@dataclass(slots=True)
class ParsedAddress:

    original: str

    street: str

    house: str


@dataclass(slots=True)
class Candidate:

    source: str

    lat: float
    lon: float

    street: str
    house: str

    label: str

    precision: str
    confidence: float

    query_street: str = ""

    score: float = 0.0


@dataclass(slots=True)
class PendingResult:

    owner_id: int
    chat_id: int

    parsed: ParsedAddress

    best: Optional[Candidate]

    candidates: list[Candidate]

    created_at: float


# ============================================================
# ADDRESS ALIASES
# ============================================================

SEED_ALIASES = {

    #
    # ВАЖНО ДЛЯ ЛЕРМОНТОВА
    #
    "лермонтова": [

        "центральний",
        "центральный",

        "проспект центральний",
        "проспект центральный",

        "просп. центральний",
        "просп. центральный",

        "центральний лермонтова",

        "центральний (лермонтова)",
        "центральный (лермонтова)",
    ],

    "центральний": [

        "лермонтова",

        "центральный",

        "проспект центральний",

        "центральний (лермонтова)",
    ],

    "центральный": [

        "лермонтова",

        "центральний",

        "проспект центральный",

        "центральный (лермонтова)",
    ],


    "одоевского": [

        "одоєвського",
        "одоевського",
        "одоєвского",
    ],

    "одоєвського": [

        "одоевского",
        "одоевського",
        "одоєвского",
    ],
}


STREET_PREFIXES = {

    "ул",
    "улица",

    "вул",
    "вулиця",

    "пр",
    "просп",
    "проспект",
    "пр-т",

    "пер",
    "переулок",

    "пров",
    "провулок",

    "бул",
    "бульвар",

    "пл",
    "площадь",
    "площа",

    "шоссе",
    "шосе",
}


LOOKALIKES = str.maketrans({

    "A": "А",
    "B": "В",
    "C": "С",
    "E": "Е",
    "H": "Н",
    "K": "К",
    "M": "М",
    "O": "О",
    "P": "Р",
    "T": "Т",
    "X": "Х",

    "a": "а",
    "b": "в",
    "c": "с",
    "e": "е",
    "h": "н",
    "k": "к",
    "m": "м",
    "o": "о",
    "p": "р",
    "t": "т",
    "x": "х",
})


# ============================================================
# PARSER
# ============================================================

#
# Для нашего бота:
#
# Лермонтова 25/11 -> дом 25
# Лермонтова 25.11 -> дом 25
# Лермонтова 25-11 -> дом 25
#
# Лермонтова 25А/11 -> дом 25А
#

ADDRESS_RE = re.compile(

    r"(?iu)^\s*"

    r"(?:"

        r"(?:"

            r"ул(?:ица)?|"
            r"вул(?:иця)?|"

            r"просп(?:ект)?|"
            r"пр-т|"

            r"пер(?:еулок)?|"
            r"пров(?:улок)?|"

            r"бул(?:ьвар)?|"

            r"пл(?:ощадь|оща)?|"

            r"шоссе|"
            r"шосе"

        r")"

        r"\.?\s+"

    r")?"

    r"(?P<street>.+?)"

    r"\s*[,№#]?\s*"

    r"(?P<house>"

        r"\d{1,4}"

        r"\s*"

        r"[A-Za-z"
        r"А-Яа-яЁё"
        r"ІіЇїЄєҐґ"
        r"]{0,2}"

    r")"

    r"(?:"

        r"\s*[/.-]\s*"

        r"\d{1,6}"

        r"[A-Za-z"
        r"А-Яа-яЁё"
        r"ІіЇїЄєҐґ"
        r"]{0,2}"

    r")?"

    r"(?:"

        r"\s*,?\s*"

        r"(?:"
            r"кв(?:артира)?\.?|"
            r"apt\.?"
        r")"

        r"\s*№?\s*"

        r"\d{1,6}"

    r")?"

    r"\s*$"
)


def normalize_text(
    value: str,
) -> str:

    value = unicodedata.normalize(
        "NFKC",
        value or "",
    )

    value = value.lower()

    value = (
        value
        .replace(
            "ё",
            "е",
        )
        .replace(
            "’",
            "'",
        )
        .replace(
            "`",
            "'",
        )
    )

    value = re.sub(

        r"[^0-9"
        r"a-z"
        r"а-я"
        r"іїєґ"
        r"'()\-\s]+",

        " ",

        value,

        flags=re.I,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def normalize_house(
    value: str,
) -> str:

    value = unicodedata.normalize(
        "NFKC",
        value or "",
    )

    value = value.translate(
        LOOKALIKES
    )

    return (
        value
        .replace(
            " ",
            "",
        )
        .upper()
        .replace(
            "Ё",
            "Е",
        )
    )


def street_core(
    value: str,
) -> str:

    value = normalize_text(
        value
    )

    value = (
        value
        .replace(
            "(",
            " ",
        )
        .replace(
            ")",
            " ",
        )
    )

    words = [

        word.strip(
            ".-"
        )

        for word
        in value.split()
    ]

    return " ".join(

        word

        for word in words

        if (
            word
            and
            word not in STREET_PREFIXES
        )
    )


def same_house(
    first: str,
    second: str,
) -> bool:

    return bool(

        first

        and

        second

        and

        normalize_house(
            first
        )
        ==
        normalize_house(
            second
        )
    )


def parse_address(
    text: str,
) -> Optional[ParsedAddress]:

    if not text:
        return None

    original = unicodedata.normalize(
        "NFKC",
        text,
    ).strip()

    if len(
        original
    ) > 140:

        return None

    if (
        "http://" in original.lower()

        or

        "https://" in original.lower()
    ):

        return None

    cleaned = re.sub(

        r"(?iu),?\s*"

        r"(?:м\.?\s*)?"

        r"(?:"

            r"кривой\s+рог|"
            r"кривий\s+ріг"

        r")"

        r"(?:"

            r",?\s*"

            r"(?:"
                r"украина|"
                r"україна"
            r")"

        r")?"

        r"\s*$",

        "",

        original,

    ).strip(
        " ,"
    )

    match = ADDRESS_RE.match(
        cleaned
    )

    if not match:

        return None

    street = match.group(
        "street"
    ).strip(
        " ,.-"
    )

    house = normalize_house(
        match.group(
            "house"
        )
    )

    if len(
        street_core(
            street
        )
    ) < 2:

        return None

    return ParsedAddress(

        original=original,

        street=street,

        house=house,
    )


# ============================================================
# DATABASE
# ============================================================

def db() -> sqlite3.Connection:

    Path(
        DB_PATH
    ).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_db() -> None:

    with db() as connection:

        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;

            CREATE TABLE IF NOT EXISTS confirmed_addresses(

                query_key TEXT PRIMARY KEY,

                original_query TEXT NOT NULL,

                street TEXT NOT NULL,

                house TEXT NOT NULL,

                lat REAL NOT NULL,

                lon REAL NOT NULL,

                label TEXT,

                source TEXT,

                confirmations INTEGER
                NOT NULL DEFAULT 1,

                updated_at INTEGER NOT NULL
            );


            CREATE TABLE IF NOT EXISTS street_aliases(

                alias TEXT PRIMARY KEY,

                canonical TEXT NOT NULL,

                confirmations INTEGER
                NOT NULL DEFAULT 1,

                updated_at INTEGER NOT NULL
            );


            CREATE TABLE IF NOT EXISTS provider_stats(

                provider TEXT PRIMARY KEY,

                good INTEGER
                NOT NULL DEFAULT 0,

                bad INTEGER
                NOT NULL DEFAULT 0,

                updated_at INTEGER NOT NULL
            );
            """
        )


def address_key(
    parsed: ParsedAddress,
) -> str:

    return (

        f"{street_core(parsed.street)}"

        "|"

        f"{normalize_house(parsed.house)}"
    )


def save_alias(
    alias: str,
    canonical: str,
) -> None:

    alias = street_core(
        alias
    )

    canonical = street_core(
        canonical
    )

    if (
        not alias

        or

        not canonical

        or

        alias == canonical
    ):

        return

    with db() as connection:

        connection.execute(

            """
            INSERT INTO street_aliases(

                alias,

                canonical,

                confirmations,

                updated_at
            )

            VALUES(
                ?,
                ?,
                1,
                ?
            )

            ON CONFLICT(alias)
            DO UPDATE SET

                canonical =
                    excluded.canonical,

                confirmations =
                    street_aliases.confirmations
                    +
                    1,

                updated_at =
                    excluded.updated_at
            """,

            (
                alias,

                canonical,

                int(
                    time.time()
                ),
            ),
        )


def learned_aliases(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    result = []

    try:

        with db() as connection:

            rows = connection.execute(

                """
                SELECT alias,canonical

                FROM street_aliases

                WHERE alias=?
                   OR canonical=?
                """,

                (
                    base,
                    base,
                ),

            ).fetchall()

        for row in rows:

            result.append(
                str(
                    row["alias"]
                )
            )

            result.append(
                str(
                    row["canonical"]
                )
            )

    except sqlite3.Error:

        pass

    return result


def street_variants(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    values = [
        street
    ]

    for canonical, aliases in SEED_ALIASES.items():

        family = {

            street_core(
                canonical
            ),

            *(

                street_core(
                    alias
                )

                for alias
                in aliases
            ),
        }

        if base in family:

            values.append(
                canonical
            )

            values.extend(
                aliases
            )

    values.extend(
        learned_aliases(
            street
        )
    )

    result = []

    seen = set()

    for value in values:

        key = street_core(
            value
        )

        if (
            key

            and

            key not in seen
        ):

            seen.add(
                key
            )

            result.append(
                value
            )

    return result[:12]


def street_similarity(
    first: str,
    second: str,
) -> float:

    first_core = street_core(
        first
    )

    second_core = street_core(
        second
    )

    if (
        not first_core

        or

        not second_core
    ):

        return 0.0

    if first_core == second_core:

        return 1.0

    if (
        first_core in second_core

        or

        second_core in first_core
    ):

        return 1.0

    first_family = {

        street_core(
            value
        )

        for value
        in street_variants(
            first
        )
    }

    second_family = {

        street_core(
            value
        )

        for value
        in street_variants(
            second
        )
    }

    if first_family & second_family:

        return 1.0

    sequence = difflib.SequenceMatcher(

        None,

        first_core,

        second_core,

    ).ratio()

    first_words = set(
        first_core.split()
    )

    second_words = set(
        second_core.split()
    )

    jaccard = (

        len(
            first_words
            &
            second_words
        )

        /

        max(
            1,

            len(
                first_words
                |
                second_words
            ),
        )
    )

    return max(
        sequence,
        jaccard,
    )


def get_learned(
    parsed: ParsedAddress,
) -> Optional[Candidate]:

    with db() as connection:

        row = connection.execute(

            """
            SELECT *

            FROM confirmed_addresses

            WHERE query_key=?
            """,

            (
                address_key(
                    parsed
                ),
            ),

        ).fetchone()

    if not row:

        return None

    return Candidate(

        source="learned",

        lat=float(
            row["lat"]
        ),

        lon=float(
            row["lon"]
        ),

        street=str(
            row["street"]
        ),

        house=str(
            row["house"]
        ),

        label=str(

            row["label"]

            or

            row["original_query"]
        ),

        precision="user_confirmed",

        confidence=1.0,

        score=10000.0,
    )


def save_learned(

    parsed: ParsedAddress,

    lat: float,

    lon: float,

    label: str,

    source: str,

) -> None:

    now = int(
        time.time()
    )

    with db() as connection:

        connection.execute(

            """
            INSERT INTO confirmed_addresses(

                query_key,

                original_query,

                street,

                house,

                lat,

                lon,

                label,

                source,

                confirmations,

                updated_at
            )

            VALUES(
                ?,?,?,?,?,?,?,?,
                1,
                ?
            )

            ON CONFLICT(query_key)

            DO UPDATE SET

                original_query =
                    excluded.original_query,

                street =
                    excluded.street,

                house =
                    excluded.house,

                lat =
                    excluded.lat,

                lon =
                    excluded.lon,

                label =
                    excluded.label,

                source =
                    excluded.source,

                confirmations =
                    confirmed_addresses.confirmations
                    +
                    1,

                updated_at =
                    excluded.updated_at
            """,

            (

                address_key(
                    parsed
                ),

                parsed.original,

                parsed.street,

                parsed.house,

                lat,

                lon,

                label,

                source,

                now,
            ),
        )


def update_provider_stat(
    provider: str,
    good: bool,
) -> None:

    if provider in {
        "learned",
        "user_correction",
    }:

        return

    with db() as connection:

        connection.execute(

            """
            INSERT INTO provider_stats(

                provider,

                good,

                bad,

                updated_at
            )

            VALUES(
                ?,?,?,?
            )

            ON CONFLICT(provider)

            DO UPDATE SET

                good =
                    provider_stats.good
                    +
                    excluded.good,

                bad =
                    provider_stats.bad
                    +
                    excluded.bad,

                updated_at =
                    excluded.updated_at
            """,

            (

                provider,

                1 if good else 0,

                0 if good else 1,

                int(
                    time.time()
                ),
            ),
        )


# ============================================================
# DISTRICT POLYGON
# ============================================================

DISTRICT_GEOJSON: Optional[
    dict[str, Any]
] = None


def point_in_ring(

    lon: float,

    lat: float,

    ring: list,

) -> bool:

    inside = False

    j = len(
        ring
    ) - 1

    for i in range(
        len(
            ring
        )
    ):

        xi = float(
            ring[i][0]
        )

        yi = float(
            ring[i][1]
        )

        xj = float(
            ring[j][0]
        )

        yj = float(
            ring[j][1]
        )

        if (
            (yi > lat)
            !=
            (yj > lat)
        ):

            cross = (

                (
                    xj
                    -
                    xi
                )

                *

                (
                    lat
                    -
                    yi
                )

                /

                (
                    (
                        yj
                        -
                        yi
                    )

                    or

                    1e-15
                )

                +

                xi
            )

            if lon < cross:

                inside = not inside

        j = i

    return inside


def point_in_geojson(

    lat: float,

    lon: float,

    geojson: dict[str, Any],

) -> bool:

    geometry_type = geojson.get(
        "type"
    )

    coordinates = (
        geojson.get(
            "coordinates"
        )
        or
        []
    )

    if geometry_type == "Polygon":

        polygons = [
            coordinates
        ]

    elif geometry_type == "MultiPolygon":

        polygons = coordinates

    else:

        return False

    for polygon in polygons:

        if not polygon:

            continue

        outer = polygon[0]

        if not point_in_ring(

            lon,

            lat,

            outer,

        ):

            continue

        inside_hole = any(

            point_in_ring(

                lon,

                lat,

                hole,

            )

            for hole
            in polygon[1:]
        )

        if not inside_hole:

            return True

    return False


def in_city_sanity(

    lat: float,

    lon: float,

) -> bool:

    return (

        CITY_LAT_MIN
        <=
        lat
        <=
        CITY_LAT_MAX

        and

        CITY_LON_MIN
        <=
        lon
        <=
        CITY_LON_MAX
    )


def in_target_district(

    lat: float,

    lon: float,

) -> bool:

    if not in_city_sanity(
        lat,
        lon,
    ):

        return False

    if DISTRICT_GEOJSON is not None:

        return point_in_geojson(

            lat,

            lon,

            DISTRICT_GEOJSON,
        )

    #
    # Если polygon временно не загрузился,
    # используем запасной bbox.
    #
    return (

        DISTRICT_LAT_MIN
        <=
        lat
        <=
        DISTRICT_LAT_MAX

        and

        DISTRICT_LON_MIN
        <=
        lon
        <=
        DISTRICT_LON_MAX
    )


# ============================================================
# HTTP
# ============================================================

async def get_json(

    session: aiohttp.ClientSession,

    url: str,

    *,

    params=None,

    headers=None,

) -> Any:

    async with session.get(

        url,

        params=params,

        headers=headers,

    ) as response:

        body = await response.text()

        if response.status != 200:

            raise RuntimeError(

                f"HTTP "
                f"{response.status}: "
                f"{body[:250]}"
            )

        return await response.json(
            content_type=None
        )


async def post_json(

    session: aiohttp.ClientSession,

    url: str,

    *,

    payload: dict[str, Any],

    headers=None,

) -> Any:

    async with session.post(

        url,

        json=payload,

        headers=headers,

    ) as response:

        body = await response.text()

        if response.status != 200:

            raise RuntimeError(

                f"HTTP "
                f"{response.status}: "
                f"{body[:250]}"
            )

        return await response.json(
            content_type=None
        )


async def safe_provider(

    name: str,

    coroutine: Any,

) -> list[Candidate]:

    started = time.perf_counter()

    try:

        result = await coroutine

        log.info(

            "%s: %d result(s), %.2fs",

            name,

            len(
                result
            ),

            time.perf_counter()
            -
            started,
        )

        return result

    except Exception as exc:

        log.warning(

            "%s failed: %s",

            name,

            exc,
        )

        return []


# ============================================================
# LOAD DISTRICT FROM OSM
# ============================================================

async def load_district_polygon(
    session: aiohttp.ClientSession,
) -> bool:

    global DISTRICT_GEOJSON

    try:

        data = await get_json(

            session,

            (
                "https://nominatim."
                "openstreetmap.org/"
                "search"
            ),

            params={

                "q":
                    (
                        "Центрально-Міський район, "
                        "Кривий Ріг, Україна"
                    ),

                "format":
                    "jsonv2",

                "polygon_geojson":
                    1,

                "addressdetails":
                    1,

                "limit":
                    5,

                "countrycodes":
                    "ua",
            },

            headers={

                "User-Agent":
                    (
                        "Metka-Kryvyi-Rih/"
                        "OSM-primary/1.0"
                    )
            },
        )

        for item in (
            data
            or
            []
        ):

            display = normalize_text(

                str(
                    item.get(
                        "display_name"
                    )
                    or
                    ""
                )
            )

            if not (

                "центрально-міський"
                in display

                or

                "центрально міський"
                in display

                or

                "центрально-город"
                in display
            ):

                continue

            geojson = item.get(
                "geojson"
            )

            if not isinstance(
                geojson,
                dict,
            ):

                continue

            if geojson.get(
                "type"
            ) not in {

                "Polygon",

                "MultiPolygon",
            }:

                continue

            DISTRICT_GEOJSON = geojson

            log.info(
                "District polygon loaded"
            )

            return True

    except Exception as exc:

        log.warning(

            "District polygon error: %s",

            exc,
        )

    return False


# ============================================================
# GEOMETRY
# ============================================================

def haversine_m(

    lat1: float,

    lon1: float,

    lat2: float,

    lon2: float,

) -> float:

    radius = 6371000.0

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

    value = (

        math.sin(
            dp / 2
        ) ** 2

        +

        math.cos(
            p1
        )

        *

        math.cos(
            p2
        )

        *

        math.sin(
            dl / 2
        ) ** 2
    )

    return (

        2

        *

        radius

        *

        math.asin(
            math.sqrt(
                value
            )
        )
    )


def distance(

    first: Candidate,

    second: Candidate,

) -> float:

    return haversine_m(

        first.lat,

        first.lon,

        second.lat,

        second.lon,
    )


def provider_family(
    source: str,
) -> str:

    if source in {
        "osm",
        "overpass",
    }:

        return "osm"

    if source in {
        "google",
        "google_places",
    }:

        return "google"

    return source


# ============================================================
# CANDIDATE VALIDATION
# ============================================================

def valid_candidate(

    parsed: ParsedAddress,

    candidate: Candidate,

) -> bool:

    if not in_target_district(

        candidate.lat,

        candidate.lon,

    ):

        return False

    if candidate.source == "learned":

        return True

    #
    # ТОЧНЫЙ номер дома обязателен.
    #
    if not same_house(

        parsed.house,

        candidate.house,

    ):

        return False

    #
    # Никогда не отдаём центр улицы.
    #
    if candidate.precision in {

        "street",

        "city",

        "center",

        "unknown",

    }:

        return False

    similarity = max(

        street_similarity(

            parsed.street,

            candidate.street
            or
            candidate.label,
        ),

        (
            street_similarity(

                candidate.query_street,

                candidate.street
                or
                candidate.label,
            )

            if candidate.query_street

            else

            0.0
        ),
    )

    return similarity >= 0.48


# ============================================================
# SCORING
# ============================================================

PROVIDER_BASE = {

    "learned":
        10000.0,

    #
    # OSM — ГЛАВНЫЙ
    #
    "overpass":
        200.0,

    "osm":
        190.0,

    #
    # Потом Google
    #
    "google_places":
        160.0,

    "google":
        155.0,

    #
    # Потом Visicom
    #
    "visicom":
        140.0,

    #
    # Потом Mapbox
    #
    "mapbox":
        125.0,
}


def score_candidate(

    parsed: ParsedAddress,

    candidate: Candidate,

) -> float:

    if candidate.source == "learned":

        return 10000.0

    if not valid_candidate(

        parsed,

        candidate,

    ):

        return -1000.0

    base = PROVIDER_BASE.get(

        candidate.source,

        80.0,
    )

    precision_bonus = {

        "building":
            60.0,

        "rooftop":
            58.0,

        "entrance":
            55.0,

        "point":
            45.0,

        "parcel":
            40.0,

        "address":
            35.0,

        "interpolated":
            4.0,

        "approximate":
            0.0,

    }.get(

        candidate.precision,

        0.0,
    )

    similarity = max(

        street_similarity(

            parsed.street,

            candidate.street
            or
            candidate.label,
        ),

        (

            street_similarity(

                candidate.query_street,

                candidate.street
                or
                candidate.label,
            )

            if candidate.query_street

            else

            0.0
        ),
    )

    return (

        base

        +

        precision_bonus

        +

        40.0
        *
        similarity

        +

        12.0
        *
        candidate.confidence
    )


def rank_candidates(

    parsed: ParsedAddress,

    candidates: list[Candidate],

) -> list[Candidate]:

    candidates = [

        candidate

        for candidate
        in candidates

        if valid_candidate(

            parsed,

            candidate,

        )
    ]

    for candidate in candidates:

        candidate.score = score_candidate(

            parsed,

            candidate,
        )

        support_families = {

            provider_family(
                other.source
            )

            for other
            in candidates

            if (

                other is not candidate

                and

                provider_family(
                    other.source
                )
                !=
                provider_family(
                    candidate.source
                )

                and

                distance(

                    candidate,

                    other,

                )
                <=
                CLUSTER_METERS
            )
        }

        candidate.score += (

            20.0

            *

            min(
                3,
                len(
                    support_families
                ),
            )
        )

    candidates.sort(

        key=lambda candidate:
            candidate.score,

        reverse=True,
    )

    result = []

    for candidate in candidates:

        duplicate = any(

            provider_family(
                candidate.source
            )
            ==
            provider_family(
                existing.source
            )

            and

            distance(

                candidate,

                existing,

            )
            <
            8.0

            for existing
            in result
        )

        if duplicate:

            continue

        result.append(
            candidate
        )

    return result[:20]


# ============================================================
# OSM OVERPASS — PRIMARY
# ============================================================

async def geocode_overpass(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    #
    # Ищем точный номер дома в пределах Кривого Рога.
    # Потом каждый результат проверяется polygon района.
    #
    wanted_house = re.escape(
        parsed.house
    )

    query = f"""
[out:json][timeout:8];

(
    node
    ["addr:housenumber"~"^{wanted_house}$",i]
    ({CITY_LAT_MIN},{CITY_LON_MIN},{CITY_LAT_MAX},{CITY_LON_MAX});

    way
    ["addr:housenumber"~"^{wanted_house}$",i]
    ({CITY_LAT_MIN},{CITY_LON_MIN},{CITY_LAT_MAX},{CITY_LON_MAX});

    relation
    ["addr:housenumber"~"^{wanted_house}$",i]
    ({CITY_LAT_MIN},{CITY_LON_MIN},{CITY_LAT_MAX},{CITY_LON_MAX});
);

out center tags 250;
"""

    async with session.post(

        (
            "https://overpass-api.de/"
            "api/interpreter"
        ),

        data={
            "data":
                query
        },

        headers={

            "User-Agent":
                (
                    "Metka-Kryvyi-Rih/"
                    "OSM-primary/1.0"
                )
        },

    ) as response:

        if response.status != 200:

            body = await response.text()

            raise RuntimeError(

                f"Overpass "
                f"{response.status}: "
                f"{body[:200]}"
            )

        data = await response.json(
            content_type=None
        )

    variants = street_variants(
        parsed.street
    )

    result = []

    for element in (

        data.get(
            "elements"
        )

        or

        []
    ):

        tags = (

            element.get(
                "tags"
            )

            or

            {}
        )

        house = str(

            tags.get(
                "addr:housenumber"
            )

            or

            ""
        )

        if not same_house(

            house,

            parsed.house,

        ):

            continue

        street = str(

            tags.get(
                "addr:street"
            )

            or

            tags.get(
                "addr:place"
            )

            or

            ""
        )

        if not street:

            continue

        similarity = max(

            street_similarity(

                variant,

                street,

            )

            for variant
            in variants
        )

        if similarity < 0.48:

            continue

        if (

            "lat" in element

            and

            "lon" in element

        ):

            lat = float(
                element["lat"]
            )

            lon = float(
                element["lon"]
            )

        else:

            center = (

                element.get(
                    "center"
                )

                or

                {}
            )

            if (

                "lat" not in center

                or

                "lon" not in center

            ):

                continue

            lat = float(
                center["lat"]
            )

            lon = float(
                center["lon"]
            )

        if not in_target_district(

            lat,

            lon,

        ):

            continue

        is_building = (

            element.get(
                "type"
            )
            in {
                "way",
                "relation",
            }

            or

            bool(
                tags.get(
                    "building"
                )
            )
        )

        result.append(

            Candidate(

                source="overpass",

                lat=lat,

                lon=lon,

                street=street,

                house=house,

                label=(
                    f"{street} "
                    f"{house}"
                ),

                precision=(

                    "building"

                    if is_building

                    else

                    "address"
                ),

                confidence=(

                    0.99

                    if is_building

                    else

                    0.93
                ),

                query_street=street,
            )
        )

    return result


# ============================================================
# OSM NOMINATIM
# ============================================================

async def geocode_osm(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    result = []

    headers = {

        "User-Agent":
            (
                "Metka-Kryvyi-Rih/"
                "OSM-primary/1.0"
            ),

        "Accept-Language":
            "uk,ru;q=0.9",
    }

    variants = street_variants(
        parsed.street
    )

    #
    # Чтобы не тормозить публичный Nominatim —
    # максимум два запроса.
    #
    for index, street in enumerate(
        variants[:2]
    ):

        data = await get_json(

            session,

            (
                "https://nominatim."
                "openstreetmap.org/"
                "search"
            ),

            params={

                "street":
                    (
                        f"{parsed.house} "
                        f"{street}"
                    ),

                "city":
                    CITY_UA,

                "country":
                    COUNTRY_UA,

                "countrycodes":
                    "ua",

                "format":
                    "jsonv2",

                "addressdetails":
                    1,

                "limit":
                    10,

                "bounded":
                    1,

                "viewbox":
                    (
                        f"{DISTRICT_LON_MIN},"
                        f"{DISTRICT_LAT_MAX},"
                        f"{DISTRICT_LON_MAX},"
                        f"{DISTRICT_LAT_MIN}"
                    ),
            },

            headers=headers,
        )

        for item in (
            data
            or
            []
        ):

            address = (

                item.get(
                    "address"
                )

                or

                {}
            )

            house = str(

                address.get(
                    "house_number"
                )

                or

                ""
            )

            if not same_house(

                house,

                parsed.house,

            ):

                continue

            lat = float(
                item["lat"]
            )

            lon = float(
                item["lon"]
            )

            if not in_target_district(

                lat,

                lon,

            ):

                continue

            street_name = str(

                address.get(
                    "road"
                )

                or

                address.get(
                    "pedestrian"
                )

                or

                address.get(
                    "residential"
                )

                or

                address.get(
                    "place"
                )

                or

                street
            )

            address_type = str(

                item.get(
                    "addresstype"
                )

                or

                item.get(
                    "type"
                )

                or

                ""
            ).lower()

            precision = (

                "building"

                if address_type
                in {

                    "building",

                    "house",

                    "residential",
                }

                else

                "address"
            )

            result.append(

                Candidate(

                    source="osm",

                    lat=lat,

                    lon=lon,

                    street=street_name,

                    house=house,

                    label=str(

                        item.get(
                            "display_name"
                        )

                        or

                        ""
                    ),

                    precision=precision,

                    confidence=(

                        0.96

                        if precision
                        ==
                        "building"

                        else

                        0.91
                    ),

                    query_street=street,
                )
            )

        if result:

            break

        if index == 0:

            await asyncio.sleep(
                1.05
            )

    return result


# ============================================================
# GOOGLE
# ============================================================

def google_component(

    item: dict[str, Any],

    kind: str,

) -> str:

    for component in (

        item.get(
            "address_components"
        )

        or

        []
    ):

        if kind in (

            component.get(
                "types"
            )

            or

            []
        ):

            return str(

                component.get(
                    "long_name"
                )

                or

                ""
            )

    return ""


async def geocode_google(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    if not GOOGLE_API_KEY:

        return []

    result = []

    for street in street_variants(
        parsed.street
    )[:6]:

        data = await get_json(

            session,

            (
                "https://maps.googleapis.com/"
                "maps/api/geocode/json"
            ),

            params={

                "address":
                    (
                        f"{street} "
                        f"{parsed.house}, "
                        f"{CITY_UA}, "
                        f"{COUNTRY_UA}"
                    ),

                "components":
                    "country:UA",

                "bounds":
                    (
                        f"{DISTRICT_LAT_MIN},"
                        f"{DISTRICT_LON_MIN}"
                        f"|"
                        f"{DISTRICT_LAT_MAX},"
                        f"{DISTRICT_LON_MAX}"
                    ),

                "language":
                    "uk",

                "region":
                    "ua",

                "key":
                    GOOGLE_API_KEY,
            },
        )

        for item in (

            data.get(
                "results"
            )

            or

            []
        ):

            if item.get(
                "partial_match"
            ):

                continue

            house = google_component(

                item,

                "street_number",
            )

            if not same_house(

                house,

                parsed.house,

            ):

                continue

            geometry = (

                item.get(
                    "geometry"
                )

                or

                {}
            )

            location = (

                geometry.get(
                    "location"
                )

                or

                {}
            )

            if (

                "lat" not in location

                or

                "lng" not in location

            ):

                continue

            lat = float(
                location["lat"]
            )

            lon = float(
                location["lng"]
            )

            if not in_target_district(

                lat,

                lon,

            ):

                continue

            location_type = str(

                geometry.get(
                    "location_type"
                )

                or

                ""
            ).upper()

            precision = {

                "ROOFTOP":
                    "rooftop",

                "RANGE_INTERPOLATED":
                    "interpolated",

                "GEOMETRIC_CENTER":
                    "approximate",

                "APPROXIMATE":
                    "approximate",

            }.get(

                location_type,

                "address",
            )

            result.append(

                Candidate(

                    source="google",

                    lat=lat,

                    lon=lon,

                    street=(

                        google_component(

                            item,

                            "route",
                        )

                        or

                        street
                    ),

                    house=house,

                    label=str(

                        item.get(
                            "formatted_address"
                        )

                        or

                        ""
                    ),

                    precision=precision,

                    confidence=(

                        0.99

                        if precision
                        ==
                        "rooftop"

                        else

                        0.82
                    ),

                    query_street=street,
                )
            )

        if any(

            candidate.precision
            ==
            "rooftop"

            for candidate
            in result
        ):

            break

    return result


# ============================================================
# GOOGLE PLACES
# ============================================================

def places_component(

    place: dict[str, Any],

    kind: str,

) -> str:

    for component in (

        place.get(
            "addressComponents"
        )

        or

        []
    ):

        if kind in (

            component.get(
                "types"
            )

            or

            []
        ):

            return str(

                component.get(
                    "longText"
                )

                or

                component.get(
                    "shortText"
                )

                or

                ""
            )

    return ""


def extract_house_from_text(

    text: str,

    wanted: str,

) -> str:

    values = re.findall(

        r"(?<!\d)"

        r"("

            r"\d{1,4}"

            r"\s*"

            r"[A-Za-z"
            r"А-Яа-яЁё"
            r"ІіЇїЄєҐґ"
            r"]{0,2}"

        r")"

        r"(?!\d)",

        text
        or
        "",
    )

    for value in values:

        if same_house(

            value,

            wanted,

        ):

            return wanted

    return ""


async def geocode_google_places(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    if not GOOGLE_API_KEY:

        return []

    result = []

    headers = {

        "X-Goog-Api-Key":
            GOOGLE_API_KEY,

        "X-Goog-FieldMask":
            (
                "places.id,"
                "places.formattedAddress,"
                "places.location,"
                "places.addressComponents,"
                "places.displayName,"
                "places.types"
            ),
    }

    for street in street_variants(
        parsed.street
    )[:5]:

        data = await post_json(

            session,

            (
                "https://places.googleapis.com/"
                "v1/places:searchText"
            ),

            payload={

                "textQuery":
                    (
                        f"{street} "
                        f"{parsed.house}, "
                        f"{CITY_UA}"
                    ),

                "languageCode":
                    "uk",

                "regionCode":
                    "UA",

                "maxResultCount":
                    10,

                "locationBias": {

                    "rectangle": {

                        "low": {

                            "latitude":
                                DISTRICT_LAT_MIN,

                            "longitude":
                                DISTRICT_LON_MIN,
                        },

                        "high": {

                            "latitude":
                                DISTRICT_LAT_MAX,

                            "longitude":
                                DISTRICT_LON_MAX,
                        },
                    }
                },
            },

            headers=headers,
        )

        for place in (

            data.get(
                "places"
            )

            or

            []
        ):

            formatted = str(

                place.get(
                    "formattedAddress"
                )

                or

                ""
            )

            house = (

                places_component(

                    place,

                    "street_number",
                )

                or

                extract_house_from_text(

                    formatted,

                    parsed.house,
                )
            )

            if not same_house(

                house,

                parsed.house,

            ):

                continue

            location = (

                place.get(
                    "location"
                )

                or

                {}
            )

            if (

                "latitude"
                not in location

                or

                "longitude"
                not in location

            ):

                continue

            lat = float(
                location["latitude"]
            )

            lon = float(
                location["longitude"]
            )

            if not in_target_district(

                lat,

                lon,

            ):

                continue

            result.append(

                Candidate(

                    source="google_places",

                    lat=lat,

                    lon=lon,

                    street=(

                        places_component(

                            place,

                            "route",
                        )

                        or

                        street
                    ),

                    house=parsed.house,

                    label=formatted,

                    precision="point",

                    confidence=0.96,

                    query_street=street,
                )
            )

        if result:

            break

    return result


# ============================================================
# VISICOM
# ============================================================

def flatten_strings(
    obj: Any,
) -> list[str]:

    result = []

    if isinstance(
        obj,
        str,
    ):

        result.append(
            obj
        )

    elif isinstance(
        obj,
        dict,
    ):

        for value in obj.values():

            result.extend(
                flatten_strings(
                    value
                )
            )

    elif isinstance(
        obj,
        list,
    ):

        for value in obj:

            result.extend(
                flatten_strings(
                    value
                )
            )

    return result


async def geocode_visicom(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    if not VISICOM_KEY:

        return []

    result = []

    variants = street_variants(
        parsed.street
    )

    queries = []

    for street in variants:

        queries.extend([

            f"{street} {parsed.house}",

            (
                f"{CITY_UA}, "
                f"{street} "
                f"{parsed.house}"
            ),
        ])

    seen = set()

    for query in queries[:12]:

        key = normalize_text(
            query
        )

        if key in seen:

            continue

        seen.add(
            key
        )

        data = await get_json(

            session,

            (
                "https://api.visicom.ua/"
                "data-api/5.0/uk/"
                "geocode.json"
            ),

            params={

                "text":
                    query,

                "categories":
                    "adr_address",

                "country":
                    "UA",

                "limit":
                    12,

                "key":
                    VISICOM_KEY,
            },
        )

        for feature in (

            data.get(
                "features"
            )

            or

            []
        ):

            properties = (

                feature.get(
                    "properties"
                )

                or

                {}
            )

            geo = (

                feature.get(
                    "geo_centroid"
                )

                or

                feature.get(
                    "geometry"
                )

                or

                {}
            )

            coords = (

                geo.get(
                    "coordinates"
                )

                or

                []
            )

            if len(
                coords
            ) < 2:

                continue

            lon = float(
                coords[0]
            )

            lat = float(
                coords[1]
            )

            if not in_target_district(

                lat,

                lon,

            ):

                continue

            text = " ".join(

                flatten_strings(
                    properties
                )
            )

            house = extract_house_from_text(

                text,

                parsed.house,
            )

            if not house:

                continue

            similarity = max(

                street_similarity(

                    variant,

                    text,

                )

                for variant
                in variants
            )

            if similarity < 0.48:

                continue

            result.append(

                Candidate(

                    source="visicom",

                    lat=lat,

                    lon=lon,

                    street=text,

                    house=parsed.house,

                    label=text[:500],

                    precision="address",

                    confidence=0.98,

                    query_street=query,
                )
            )

        if result:

            break

    return result


# ============================================================
# MAPBOX
# ============================================================

async def geocode_mapbox(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    if not MAPBOX_TOKEN:

        return []

    result = []

    for street in street_variants(
        parsed.street
    )[:6]:

        data = await get_json(

            session,

            (
                "https://api.mapbox.com/"
                "search/geocode/v6/"
                "forward"
            ),

            params={

                "address_number":
                    parsed.house,

                "street":
                    street,

                "place":
                    CITY_UA,

                "country":
                    "ua",

                "language":
                    "uk,ru",

                "autocomplete":
                    "false",

                "limit":
                    8,

                "access_token":
                    MAPBOX_TOKEN,
            },
        )

        for feature in (

            data.get(
                "features"
            )

            or

            []
        ):

            properties = (

                feature.get(
                    "properties"
                )

                or

                {}
            )

            context = (

                properties.get(
                    "context"
                )

                or

                {}
            )

            address_data = (

                context.get(
                    "address"
                )

                or

                {}
            )

            street_data = (

                context.get(
                    "street"
                )

                or

                {}
            )

            match_code = (

                properties.get(
                    "match_code"
                )

                or

                {}
            )

            house = str(

                address_data.get(
                    "address_number"
                )

                or

                properties.get(
                    "address_number"
                )

                or

                properties.get(
                    "address"
                )

                or

                ""
            )

            if (

                not house

                and

                str(

                    match_code.get(
                        "address_number"
                    )

                    or

                    ""
                ).lower()
                ==
                "matched"

            ):

                house = parsed.house

            if not same_house(

                house,

                parsed.house,

            ):

                continue

            if (

                str(

                    match_code.get(
                        "street"
                    )

                    or

                    ""
                ).lower()

                ==

                "unmatched"

            ):

                continue

            coordinate_data = (

                properties.get(
                    "coordinates"
                )

                or

                {}
            )

            lat = None
            lon = None

            precision = str(

                coordinate_data.get(
                    "accuracy"
                )

                or

                "address"
            ).lower()

            for point in (

                coordinate_data.get(
                    "routable_points"
                )

                or

                []
            ):

                if (

                    str(

                        point.get(
                            "name"
                        )

                        or

                        ""
                    ).lower()

                    ==

                    "entrance"

                ):

                    lat = float(
                        point["latitude"]
                    )

                    lon = float(
                        point["longitude"]
                    )

                    precision = "entrance"

                    break

            if lat is None:

                coords = (

                    (
                        feature.get(
                            "geometry"
                        )

                        or

                        {}
                    ).get(
                        "coordinates"
                    )

                    or

                    []
                )

                if len(
                    coords
                ) >= 2:

                    lon = float(
                        coords[0]
                    )

                    lat = float(
                        coords[1]
                    )

            if (

                lat is None

                or

                lon is None

            ):

                continue

            if not in_target_district(

                lat,

                lon,

            ):

                continue

            confidence = {

                "exact":
                    0.99,

                "high":
                    0.94,

                "medium":
                    0.80,

                "low":
                    0.60,

            }.get(

                str(

                    match_code.get(
                        "confidence"
                    )

                    or

                    ""
                ).lower(),

                0.82,
            )

            if precision not in {

                "rooftop",

                "entrance",

                "point",

                "parcel",

                "interpolated",

                "approximate",

            }:

                precision = "address"

            result.append(

                Candidate(

                    source="mapbox",

                    lat=lat,

                    lon=lon,

                    street=str(

                        street_data.get(
                            "name"
                        )

                        or

                        street
                    ),

                    house=house,

                    label=str(

                        properties.get(
                            "full_address"
                        )

                        or

                        properties.get(
                            "name"
                        )

                        or

                        ""
                    ),

                    precision=precision,

                    confidence=confidence,

                    query_street=street,
                )
            )

        if result:

            break

    return result


# ============================================================
# CLUSTERS
# ============================================================

def cluster_medoid(
    candidates: list[Candidate],
) -> Candidate:

    #
    # Выбираем РЕАЛЬНУЮ найденную точку.
    # Координаты не усредняем.
    #

    return min(

        candidates,

        key=lambda candidate:

            sum(

                distance(

                    candidate,

                    other,

                )

                for other
                in candidates
            )

            -

            candidate.score
            *
            0.03
    )


def find_other_cluster(

    candidates: list[Candidate],

) -> Optional[list[Candidate]]:

    best = None

    best_families = 0

    for seed in candidates:

        cluster = [

            candidate

            for candidate
            in candidates

            if distance(

                seed,

                candidate,

            )
            <=
            CLUSTER_METERS
        ]

        families = {

            provider_family(
                candidate.source
            )

            for candidate
            in cluster
        }

        if len(
            families
        ) > best_families:

            best = cluster

            best_families = len(
                families
            )

    return best


# ============================================================
# CHOICE — OSM PRIMARY
# ============================================================

def deterministic_choice(

    parsed: ParsedAddress,

    ranked: list[Candidate],

) -> Optional[Candidate]:

    if not ranked:

        return None

    learned = next(

        (

            candidate

            for candidate
            in ranked

            if candidate.source
            ==
            "learned"

        ),

        None,
    )

    if learned:

        return learned


    # ========================================================
    # OPENSTREETMAP — ГЛАВНЫЙ
    # ========================================================

    osm_candidates = [

        candidate

        for candidate
        in ranked

        if candidate.source
        in {
            "overpass",
            "osm",
        }
    ]

    osm_candidates.sort(

        key=lambda candidate: (

            candidate.source
            ==
            "overpass",

            candidate.precision
            ==
            "building",

            candidate.score,
        ),

        reverse=True,
    )


    if osm_candidates:

        osm_best = osm_candidates[0]


        #
        # OSM подтверждён хотя бы одним
        # независимым источником.
        #
        supporter = next(

            (

                candidate

                for candidate
                in ranked

                if (

                    provider_family(
                        candidate.source
                    )
                    !=
                    "osm"

                    and

                    distance(

                        osm_best,

                        candidate,

                    )
                    <=
                    CLUSTER_METERS
                )

            ),

            None,
        )

        if supporter:

            return osm_best


        #
        # Проверяем, не ошибся ли OSM.
        #
        others = [

            candidate

            for candidate
            in ranked

            if provider_family(
                candidate.source
            )
            !=
            "osm"
        ]


        cluster = find_other_cluster(
            others
        )


        if cluster:

            families = {

                provider_family(
                    candidate.source
                )

                for candidate
                in cluster
            }


            if len(
                families
            ) >= 2:

                nearest_to_osm = min(

                    distance(

                        osm_best,

                        candidate,

                    )

                    for candidate
                    in cluster
                )


                if (
                    nearest_to_osm
                    >=
                    OSM_CONFLICT_METERS
                ):

                    #
                    # Два независимых других
                    # источника согласны,
                    # OSM далеко.
                    #
                    return cluster_medoid(
                        cluster
                    )


        #
        # Если Overpass реально нашёл building —
        # можно принять даже без подтверждения.
        #
        if (

            osm_best.source
            ==
            "overpass"

            and

            osm_best.precision
            ==
            "building"

            and

            osm_best.confidence
            >=
            0.95

        ):

            return osm_best


        #
        # Nominatim building тоже сильный.
        #
        if (

            osm_best.source
            ==
            "osm"

            and

            osm_best.precision
            ==
            "building"

            and

            osm_best.confidence
            >=
            0.94

        ):

            return osm_best


    # ========================================================
    # OSM НЕ НАШЁЛ — GOOGLE
    # ========================================================

    google_places = next(

        (

            candidate

            for candidate
            in ranked

            if (

                candidate.source
                ==
                "google_places"

                and

                candidate.confidence
                >=
                0.93
            )

        ),

        None,
    )

    if google_places:

        return google_places


    google = next(

        (

            candidate

            for candidate
            in ranked

            if (

                candidate.source
                ==
                "google"

                and

                candidate.precision
                ==
                "rooftop"

            )

        ),

        None,
    )

    if google:

        return google


    # ========================================================
    # VISICOM
    # ========================================================

    visicom = next(

        (

            candidate

            for candidate
            in ranked

            if (

                candidate.source
                ==
                "visicom"

                and

                candidate.confidence
                >=
                0.94
            )

        ),

        None,
    )

    if visicom:

        return visicom


    # ========================================================
    # MAPBOX
    # ========================================================

    mapbox = next(

        (

            candidate

            for candidate
            in ranked

            if (

                candidate.source
                ==
                "mapbox"

                and

                candidate.precision
                in {

                    "rooftop",

                    "entrance",

                    "point",

                    "address",
                }

                and

                candidate.confidence
                >=
                0.90
            )

        ),

        None,
    )

    if mapbox:

        return mapbox


    # ========================================================
    # LAST CONSENSUS FALLBACK
    # ========================================================

    cluster = find_other_cluster(
        ranked
    )

    if cluster:

        families = {

            provider_family(
                candidate.source
            )

            for candidate
            in cluster
        }

        if len(
            families
        ) >= 2:

            return cluster_medoid(
                cluster
            )

    return None


# ============================================================
# SEARCH PIPELINE
# ============================================================

async def verify_with_other_maps(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> list[Candidate]:

    groups = await asyncio.gather(

        safe_provider(

            "google_places",

            geocode_google_places(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "google",

            geocode_google(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "visicom",

            geocode_visicom(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "mapbox",

            geocode_mapbox(

                session,

                parsed,
            ),
        ),
    )

    return [

        candidate

        for group
        in groups

        for candidate
        in group
    ]


async def resolve_address(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> tuple[
    Optional[Candidate],
    list[Candidate],
]:

    # ========================================================
    # 1. РУЧНАЯ СОХРАНЁННАЯ ТОЧКА
    # ========================================================

    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )


    # ========================================================
    # 2. OPENSTREETMAP FIRST
    # ========================================================

    #
    # Overpass и Nominatim запускаем одновременно.
    #
    osm_groups = await asyncio.gather(

        safe_provider(

            "overpass",

            geocode_overpass(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "osm",

            geocode_osm(

                session,

                parsed,
            ),
        ),
    )

    osm_candidates = [

        candidate

        for group
        in osm_groups

        for candidate
        in group
    ]


    # ========================================================
    # 3. OSM НАШЁЛ — ОСТАЛЬНЫЕ ПРОВЕРЯЮТ
    # ========================================================

    if osm_candidates:

        verification = await verify_with_other_maps(

            session,

            parsed,
        )

        all_candidates = [

            *osm_candidates,

            *verification,
        ]

        ranked = rank_candidates(

            parsed,

            all_candidates,
        )

        chosen = deterministic_choice(

            parsed,

            ranked,
        )

        if chosen:

            return (
                chosen,
                ranked,
            )


    # ========================================================
    # 4. OSM НЕ НАШЁЛ —
    # GOOGLE / VISICOM / MAPBOX
    # ========================================================

    verification = await verify_with_other_maps(

        session,

        parsed,
    )

    all_candidates = [

        *osm_candidates,

        *verification,
    ]

    ranked = rank_candidates(

        parsed,

        all_candidates,
    )

    chosen = deterministic_choice(

        parsed,

        ranked,
    )

    if chosen:

        return (
            chosen,
            ranked,
        )


    #
    # Не выдаём сомнительный адрес.
    #
    return (
        None,
        ranked,
    )


# ============================================================
# CACHE
# ============================================================

memory_cache: dict[

    str,

    tuple[

        float,

        Candidate,

        list[Candidate],
    ],

] = {}


inflight: dict[
    str,
    asyncio.Task,
] = {}


pending_results: dict[
    str,
    PendingResult,
] = {}


awaiting_correction: dict[

    tuple[int, int],

    PendingResult,

] = {}


async def resolve_cached(

    session: aiohttp.ClientSession,

    parsed: ParsedAddress,

) -> tuple[
    Optional[Candidate],
    list[Candidate],
]:

    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    key = address_key(
        parsed
    )

    cached = memory_cache.get(
        key
    )

    if (

        cached

        and

        time.time()
        -
        cached[0]
        <=
        CACHE_TTL

    ):

        return (
            cached[1],
            cached[2],
        )

    if key in inflight:

        return await inflight[
            key
        ]

    async def worker():

        best, ranked = await resolve_address(

            session,

            parsed,
        )

        if (

            best

            and

            best.source
            !=
            "mapbox"

        ):

            memory_cache[
                key
            ] = (

                time.time(),

                best,

                ranked,
            )

        return (
            best,
            ranked,
        )

    task = asyncio.create_task(
        worker()
    )

    inflight[
        key
    ] = task

    try:

        return await task

    finally:

        inflight.pop(
            key,
            None,
        )


# ============================================================
# MAP LINKS
# ============================================================

def maps_url(

    lat: float,

    lon: float,

) -> str:

    return (

        "https://www.google.com/"
        "maps/search/"
        "?api=1&query="

        f"{lat:.7f},"
        f"{lon:.7f}"
    )


def maps_address_url(
    parsed: ParsedAddress,
) -> str:

    query = quote(

        (
            f"{parsed.street} "
            f"{parsed.house}, "
            f"{CITY_RU}"
        )
    )

    return (

        "https://www.google.com/"
        "maps/search/"
        "?api=1&query="

        f"{query}"
    )


# ============================================================
# MANUAL GOOGLE MAPS CORRECTION
# ============================================================

def coords_from_text(
    text: str,
) -> Optional[
    tuple[float, float]
]:

    decoded = unquote(
        text
        or
        ""
    )

    patterns = [

        (
            r"@"
            r"(-?\d{1,2}\.\d+),"
            r"(-?\d{1,3}\.\d+)"
        ),

        (
            r"!3d"
            r"(-?\d{1,2}\.\d+)"
            r"!4d"
            r"(-?\d{1,3}\.\d+)"
        ),

        (
            r"[?&]"
            r"(?:q|query|ll)="
            r"(-?\d{1,2}\.\d+)"
            r"[,%20\s]+"
            r"(-?\d{1,3}\.\d+)"
        ),

        (
            r"(?<!\d)"
            r"(-?\d{1,2}\.\d+)"
            r"\s*[,; ]\s*"
            r"(-?\d{1,3}\.\d+)"
            r"(?!\d)"
        ),
    ]

    for pattern in patterns:

        match = re.search(

            pattern,

            decoded,

            flags=re.I,
        )

        if not match:

            continue

        lat = float(
            match.group(1)
        )

        lon = float(
            match.group(2)
        )

        if in_target_district(

            lat,

            lon,

        ):

            return (
                lat,
                lon,
            )

    return None


async def coords_from_google_link(

    session: aiohttp.ClientSession,

    text: str,

) -> Optional[
    tuple[float, float]
]:

    direct = coords_from_text(
        text
    )

    if direct:

        return direct

    match = re.search(

        r"https?://[^\s]+",

        text,

        flags=re.I,
    )

    if not match:

        return None

    url = match.group(
        0
    ).rstrip(
        ".,);]"
    )

    if not any(

        domain
        in url.lower()

        for domain in (

            "google.com/maps",

            "maps.app.goo.gl",

            "goo.gl/maps",
        )
    ):

        return None

    try:

        async with session.get(

            url,

            allow_redirects=True,

            headers={
                "User-Agent":
                    "Mozilla/5.0"
            },

        ) as response:

            coords = coords_from_text(

                str(
                    response.url
                )
            )

            if coords:

                return coords

            html = await response.text()

            return coords_from_text(
                html[:700000]
            )

    except Exception as exc:

        log.warning(

            "Google link decode: %s",

            exc,
        )

        return None


async def save_manual_correction(

    update: Update,

    pending: PendingResult,

    lat: float,

    lon: float,

) -> None:

    for candidate in pending.candidates:

        meters = haversine_m(

            lat,

            lon,

            candidate.lat,

            candidate.lon,
        )

        if meters <= 55:

            update_provider_stat(

                candidate.source,

                True,
            )

        elif meters >= 140:

            update_provider_stat(

                candidate.source,

                False,
            )

    save_learned(

        pending.parsed,

        lat,

        lon,

        pending.parsed.original,

        "user_correction",
    )

    memory_cache.pop(

        address_key(
            pending.parsed
        ),

        None,
    )

    if update.message:

        await update.message.reply_text(

            (
                "✅ Точная точка сохранена.\n\n"

                f"📍 "
                f"{pending.parsed.original}"
            ),

            reply_markup=InlineKeyboardMarkup([

                [

                    InlineKeyboardButton(

                        "📍 Открыть точку ↗",

                        url=maps_url(
                            lat,
                            lon,
                        ),
                    )
                ]
            ]),
        )


# ============================================================
# TELEGRAM UI
# ============================================================

def result_keyboard(

    token: str,

    best: Candidate,

) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup([

        [

            InlineKeyboardButton(

                "📍 Открыть Google Maps ↗",

                url=maps_url(

                    best.lat,

                    best.lon,
                ),
            )
        ],

        [

            InlineKeyboardButton(

                "✅ Метка верная",

                callback_data=(
                    f"ok:{token}"
                ),
            ),

            InlineKeyboardButton(

                "🎯 Уточнить",

                callback_data=(
                    f"fix:{token}"
                ),
            ),
        ],
    ])


def not_found_keyboard(

    token: str,

    parsed: ParsedAddress,

) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup([

        [

            InlineKeyboardButton(

                "🔎 Открыть Google Maps ↗",

                url=maps_address_url(
                    parsed
                ),
            )
        ],

        [

            InlineKeyboardButton(

                "🎯 Сохранить точную точку",

                callback_data=(
                    f"fix:{token}"
                ),
            )
        ],
    ])


# ============================================================
# COMMANDS
# ============================================================

async def start_cmd(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    if not update.message:

        return

    await update.message.reply_text(

        (
            "Отправь улицу и дом.\n\n"

            "Например:\n"

            "Лермонтова 25\n\n"

            "Поиск только в "
            "Центрально-Городском районе."
        )
    )


async def status_cmd(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    if not update.message:

        return

    await update.message.reply_text(

        (
            "Главный: OpenStreetMap ✅\n"

            f"Google: "
            f"{'✅' if GOOGLE_API_KEY else '—'}\n"

            f"Visicom: "
            f"{'✅' if VISICOM_KEY else '—'}\n"

            f"Mapbox: "
            f"{'✅' if MAPBOX_TOKEN else '—'}\n"

            f"Граница района: "
            f"{'✅' if DISTRICT_GEOJSON else '⚠️ bbox'}"
        )
    )


# ============================================================
# TEST — ЛЕРМОНТОВА 25
# ============================================================

async def test_cmd(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    if not update.message:

        return

    session = context.application.bot_data[
        "http"
    ]

    parsed = ParsedAddress(

        original="Лермонтова 25",

        street="Лермонтова",

        house="25",
    )

    groups = await asyncio.gather(

        safe_provider(

            "overpass",

            geocode_overpass(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "osm",

            geocode_osm(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "google_places",

            geocode_google_places(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "google",

            geocode_google(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "visicom",

            geocode_visicom(

                session,

                parsed,
            ),
        ),

        safe_provider(

            "mapbox",

            geocode_mapbox(

                session,

                parsed,
            ),
        ),
    )

    ranked = rank_candidates(

        parsed,

        [

            candidate

            for group
            in groups

            for candidate
            in group
        ],
    )

    if not ranked:

        await update.message.reply_text(

            "Лермонтова 25: "
            "точный дом не найден."
        )

        return

    chosen = deterministic_choice(

        parsed,

        ranked,
    )

    lines = [
        "🧪 Лермонтова 25"
    ]

    for candidate in ranked[:12]:

        lines.append(

            (
                f"\n{candidate.source}: "
                f"{candidate.lat:.7f}, "
                f"{candidate.lon:.7f}\n"

                f"{candidate.precision} | "
                f"score "
                f"{candidate.score:.1f}"
            )
        )

    if chosen:

        lines.append(
            "\n"
        )

        lines.append(

            (
                "✅ ВЫБРАНО: "
                f"{chosen.source}\n"

                f"{chosen.lat:.7f}, "
                f"{chosen.lon:.7f}"
            )
        )

    await update.message.reply_text(

        "\n".join(
            lines
        )
    )


# ============================================================
# TEXT HANDLER
# ============================================================

def cleanup_pending() -> None:

    cutoff = (

        time.time()

        -

        PENDING_TTL
    )

    for token in list(
        pending_results
    ):

        if (

            pending_results[
                token
            ].created_at

            <
            cutoff

        ):

            pending_results.pop(
                token,
                None,
            )

    for key in list(
        awaiting_correction
    ):

        if (

            awaiting_correction[
                key
            ].created_at

            <
            cutoff

        ):

            awaiting_correction.pop(
                key,
                None,
            )


async def handle_text(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    if (

        not update.message

        or

        not update.message.text

        or

        not update.effective_user

    ):

        return

    cleanup_pending()

    session = context.application.bot_data[
        "http"
    ]

    user = update.effective_user

    text = update.message.text.strip()

    correction_key = (

        update.message.chat.id,

        user.id,
    )

    pending = awaiting_correction.get(
        correction_key
    )

    if pending:

        coords = await coords_from_google_link(

            session,

            text,
        )

        if coords:

            awaiting_correction.pop(

                correction_key,

                None,
            )

            await save_manual_correction(

                update,

                pending,

                coords[0],

                coords[1],
            )

            return

        if "http" in text.lower():

            await update.message.reply_text(

                (
                    "Не смог получить координаты.\n\n"

                    "В Google Maps зажми точный дом → "
                    "Поделиться → "
                    "Копировать ссылку → "
                    "отправь её сюда."
                )
            )

            return

    parsed = parse_address(
        text
    )

    if not parsed:

        return

    best, ranked = await resolve_cached(

        session,

        parsed,
    )

    token = uuid.uuid4().hex[:12]

    pending_results[
        token
    ] = PendingResult(

        owner_id=user.id,

        chat_id=update.message.chat.id,

        parsed=parsed,

        best=best,

        candidates=ranked,

        created_at=time.time(),
    )

    if not best:

        await update.message.reply_text(

            (
                "🔎 Точный дом не подтверждён:\n"

                f"{parsed.original}\n\n"

                "Неправильную метку "
                "бот отправлять не будет."
            ),

            reply_markup=not_found_keyboard(

                token,

                parsed,
            ),
        )

        return

    await update.message.reply_text(

        (
            f"📍 Улица: "
            f"{parsed.original}\n"

            f"🏙 Кривой Рог\n\n"

            "Нажми кнопку ниже 👇"
        ),

        reply_markup=result_keyboard(

            token,

            best,
        ),
    )


# ============================================================
# CALLBACK
# ============================================================

async def callback_handler(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    query = update.callback_query

    user = update.effective_user

    if (

        not query

        or

        not user

    ):

        return

    data = query.data or ""

    if ":" not in data:

        await query.answer()

        return

    action, token = data.split(
        ":",
        1,
    )

    pending = pending_results.get(
        token
    )

    if not pending:

        await query.answer(

            "Метка устарела",

            show_alert=True,
        )

        return

    if user.id != pending.owner_id:

        await query.answer(

            "Изменить может автор запроса",

            show_alert=True,
        )

        return

    if action == "ok":

        if not pending.best:

            await query.answer(
                "Нет точки"
            )

            return

        save_learned(

            pending.parsed,

            pending.best.lat,

            pending.best.lon,

            pending.best.label
            or
            pending.parsed.original,

            pending.best.source,
        )

        update_provider_stat(

            pending.best.source,

            True,
        )

        memory_cache.pop(

            address_key(
                pending.parsed
            ),

            None,
        )

        await query.answer(
            "Сохранил ✅"
        )

        if query.message:

            await query.edit_message_text(

                (
                    f"📍 Улица: "
                    f"{pending.parsed.original}\n"

                    f"🏙 Кривой Рог\n\n"

                    "✅ Метка подтверждена."
                ),

                reply_markup=InlineKeyboardMarkup([

                    [

                        InlineKeyboardButton(

                            "📍 Открыть Google Maps ↗",

                            url=maps_url(

                                pending.best.lat,

                                pending.best.lon,
                            ),
                        )
                    ]
                ]),
            )

        return

    if action == "fix":

        await query.answer()

        awaiting_correction[
            (
                pending.chat_id,

                pending.owner_id,
            )
        ] = pending

        url = (

            maps_url(

                pending.best.lat,

                pending.best.lon,
            )

            if pending.best

            else

            maps_address_url(
                pending.parsed
            )
        )

        if query.message:

            await query.message.reply_text(

                (
                    "🎯 Уточнение точки\n\n"

                    "1. Открой Google Maps.\n"

                    "2. Зажми нужный дом.\n"

                    "3. Поделиться → "
                    "Копировать ссылку.\n"

                    "4. Пришли ссылку сюда."
                ),

                reply_markup=InlineKeyboardMarkup([

                    [

                        InlineKeyboardButton(

                            "🗺 Открыть карту ↗",

                            url=url,
                        )
                    ]
                ]),
            )


# ============================================================
# TELEGRAM LOCATION
# ============================================================

async def handle_location(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    if (

        not update.message

        or

        not update.message.location

        or

        not update.effective_user

    ):

        return

    key = (

        update.message.chat.id,

        update.effective_user.id,
    )

    pending = awaiting_correction.get(
        key
    )

    if not pending:

        return

    lat = float(
        update.message.location.latitude
    )

    lon = float(
        update.message.location.longitude
    )

    if not in_target_district(

        lat,

        lon,

    ):

        await update.message.reply_text(

            (
                "❌ Точка вне "
                "Центрально-Городского района."
            )
        )

        return

    awaiting_correction.pop(
        key,
        None,
    )

    await save_manual_correction(

        update,

        pending,

        lat,

        lon,
    )


# ============================================================
# ERRORS
# ============================================================

async def error_handler(

    update: object,

    context: ContextTypes.DEFAULT_TYPE,

) -> None:

    log.exception(

        "Telegram error",

        exc_info=context.error,
    )


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def post_init(
    application: Application,
) -> None:

    session = aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT
    )

    application.bot_data[
        "http"
    ] = session

    district_loaded = await load_district_polygon(
        session
    )

    if district_loaded:

        log.info(
            "Central district polygon OK"
        )

    else:

        log.warning(

            "District polygon not loaded. "
            "Using bbox fallback."
        )


async def post_shutdown(
    application: Application,
) -> None:

    session = application.bot_data.get(
        "http"
    )

    if (

        isinstance(
            session,
            aiohttp.ClientSession,
        )

        and

        not session.closed

    ):

        await session.close()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    if not BOT_TOKEN:

        raise RuntimeError(
            "Не задан BOT_TOKEN"
        )

    init_db()

    log.info(
        "================================"
    )

    log.info(
        "OSM PRIMARY MODE"
    )

    log.info(
        "Google=%s",
        bool(
            GOOGLE_API_KEY
        ),
    )

    log.info(
        "Visicom=%s",
        bool(
            VISICOM_KEY
        ),
    )

    log.info(
        "Mapbox=%s",
        bool(
            MAPBOX_TOKEN
        ),
    )

    log.info(
        "DB=%s",
        DB_PATH,
    )

    log.info(
        "================================"
    )

    application = (

        Application
        .builder()

        .token(
            BOT_TOKEN
        )

        .post_init(
            post_init
        )

        .post_shutdown(
            post_shutdown
        )

        .build()
    )

    application.add_handler(

        CommandHandler(

            "start",

            start_cmd,
        )
    )

    application.add_handler(

        CommandHandler(

            "status",

            status_cmd,
        )
    )

    application.add_handler(

        CommandHandler(

            "test",

            test_cmd,
        )
    )

    application.add_handler(

        CallbackQueryHandler(
            callback_handler
        )
    )

    application.add_handler(

        MessageHandler(

            filters.LOCATION,

            handle_location,
        )
    )

    application.add_handler(

        MessageHandler(

            filters.TEXT

            &

            ~filters.COMMAND,

            handle_text,
        )
    )

    application.add_error_handler(
        error_handler
    )

    application.run_polling(

        allowed_updates=
            Update.ALL_TYPES,

        drop_pending_updates=False,
    )


if __name__ == "__main__":

    main()
