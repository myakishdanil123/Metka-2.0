# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
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

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None


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

# Центрально-Міський район в Visicom.
VISICOM_DISTRICT_ID = "DST1TKQ"

# Центрально-Міський район в OSM.
OSM_DISTRICT_RELATION_ID = 1827713
OSM_DISTRICT_AREA_ID = 3600000000 + OSM_DISTRICT_RELATION_ID

# Запасной bbox района.
DISTRICT_LAT_MIN = 47.78732
DISTRICT_LAT_MAX = 48.01169
DISTRICT_LON_MIN = 33.21914
DISTRICT_LON_MAX = 33.37479

# Общая защита по Кривому Рогу.
CITY_LAT_MIN = 47.65
CITY_LAT_MAX = 48.20
CITY_LON_MIN = 32.75
CITY_LON_MAX = 33.80

HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=8.0,
    connect=2.5,
    sock_read=6.5,
)

AI_TIMEOUT = 7.0
CACHE_TTL = 24 * 3600
PENDING_TTL = 2 * 3600

PRIMARY_CLUSTER_METERS = 60.0
STRONG_CONFLICT_METERS = 120.0
MAX_CANDIDATES = 16

if Path("/app/data").exists():
    DEFAULT_DB = "/app/data/metka_precision.sqlite3"
else:
    DEFAULT_DB = "metka_precision.sqlite3"

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
    "metka-precision"
)

openai_client: Optional[AsyncOpenAI] = None

if (
    OPENAI_API_KEY
    and
    OPENAI_MODEL
    and
    AsyncOpenAI is not None
):
    try:
        openai_client = AsyncOpenAI(
            api_key=OPENAI_API_KEY
        )
    except Exception as exc:
        log.warning(
            "OpenAI disabled: %s",
            exc,
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

    def compact(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lat": round(self.lat, 7),
            "lon": round(self.lon, 7),
            "street": self.street[:180],
            "house": self.house[:30],
            "label": self.label[:300],
            "precision": self.precision,
            "confidence": round(
                self.confidence,
                3,
            ),
            "score": round(
                self.score,
                2,
            ),
        }


@dataclass(slots=True)
class PendingResult:
    owner_id: int
    chat_id: int

    parsed: ParsedAddress

    best: Optional[Candidate]

    candidates: list[Candidate]

    created_at: float


# ============================================================
# NORMALIZATION
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
    "б-р",

    "пл",
    "площадь",
    "площа",

    "шоссе",
    "шосе",

    "наб",
    "набережная",
}


