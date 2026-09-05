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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "AIzaSyDrf2qAL0FQJJ2_TrKWkz5IVedU-yok-uc").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg").strip()

CITY_UA = "Кривий Ріг"
CITY_RU = "Кривой Рог"
COUNTRY_UA = "Україна"

# Центрально-Городской район
DISTRICT_LAT_MIN = 47.78
DISTRICT_LAT_MAX = 48.02
DISTRICT_LON_MIN = 33.20
DISTRICT_LON_MAX = 33.39

CITY_LAT_MIN = 47.60
CITY_LAT_MAX = 48.25
CITY_LON_MIN = 32.70
CITY_LON_MAX = 33.90

# Скорость
FAST_DEADLINE = 2.8
PROVIDER_TIMEOUT = 3.2
OVERPASS_TIMEOUT = 2.7
NOMINATIM_TIMEOUT = 2.4

# Проверка источников
CLUSTER_M = 65.0
OSM_CONFLICT_M = 120.0

CACHE_TTL = 7 * 24 * 3600
PENDING_TTL = 2 * 3600

HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=PROVIDER_TIMEOUT,
    connect=1.5,
    sock_read=2.8,
)

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

if Path("/app/data").exists():
    DEFAULT_DB = "/app/data/metka_fast.sqlite3"
else:
    DEFAULT_DB = "metka_fast.sqlite3"

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
    "metka-fast"
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
# УЛИЦЫ / СТАРЫЕ НАЗВАНИЯ
# ============================================================

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

    "наб",
    "набережная",
}


