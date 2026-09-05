from __future__ import annotations

import asyncio
import difflib
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

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

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
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "AIzaSyDrf2qAL0FQJJ2_TrKWkz5IVedU-yok-uc").strip()
VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg05:04").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()

# Новая база специально, чтобы старые ошибочные точки не мешали.
if Path("/app/data").exists():
    DEFAULT_DB = "/app/data/metka_v8.sqlite3"
else:
    DEFAULT_DB = "metka_v8.sqlite3"

DB_PATH = os.getenv(
    "DB_PATH",
    DEFAULT_DB,
).strip()


CITY_UA = "Кривий Ріг"
CITY_RU = "Кривой Рог"

COUNTRY_UA = "Україна"
COUNTRY_RU = "Украина"


# Границы Кривого Рога.
# Это только дополнительная защита.
LAT_MIN = 47.65
LAT_MAX = 48.20

LON_MIN = 32.75
LON_MAX = 33.80


HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=9.0,
    connect=2.5,
    sock_read=7.0,
)

AI_TIMEOUT = 7.0

CACHE_TTL = 24 * 60 * 60
PENDING_TTL = 2 * 60 * 60

# Подтверждение Google другими картами.
GOOGLE_CONFIRM_METERS = 70.0

# Считаем две точки совпавшими.
CONSENSUS_METERS = 55.0

# Если Google дальше этого расстояния от двух
# совпавших независимых источников — считаем спором.
GOOGLE_CONFLICT_METERS = 120.0

MAX_AI_CANDIDATES = 10


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
    "metka-v8"
)


# ============================================================
# OPENAI
# ============================================================

ai_client: Optional[AsyncOpenAI] = None

if (
    OPENAI_API_KEY
    and
    AsyncOpenAI is not None
):
    ai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# МОДЕЛИ
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

    street: str = ""
    house: str = ""

    label: str = ""

    precision: str = "unknown"
    confidence: float = 0.0

    # Каким вариантом улицы был найден адрес.
    query_street: str = ""

    score: float = 0.0


@dataclass(slots=True)
class PendingResult:
    owner_id: int
    chat_id: int

    parsed: ParsedAddress

    best: Candidate | None

    candidates: list[Candidate]

    created_at: float


# ============================================================
# УЛИЦЫ
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