SEED_ALIASES: dict[str, list[str]] = {

    # КОНТРОЛЬНЫЙ АДРЕС:
    # Лермонтова -> проспект Центральний.
    "лермонтова": [
        "центральний",
        "центральный",

        "проспект центральний",
        "проспект центральный",

        "просп. центральний",
        "просп. центральный",

        "центральний лермонтова",
        "центральный лермонтова",

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

    "волгоградская": [
        "волгоградська",
    ],

    "волгоградська": [
        "волгоградская",
    ],

    "дзержинского": [
        "дзержинського",
    ],

    "дзержинського": [
        "дзержинского",
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


# ВАЖНО:
# 25/11 -> дом 25
# 25.11 -> дом 25
# 25-11 -> дом 25
# 25А/11 -> дом 25А

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
    r"б-р|"

    r"пл(?:ощадь|оща)?|"

    r"шоссе|"
    r"шосе|"

    r"наб(?:ережная)?"

    r")"
    r"\.?\s+"
    r")?"

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
    r"apt\.?|"
    r"apartment"
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

    if len(original) > 140:
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

    if not re.search(
        r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]",
        street,
    ):
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
                NOT NULL
                DEFAULT 1,

                updated_at INTEGER NOT NULL
            );


            CREATE TABLE IF NOT EXISTS provider_stats(

                provider TEXT PRIMARY KEY,

                good INTEGER
                NOT NULL
                DEFAULT 0,

                bad INTEGER
                NOT NULL
                DEFAULT 0,

                updated_at INTEGER NOT NULL
            );


            CREATE TABLE IF NOT EXISTS street_aliases(

                alias TEXT PRIMARY KEY,

                canonical TEXT NOT NULL,

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


def learned_aliases(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    result: list[str] = []

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

            result.extend([
                str(
                    row["alias"]
                ),

                str(
                    row["canonical"]
                ),
            ])

    except sqlite3.Error:
        pass

    return result


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
                ?,?,
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
                for alias in aliases
            ),
        }

        if base in family:

            values.extend([
                canonical,
                *aliases,
            ])

    values.extend(
        learned_aliases(
            street
        )
    )

    result: list[str] = []

    seen: set[str] = set()

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

    return result[:10]


def street_similarity(
    first: str,
    second: str,
) -> float:

    import difflib

    a = street_core(
        first
    )

    b = street_core(
        second
    )

    if (
        not a
        or
        not b
    ):
        return 0.0

    if a == b:
        return 1.0

    if (
        a in b
        or
        b in a
    ):
        return 1.0

    family_a = {
        street_core(
            value
        )
        for value in street_variants(
            first
        )
    }

    family_b = {
        street_core(
            value
        )
        for value in street_variants(
            second
        )
    }

    if (
        family_a
        &
        family_b
    ):
        return 1.0

    words_a = set(
        a.split()
    )

    words_b = set(
        b.split()
    )

    jaccard = (
        len(
            words_a
            &
            words_b
        )
        /
        max(
            1,
            len(
                words_a
                |
                words_b
            ),
        )
    )

    sequence = difflib.SequenceMatcher(
        None,
        a,
        b,
    ).ratio()

    return max(
        jaccard,
        sequence,
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

        confidence=0.999,

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


def provider_multiplier(
    provider: str,
) -> float:

    try:

        with db() as connection:

            row = connection.execute(

                """
                SELECT good,bad
                FROM provider_stats
                WHERE provider=?
                """,

                (
                    provider,
                ),

            ).fetchone()

        if not row:
            return 1.0

        good = int(
            row["good"]
        )

        bad = int(
            row["bad"]
        )

        ratio = (
            good + 5
        ) / (
            good + bad + 10
        )

        return (
            0.92
            +
            0.16
            *
            ratio
        )

    except sqlite3.Error:

        return 1.0


# ============================================================
# DISTRICT
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

            cross_lon = (

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

            if lon < cross_lon:
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
                "lookup"
            ),

            params={

                "osm_ids":
                    f"R{OSM_DISTRICT_RELATION_ID}",

                "format":
                    "jsonv2",

                "polygon_geojson":
                    1,

                "addressdetails":
                    1,
            },

            headers={
                "User-Agent":
                    "Metka-Central-Kryvyi-Rih/12.0"
            },
        )

        if (
            isinstance(
                data,
                list,
            )
            and
            data
        ):

            geo = data[0].get(
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
                    "Central district polygon loaded"
                )

                return True

    except Exception as exc:

        log.warning(
            "District polygon load failed: %s",
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
        "google",
        "google_places",
    }:
        return "google"

    if source in {
        "osm",
        "overpass",
    }:
        return "osm"

    return source


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

    # Номер дома должен совпадать ТОЧНО.
    if not same_house(
        parsed.house,
        candidate.house,
    ):
        return False

    # Никогда не отдаём центр улицы.
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

    # Visicom главный.
    base = {

        "visicom":
            180.0,

        "google_places":
            148.0,

        "google":
            142.0,

        "mapbox":
            122.0,

        "overpass":
            116.0,

        "osm":
            108.0,

    }.get(
        candidate.source,
        80.0,
    )

    precision = {

        "rooftop":
            48.0,

        "building":
            44.0,

        "entrance":
            43.0,

        "parcel":
            36.0,

        "point":
            34.0,

        "address":
            30.0,

        "interpolated":
            5.0,

        "approximate":
            1.0,

        "user_confirmed":
            100.0,

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

        (
            base
            +
            precision
            +
            38.0
            *
            similarity
            +
            12.0
            *
            max(
                0.0,
                min(
                    1.0,
                    candidate.confidence,
                ),
            )
        )

        *

        provider_multiplier(
            candidate.source
        )
    )


def rank_candidates(
    parsed: ParsedAddress,
    candidates: list[Candidate],
) -> list[Candidate]:

    good = [

        candidate

        for candidate in candidates

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

        supporting_families = {

            provider_family(
                other.source
            )

            for other in good

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
                PRIMARY_CLUSTER_METERS
            )
        }

        candidate.score += min(
            54.0,
            18.0
            *
            len(
                supporting_families
            ),
        )

    good.sort(
        key=lambda candidate:
            candidate.score,
        reverse=True,
    )

    result: list[Candidate] = []

    for candidate in good:

        duplicate = any(

            provider_family(
                existing.source
            )
            ==
            provider_family(
                candidate.source
            )

            and

            distance(
                existing,
                candidate,
            )
            <
            8

            for existing in result
        )

        if duplicate:
            continue

        result.append(
            candidate
        )

    return result[
        :MAX_CANDIDATES
    ]


def build_clusters(
    candidates: list[Candidate],
) -> list[list[Candidate]]:

    clusters: list[
        list[Candidate]
    ] = []

    for seed in candidates:

        cluster = [

            candidate

            for candidate in candidates

            if distance(
                seed,
                candidate,
            )
            <=
            PRIMARY_CLUSTER_METERS
        ]

        families = {
            provider_family(
                candidate.source
            )
            for candidate in cluster
        }

        if not families:
            continue

        duplicate = False

        for existing in clusters:

            existing_families = {
                provider_family(
                    candidate.source
                )
                for candidate in existing
            }

            if (
                existing_families
                ==
                families

                and

                distance(
                    seed,
                    existing[0],
                )
                <
                15
            ):

                duplicate = True
                break

        if not duplicate:

            clusters.append(
                cluster
            )

    clusters.sort(

        key=lambda cluster: (

            len({
                provider_family(
                    candidate.source
                )
                for candidate in cluster
            }),

            max(
                (
                    candidate.score

                    for candidate
                    in cluster
                ),
                default=-9999,
            ),
        ),

        reverse=True,
    )

    return clusters


def cluster_medoid(
    cluster: list[Candidate],
) -> Candidate:

    # Выбираем реальную найденную точку.
    # Никаких усреднённых координат.
    return min(

        cluster,

        key=lambda candidate:

            sum(

                distance(
                    candidate,
                    other,
                )

                for other in cluster
            )

            -

            0.05
            *
            candidate.score
    )


def deterministic_choice(
    parsed: ParsedAddress,
    ranked: list[Candidate],
    allow_single_visicom: bool = True,
) -> Optional[Candidate]:

    if not ranked:
        return None

    if ranked[0].source == "learned":
        return ranked[0]

    visicom = next(
        (
            candidate

            for candidate in ranked

            if candidate.source
            ==
            "visicom"
        ),
        None,
    )

    clusters = build_clusters(
        ranked
    )

    strongest = (
        clusters[0]
        if clusters
        else
        []
    )

    strongest_families = {
        provider_family(
            candidate.source
        )
        for candidate in strongest
    }

    if visicom:

        # Visicom + хотя бы один другой источник рядом.
        if any(

            provider_family(
                other.source
            )
            !=
            "visicom"

            and

            distance(
                visicom,
                other,
            )
            <=
            PRIMARY_CLUSTER_METERS

            for other in ranked

        ):

            return visicom

        # Если Visicom явно далеко, а минимум
        # 2 независимых семейства совпали между собой,
        # они могут исправить Visicom.
        non_vis_cluster = [

            candidate

            for candidate in strongest

            if candidate.source
            !=
            "visicom"
        ]

        non_vis_families = {
            provider_family(
                candidate.source
            )
            for candidate in non_vis_cluster
        }

        if (
            len(
                non_vis_families
            )
            >=
            2

            and

            non_vis_cluster
        ):

            far = min(

                distance(
                    visicom,
                    candidate,
                )

                for candidate
                in non_vis_cluster
            )

            if far >= STRONG_CONFLICT_METERS:

                return cluster_medoid(
                    non_vis_cluster
                )

        # Точный Visicom можно принять и один.
        if (
            allow_single_visicom

            and

            visicom.precision in {
                "address",
                "building",
                "point",
            }

            and

            visicom.confidence
            >=
            0.95
        ):

            return visicom

    # Кластер минимум из двух источников.
    if len(
        strongest_families
    ) >= 2:

        return cluster_medoid(
            strongest
        )

    # Сильные одиночные fallback.
    for candidate in ranked:

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
            0.95
        ):
            return candidate

        if (
            candidate.source
            ==
            "google_places"

            and

            candidate.confidence
            >=
            0.94
        ):
            return candidate

        if (
            candidate.source
            ==
            "overpass"

            and

            candidate.precision
            ==
            "building"

            and

            candidate.confidence
            >=
            0.94
        ):
            return candidate

        if (
            candidate.source
            ==
            "mapbox"

            and

            candidate.precision in {
                "rooftop",
                "entrance",
            }

            and

            candidate.confidence
            >=
            0.94
        ):
            return candidate

    return None


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
                f"{body[:300]}"
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
                f"HTTP {response.status}: "
                f"{body[:300]}"
            )

        return await response.json(
            content_type=None
        )