SEED_ALIASES = {

    # Лермонтова = Центральный проспект
    "лермонтова": [
        "центральний",
        "центральный",

        "проспект центральний",
        "проспект центральный",

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


# 25/11 -> дом 25
# 25.11 -> дом 25
# 25-11 -> дом 25
# 25А/11 -> дом 25А

ADDRESS_RE = re.compile(

    r"(?iu)^\s*"

    r"(?:(?:"

    r"ул(?:ица)?|"
    r"вул(?:иця)?|"

    r"просп(?:ект)?|"
    r"пр-т|"

    r"пер(?:еулок)?|"
    r"пров(?:улок)?|"

    r"бул(?:ьвар)?|"

    r"пл(?:ощадь|оща)?|"

    r"шоссе|"
    r"шосе|"

    r"наб(?:ережная)?"

    r")\.?\s+)?"

    r"(?P<street>.+?)"

    r"\s*[,№#]?\s*"

    r"(?P<house>"

    r"\d{1,4}"

    r"\s*"

    r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"

    r")"

    r"(?:"

    r"\s*[/.-]\s*"

    r"\d{1,6}"

    r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"

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
    ).lower()

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
        r"[^0-9a-zа-яіїєґ'()\-\s]+",
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
        for word
        in words
        if (
            word
            and
            word not in STREET_PREFIXES
        )
    )


def same_house(
    a: str,
    b: str,
) -> bool:

    return bool(
        a
        and
        b
        and
        normalize_house(
            a
        )
        ==
        normalize_house(
            b
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

    if (
        len(
            original
        ) > 140
        or
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
                    x
                )
                for x
                in aliases
            ),
        }

        if base in family:

            values.extend([
                canonical,
                *aliases,
            ])

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

    return result[:8]


def street_similarity(
    a: str,
    b: str,
) -> float:

    aa = street_core(
        a
    )

    bb = street_core(
        b
    )

    if (
        not aa
        or
        not bb
    ):
        return 0.0

    if (
        aa == bb
        or
        aa in bb
        or
        bb in aa
    ):
        return 1.0

    fa = {
        street_core(
            x
        )
        for x
        in street_variants(
            a
        )
    }

    fb = {
        street_core(
            x
        )
        for x
        in street_variants(
            b
        )
    }

    if fa & fb:
        return 1.0

    sequence = difflib.SequenceMatcher(
        None,
        aa,
        bb,
    ).ratio()

    wa = set(
        aa.split()
    )

    wb = set(
        bb.split()
    )

    jaccard = (
        len(
            wa & wb
        )
        /
        max(
            1,
            len(
                wa | wb
            ),
        )
    )

    return max(
        sequence,
        jaccard,
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
                NOT NULL
                DEFAULT 1,

                updated_at INTEGER NOT NULL
            );
            """
        )


def address_key(
    parsed: ParsedAddress,
) -> str:

    return (
        f"{street_core(parsed.street)}"
        f"|"
        f"{normalize_house(parsed.house)}"
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

                int(
                    time.time()
                ),
            ),
        )


# ============================================================
# ЦЕНТРАЛЬНО-ГОРОДСКОЙ РАЙОН
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
                    xj - xi
                )
                *
                (
                    lat - yi
                )
                /
                (
                    (
                        yj - yi
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

        if not point_in_ring(
            lon,
            lat,
            polygon[0],
        ):
            continue

        if any(
            point_in_ring(
                lon,
                lat,
                hole,
            )
            for hole
            in polygon[1:]
        ):
            continue

        return True

    return False


def in_target_district(
    lat: float,
    lon: float,
) -> bool:

    if not (
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
    ):

        return False

    if DISTRICT_GEOJSON:

        return point_in_geojson(
            lat,
            lon,
            DISTRICT_GEOJSON,
        )

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
                f"HTTP {response.status}: "
                f"{body[:220]}"
            )

        return await response.json(
            content_type=None
        )


async def load_district_polygon(
    session: aiohttp.ClientSession,
) -> None:

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
                    3,

                "countrycodes":
                    "ua",
            },

            headers={
                "User-Agent":
                    "Metka-Kryvyi-Rih-Fast/1.0"
            },
        )

        for item in (
            data
            or
            []
        ):

            geo = item.get(
                "geojson"
            )

            if (
                isinstance(
                    geo,
                    dict,
                )

                and

                geo.get(
                    "type"
                )
                in {
                    "Polygon",
                    "MultiPolygon",
                }
            ):

                DISTRICT_GEOJSON = geo

                log.info(
                    "District polygon loaded"
                )

                return

    except Exception as exc:

        log.warning(
            "District polygon fallback to bbox: %s",
            exc,
        )


# ============================================================
# DISTANCE
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

    a = (
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
                a
            )
        )
    )


def distance(
    a: Candidate,
    b: Candidate,
) -> float:

    return haversine_m(
        a.lat,
        a.lon,
        b.lat,
        b.lon,
    )


def family(
    source: str,
) -> str:

    if source in {
        "overpass",
        "osm",
    }:

        return "osm"

    return source


# ============================================================
# ПРОВЕРКА КАНДИДАТА
# ============================================================

def valid_candidate(
    parsed: ParsedAddress,
    candidate: Candidate,
) -> bool:

    if candidate.source == "learned":
        return True

    if not in_target_district(
        candidate.lat,
        candidate.lon,
    ):

        return False

    # Номер дома обязан совпасть
    if not same_house(
        parsed.house,
        candidate.house,
    ):

        return False

    # Центр улицы не принимаем
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
# ВЕСА
# ============================================================

PROVIDER_BASE = {

    "learned":
        10000.0,

    # OSM главный
    "overpass":
        220.0,

    "osm":
        205.0,

    # Google второй
    "google":
        175.0,

    # Mapbox третий
    "mapbox":
        145.0,
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

    precision_bonus = {

        "building":
            65.0,

        "rooftop":
            62.0,

        "entrance":
            58.0,

        "point":
            48.0,

        "parcel":
            42.0,

        "address":
            36.0,

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
        PROVIDER_BASE.get(
            candidate.source,
            80.0,
        )

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

    good = [
        candidate

        for candidate
        in candidates

        if valid_candidate(
            parsed,
            candidate,
        )
    ]

    for candidate in good:

        candidate.score = score_candidate(
            parsed,
            candidate,
        )

        supporters = {

            family(
                other.source
            )

            for other
            in good

            if (
                other is not candidate

                and

                family(
                    other.source
                )
                !=
                family(
                    candidate.source
                )

                and

                distance(
                    candidate,
                    other,
                )
                <=
                CLUSTER_M
            )
        }

        candidate.score += (
            20.0
            *
            min(
                2,
                len(
                    supporters
                ),
            )
        )

    good.sort(
        key=lambda candidate:
            candidate.score,
        reverse=True,
    )

    result = []

    for candidate in good:

        if any(

            family(
                candidate.source
            )
            ==
            family(
                existing.source
            )

            and

            distance(
                candidate,
                existing,
            )
            <
            8

            for existing
            in result

        ):

            continue

        result.append(
            candidate
        )

    return result[:12]


# ============================================================
# ВЫБОР ТОЧКИ
# ============================================================

def choose_result(
    parsed: ParsedAddress,
    ranked: list[Candidate],
) -> Optional[Candidate]:

    if not ranked:
        return None

    # Сохранённая подтверждённая метка
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
    # OSM ГЛАВНЫЙ
    # ========================================================

    osm = next(
        (
            candidate
            for candidate
            in ranked
            if candidate.source
            in {
                "overpass",
                "osm",
            }
        ),
        None,
    )

    if osm:

        # Google или Mapbox рядом с OSM
        verifier = next(
            (
                candidate

                for candidate
                in ranked

                if (
                    family(
                        candidate.source
                    )
                    !=
                    "osm"

                    and

                    distance(
                        osm,
                        candidate,
                    )
                    <=
                    CLUSTER_M
                )
            ),
            None,
        )

        if verifier:

            # Оставляем координату OSM
            return osm

        google = next(
            (
                candidate
                for candidate
                in ranked
                if candidate.source
                ==
                "google"
            ),
            None,
        )

        mapbox = next(
            (
                candidate
                for candidate
                in ranked
                if candidate.source
                ==
                "mapbox"
            ),
            None,
        )

        # Google + Mapbox согласны,
        # а OSM сильно далеко
        if (
            google
            and
            mapbox
            and
            distance(
                google,
                mapbox,
            )
            <=
            CLUSTER_M
        ):

            if min(
                distance(
                    osm,
                    google,
                ),
                distance(
                    osm,
                    mapbox,
                ),
            ) >= OSM_CONFLICT_M:

                return google

        # Реальный OSM building
        if (
            osm.precision
            ==
            "building"

            and

            osm.confidence
            >=
            0.95
        ):

            return osm

    # ========================================================
    # GOOGLE
    # ========================================================

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

                and

                candidate.confidence
                >=
                0.94
            )
        ),
        None,
    )

    if google:
        return google

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

    # Последняя проверка Google + Mapbox
    google = next(
        (
            candidate
            for candidate
            in ranked
            if candidate.source
            ==
            "google"
        ),
        None,
    )

    mapbox = next(
        (
            candidate
            for candidate
            in ranked
            if candidate.source
            ==
            "mapbox"
        ),
        None,
    )

    if (
        google
        and
        mapbox
        and
        distance(
            google,
            mapbox,
        )
        <=
        CLUSTER_M
    ):

        return google

    return None


# ============================================================
# OSM OVERPASS
# ============================================================

def overpass_street_regex(
    parsed: ParsedAddress,
) -> str:

    values = []

    for street in street_variants(
        parsed.street
    ):

        core = street_core(
            street
        )

        if core:

            values.append(
                re.escape(
                    core
                )
            )

    values = sorted(
        set(
            values
        ),
        key=len,
        reverse=True,
    )

    return (
        "("
        +
        "|".join(
            values[:8]
        )
        +
        ")"
    )


def overpass_house_regex(
    parsed: ParsedAddress,
) -> str:

    number = re.match(
        r"\d+",
        normalize_house(
            parsed.house
        ),
    )

    if not number:

        return re.escape(
            parsed.house
        )

    return (
        re.escape(
            number.group(
                0
            )
        )

        +

        r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"
    )


async def geocode_overpass(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    house_regex = overpass_house_regex(
        parsed
    )

    street_regex = overpass_street_regex(
        parsed
    )

    bbox = (
        f"{DISTRICT_LAT_MIN},"
        f"{DISTRICT_LON_MIN},"
        f"{DISTRICT_LAT_MAX},"
        f"{DISTRICT_LON_MAX}"
    )

    query = f"""
[out:json][timeout:2];

(
  node
  ["addr:housenumber"~"^{house_regex}$",i]
  ["addr:street"~"{street_regex}",i]
  ({bbox});

  way
  ["addr:housenumber"~"^{house_regex}$",i]
  ["addr:street"~"{street_regex}",i]
  ({bbox});

  relation
  ["addr:housenumber"~"^{house_regex}$",i]
  ["addr:street"~"{street_regex}",i]
  ({bbox});

  node
  ["addr:housenumber"~"^{house_regex}$",i]
  ["addr:place"~"{street_regex}",i]
  ({bbox});

  way
  ["addr:housenumber"~"^{house_regex}$",i]
  ["addr:place"~"{street_regex}",i]
  ({bbox});

  relation
  ["addr:housenumber"~"^{house_regex}$",i]
  ["addr:place"~"{street_regex}",i]
  ({bbox});
);

out center tags 30;
"""

    data = None
    last_error = None

    for url in OVERPASS_URLS:

        try:

            async with session.post(
                url,

                data={
                    "data":
                        query
                },

                timeout=aiohttp.ClientTimeout(
                    total=OVERPASS_TIMEOUT
                ),

                headers={
                    "User-Agent":
                        "Metka-Kryvyi-Rih-Fast/1.0"
                },

            ) as response:

                body = await response.text()

                if response.status != 200:

                    raise RuntimeError(
                        f"Overpass "
                        f"{response.status}: "
                        f"{body[:200]}"
                    )

                data = await response.json(
                    content_type=None
                )

                break

        except Exception as exc:

            last_error = exc

    if data is None:

        if last_error:
            raise last_error

        return []

    result = []

    for element in (
        data.get(
            "elements",
            [],
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

        if (
            not same_house(
                house,
                parsed.house,
            )
            or
            not street
        ):

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

                query_street=parsed.street,
            )
        )

    return result


# ============================================================
# OSM NOMINATIM — FALLBACK
# ============================================================

async def geocode_nominatim(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    result = []

    for index, street in enumerate(
        street_variants(
            parsed.street
        )[:2]
    ):

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
                        f"{parsed.house} "
                        f"{street}, "
                        f"{CITY_UA}, "
                        f"{COUNTRY_UA}"
                    ),

                "format":
                    "jsonv2",

                "addressdetails":
                    1,

                "limit":
                    5,

                "countrycodes":
                    "ua",

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

            headers={
                "User-Agent":
                    "Metka-Kryvyi-Rih-Fast/1.0",

                "Accept-Language":
                    "uk,ru;q=0.9",
            },
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

            road = str(
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

                    street=road,

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
                        0.95
                        if precision
                        ==
                        "building"
                        else
                        0.90
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
            "address_components",
            [],
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

    # Для скорости максимум 2 названия улицы
    for street in street_variants(
        parsed.street
    )[:2]:

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
                "results",
                [],
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
    )[:2]:

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
                    5,

                "access_token":
                    MAPBOX_TOKEN,
            },
        )

        for feature in (
            data.get(
                "features",
                [],
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

            coordinates_data = (
                properties.get(
                    "coordinates"
                )
                or
                {}
            )

            lat = None
            lon = None

            precision = str(
                coordinates_data.get(
                    "accuracy"
                )
                or
                "address"
            ).lower()

            # Если Mapbox знает въезд/вход —
            # берём его
            for point in (
                coordinates_data.get(
                    "routable_points",
                    [],
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
                        point[
                            "latitude"
                        ]
                    )

                    lon = float(
                        point[
                            "longitude"
                        ]
                    )

                    precision = "entrance"

                    break

            if lat is None:

                coordinates = (
                    (
                        feature.get(
                            "geometry"
                        )
                        or
                        {}
                    )
                    .get(
                        "coordinates"
                    )
                    or
                    []
                )

                if len(
                    coordinates
                ) >= 2:

                    lon = float(
                        coordinates[0]
                    )

                    lat = float(
                        coordinates[1]
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
# БЕЗОПАСНЫЙ ВЫЗОВ ПРОВАЙДЕРА
# ============================================================

async def safe_call(
    name: str,
    coroutine: Any,
    timeout: float,
) -> list[Candidate]:

    started = time.perf_counter()

    try:

        result = await asyncio.wait_for(
            coroutine,
            timeout=timeout,
        )

        log.info(
            "%s: %d result(s) %.2fs",
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
            "%s failed %.2fs: %s",
            name,
            time.perf_counter()
            -
            started,
            exc,
        )

        return []


# ============================================================
# БЫСТРЫЙ ПОИСК
# ============================================================

async def resolve_address(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> tuple[
    Optional[Candidate],
    list[Candidate],
]:

    # Сначала наша подтверждённая база
    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    # ========================================================
    # OSM + GOOGLE + MAPBOX ЗАПУСКАЕМ ОДНОВРЕМЕННО
    # ========================================================

    tasks = {

        asyncio.create_task(
            safe_call(
                "overpass",

                geocode_overpass(
                    session,
                    parsed,
                ),

                OVERPASS_TIMEOUT
                +
                0.2,
            )
        ):
            "overpass",

        asyncio.create_task(
            safe_call(
                "google",

                geocode_google(
                    session,
                    parsed,
                ),

                PROVIDER_TIMEOUT,
            )
        ):
            "google",

        asyncio.create_task(
            safe_call(
                "mapbox",

                geocode_mapbox(
                    session,
                    parsed,
                ),

                PROVIDER_TIMEOUT,
            )
        ):
            "mapbox",
    }

    collected = []

    started = time.monotonic()

    overpass_done = False

    try:

        while (
            tasks

            and

            time.monotonic()
            -
            started
            <
            FAST_DEADLINE
        ):

            remaining = (
                FAST_DEADLINE
                -
                (
                    time.monotonic()
                    -
                    started
                )
            )

            done, _ = await asyncio.wait(
                tasks.keys(),

                timeout=min(
                    0.35,
                    remaining,
                ),

                return_when=
                    asyncio.FIRST_COMPLETED,
            )

            if not done:
                continue

            for task in done:

                name = tasks.pop(
                    task
                )

                try:

                    values = task.result()

                except Exception:

                    values = []

                collected.extend(
                    values
                )

                if name == "overpass":

                    overpass_done = True

            ranked = rank_candidates(
                parsed,
                collected,
            )

            chosen = choose_result(
                parsed,
                ranked,
            )

            # =================================================
            # OSM + подтверждение = сразу ответ
            # =================================================

            if (
                chosen
                and
                chosen.source
                ==
                "overpass"
            ):

                confirmed = any(

                    family(
                        candidate.source
                    )
                    !=
                    "osm"

                    and

                    distance(
                        chosen,
                        candidate,
                    )
                    <=
                    CLUSTER_M

                    for candidate
                    in ranked
                )

                if confirmed:

                    for pending_task in tasks:

                        pending_task.cancel()

                    return (
                        chosen,
                        ranked,
                    )

            # =================================================
            # OSM уже ответил, дома у него нет.
            # Google/Mapbox нашли — не ждём дальше.
            # =================================================

            if (
                overpass_done

                and

                chosen

                and

                chosen.source
                in {
                    "google",
                    "mapbox",
                }
            ):

                for pending_task in tasks:

                    pending_task.cancel()

                return (
                    chosen,
                    ranked,
                )

        # Забираем те задачи,
        # которые успели закончиться
        for task in list(
            tasks
        ):

            if task.done():

                try:

                    collected.extend(
                        task.result()
                    )

                except Exception:

                    pass

    finally:

        for task in tasks:

            if not task.done():

                task.cancel()

    ranked = rank_candidates(
        parsed,
        collected,
    )

    chosen = choose_result(
        parsed,
        ranked,
    )

    if chosen:

        return (
            chosen,
            ranked,
        )

    # ========================================================
    # ПОСЛЕДНИЙ FALLBACK — NOMINATIM
    # ========================================================

    nominatim = await safe_call(
        "nominatim",

        geocode_nominatim(
            session,
            parsed,
        ),

        NOMINATIM_TIMEOUT
        +
        1.2,
    )

    collected.extend(
        nominatim
    )

    ranked = rank_candidates(
        parsed,
        collected,
    )

    chosen = choose_result(
        parsed,
        ranked,
    )

    return (
        chosen,
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

        if best:

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
# GOOGLE MAPS LINKS
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
# РУЧНОЕ ИСПРАВЛЕНИЕ
# ============================================================

def coords_from_text(
    text: str,
) -> Optional[
    tuple[
        float,
        float,
    ]
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
            match.group(
                1
            )
        )

        lon = float(
            match.group(
                2
            )
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
    tuple[
        float,
        float,
    ]
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
        domain in url.lower()

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

            timeout=aiohttp.ClientTimeout(
                total=4.0
            ),

        ) as response:

            direct = coords_from_text(
                str(
                    response.url
                )
            )

            if direct:
                return direct

            html = await response.text()

            return coords_from_text(
                html[:600000]
            )

    except Exception:

        return None


# ============================================================
# BUTTONS
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


# ============================================================
# TELEGRAM
# ============================================================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if update.message:

        await update.message.reply_text(
            (
                "Отправь улицу и номер дома.\n"

                "Например: Лермонтова 25\n\n"

                "OpenStreetMap — главный, "
                "Google и Mapbox проверяют."
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
            "OpenStreetMap: ✅ главный\n"

            f"Google: "
            f"{'✅' if GOOGLE_API_KEY else '—'}\n"

            f"Mapbox: "
            f"{'✅' if MAPBOX_TOKEN else '—'}\n"

            f"Граница района: "
            f"{'✅ polygon' if DISTRICT_GEOJSON else '⚠️ bbox'}\n"

            f"Кэш: "
            f"{len(memory_cache)} адресов"
        )
    )


# ============================================================
# TEST
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

    started = time.perf_counter()

    best, ranked = await resolve_address(
        session,
        parsed,
    )

    elapsed = (
        time.perf_counter()
        -
        started
    )

    lines = [
        f"🧪 Лермонтова 25 — "
        f"{elapsed:.2f} сек"
    ]

    for candidate in ranked:

        lines.append(
            (
                f"{candidate.source}: "

                f"{candidate.lat:.7f}, "
                f"{candidate.lon:.7f} | "

                f"{candidate.precision} | "

                f"score "
                f"{candidate.score:.1f}"
            )
        )

    if best:

        lines.append(
            (
                f"\n✅ Выбрано: "
                f"{best.source}\n"

                f"{best.lat:.7f}, "
                f"{best.lon:.7f}"
            )
        )

    else:

        lines.append(
            "\n❌ Надёжная точка не выбрана"
        )

    await update.message.reply_text(
        "\n".join(
            lines
        )
    )


# ============================================================
# TEXT
# ============================================================

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

            save_learned(
                pending.parsed,

                coords[0],
                coords[1],

                pending.parsed.original,

                "user_correction",
            )

            memory_cache.pop(
                address_key(
                    pending.parsed
                ),
                None,
            )

            awaiting_correction.pop(
                correction_key,
                None,
            )

            await update.message.reply_text(
                "✅ Точная точка сохранена.",

                reply_markup=InlineKeyboardMarkup([

                    [
                        InlineKeyboardButton(
                            "📍 Открыть точку ↗",

                            url=maps_url(
                                coords[0],
                                coords[1],
                            ),
                        )
                    ]
                ]),
            )

            return

        if "http" in text.lower():

            await update.message.reply_text(
                (
                    "Не смог получить координаты. "

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

    started = time.perf_counter()

    best, ranked = await resolve_cached(
        session,
        parsed,
    )

    log.info(
        "RESULT %s -> %s %.2fs",

        parsed.original,

        best.source
        if best
        else
        "none",

        time.perf_counter()
        -
        started,
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
                f"🔎 Точный дом не подтверждён:\n"

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

        disable_web_page_preview=True,
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
                "Нет точки",
                show_alert=True,
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

        awaiting_correction[
            (
                pending.chat_id,
                pending.owner_id,
            )
        ] = pending

        await query.answer()

        if pending.best:

            url = maps_url(
                pending.best.lat,
                pending.best.lon,
            )

        else:

            url = maps_address_url(
                pending.parsed
            )

        if query.message:

            await query.message.reply_text(
                (
                    "🎯 Уточнение точки\n\n"

                    "1. Открой карту.\n"

                    "2. Зажми точный дом.\n"

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
# LOCATION
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

    awaiting_correction.pop(
        key,
        None,
    )

    await update.message.reply_text(
        "✅ Точная точка сохранена.",

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
# STARTUP
# ============================================================

async def post_init(
    application: Application,
) -> None:

    connector = aiohttp.TCPConnector(
        limit=30,

        limit_per_host=10,

        ttl_dns_cache=600,

        keepalive_timeout=30,
    )

    session = aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT,

        connector=connector,

        headers={
            "User-Agent":
                "Metka-Kryvyi-Rih-Fast/1.0"
        },
    )

    application.bot_data[
        "http"
    ] = session

    # Загружается один раз,
    # а не на каждый адрес
    await load_district_polygon(
        session
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

    log.info(
        (
            "Starting fast mode | "
            "OSM primary | "
            "Google=%s | "
            "Mapbox=%s | "
            "DB=%s"
        ),

        bool(
            GOOGLE_API_KEY
        ),

        bool(
            MAPBOX_TOKEN
        ),

        DB_PATH,
    )

    application.run_polling(
        allowed_updates=
            Update.ALL_TYPES,

        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