SEED_ALIASES = {

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


CYR_LOOKALIKES = str.maketrans({

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
# ПАРСЕР АДРЕСОВ
# ============================================================

# Лермонтова 25       -> дом 25
# Лермонтова 25/11    -> дом 25
# Лермонтова 25.11    -> дом 25
# Лермонтова 25-11    -> дом 25
# Лермонтова 25А/11   -> дом 25А
# Лермонтова 25, кв 4 -> дом 25

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
    r"б-р|"

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

    # квартира после / . -
    r"(?:"

    r"\s*[/.-]\s*"

    r"\d{1,6}"

    r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"

    r")?"

    # квартира через "кв"
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
    )

    value = value.lower()

    value = (
        value
        .replace("ё", "е")
        .replace("’", "'")
        .replace("`", "'")
    )

    value = re.sub(
        r"[^0-9a-zа-яіїєґ'\-\s]+",
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
        CYR_LOOKALIKES
    )

    return (
        value
        .replace(" ", "")
        .upper()
        .replace("Ё", "Е")
    )


def street_core(
    value: str,
) -> str:

    words = [

        word.strip(".-")

        for word in normalize_text(
            value
        ).split()
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
        normalize_house(first)
        ==
        normalize_house(second)
    )


def parse_address(
    text: str,
) -> ParsedAddress | None:

    if not text:
        return None

    original = unicodedata.normalize(
        "NFKC",
        text,
    ).strip()

    if len(original) > 140:
        return None

    lower = original.lower()

    if (
        "http://" in lower
        or
        "https://" in lower
    ):
        return None

    # Если пользователь сам написал город —
    # убираем его перед разбором.
    cleaned = re.sub(

        r"(?iu),?\s*"
        r"(?:м\.?\s*)?"
        r"(?:"
        r"кривой\s+рог|"
        r"кривий\s+ріг"
        r")"
        r"(?:"
        r",?\s*"
        r"(?:украина|україна)"
        r")?"
        r"\s*$",

        "",

        original,

    ).strip(" ,")

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
        street_core(street)
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


def street_similarity(
    first: str,
    second: str,
) -> float:

    first = street_core(
        first
    )

    second = street_core(
        second
    )

    if not first or not second:
        return 0.0

    if first == second:
        return 1.0

    sequence = difflib.SequenceMatcher(
        None,
        first,
        second,
    ).ratio()

    first_words = set(
        first.split()
    )

    second_words = set(
        second.split()
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


# ============================================================
# SQLITE / ОБУЧЕНИЕ
# ============================================================

def ensure_db_dir() -> None:

    Path(
        DB_PATH
    ).parent.mkdir(
        parents=True,
        exist_ok=True,
    )


def db() -> sqlite3.Connection:

    ensure_db_dir()

    connection = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_db() -> None:

    with db() as conn:

        conn.execute(
            "PRAGMA journal_mode=WAL"
        )

        conn.execute(
            "PRAGMA synchronous=NORMAL"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS confirmed_addresses (

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
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_stats (

                provider TEXT PRIMARY KEY,

                good INTEGER
                NOT NULL
                DEFAULT 0,

                bad INTEGER
                NOT NULL
                DEFAULT 0,

                updated_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS street_aliases (

                alias TEXT PRIMARY KEY,

                canonical TEXT NOT NULL,

                confirmations INTEGER
                NOT NULL
                DEFAULT 1,

                updated_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS corrections_log (

                id INTEGER
                PRIMARY KEY
                AUTOINCREMENT,

                query_key TEXT NOT NULL,

                old_lat REAL,

                old_lon REAL,

                new_lat REAL NOT NULL,

                new_lon REAL NOT NULL,

                old_source TEXT,

                created_at INTEGER NOT NULL
            )
        """)


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
) -> Candidate | None:

    with db() as conn:

        row = conn.execute(

            """
            SELECT *
            FROM confirmed_addresses
            WHERE query_key=?
            """,

            (
                address_key(parsed),
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

        confidence=min(

            0.999,

            0.97
            +
            0.005
            *
            int(
                row["confirmations"]
            ),
        ),

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

    with db() as conn:

        conn.execute(
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
                ?,?,?,?,?,?,?,?,1,?
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
                address_key(parsed),

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
        "user",
        "user_correction",
    }:
        return

    now = int(
        time.time()
    )

    with db() as conn:

        conn.execute(
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

                now,
            ),
        )


def provider_multiplier(
    provider: str,
) -> float:

    if provider == "learned":
        return 1.5

    try:

        with db() as conn:

            row = conn.execute(

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
            good
            +
            bad
            +
            10
        )

        # История влияет немного.
        # Она не может победить очевидно правильный Google.
        return (
            0.90
            +
            ratio
            *
            0.20
        )

    except sqlite3.Error:

        return 1.0


def learned_aliases(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    result: list[str] = []

    with db() as conn:

        rows = conn.execute(

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

    if not alias or not canonical:
        return

    with db() as conn:

        conn.execute(
            """
            INSERT INTO street_aliases(
                alias,
                canonical,
                confirmations,
                updated_at
            )

            VALUES(
                ?,?,1,?
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


def static_street_variants(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    variants = [
        street
    ]

    for canonical, aliases in SEED_ALIASES.items():

        family = {
            street_core(
                canonical
            ),
            *(
                street_core(alias)
                for alias in aliases
            ),
        }

        if base in family:

            variants.append(
                canonical
            )

            variants.extend(
                aliases
            )

    variants.extend(
        learned_aliases(
            street
        )
    )

    result = []

    seen = set()

    for value in variants:

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

    return result[:6]


# ============================================================
# ГЕОМЕТРИЯ
# ============================================================

def in_city(
    lat: float,
    lon: float,
) -> bool:

    return (
        LAT_MIN <= lat <= LAT_MAX
        and
        LON_MIN <= lon <= LON_MAX
    )


def distance_coords(
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

        math.cos(p1)
        *
        math.cos(p2)

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

    return distance_coords(
        first.lat,
        first.lon,
        second.lat,
        second.lon,
    )


# ============================================================
# ПРОВЕРКА КАНДИДАТА
# ============================================================

def street_match_score(
    parsed: ParsedAddress,
    candidate: Candidate,
) -> float:

    returned = (
        candidate.street
        or
        candidate.label
    )

    scores = [

        street_similarity(
            parsed.street,
            returned,
        )
    ]

    if candidate.query_street:

        scores.append(

            0.93
            *
            street_similarity(
                candidate.query_street,
                returned,
            )
        )

    for variant in static_street_variants(
        parsed.street
    ):

        scores.append(

            0.96
            *
            street_similarity(
                variant,
                returned,
            )
        )

    return max(
        scores
    )


def valid_candidate(
    parsed: ParsedAddress,
    candidate: Candidate,
) -> bool:

    if not in_city(
        candidate.lat,
        candidate.lon,
    ):
        return False

    if candidate.source == "learned":
        return True

    # Главное правило:
    # номер дома должен совпасть.
    if not same_house(
        parsed.house,
        candidate.house,
    ):
        return False

    if street_match_score(
        parsed,
        candidate,
    ) < 0.52:

        return False

    # Никогда не даём просто точку улицы/города.
    if candidate.precision in {
        "street",
        "city",
        "center",
        "unknown",
    }:
        return False

    return True


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

    provider = {

        # GOOGLE ГЛАВНЫЙ
        "google": 145,

        # Google Places fallback
        "google_places": 132,

        "visicom": 118,

        "mapbox": 112,

        "overpass": 103,

        "osm": 98,

    }.get(
        candidate.source,
        80,
    )

    precision = {

        "rooftop": 44,

        "building": 38,

        "entrance": 38,

        "parcel": 32,

        "point": 30,

        "address": 25,

        "interpolated": 7,

        "approximate": 2,

        "user_confirmed": 100,

    }.get(
        candidate.precision,
        0,
    )

    score = (

        provider

        +

        precision

        +

        38
        *
        street_match_score(
            parsed,
            candidate,
        )

        +

        12
        *
        max(
            0.0,
            min(
                1.0,
                candidate.confidence,
            ),
        )
    )

    return (

        score

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

        agreeing = {

            other.source

            for other in good

            if (
                other is not candidate
                and
                other.source != candidate.source
                and
                distance(
                    candidate,
                    other,
                )
                <=
                CONSENSUS_METERS
            )
        }

        candidate.score += min(
            48,
            len(
                agreeing
            )
            *
            18,
        )

    good.sort(
        key=lambda item: item.score,
        reverse=True,
    )

    result = []

    for candidate in good:

        duplicate = any(

            existing.source
            ==
            candidate.source

            and

            distance(
                existing,
                candidate,
            )
            <
            7

            for existing in result
        )

        if not duplicate:

            result.append(
                candidate
            )

    return result


def best_non_google_consensus(
    ranked: list[Candidate],
) -> Candidate | None:

    candidates = [

        candidate

        for candidate in ranked

        if candidate.source not in {
            "google",
            "google_places",
            "learned",
        }
    ]

    best = None
    best_count = 0

    for candidate in candidates:

        sources = {

            other.source

            for other in candidates

            if distance(
                candidate,
                other,
            ) <= CONSENSUS_METERS
        }

        if (
            len(sources) >= 2
            and
            len(sources) > best_count
        ):

            best = candidate

            best_count = len(
                sources
            )

    return best


def deterministic_choice(
    parsed: ParsedAddress,
    ranked: list[Candidate],
) -> Candidate | None:

    if not ranked:
        return None

    if ranked[0].source == "learned":
        return ranked[0]

    google = next(
        (
            candidate

            for candidate in ranked

            if candidate.source == "google"
        ),
        None,
    )

    alternative = best_non_google_consensus(
        ranked
    )

    # ========================================================
    # GOOGLE НАШЛЁЛ
    # ========================================================

    if google:

        google_confirmed = any(

            candidate.source not in {
                "google",
                "google_places",
            }

            and

            distance(
                google,
                candidate,
            )
            <=
            GOOGLE_CONFIRM_METERS

            for candidate in ranked
        )

        # Два других источника совпали,
        # но Google находится далеко.
        #
        # Автоматически никого не выбираем.
        # Отправляем ситуацию ИИ.
        if (
            alternative
            and
            distance(
                google,
                alternative,
            )
            >=
            GOOGLE_CONFLICT_METERS
        ):
            return None

        # Google дал настоящий rooftop.
        if google.precision == "rooftop":
            return google

        # Google не rooftop,
        # но его подтверждает другая карта.
        if google_confirmed:
            return google

    # ========================================================
    # GOOGLE PLACES
    # ========================================================

    google_places = next(
        (
            candidate

            for candidate in ranked

            if candidate.source
            ==
            "google_places"
        ),
        None,
    )

    if google_places:

        confirmed = any(

            candidate.source not in {
                "google",
                "google_places",
            }

            and

            distance(
                google_places,
                candidate,
            )
            <=
            GOOGLE_CONFIRM_METERS

            for candidate in ranked
        )

        if confirmed:
            return google_places

    # ========================================================
    # ДВА НЕЗАВИСИМЫХ ИСТОЧНИКА
    # ========================================================

    if alternative:
        return alternative

    # Если Google вообще ничего не дал —
    # разрешаем сильный одиночный источник.
    if not google:

        best = ranked[0]

        if (
            best.source == "mapbox"
            and
            best.precision in {
                "rooftop",
                "entrance",
                "building",
                "parcel",
                "point",
            }
        ):
            return best

        if (
            best.source == "overpass"
            and
            best.precision == "building"
        ):
            return best

        if (
            best.source == "visicom"
            and
            best.precision in {
                "building",
                "address",
                "point",
            }
        ):
            return best

    return None


# ============================================================
# HTTP
# ============================================================

async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:

    async with session.get(
        url,
        params=params,
        headers=headers,
    ) as response:

        text = await response.text()

        if response.status != 200:

            raise RuntimeError(
                f"HTTP {response.status}: "
                f"{text[:300]}"
            )

        return await response.json(
            content_type=None
        )


async def post_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Any:

    async with session.post(
        url,
        json=payload,
        headers=headers,
    ) as response:

        text = await response.text()

        if response.status != 200:

            raise RuntimeError(
                f"HTTP {response.status}: "
                f"{text[:300]}"
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
            len(result),
            time.perf_counter()
            -
            started,
        )

        return result

    except Exception as error:

        log.warning(
            "%s failed in %.2fs: %s",
            name,
            time.perf_counter()
            -
            started,
            error,
        )

        return []


# ============================================================
# ОБЩИЕ ФУНКЦИИ API
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


def extract_matching_house(
    text: str,
    wanted: str,
) -> str:

    numbers = re.findall(

        r"(?<!\d)"
        r"("
        r"\d{1,4}"
        r"\s*"
        r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"
        r")"
        r"(?!\d)",

        text or "",
    )

    for number in numbers:

        if same_house(
            number,
            wanted,
        ):
            return wanted

    return ""


def city_text_ok(
    text: str,
) -> bool:

    value = normalize_text(
        text
    )

    return (

        "кривий ріг" in value
        or
        "кривой рог" in value
        or
        "kryvyi rih" in value
        or
        "krivoy rog" in value
    )


# ============================================================
# GOOGLE GEOCODING — ОСНОВНОЙ
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


def google_is_kryvyi_rih(
    item: dict[str, Any],
) -> bool:

    parts = [

        str(
            item.get(
                "formatted_address"
            )
            or
            ""
        )
    ]

    for component in (
        item.get(
            "address_components"
        )
        or
        []
    ):

        parts.append(
            str(
                component.get(
                    "long_name"
                )
                or
                ""
            )
        )

    return city_text_ok(
        " ".join(
            parts
        )
    )


async def geocode_google(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    streets: list[str] | None = None,
) -> list[Candidate]:

    if not GOOGLE_API_KEY:
        return []

    result = []

    variants = (
        streets
        or
        static_street_variants(
            parsed.street
        )
    )

    for street in variants[:6]:

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
                        f"{LAT_MIN},"
                        f"{LON_MIN}"
                        f"|"
                        f"{LAT_MAX},"
                        f"{LON_MAX}"
                    ),

                "language":
                    "uk",

                "region":
                    "ua",

                "key":
                    GOOGLE_API_KEY,
            },
        )

        status = str(
            data.get(
                "status"
            )
            or
            ""
        )

        if status not in {
            "OK",
            "ZERO_RESULTS",
        }:

            log.warning(
                "Google status=%s error=%s",
                status,
                data.get(
                    "error_message"
                ),
            )

        for item in (
            data.get(
                "results"
            )
            or
            []
        ):

            # Google сам сообщил,
            # что совпадение частичное.
            if item.get(
                "partial_match"
            ):
                continue

            if not google_is_kryvyi_rih(
                item
            ):
                continue

            house = google_component(
                item,
                "street_number",
            )

            route = google_component(
                item,
                "route",
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

                str(
                    geometry.get(
                        "location_type"
                    )
                    or
                    ""
                ).upper(),

                "address",
            )

            candidate = Candidate(

                source="google",

                lat=float(
                    location["lat"]
                ),

                lon=float(
                    location["lng"]
                ),

                street=(
                    route
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

                    0.80
                ),

                query_street=street,
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                result.append(
                    candidate
                )

        # Нашли rooftop —
        # нет смысла гонять остальные варианты Google.
        if any(
            candidate.precision == "rooftop"
            for candidate in result
        ):
            break

    return result


# ============================================================
# GOOGLE PLACES — ВТОРОЙ СПОСОБ GOOGLE
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


async def geocode_google_places(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    streets: list[str] | None = None,
) -> list[Candidate]:

    if not GOOGLE_API_KEY:
        return []

    result = []

    headers = {

        "X-Goog-Api-Key":
            GOOGLE_API_KEY,

        "X-Goog-FieldMask":
            (
                "places.formattedAddress,"
                "places.location,"
                "places.addressComponents,"
                "places.displayName,"
                "places.types"
            ),
    }

    variants = (
        streets
        or
        static_street_variants(
            parsed.street
        )
    )

    for street in variants[:4]:

        payload = {

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
                            LAT_MIN,
                        "longitude":
                            LON_MIN,
                    },

                    "high": {
                        "latitude":
                            LAT_MAX,
                        "longitude":
                            LON_MAX,
                    },
                }
            },
        }

        data = await post_json(

            session,

            (
                "https://places.googleapis.com/"
                "v1/places:searchText"
            ),

            payload=payload,

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

            if not city_text_ok(
                formatted
            ):
                continue

            house = places_component(
                place,
                "street_number",
            )

            route = places_component(
                place,
                "route",
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
                "latitude" not in location
                or
                "longitude" not in location
            ):
                continue

            candidate = Candidate(

                source="google_places",

                lat=float(
                    location["latitude"]
                ),

                lon=float(
                    location["longitude"]
                ),

                street=(
                    route
                    or
                    street
                ),

                house=house,

                label=formatted,

                precision="address",

                confidence=0.88,

                query_street=street,
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                result.append(
                    candidate
                )

        if result:
            break

    return result


# ============================================================
# VISICOM
# ============================================================

def extract_visicom_coords(
    feature: dict[str, Any],
) -> tuple[float, float] | None:

    for key in (
        "geo_centroid",
        "geometry",
    ):

        coordinates = (

            (
                feature.get(
                    key
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

            try:

                return (
                    float(
                        coordinates[1]
                    ),
                    float(
                        coordinates[0]
                    ),
                )

            except (
                TypeError,
                ValueError,
            ):
                pass

    return None


async def geocode_visicom(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    streets: list[str] | None = None,
) -> list[Candidate]:

    if not VISICOM_KEY:
        return []

    result = []

    variants = (
        streets
        or
        static_street_variants(
            parsed.street
        )
    )

    for street in variants[:6]:

        for language, city in (
            (
                "ru",
                CITY_RU,
            ),
            (
                "uk",
                CITY_UA,
            ),
        ):

            data = await get_json(

                session,

                (
                    "https://api.visicom.ua/"
                    "data-api/5.0/"
                    f"{language}/"
                    "geocode.json"
                ),

                params={

                    "text":
                        (
                            f"{city}, "
                            f"{street} "
                            f"{parsed.house}"
                        ),

                    "categories":
                        "adr_address",

                    "country":
                        "UA",

                    "limit":
                        10,

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

                categories = json.dumps(
                    properties.get(
                        "categories"
                    )
                    or
                    "",
                    ensure_ascii=False,
                )

                if "adr_address" not in categories:
                    continue

                coords = extract_visicom_coords(
                    feature
                )

                if not coords:
                    continue

                lat, lon = coords

                all_text = " ".join(
                    flatten_strings(
                        properties
                    )
                )

                house = extract_matching_house(
                    all_text,
                    parsed.house,
                )

                if not house:
                    continue

                raw_street = properties.get(
                    "street"
                )

                if isinstance(
                    raw_street,
                    dict,
                ):

                    values = flatten_strings(
                        raw_street
                    )

                    street_name = (
                        values[0]
                        if values
                        else street
                    )

                else:

                    street_name = str(
                        raw_street
                        or
                        street
                    )

                candidate = Candidate(

                    source="visicom",

                    lat=lat,
                    lon=lon,

                    street=street_name,

                    house=house,

                    label=str(
                        properties.get(
                            "name"
                        )
                        or
                        all_text[:260]
                    ),

                    precision="address",

                    confidence=0.97,

                    query_street=street,
                )

                if valid_candidate(
                    parsed,
                    candidate,
                ):

                    result.append(
                        candidate
                    )

            if result:
                return result

    return result


# ============================================================
# MAPBOX
# ============================================================

def mapbox_context_value(
    properties: dict[str, Any],
    section: str,
    key: str,
) -> str:

    context = (
        properties.get(
            "context"
        )
        or
        {}
    )

    obj = (
        context.get(
            section
        )
        or
        {}
    )

    if isinstance(
        obj,
        dict,
    ):

        return str(
            obj.get(
                key
            )
            or
            ""
        )

    return ""


def mapbox_best_coords(
    feature: dict[str, Any],
) -> tuple[
    float,
    float,
    str,
] | None:

    properties = (
        feature.get(
            "properties"
        )
        or
        {}
    )

    coordinate_info = (
        properties.get(
            "coordinates"
        )
        or
        {}
    )

    routable = (
        coordinate_info.get(
            "routable_points"
        )
        or
        []
    )

    # Если Mapbox знает вход в здание —
    # используем его.
    for point in routable:

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

                return (
                    float(
                        point["latitude"]
                    ),
                    float(
                        point["longitude"]
                    ),
                    "entrance",
                )

            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                pass

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

        try:

            return (
                float(
                    coordinates[1]
                ),
                float(
                    coordinates[0]
                ),
                "",
            )

        except (
            TypeError,
            ValueError,
        ):
            pass

    return None


async def geocode_mapbox(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    streets: list[str] | None = None,
) -> list[Candidate]:

    if not MAPBOX_TOKEN:
        return []

    result = []

    variants = (
        streets
        or
        static_street_variants(
            parsed.street
        )
    )

    for street in variants[:6]:

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

            coords = mapbox_best_coords(
                feature
            )

            if not coords:
                continue

            lat, lon, entrance_precision = coords

            match_code = (
                properties.get(
                    "match_code"
                )
                or
                {}
            )

            house = (

                mapbox_context_value(
                    properties,
                    "address",
                    "address_number",
                )

                or

                str(
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

            place_name = mapbox_context_value(
                properties,
                "place",
                "name",
            )

            if (
                place_name
                and
                not city_text_ok(
                    place_name
                )
            ):
                continue

            street_name = (

                mapbox_context_value(
                    properties,
                    "street",
                    "name",
                )

                or

                str(
                    properties.get(
                        "street"
                    )
                    or
                    street
                )
            )

            accuracy = str(

                (
                    properties.get(
                        "coordinates"
                    )
                    or
                    {}
                )
                .get(
                    "accuracy"
                )

                or

                ""
            ).lower()

            feature_type = str(
                properties.get(
                    "feature_type"
                )
                or
                ""
            ).lower()

            precision = (

                entrance_precision

                or

                {

                    "rooftop":
                        "rooftop",

                    "parcel":
                        "parcel",

                    "point":
                        "point",

                    "interpolated":
                        "interpolated",

                    "approximate":
                        "approximate",

                }.get(

                    accuracy,

                    (
                        "address"

                        if feature_type
                        ==
                        "address"

                        else

                        "unknown"
                    ),
                )
            )

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

                0.82,
            )

            candidate = Candidate(

                source="mapbox",

                lat=lat,
                lon=lon,

                street=street_name,

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

            if valid_candidate(
                parsed,
                candidate,
            ):

                result.append(
                    candidate
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
    streets: list[str] | None = None,
) -> list[Candidate]:

    result = []

    headers = {

        "User-Agent":
            "Metka-Kryvyi-Rih-Telegram-Bot/8.0",

        "Accept-Language":
            "uk,ru;q=0.9",
    }

    variants = (
        streets
        or
        static_street_variants(
            parsed.street
        )
    )

    for street in variants[:2]:

        data = await get_json(

            session,

            (
                "https://nominatim."
                "openstreetmap.org/"
                "search"
            ),

            params={

                "street":
                    f"{parsed.house} {street}",

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
                        f"{LON_MIN},"
                        f"{LAT_MAX},"
                        f"{LON_MAX},"
                        f"{LAT_MIN}"
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

            candidate = Candidate(

                source="osm",

                lat=float(
                    item["lat"]
                ),

                lon=float(
                    item["lon"]
                ),

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

                    0.70
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

            if valid_candidate(
                parsed,
                candidate,
            ):

                result.append(
                    candidate
                )

        if result:
            break

        # Для публичного Nominatim.
        await asyncio.sleep(
            1.05
        )

    return result


# ============================================================
# OVERPASS — ПОИСК САМОГО ЗДАНИЯ
# ============================================================

async def geocode_overpass(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    streets: list[str] | None = None,
) -> list[Candidate]:

    house = re.escape(
        normalize_house(
            parsed.house
        )
    )

    bbox = (
        f"{LAT_MIN},"
        f"{LON_MIN},"
        f"{LAT_MAX},"
        f"{LON_MAX}"
    )

    query = f"""
[out:json][timeout:8];

(
  node["addr:housenumber"~"^{house}$",i]({bbox});
  way["addr:housenumber"~"^{house}$",i]({bbox});
  relation["addr:housenumber"~"^{house}$",i]({bbox});
);

out center tags 150;
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
                "Metka-Kryvyi-Rih-Telegram-Bot/8.0"
        },

    ) as response:

        text = await response.text()

        if response.status != 200:

            raise RuntimeError(
                f"Overpass HTTP "
                f"{response.status}: "
                f"{text[:300]}"
            )

        data = await response.json(
            content_type=None
        )

    variants = (
        streets
        or
        static_street_variants(
            parsed.street
        )
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

        returned_house = str(
            tags.get(
                "addr:housenumber"
            )
            or
            ""
        )

        if not same_house(
            returned_house,
            parsed.house,
        ):
            continue

        returned_street = str(

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

        best_similarity = max(

            street_similarity(
                variant,
                returned_street,
            )

            for variant in variants
        )

        if best_similarity < 0.52:
            continue

        query_street = max(

            variants,

            key=lambda variant:
                street_similarity(
                    variant,
                    returned_street,
                ),
        )

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

        candidate = Candidate(

            source="overpass",

            lat=lat,
            lon=lon,

            street=returned_street,

            house=returned_house,

            label=(
                f"{returned_street}, "
                f"{returned_house}"
            ),

            precision=(
                "building"
                if is_building
                else
                "address"
            ),

            confidence=(
                0.96
                if is_building
                else
                0.87
            ),

            query_street=query_street,
        )

        if valid_candidate(
            parsed,
            candidate,
        ):

            result.append(
                candidate
            )

    return result


# ============================================================
# AI
# ============================================================

def parse_json_object(
    text: str,
) -> dict[str, Any] | None:

    text = (
        text
        or
        ""
    ).strip()

    try:

        result = json.loads(
            text
        )

        if isinstance(
            result,
            dict,
        ):
            return result

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

        result = json.loads(
            match.group(0)
        )

        if isinstance(
            result,
            dict,
        ):
            return result

    except Exception:
        pass

    return None


async def ai_choose(
    parsed: ParsedAddress,
    ranked: list[Candidate],
) -> Candidate | None:

    if (
        not ai_client
        or
        not ranked
    ):
        return None

    shortlist = ranked[
        :MAX_AI_CANDIDATES
    ]

    payload = []

    for index, candidate in enumerate(
        shortlist
    ):

        payload.append({

            "index":
                index,

            "provider":
                candidate.source,

            "lat":
                round(
                    candidate.lat,
                    7,
                ),

            "lon":
                round(
                    candidate.lon,
                    7,
                ),

            "street":
                candidate.street,

            "house":
                candidate.house,

            "label":
                candidate.label,

            "precision":
                candidate.precision,

            "score":
                round(
                    candidate.score,
                    2,
                ),
        })

    prompt = f"""
Ты проверяешь адрес ТОЛЬКО в Кривом Роге, Украина.

Запрос:
улица={parsed.street!r}
дом={parsed.house!r}

Google является основным источником,
но иногда Google может ошибаться.

Ты можешь выбрать ТОЛЬКО один реальный
кандидат из списка ниже.

Никогда не придумывай и не изменяй координаты.

Правила:

1. Номер дома обязан совпадать.

2. Учитывай:
- русский / украинский язык;
- старые / новые названия;
- небольшие опечатки.

3. Google ROOFTOP —
очень сильный сигнал.

4. Но если минимум ДВА независимых других
источника показывают одну точку примерно
в пределах 55 метров, а Google находится
далеко от них, это серьёзный аргумент
против Google.

5. rooftop / building / entrance / parcel /
point лучше interpolated / approximate.

6. Если уверенности нет:
found=false.

Кандидаты:

{json.dumps(payload, ensure_ascii=False)}

Ответ ТОЛЬКО JSON:

{{
    "found": true,
    "index": 0,
    "confidence": 0.95,
    "reason": "коротко"
}}

или

{{
    "found": false,
    "index": -1,
    "confidence": 0.0,
    "reason": "коротко"
}}
""".strip()

    try:

        response = await asyncio.wait_for(

            ai_client.responses.create(

                model=OPENAI_MODEL,

                input=prompt,
            ),

            timeout=AI_TIMEOUT,
        )

        result = parse_json_object(
            response.output_text
        )

        if not result:
            return None

        if not result.get(
            "found"
        ):
            return None

        index = int(
            result.get(
                "index",
                -1,
            )
        )

        confidence = float(
            result.get(
                "confidence",
                0.0,
            )
        )

        if not (
            0
            <=
            index
            <
            len(shortlist)
        ):
            return None

        if confidence < 0.62:
            return None

        chosen = shortlist[
            index
        ]

        if not valid_candidate(
            parsed,
            chosen,
        ):
            return None

        return chosen

    except Exception as error:

        log.warning(
            "AI chooser failed: %s",
            error,
        )

        return None


# ============================================================
# AI ИЩЕТ ВАРИАНТЫ НАЗВАНИЯ УЛИЦЫ
# ============================================================

async def ai_street_variants(
    street: str,
) -> list[str]:

    if not ai_client:
        return []

    prompt = f"""
Для поиска адреса в Кривом Роге, Украина,
пользователь написал улицу:

{street!r}

Дай до 6 наиболее вероятных вариантов
названия ЭТОЙ ЖЕ улицы:

- исправление опечатки;
- русский вариант;
- украинский вариант;
- возможное старое название;
- возможное новое название.

Не придумывай координаты.

Если не уверен в переименовании,
не придумывай его.

Ответ ТОЛЬКО JSON:

{{
    "variants": [
        "вариант 1",
        "вариант 2"
    ]
}}
""".strip()

    try:

        response = await asyncio.wait_for(

            ai_client.responses.create(
                model=OPENAI_MODEL,
                input=prompt,
            ),

            timeout=AI_TIMEOUT,
        )

        result = (
            parse_json_object(
                response.output_text
            )
            or
            {}
        )

        values = (
            result.get(
                "variants"
            )
            or
            []
        )

        output = []

        seen = set()

        for value in values:

            if not isinstance(
                value,
                str,
            ):
                continue

            value = value.strip()

            key = street_core(
                value
            )

            if (
                len(key) >= 2
                and
                key not in seen
            ):

                seen.add(
                    key
                )

                output.append(
                    value
                )

        return output[:6]

    except Exception as error:

        log.warning(
            "AI street variants failed: %s",
            error,
        )

        return []


# ============================================================
# ОСНОВНОЙ БЫСТРЫЙ ПОИСК
# ============================================================

async def search_primary(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    streets: list[str] | None = None,
) -> list[Candidate]:

    # Google + Visicom + Mapbox работают одновременно.
    results = await asyncio.gather(

        safe_provider(
            "google",
            geocode_google(
                session,
                parsed,
                streets,
            ),
        ),

        safe_provider(
            "visicom",
            geocode_visicom(
                session,
                parsed,
                streets,
            ),
        ),

        safe_provider(
            "mapbox",
            geocode_mapbox(
                session,
                parsed,
                streets,
            ),
        ),
    )

    return [

        candidate

        for group in results

        for candidate in group
    ]


# ============================================================
# ПОЛНЫЙ ПОИСК
# ============================================================

async def resolve_address(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> tuple[
    Candidate | None,
    list[Candidate],
]:

    # 1. Ручное обучение.
    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    # ========================================================
    # 2. GOOGLE + VISICOM + MAPBOX
    # ========================================================

    candidates = await search_primary(
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
    )

    if chosen:

        return (
            chosen,
            ranked,
        )

    # Если есть спорные реальные варианты —
    # подключаем ИИ.
    if ranked and ai_client:

        ai_best = await ai_choose(
            parsed,
            ranked,
        )

        if ai_best:

            return (
                ai_best,
                ranked,
            )

    # ========================================================
    # 3. ГЛУБОКИЙ ПОИСК
    # ========================================================

    deep_results = await asyncio.gather(

        # Второй способ поиска Google.
        safe_provider(
            "google_places",
            geocode_google_places(
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
            "overpass",
            geocode_overpass(
                session,
                parsed,
            ),
        ),
    )

    for group in deep_results:

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
    )

    if chosen:

        return (
            chosen,
            ranked,
        )

    if ranked and ai_client:

        ai_best = await ai_choose(
            parsed,
            ranked,
        )

        if ai_best:

            return (
                ai_best,
                ranked,
            )

    # ========================================================
    # 4. НЕ НАШЛИ УЛИЦУ —
    #    ИИ ДАЁТ ВАРИАНТЫ НАЗВАНИЯ
    # ========================================================

    ai_variants = await ai_street_variants(
        parsed.street
    )

    if ai_variants:

        merged = []

        seen = set()

        all_variants = (

            static_street_variants(
                parsed.street
            )

            +

            ai_variants
        )

        for value in all_variants:

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

                merged.append(
                    value
                )

        # Опять реальные карты,
        # но уже по исправленным названиям.
        extra = await search_primary(

            session,

            parsed,

            merged[:8],
        )

        candidates.extend(
            extra
        )

        # И Google Places тоже.
        candidates.extend(

            await safe_provider(

                "google_places_ai",

                geocode_google_places(

                    session,

                    parsed,

                    merged[:6],
                ),
            )
        )

        ranked = rank_candidates(
            parsed,
            candidates,
        )

        chosen = deterministic_choice(
            parsed,
            ranked,
        )

        if chosen:

            if (
                chosen.query_street
                and
                street_core(
                    chosen.query_street
                )
                !=
                street_core(
                    parsed.street
                )
            ):

                save_alias(
                    parsed.street,
                    chosen.query_street,
                )

            return (
                chosen,
                ranked,
            )

        if ranked and ai_client:

            ai_best = await ai_choose(
                parsed,
                ranked,
            )

            if ai_best:

                if (
                    ai_best.query_street
                    and
                    street_core(
                        ai_best.query_street
                    )
                    !=
                    street_core(
                        parsed.street
                    )
                ):

                    save_alias(
                        parsed.street,
                        ai_best.query_street,
                    )

                return (
                    ai_best,
                    ranked,
                )

    return (
        None,
        ranked,
    )


# ============================================================
# КЭШ
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

    stale_tokens = [

        token

        for token, item in pending_results.items()

        if item.created_at < cutoff
    ]

    for token in stale_tokens:

        pending_results.pop(
            token,
            None,
        )

    stale_corrections = [

        key

        for key, item in awaiting_correction.items()

        if item.created_at < cutoff
    ]

    for key in stale_corrections:

        awaiting_correction.pop(
            key,
            None,
        )


async def resolve_cached(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> tuple[
    Candidate | None,
    list[Candidate],
]:

    key = address_key(
        parsed
    )

    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    now = time.time()

    cached = memory_cache.get(
        key
    )

    if (
        cached
        and
        now - cached[0]
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
            best.source != "mapbox"
        ):

            memory_cache[key] = (
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

    inflight[key] = task

    try:

        return await task

    finally:

        inflight.pop(
            key,
            None,
        )


# ============================================================
# GOOGLE MAPS URL
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

    value = quote(
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
        f"{value}"
    )


# ============================================================
# ПОЛУЧЕНИЕ КООРДИНАТ ИЗ GOOGLE MAPS ССЫЛКИ
# ============================================================

def coords_from_text(
    text: str,
) -> tuple[
    float,
    float,
] | None:

    decoded = unquote(
        text
        or
        ""
    )

    patterns = [

        # @47.123,33.123
        r"@(-?\d{1,2}\.\d+),"
        r"(-?\d{1,3}\.\d+)",

        # !3d47.123!4d33.123
        r"!3d(-?\d{1,2}\.\d+)"
        r"!4d(-?\d{1,3}\.\d+)",

        # ?q=47.123,33.123
        r"[?&]"
        r"(?:q|query|ll)="
        r"(-?\d{1,2}\.\d+)"
        r"(?:,|%2C|\s)+"
        r"(-?\d{1,3}\.\d+)",

        # просто координаты
        r"(?<!\d)"
        r"(-?\d{1,2}\.\d+)"
        r"\s*[,; ]\s*"
        r"(-?\d{1,3}\.\d+)"
        r"(?!\d)",
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

        if in_city(
            lat,
            lon,
        ):

            return (
                lat,
                lon,
            )

    return None


async def extract_google_maps_coords(
    session: aiohttp.ClientSession,
    text: str,
) -> tuple[
    float,
    float,
] | None:

    # Сначала пробуем сам текст.
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

    lower = url.lower()

    if not any(
        domain in lower
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

            final_url = str(
                response.url
            )

            coords = coords_from_text(
                final_url
            )

            if coords:
                return coords

            html = await response.text()

            return coords_from_text(
                html[:500000]
            )

    except Exception as error:

        log.warning(
            "Google Maps URL error: %s",
            error,
        )

        return None


# ============================================================
# TELEGRAM UI
# ============================================================

def source_title(
    source: str,
) -> str:

    return {

        "learned":
            "сохранённая точка",

        "google":
            "Google",

        "google_places":
            "Google",

        "visicom":
            "Visicom",

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
                "🔎 Найти в Google Maps ↗",
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
# START
# ============================================================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not update.message:
        return

    await update.message.reply_text(
        "Отправь улицу и номер дома.\n"
        "Например: Лермонтова 25\n\n"
        "25/11, 25.11 и 25-11 "
        "будут искаться как дом 25."
    )


# ============================================================
# STATUS
# ============================================================

async def status_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not update.message:
        return

    with db() as conn:

        learned = conn.execute(
            """
            SELECT COUNT(*) n
            FROM confirmed_addresses
            """
        ).fetchone()["n"]

        stats = conn.execute(
            """
            SELECT provider,good,bad
            FROM provider_stats
            ORDER BY provider
            """
        ).fetchall()

    lines = [

        f"Google: "
        f"{'✅' if GOOGLE_API_KEY else '—'}",

        f"Visicom: "
        f"{'✅' if VISICOM_KEY else '—'}",

        f"Mapbox: "
        f"{'✅' if MAPBOX_TOKEN else '—'}",

        "OpenStreetMap: ✅",

        f"ИИ: "
        f"{'✅ ' + OPENAI_MODEL if ai_client else '—'}",

        f"Сохранённых точек: "
        f"{learned}",
    ]

    if stats:

        lines.append(
            ""
        )

        lines.append(
            "Обучение источников:"
        )

        for row in stats:

            lines.append(
                f"{row['provider']}: "
                f"✅{row['good']} / "
                f"❌{row['bad']}"
            )

    await update.message.reply_text(
        "\n".join(
            lines
        )
    )


# ============================================================
# СОХРАНЕНИЕ РУЧНОЙ КОРРЕКТИРОВКИ
# ============================================================

async def save_manual_correction(
    update: Update,
    pending: PendingResult,
    lat: float,
    lon: float,
) -> None:

    # Учим рейтинг карт.
    for candidate in pending.candidates:

        distance_to_correct = distance_coords(

            lat,
            lon,

            candidate.lat,
            candidate.lon,
        )

        # Карта была почти точно права.
        if distance_to_correct <= 55:

            update_provider_stat(
                candidate.source,
                True,
            )

        # Карта явно ошиблась.
        elif distance_to_correct >= 140:

            update_provider_stat(
                candidate.source,
                False,
            )

    # История исправлений.
    with db() as conn:

        conn.execute(
            """
            INSERT INTO corrections_log(

                query_key,

                old_lat,
                old_lon,

                new_lat,
                new_lon,

                old_source,

                created_at
            )

            VALUES(
                ?,?,?,?,?,?,?
            )
            """,

            (
                address_key(
                    pending.parsed
                ),

                (
                    pending.best.lat
                    if pending.best
                    else None
                ),

                (
                    pending.best.lon
                    if pending.best
                    else None
                ),

                lat,
                lon,

                (
                    pending.best.source
                    if pending.best
                    else None
                ),

                int(
                    time.time()
                ),
            ),
        )

    # Сохраняем как абсолютную истину
    # для этого адреса.
    save_learned(

        pending.parsed,

        lat,
        lon,

        (
            f"{pending.parsed.street} "
            f"{pending.parsed.house}, "
            f"{CITY_RU}"
        ),

        "user_correction",
    )

    # Старый RAM-кэш удаляем.
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
                f"📍 {pending.parsed.original}\n\n"
                "В следующий раз этот адрес "
                "откроется сразу по сохранённым "
                "координатам."
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
# ОБЫЧНЫЕ СООБЩЕНИЯ
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if (
        not update.message
        or
        not update.message.text
    ):
        return

    user = update.effective_user

    if not user:
        return

    cleanup_pending()

    session: aiohttp.ClientSession = (
        context.application.bot_data[
            "http"
        ]
    )

    text = update.message.text.strip()

    correction_key = (
        update.message.chat.id,
        user.id,
    )

    pending_correction = (
        awaiting_correction.get(
            correction_key
        )
    )

    # ========================================================
    # ПОЛЬЗОВАТЕЛЬ СЕЙЧАС УТОЧНЯЕТ ТОЧКУ
    # ========================================================

    if pending_correction:

        coords = await extract_google_maps_coords(
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

                pending_correction,

                coords[0],
                coords[1],
            )

            return

        # Если прислал ссылку,
        # но координаты не достали.
        if (
            "http" in text.lower()
            or
            re.search(
                r"\d+\.\d+\s*[,; ]\s*\d+\.\d+",
                text,
            )
        ):

            await update.message.reply_text(
                "❌ Не смог получить координаты.\n\n"
                "В Google Maps зажми ТОЧНЫЙ дом → "
                "«Поделиться» → "
                "«Копировать ссылку» → "
                "отправь ссылку сюда."
            )

            return

    # ========================================================
    # ОБЫЧНЫЙ АДРЕС
    # ========================================================

    parsed = parse_address(
        text
    )

    # Обычный разговор в группе игнорируется.
    if not parsed:
        return

    best, ranked = await resolve_cached(
        session,
        parsed,
    )

    token = uuid.uuid4().hex[
        :12
    ]

    pending = PendingResult(

        owner_id=user.id,

        chat_id=update.message.chat.id,

        parsed=parsed,

        best=best,

        candidates=ranked,

        created_at=time.time(),
    )

    pending_results[
        token
    ] = pending

    # ========================================================
    # АВТОМАТИЧЕСКИ НЕ НАШЁЛ
    # ========================================================

    if not best:

        # Не пишем тупо "улица не найдена".
        # Сразу даём Google Maps и обучение.
        await update.message.reply_text(

            (
                "🔎 Автоматически не подтвердил "
                "точный дом:\n"
                f"{parsed.original}\n\n"
                "Открой Google Maps и поставь "
                "точную точку — бот её запомнит."
            ),

            reply_markup=not_found_keyboard(
                token,
                parsed,
            ),

            disable_web_page_preview=True,
        )

        return

    # ========================================================
    # НАШЁЛ
    # ========================================================

    await update.message.reply_text(

        (
            f"📍  Улица: "
            f"{parsed.original}\n"

            f"🏙  Кривой Рог\n\n"

            f"Источник: "
            f"{source_title(best.source)}\n"

            f"Нажми кнопку ниже 👇"
        ),

        reply_markup=result_keyboard(
            token,
            best,
        ),

        disable_web_page_preview=True,
    )


# ============================================================
# КНОПКИ
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query:
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
            "Эта метка уже устарела.",
            show_alert=True,
        )

        return

    user = update.effective_user

    if (
        not user
        or
        user.id != pending.owner_id
    ):

        await query.answer(
            "Изменить метку может автор запроса.",
            show_alert=True,
        )

        return

    # ========================================================
    # МЕТКА ПРАВИЛЬНАЯ
    # ========================================================

    if action == "ok":

        if not pending.best:

            await query.answer(
                "Сначала укажи точную точку.",
                show_alert=True,
            )

            return

        await query.answer(
            "Сохранил"
        )

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

        if query.message:

            await query.edit_message_text(

                (
                    f"📍  Улица: "
                    f"{pending.parsed.original}\n"

                    f"🏙  Кривой Рог\n\n"

                    f"✅ Метка подтверждена "
                    f"и запомнена."
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

    # ========================================================
    # УТОЧНИТЬ КООРДИНАТЫ
    # ========================================================

    if action == "fix":

        await query.answer()

        if not query.message:
            return

        awaiting_correction[
            (
                pending.chat_id,
                user.id,
            )
        ] = pending

        # Если уже нашли приблизительную точку —
        # открываем её.
        if pending.best:

            url = maps_url(
                pending.best.lat,
                pending.best.lon,
            )

        else:

            # Если вообще не нашли —
            # открываем поиск адреса.
            url = maps_address_url(
                pending.parsed
            )

        await query.message.reply_text(

            (
                "🎯 Уточнение координат\n\n"

                "1. Открой Google Maps.\n"

                "2. Зажми пальцем ТОЧНЫЙ дом.\n"

                "3. Нажми «Поделиться» → "
                "«Копировать ссылку».\n"

                "4. Отправь эту ссылку сюда.\n\n"

                "После этого бот навсегда "
                "запомнит эту точку для адреса."
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


# ============================================================
# TELEGRAM LOCATION
# ============================================================

async def handle_location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    # Оставляем ещё один способ:
    # можно прислать Telegram-геолокацию.
    if (
        not update.message
        or
        not update.message.location
    ):
        return

    user = update.effective_user

    if not user:
        return

    key = (
        update.message.chat.id,
        user.id,
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

    if not in_city(
        lat,
        lon,
    ):

        await update.message.reply_text(
            "❌ Эта точка находится "
            "вне Кривого Рога."
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
# ERROR
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    log.exception(
        "Telegram handler error",
        exc_info=context.error,
    )


# ============================================================
# ОДИН HTTP SESSION НА ВЕСЬ БОТ
# ============================================================

async def post_init(
    application: Application,
) -> None:

    application.bot_data[
        "http"
    ] = aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT
    )

    log.info(
        "HTTP session initialized"
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
# CONFIG CHECK
# ============================================================

def validate_config() -> None:

    if not BOT_TOKEN:

        raise RuntimeError(
            "Не задан BOT_TOKEN"
        )

    if not GOOGLE_API_KEY:

        log.warning(
            "GOOGLE_API_KEY отсутствует. "
            "Главный поиск Google отключён."
        )

    if not any((
        GOOGLE_API_KEY,
        VISICOM_KEY,
        MAPBOX_TOKEN,
    )):

        log.warning(
            "Google / Visicom / Mapbox "
            "не настроены."
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    validate_config()

    init_db()

    app = (

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

    app.add_handler(
        CommandHandler(
            "start",
            start_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "status",
            status_cmd,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.LOCATION,
            handle_location,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            &
            ~filters.COMMAND,
            handle_text,
        )
    )

    app.add_error_handler(
        error_handler
    )

    log.info(
        "Bot v8 starting | "
        "Google=%s "
        "Visicom=%s "
        "Mapbox=%s "
        "OSM=yes "
        "AI=%s "
        "model=%s "
        "DB=%s",

        bool(
            GOOGLE_API_KEY
        ),

        bool(
            VISICOM_KEY
        ),

        bool(
            MAPBOX_TOKEN
        ),

        bool(
            ai_client
        ),

        OPENAI_MODEL,

        DB_PATH,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