async def safe_provider(
    name: str,
    coro: Any,
) -> list[Candidate]:

    started = time.perf_counter()

    try:

        result = await coro

        log.info(

            "%s: %d candidates in %.2fs",

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

            "%s failed in %.2fs: %s",

            name,

            time.perf_counter()
            -
            started,

            exc,
        )

        return []


def flatten_strings(
    obj: Any,
) -> list[str]:

    result: list[str] = []

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


def extract_matching_house(
    text: str,
    wanted: str,
) -> str:

    values = re.findall(

        r"(?<!\d)"
        r"("
        r"\d{1,4}"
        r"\s*"
        r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"
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


# ============================================================
# VISICOM
# ============================================================

async def geocode_visicom(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    if not VISICOM_KEY:
        return []

    variants = street_variants(
        parsed.street
    )

    queries: list[str] = []

    for street in variants:

        queries.extend([

            f"{street} {parsed.house}",

            f"вул. {street} {parsed.house}",

            f"просп. {street} {parsed.house}",
        ])

    unique_queries: list[str] = []

    seen: set[str] = set()

    for query in queries:

        key = normalize_text(
            query
        )

        if key not in seen:

            seen.add(
                key
            )

            unique_queries.append(
                query
            )

    result: list[Candidate] = []

    # Не долбим 30 запросов:
    # первые самые важные.
    for query in unique_queries[:14]:

        data = await get_json(

            session,

            (
                "https://api.visicom.ua/"
                "data-api/5.0/uk/"
                "geocode.json"
            ),

            params={

                "categories":
                    "adr_address",

                "text":
                    query,

                # ТОЛЬКО Центрально-Міський район.
                "contains":
                    VISICOM_DISTRICT_ID,

                "country":
                    "UA",

                "limit":
                    12,

                "key":
                    VISICOM_KEY,
            },
        )

        features = (
            data.get(
                "features",
                [],
            )

            if isinstance(
                data,
                dict,
            )

            else

            []
        )

        for feature in features:

            properties = (
                feature.get(
                    "properties"
                )
                or
                {}
            )

            # Visicom geo_centroid предпочтительнее.
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

            if len(coords) < 2:
                continue

            try:

                lon = float(
                    coords[0]
                )

                lat = float(
                    coords[1]
                )

            except Exception:
                continue

            if not in_target_district(
                lat,
                lon,
            ):
                continue

            all_text = " ".join(
                flatten_strings(
                    properties
                )
            )

            house = extract_matching_house(
                all_text,
                parsed.house,
            )

            # Нет точного номера — отбрасываем.
            if not house:
                continue

            similarity = max(

                street_similarity(
                    variant,
                    all_text,
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

                    street=all_text,

                    house=parsed.house,

                    label=all_text[:500],

                    precision="address",

                    confidence=0.995,

                    query_street=query,
                )
            )

        # Как только Visicom реально нашёл точный дом,
        # дальше его не тормозим лишними запросами.
        if any(
            valid_candidate(
                parsed,
                candidate,
            )
            for candidate in result
        ):

            break

    return result


# ============================================================
# GOOGLE GEOCODING
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

    result: list[Candidate] = []

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
                "results",
                []
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
                        0.995
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

            for candidate in result
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
            "addressComponents",
            []
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


async def geocode_google_places(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    if not GOOGLE_API_KEY:
        return []

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

    result: list[Candidate] = []

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
                        f"{CITY_UA}, "
                        f"{COUNTRY_UA}"
                    ),

                "languageCode":
                    "uk",

                "regionCode":
                    "UA",

                "maxResultCount":
                    8,

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
                "places",
                []
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

                extract_matching_house(
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
                location[
                    "latitude"
                ]
            )

            lon = float(
                location[
                    "longitude"
                ]
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

                    confidence=0.95,

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

    result: list[Candidate] = []

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
                "features",
                []
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

            address_object = (
                context.get(
                    "address"
                )
                or
                {}
            )

            street_object = (
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

                address_object.get(
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

            lat = None
            lon = None

            coordinate_info = (
                properties.get(
                    "coordinates"
                )
                or
                {}
            )

            precision = str(
                coordinate_info.get(
                    "accuracy"
                )
                or
                "address"
            ).lower()

            for point in (
                coordinate_info.get(
                    "routable_points",
                    []
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

                    try:

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

                    except Exception:
                        pass

            if lat is None:

                coords = (

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

                if len(coords) >= 2:

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

            if precision not in {

                "rooftop",
                "parcel",
                "point",
                "interpolated",
                "approximate",
                "entrance",

            }:

                precision = "address"

            confidence = {

                "exact":
                    0.99,

                "high":
                    0.94,

                "medium":
                    0.80,

                "low":
                    0.62,

            }.get(

                str(
                    match_code.get(
                        "confidence"
                    )
                    or
                    ""
                ).lower(),

                0.84,
            )

            result.append(
                Candidate(

                    source="mapbox",

                    lat=lat,
                    lon=lon,

                    street=str(
                        street_object.get(
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
# OPENSTREETMAP NOMINATIM
# ============================================================

async def geocode_osm(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    result: list[Candidate] = []

    headers = {

        "User-Agent":
            "Metka-Central-Kryvyi-Rih/12.0",

        "Accept-Language":
            "uk,ru;q=0.9",
    }

    # Nominatim публичный —
    # запросов делаем минимум.
    for street in street_variants(
        parsed.street
    )[:2]:

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
                    8,

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

                if address_type in {
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

                    confidence=min(

                        0.92,

                        0.72
                        +
                        float(
                            item.get(
                                "importance"
                            )
                            or
                            0.0
                        ),
                    ),

                    query_street=street,
                )
            )

        if result:
            break

        await asyncio.sleep(
            1.05
        )

    return result


# ============================================================
# OVERPASS
# ============================================================

async def geocode_overpass(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    house_regex = re.escape(
        normalize_house(
            parsed.house
        )
    )

    query = f"""
[out:json][timeout:8];

area({OSM_DISTRICT_AREA_ID})->.searchArea;

(
  node["addr:housenumber"~"^{house_regex}$",i](area.searchArea);
  way["addr:housenumber"~"^{house_regex}$",i](area.searchArea);
  relation["addr:housenumber"~"^{house_regex}$",i](area.searchArea);
);

out center tags 180;
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
                "Metka-Central-Kryvyi-Rih/12.0"
        },

    ) as response:

        if response.status != 200:

            body = await response.text()

            raise RuntimeError(
                f"Overpass HTTP "
                f"{response.status}: "
                f"{body[:200]}"
            )

        data = await response.json(
            content_type=None
        )

    variants = street_variants(
        parsed.street
    )

    result: list[Candidate] = []

    for element in (
        data.get(
            "elements",
            []
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

            for variant in variants
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
                    0.97
                    if is_building
                    else
                    0.89
                ),

                query_street=street,
            )
        )

    return result


# ============================================================
# OPTIONAL AI
# ============================================================

def parse_json_object(
    text: str,
) -> Optional[dict[str, Any]]:

    text = (
        text
        or
        ""
    ).strip()

    try:

        obj = json.loads(
            text
        )

        return (
            obj
            if isinstance(
                obj,
                dict,
            )
            else
            None
        )

    except Exception:
        pass

    match = re.search(
        r"\{.*\}",
        text,
        flags=re.S,
    )

    if not match:
        return None

    try:

        obj = json.loads(
            match.group(0)
        )

        return (
            obj
            if isinstance(
                obj,
                dict,
            )
            else
            None
        )

    except Exception:
        return None


async def ai_choose(
    parsed: ParsedAddress,
    ranked: list[Candidate],
) -> Optional[Candidate]:

    if (
        not openai_client
        or
        not ranked
    ):
        return None

    shortlist = ranked[:10]

    prompt = f"""
Ты проверяешь адрес только в Центрально-Міському районе Кривого Рога.

Искомый адрес:
{parsed.street} {parsed.house}

Даны только реальные координаты геокодеров.

Нельзя:
- придумывать координаты;
- усреднять координаты;
- изменять координаты.

Visicom — основной источник.

Но если минимум два независимых других семейства
Google / Mapbox / OSM
образуют один кластер до примерно 60 метров,
а Visicom сильно далеко,
они могут исправить Visicom.

Номер дома обязан совпадать точно.

Учитывай:
- русские и украинские варианты;
- старые названия;
- новые названия;
- Лермонтова = Центральний.

Если надёжного выбора нет:
found=false.

Кандидаты:
{json.dumps(
    [
        candidate.compact()
        for candidate in shortlist
    ],
    ensure_ascii=False,
)}

Ответ только JSON:

{{
    "found": true,
    "index": 0,
    "confidence": 0.95
}}
""".strip()

    try:

        response = await asyncio.wait_for(

            openai_client.responses.create(
                model=OPENAI_MODEL,
                input=prompt,
            ),

            timeout=AI_TIMEOUT,
        )

        obj = (
            parse_json_object(
                response.output_text
            )
            or
            {}
        )

        if not obj.get(
            "found"
        ):
            return None

        confidence = float(
            obj.get(
                "confidence",
                0,
            )
        )

        if confidence < 0.68:
            return None

        index = int(
            obj.get(
                "index",
                -1,
            )
        )

        if not (
            0
            <=
            index
            <
            len(
                shortlist
            )
        ):
            return None

        chosen = shortlist[
            index
        ]

        if valid_candidate(
            parsed,
            chosen,
        ):
            return chosen

    except Exception as exc:

        log.warning(
            "AI choose failed: %s",
            exc,
        )

    return None


# ============================================================
# SEARCH PIPELINE
# ============================================================

async def collect_primary(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    groups = await asyncio.gather(

        safe_provider(
            "visicom",
            geocode_visicom(
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
            "mapbox",
            geocode_mapbox(
                session,
                parsed,
            ),
        ),
    )

    return [

        candidate

        for group in groups

        for candidate in group
    ]


async def resolve_address(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> tuple[
    Optional[Candidate],
    list[Candidate],
]:

    # 1. Сохранённая вручную точка.
    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    # 2. Visicom + Google + Places + Mapbox одновременно.
    candidates = await collect_primary(
        session,
        parsed,
    )

    ranked = rank_candidates(
        parsed,
        candidates,
    )

    chosen = deterministic_choice(
        parsed,
        ranked,
        allow_single_visicom=True,
    )

    # Visicom нашли точно.
    if (
        chosen
        and
        chosen.source
        ==
        "visicom"
    ):

        conflicting = [

            candidate

            for candidate in ranked

            if (
                provider_family(
                    candidate.source
                )
                !=
                "visicom"

                and

                distance(
                    chosen,
                    candidate,
                )
                >=
                STRONG_CONFLICT_METERS
            )
        ]

        # Один странный источник не отменяет Visicom.
        if len({
            provider_family(
                candidate.source
            )
            for candidate in conflicting
        }) < 2:

            return (
                chosen,
                ranked,
            )

    elif chosen:

        return (
            chosen,
            ranked,
        )

    # 3. Если есть спор — OSM + Overpass.
    deep = await asyncio.gather(

        safe_provider(
            "osm",
            geocode_osm(
                session,
                parsed,
            ),
        ),

        safe_provider(
            "overpass",
            geocode_overpass(
                session,
                parsed,
            ),
        ),
    )

    for group in deep:

        candidates.extend(
            group
        )

    ranked = rank_candidates(
        parsed,
        candidates,
    )

    chosen = deterministic_choice(
        parsed,
        ranked,
        allow_single_visicom=True,
    )

    if chosen:

        return (
            chosen,
            ranked,
        )

    # 4. ИИ — только если обычная логика
    # не смогла решить спор.
    ai_best = await ai_choose(
        parsed,
        ranked,
    )

    if ai_best:

        return (
            ai_best,
            ranked,
        )

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


def cleanup_pending() -> None:

    cutoff = (
        time.time()
        -
        PENDING_TTL
    )

    for token in [

        key

        for key, value
        in pending_results.items()

        if value.created_at
        <
        cutoff

    ]:

        pending_results.pop(
            token,
            None,
        )

    for key in [

        key

        for key, value
        in awaiting_correction.items()

        if value.created_at
        <
        cutoff

    ]:

        awaiting_correction.pop(
            key,
            None,
        )


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
# GOOGLE MAPS CORRECTION
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
            f"{CITY_RU}, "
            f"{COUNTRY_RU}"
        )
    )

    return (
        "https://www.google.com/"
        "maps/search/"
        "?api=1&query="
        f"{query}"
    )


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

    url = (
        match.group(0)
        .rstrip(
            ".,);]"
        )
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
                html[:700000]
            )

    except Exception as exc:

        log.warning(
            "Google Maps link decode failed: %s",
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

        distance_to_correct = haversine_m(

            lat,
            lon,

            candidate.lat,
            candidate.lon,
        )

        if distance_to_correct <= 55:

            update_provider_stat(
                candidate.source,
                True,
            )

        elif distance_to_correct >= 140:

            update_provider_stat(
                candidate.source,
                False,
            )

    save_learned(

        pending.parsed,

        lat,
        lon,

        (
            f"{pending.parsed.street} "
            f"{pending.parsed.house}"
        ),

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
                f"{pending.parsed.original}\n\n"

                "Теперь этот адрес будет "
                "открываться по сохранённой точке."
            ),

            reply_markup=InlineKeyboardMarkup([

                [
                    InlineKeyboardButton(

                        "📍 Открыть сохранённую точку ↗",

                        url=maps_url(
                            lat,
                            lon,
                        ),
                    )
                ]
            ]),

            disable_web_page_preview=True,
        )


# ============================================================
# TELEGRAM
# ============================================================

def source_title(
    source: str,
) -> str:

    return {

        "learned":
            "сохранённая точка",

        "visicom":
            "Visicom",

        "google_places":
            "Google Places",

        "google":
            "Google",

        "mapbox":
            "Mapbox",

        "osm":
            "OpenStreetMap",

        "overpass":
            "OpenStreetMap",

    }.get(
        source,
        source,
    )


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

                "🎯 Уточнить координаты",

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

                "🔎 Открыть адрес в Google Maps ↗",

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


async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if update.message:

        await update.message.reply_text(

            (
                "Отправь улицу и номер дома "
                "Центрально-Городского района.\n"

                "Например: Лермонтова 25\n\n"

                "25/11, 25.11 и 25-11 "
                "ищутся как дом 25."
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
            "Режим: только "
            "Центрально-Городской район\n"

            f"Visicom: "
            f"{'✅' if VISICOM_KEY else '—'}\n"

            f"Google Places/Maps: "
            f"{'✅' if GOOGLE_API_KEY else '—'}\n"

            f"Mapbox: "
            f"{'✅' if MAPBOX_TOKEN else '—'}\n"

            "OpenStreetMap: ✅\n"

            f"Граница района: "
            f"{'✅ polygon' if DISTRICT_GEOJSON else '⚠️ bbox fallback'}\n"

            f"ИИ: "
            f"{'✅' if openai_client else '—'}"
        )
    )


# ============================================================
# TEST ЛЕРМОНТОВА 25
# ============================================================

async def test_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not update.message:
        return

    session: aiohttp.ClientSession = (
        context.application.bot_data[
            "http"
        ]
    )

    parsed = ParsedAddress(
        original="Лермонтова 25",
        street="Лермонтова",
        house="25",
    )

    groups = await asyncio.gather(

        safe_provider(
            "visicom",
            geocode_visicom(
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
            "mapbox",
            geocode_mapbox(
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

    candidates = rank_candidates(

        parsed,

        [
            candidate

            for group in groups

            for candidate in group
        ],
    )

    if not candidates:

        await update.message.reply_text(

            (
                "Тест Лермонтова 25:\n"
                "ни один источник "
                "не вернул точный дом."
            )
        )

        return

    # Контрольная точка пользователя.
    reference_lat = 47.9050160
    reference_lon = 33.3523642

    lines = [
        "🧪 Тест Лермонтова 25:"
    ]

    for candidate in candidates[:10]:

        meters = haversine_m(

            reference_lat,
            reference_lon,

            candidate.lat,
            candidate.lon,
        )

        lines.append(

            (
                f"{source_title(candidate.source)}: "

                f"{candidate.lat:.7f}, "
                f"{candidate.lon:.7f}"

                f" | {candidate.precision}"

                f" | ≈ {meters:.1f} м "
                f"от контрольной точки"
            )
        )

    best = deterministic_choice(
        parsed,
        candidates,
    )

    if best:

        lines.append(
            ""
        )

        lines.append(

            (
                f"✅ Выбрано: "
                f"{source_title(best.source)}"
            )
        )

    await update.message.reply_text(
        "\n".join(
            lines
        )
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

    session: aiohttp.ClientSession = (
        context.application.bot_data[
            "http"
        ]
    )

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
                    "❌ Не смог получить "
                    "координаты из ссылки.\n\n"

                    "В Google Maps зажми "
                    "точный дом → "

                    "Поделиться → "

                    "Копировать ссылку → "

                    "отправь сюда."
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

    token = uuid.uuid4().hex[
        :12
    ]

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
                "🔎 Не смог надёжно подтвердить "
                "точный дом:\n"

                f"{parsed.original}\n\n"

                "Я не буду отправлять "
                "центр улицы вместо дома.\n"

                "Можно сохранить "
                "точную точку вручную."
            ),

            reply_markup=not_found_keyboard(
                token,
                parsed,
            ),

            disable_web_page_preview=True,
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

    cleanup_pending()

    data = (
        query.data
        or
        ""
    )

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

                "Сначала укажи точную точку",

                show_alert=True,
            )

            return

        save_learned(

            pending.parsed,

            pending.best.lat,
            pending.best.lon,

            (
                pending.best.label
                or
                pending.parsed.original
            ),

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

        pending_results.pop(
            token,
            None,
        )

        await query.answer(
            "Сохранил"
        )

        if query.message:

            await query.edit_message_text(

                (
                    f"📍 Улица: "
                    f"{pending.parsed.original}\n"

                    f"🏙 Кривой Рог\n\n"

                    "✅ Метка подтверждена "
                    "и запомнена."
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
                    "🎯 Уточнение координат\n\n"

                    "1. Открой Google Maps.\n"

                    "2. Зажми точный дом.\n"

                    "3. Нажми "
                    "«Поделиться» → "
                    "«Копировать ссылку».\n"

                    "4. Отправь ссылку сюда.\n\n"

                    "После этого бот "
                    "запомнит точку."
                ),

                reply_markup=InlineKeyboardMarkup([

                    [
                        InlineKeyboardButton(

                            "🗺 Открыть Google Maps ↗",

                            url=url,
                        )
                    ]
                ]),
            )

        return

    await query.answer()


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
                "❌ Эта точка находится вне "
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


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    log.exception(
        "Telegram handler error",
        exc_info=context.error,
    )


# ============================================================
# STARTUP
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

    ok = await load_district_polygon(
        session
    )

    if not ok:

        log.warning(

            "Using district bbox fallback "
            "because OSM polygon was not loaded"
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


def validate_config() -> None:

    if not BOT_TOKEN:

        raise RuntimeError(
            "Не задан BOT_TOKEN"
        )

    if not VISICOM_KEY:

        log.warning(
            "VISICOM_KEY отсутствует — "
            "главный источник выключен"
        )

    if not any((
        VISICOM_KEY,
        GOOGLE_API_KEY,
        MAPBOX_TOKEN,
    )):

        log.warning(
            "Работает только OSM fallback"
        )


def main() -> None:

    validate_config()

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
            "Starting | "
            "district=Central | "
            "Visicom=%s "
            "Google=%s "
            "Mapbox=%s "
            "OSM=yes "
            "AI=%s "
            "DB=%s"
        ),

        bool(
            VISICOM_KEY
        ),

        bool(
            GOOGLE_API_KEY
        ),

        bool(
            MAPBOX_TOKEN
        ),

        bool(
            openai_client
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
