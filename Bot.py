# -*- coding: utf-8 -*-
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

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


BOT_TOKEN = os.getenv("BOT_TOKEN", "8055606612:AAGuCO3QbJseCRnXU3O4rhlN4EP-Drk5De4").strip()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "AIzaSyDrf2qAL0FQJJ2_TrKWkz5IVedU-yok-uc").strip()
VISICOM_KEY = os.getenv("VISICOM_KEY", "e14865d659080719d865805b00e967e6").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.eyJ1IjoibXlha2lzaDEiLCJhIjoiY210bnFscjVtMGd0NzJ3cjM5Y3Z6anJrciJ9.nHWBCkwZk2fLsHp1cFsjpg05:04").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()

UKLON_ENABLED = (
    os.getenv("UKLON_ENABLED", "1").strip().lower()
    not in {"0", "false", "no"}
)

UKLON_URL = os.getenv(
    "UKLON_URL",
    "https://app.uklon.com.ua/"
).strip()

DEFAULT_DB = (
    "/app/data/metka_v10.sqlite3"
    if Path("/app/data").exists()
    else "metka_v10.sqlite3"
)

DB_PATH = os.getenv(
    "DB_PATH",
    DEFAULT_DB
).strip()

CITY_RU = "Кривой Рог"
CITY_UA = "Кривий Ріг"
COUNTRY_UA = "Україна"

LAT_MIN = 47.65
LAT_MAX = 48.20
LON_MIN = 32.75
LON_MAX = 33.80

HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=10,
    connect=3,
    sock_read=8,
)

CACHE_TTL = 24 * 3600
PENDING_TTL = 2 * 3600

CLUSTER_M = 60.0
STRONG_CONFLICT_M = 120.0

AI_TIMEOUT = 7.0

logging.basicConfig(
    level=getattr(
        logging,
        os.getenv(
            "LOG_LEVEL",
            "INFO"
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
    "metka-v10"
)

ai_client: Optional[AsyncOpenAI] = None

if (
    OPENAI_API_KEY
    and
    AsyncOpenAI is not None
):
    ai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


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

    best: Candidate | None
    candidates: list[Candidate]

    created_at: float


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

    words = [
        word.strip(
            ".-"
        )
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
) -> ParsedAddress | None:

    if not text:
        return None

    original = unicodedata.normalize(
        "NFKC",
        text,
    ).strip()

    if (
        len(
            original
        )
        >
        140
    ):
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
        r"(?:украина|україна)"
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

    if (
        len(
            street_core(
                street
            )
        )
        <
        2
    ):
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

    if (
        not first
        or
        not second
    ):
        return 0.0

    if first == second:
        return 1.0

    if (
        first in second
        or
        second in first
    ):
        return 0.97

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

        connection.execute(
            "PRAGMA journal_mode=WAL"
        )

        connection.execute(
            "PRAGMA synchronous=NORMAL"
        )

        connection.executescript(
            """
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

            CREATE TABLE IF NOT EXISTS street_aliases(
                alias TEXT PRIMARY KEY,

                canonical TEXT NOT NULL,

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
) -> Candidate | None:

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
        alias
        ==
        canonical
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


def street_variants(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    output = [
        street
    ]

    for canonical, aliases in SEED_ALIASES.items():

        family = {
            street_core(
                canonical
            ),

            *(
                street_core(
                    value
                )
                for value in aliases
            ),
        }

        if base in family:

            output.append(
                canonical
            )

            output.extend(
                aliases
            )

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

        output.extend([
            str(
                row["alias"]
            ),

            str(
                row["canonical"]
            ),
        ])

    result = []

    seen = set()

    for value in output:

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

                now,
            ),
        )


def provider_multiplier(
    provider: str,
) -> float:

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
        good
        +
        bad
        +
        10
    )

    return (
        0.90
        +
        0.20
        *
        ratio
    )


def in_city(
    lat: float,
    lon: float,
) -> bool:

    return (
        LAT_MIN
        <=
        lat
        <=
        LAT_MAX
        and
        LON_MIN
        <=
        lon
        <=
        LON_MAX
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

    return distance_coords(

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

    if not in_city(
        candidate.lat,
        candidate.lon,
    ):
        return False

    if candidate.source == "learned":
        return True

    if not same_house(
        parsed.house,
        candidate.house,
    ):
        return False

    similarity = max(

        street_similarity(
            parsed.street,
            candidate.street
            or
            candidate.label,
        ),

        street_similarity(
            candidate.query_street,
            candidate.street
            or
            candidate.label,
        ),
    )

    if similarity < 0.50:
        return False

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

    provider_score = {

        "uklon":
            155,

        "google_places":
            145,

        "google":
            138,

        "visicom":
            120,

        "mapbox":
            115,

        "overpass":
            105,

        "osm":
            100,

    }.get(
        candidate.source,
        80,
    )

    precision_score = {

        "rooftop":
            45,

        "building":
            40,

        "entrance":
            40,

        "parcel":
            33,

        "point":
            31,

        "address":
            27,

        "interpolated":
            6,

        "approximate":
            2,

    }.get(
        candidate.precision,
        0,
    )

    similarity = max(

        street_similarity(
            parsed.street,
            candidate.street,
        ),

        street_similarity(
            parsed.street,
            candidate.label,
        ),
    )

    score = (

        provider_score

        +

        precision_score

        +

        35
        *
        similarity

        +

        10
        *
        candidate.confidence
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

        support = {

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
                CLUSTER_M
            )
        }

        candidate.score += min(
            50,
            18
            *
            len(
                support
            ),
        )

    good.sort(
        key=lambda item:
            item.score,
        reverse=True,
    )

    output = []

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
                candidate,
                existing,
            )
            <
            7

            for existing in output
        )

        if not duplicate:

            output.append(
                candidate
            )

    return output


def clusters(
    candidates: list[Candidate],
) -> list[list[Candidate]]:

    result = []

    for seed in candidates:

        cluster = [

            candidate

            for candidate in candidates

            if distance(
                seed,
                candidate,
            )
            <=
            CLUSTER_M
        ]

        families = {
            provider_family(
                candidate.source
            )
            for candidate in cluster
        }

        duplicate = False

        for existing in result:

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

            result.append(
                cluster
            )

    result.sort(

        key=lambda cluster: (

            len({
                provider_family(
                    candidate.source
                )
                for candidate in cluster
            }),

            max(
                candidate.score
                for candidate in cluster
            ),
        ),

        reverse=True,
    )

    return result


def best_from_cluster(
    cluster: list[Candidate],
) -> Candidate:

    return max(
        cluster,
        key=lambda candidate:
            candidate.score,
    )


def deterministic_choice(
    parsed: ParsedAddress,
    ranked: list[Candidate],
) -> Candidate | None:

    if not ranked:
        return None

    if ranked[0].source == "learned":
        return ranked[0]

    uklon = next(
        (
            candidate

            for candidate in ranked

            if candidate.source == "uklon"
        ),
        None,
    )

    all_clusters = clusters(
        ranked
    )

    if uklon:

        confirmed = any(

            provider_family(
                candidate.source
            )
            !=
            "uklon"

            and

            distance(
                uklon,
                candidate,
            )
            <=
            80

            for candidate in ranked
        )

        if confirmed:
            return uklon

        for cluster in all_clusters:

            families = {

                provider_family(
                    candidate.source
                )

                for candidate in cluster

                if provider_family(
                    candidate.source
                )
                !=
                "uklon"
            }

            if (
                len(
                    families
                )
                >=
                3
            ):

                far_from_uklon = min(

                    distance(
                        uklon,
                        candidate,
                    )

                    for candidate in cluster
                )

                if (
                    far_from_uklon
                    >=
                    STRONG_CONFLICT_M
                ):

                    return best_from_cluster(
                        cluster
                    )

        if (
            uklon.confidence
            >=
            0.90

            and

            uklon.precision in {
                "address",
                "point",
                "building",
                "entrance",
                "rooftop",
            }
        ):

            return uklon

    for cluster in all_clusters:

        families = {
            provider_family(
                candidate.source
            )
            for candidate in cluster
        }

        if (
            len(
                families
            )
            >=
            2
        ):

            return best_from_cluster(
                cluster
            )

    best = ranked[0]

    if (
        best.source
        ==
        "google_places"

        and

        best.confidence
        >=
        0.92
    ):
        return best

    if (
        best.source
        ==
        "google"

        and

        best.precision
        ==
        "rooftop"
    ):
        return best

    if (
        best.precision in {
            "rooftop",
            "building",
            "entrance",
        }

        and

        best.confidence
        >=
        0.95
    ):

        return best

    return None


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

        text = await response.text()

        if response.status != 200:

            raise RuntimeError(
                f"HTTP {response.status}: "
                f"{text[:250]}"
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

        text = await response.text()

        if response.status != 200:

            raise RuntimeError(
                f"HTTP {response.status}: "
                f"{text[:250]}"
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
            "%s: %d candidate(s) %.2fs",
            name,
            len(
                result
            ),
            time.perf_counter()
            -
            started,
        )

        return result

    except Exception as error:

        log.warning(
            "%s failed: %s",
            name,
            error,
        )

        return []


def flatten_strings(
    obj: Any,
) -> list[str]:

    output = []

    if isinstance(
        obj,
        str,
    ):

        output.append(
            obj
        )

    elif isinstance(
        obj,
        dict,
    ):

        for value in obj.values():

            output.extend(
                flatten_strings(
                    value
                )
            )

    elif isinstance(
        obj,
        list,
    ):

        for value in obj:

            output.extend(
                flatten_strings(
                    value
                )
            )

    return output


def matching_house_from_text(
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

    return (
        wanted

        if any(
            same_house(
                value,
                wanted,
            )
            for value in values
        )

        else

        ""
    )


def city_text_ok(
    text: str,
) -> bool:

    value = normalize_text(
        text
    )

    return any(
        city in value

        for city in (
            "кривий ріг",
            "кривой рог",
            "kryvyi rih",
            "krivoy rog",
        )
    )


class UklonBrowser:

    def __init__(
        self,
    ) -> None:

        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

        self.lock = asyncio.Lock()


    async def start(
        self,
    ) -> None:

        if (
            not UKLON_ENABLED
            or
            async_playwright is None
        ):
            return

        self.pw = await async_playwright().start()

        self.browser = await self.pw.chromium.launch(

            headless=True,

            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        self.context = await self.browser.new_context(

            locale="uk-UA",

            viewport={
                "width":
                    1280,

                "height":
                    900,
            },
        )

        self.page = await self.context.new_page()

        await self.page.goto(
            UKLON_URL,
            wait_until="domcontentloaded",
            timeout=15000,
        )

        log.info(
            "Uklon browser started"
        )


    async def stop(
        self,
    ) -> None:

        try:

            if self.context:
                await self.context.close()

            if self.browser:
                await self.browser.close()

            if self.pw:
                await self.pw.stop()

        except Exception:
            pass


    async def _input(
        self,
    ):

        if not self.page:
            return None

        selectors = [

            'input[placeholder*="Звідки" i]',

            'input[placeholder*="Куди" i]',

            'input[placeholder*="Адрес" i]',

            'input[placeholder*="адрес" i]',

            'input[placeholder*="Address" i]',
        ]

        for selector in selectors:

            locator = self.page.locator(
                selector
            )

            for index in range(
                await locator.count()
            ):

                item = locator.nth(
                    index
                )

                if (
                    await item.is_visible()
                    and
                    await item.is_enabled()
                ):

                    return item

        locator = self.page.locator(
            "input"
        )

        for index in range(
            await locator.count()
        ):

            item = locator.nth(
                index
            )

            try:

                if (
                    await item.is_visible()
                    and
                    await item.is_enabled()
                ):

                    return item

            except Exception:
                pass

        return None


    @staticmethod
    def _candidate_from_object(
        obj: Any,
        parsed: ParsedAddress,
        query_street: str,
    ) -> list[Candidate]:

        output = []

        def walk(
            node: Any,
        ):

            if isinstance(
                node,
                dict,
            ):

                text = " ".join(
                    flatten_strings(
                        node
                    )
                )[:5000]

                house = matching_house_from_text(
                    text,
                    parsed.house,
                )

                similarity = max(

                    street_similarity(
                        parsed.street,
                        text,
                    ),

                    street_similarity(
                        query_street,
                        text,
                    ),
                )

                pairs = []

                for lat_key, lon_key in (

                    (
                        "lat",
                        "lng",
                    ),

                    (
                        "lat",
                        "lon",
                    ),

                    (
                        "latitude",
                        "longitude",
                    ),

                    (
                        "Latitude",
                        "Longitude",
                    ),
                ):

                    if (
                        lat_key in node
                        and
                        lon_key in node
                    ):

                        pairs.append(
                            (
                                node.get(
                                    lat_key
                                ),
                                node.get(
                                    lon_key
                                ),
                            )
                        )

                coordinates = node.get(
                    "coordinates"
                )

                if (
                    isinstance(
                        coordinates,
                        list,
                    )
                    and
                    len(
                        coordinates
                    )
                    >=
                    2
                ):

                    try:

                        pairs.append(
                            (
                                float(
                                    coordinates[1]
                                ),

                                float(
                                    coordinates[0]
                                ),
                            )
                        )

                    except Exception:
                        pass

                for lat_value, lon_value in pairs:

                    try:

                        lat = float(
                            lat_value
                        )

                        lon = float(
                            lon_value
                        )

                    except Exception:
                        continue

                    if (
                        in_city(
                            lat,
                            lon,
                        )

                        and

                        house

                        and

                        similarity
                        >=
                        0.50
                    ):

                        output.append(
                            Candidate(

                                source="uklon",

                                lat=lat,
                                lon=lon,

                                street=query_street,

                                house=parsed.house,

                                label=text[:500],

                                precision="address",

                                confidence=0.96,

                                query_street=query_street,
                            )
                        )

                for value in node.values():

                    walk(
                        value
                    )

            elif isinstance(
                node,
                list,
            ):

                for value in node:

                    walk(
                        value
                    )

        walk(
            obj
        )

        return output


    async def _page_state(
        self,
    ) -> list[Any]:

        if not self.page:
            return []

        result = []

        try:

            storage = await self.page.evaluate(
                """
                () => {
                    const out = {
                        local: {},
                        session: {}
                    };

                    for (
                        let i = 0;
                        i < localStorage.length;
                        i++
                    ) {
                        const k =
                            localStorage.key(i);

                        out.local[k] =
                            localStorage.getItem(k);
                    }

                    for (
                        let i = 0;
                        i < sessionStorage.length;
                        i++
                    ) {
                        const k =
                            sessionStorage.key(i);

                        out.session[k] =
                            sessionStorage.getItem(k);
                    }

                    return out;
                }
                """
            )

            result.append(
                storage
            )

            for section in (
                storage.get(
                    "local",
                    {},
                ),

                storage.get(
                    "session",
                    {},
                ),
            ):

                for value in section.values():

                    if (
                        isinstance(
                            value,
                            str,
                        )

                        and

                        value[:1] in "[{"
                    ):

                        try:

                            result.append(
                                json.loads(
                                    value
                                )
                            )

                        except Exception:
                            pass

        except Exception:
            pass

        try:

            html = await self.page.locator(
                "html"
            ).evaluate(
                "el => el.outerHTML"
            )

            result.append({
                "html":
                    html[:1000000]
            })

        except Exception:
            pass

        return result


    async def search(
        self,
        parsed: ParsedAddress,
        variants: list[str] | None = None,
    ) -> list[Candidate]:

        if not self.page:
            return []

        async with self.lock:

            try:

                await self.page.goto(
                    UKLON_URL,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )

                await self.page.wait_for_timeout(
                    700
                )

            except Exception:
                pass

            streets = (
                variants
                or
                street_variants(
                    parsed.street
                )
            )

            for query_street in streets[:6]:

                input_box = await self._input()

                if input_box is None:
                    return []

                query = (
                    f"{query_street} "
                    f"{parsed.house}, "
                    f"{CITY_UA}"
                )

                try:

                    await input_box.click()

                    await input_box.fill(
                        ""
                    )

                    await input_box.fill(
                        query
                    )

                    await self.page.wait_for_timeout(
                        1500
                    )

                except Exception:
                    continue

                best_option = None
                best_score = -1.0

                selectors = (

                    '[role="option"]',

                    "li",

                    '[class*="suggest" i]',

                    '[class*="autocomplete" i]',
                )

                for selector in selectors:

                    locator = self.page.locator(
                        selector
                    )

                    try:

                        count = min(
                            await locator.count(),
                            80,
                        )

                    except Exception:
                        continue

                    for index in range(
                        count
                    ):

                        item = locator.nth(
                            index
                        )

                        try:

                            if not await item.is_visible():
                                continue

                            text = (
                                await item.inner_text()
                            ).strip()

                        except Exception:
                            continue

                        if not matching_house_from_text(
                            text,
                            parsed.house,
                        ):
                            continue

                        score = max(

                            street_similarity(
                                parsed.street,
                                text,
                            ),

                            street_similarity(
                                query_street,
                                text,
                            ),
                        )

                        if score > best_score:

                            best_option = item
                            best_score = score

                if (
                    best_option is None
                    or
                    best_score
                    <
                    0.50
                ):
                    continue

                try:

                    chosen_text = (
                        await best_option.inner_text()
                    ).strip()

                    await best_option.click(
                        timeout=3000
                    )

                    await self.page.wait_for_timeout(
                        900
                    )

                except Exception:
                    continue

                found = []

                for obj in await self._page_state():

                    found.extend(

                        self._candidate_from_object(

                            obj,

                            parsed,

                            query_street,
                        )
                    )

                direct = extract_coords_from_text(
                    self.page.url
                )

                if (
                    direct

                    and

                    matching_house_from_text(
                        chosen_text,
                        parsed.house,
                    )
                ):

                    found.append(
                        Candidate(

                            source="uklon",

                            lat=direct[0],
                            lon=direct[1],

                            street=query_street,

                            house=parsed.house,

                            label=chosen_text,

                            precision="address",

                            confidence=0.95,

                            query_street=query_street,
                        )
                    )

                result = []

                for candidate in found:

                    if (
                        valid_candidate(
                            parsed,
                            candidate,
                        )

                        and

                        not any(
                            distance(
                                candidate,
                                existing,
                            )
                            <
                            8

                            for existing in result
                        )
                    ):

                        result.append(
                            candidate
                        )

                if result:

                    result.sort(
                        key=lambda item:
                            item.confidence,
                        reverse=True,
                    )

                    return result[:5]

            return []


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
    variants=None,
) -> list[Candidate]:

    if not GOOGLE_API_KEY:
        return []

    headers = {

        "X-Goog-Api-Key":
            GOOGLE_API_KEY,

        "X-Goog-FieldMask":
            (
                "places.formattedAddress,"
                "places.location,"
                "places.addressComponents,"
                "places.displayName,"
                "places.types,"
                "places.id"
            ),
    }

    output = []

    streets = (
        variants
        or
        street_variants(
            parsed.street
        )
    )

    for street in streets[:5]:

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

            if (
                formatted
                and
                not city_text_ok(
                    formatted
                )
            ):
                continue

            house = (
                places_component(
                    place,
                    "street_number",
                )
                or
                matching_house_from_text(
                    formatted,
                    parsed.house,
                )
            )

            route = (
                places_component(
                    place,
                    "route",
                )
                or
                street
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
                    location[
                        "latitude"
                    ]
                ),

                lon=float(
                    location[
                        "longitude"
                    ]
                ),

                street=route,

                house=parsed.house,

                label=formatted,

                precision="address",

                confidence=0.94,

                query_street=street,
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                output.append(
                    candidate
                )

        if output:
            break

    return output


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
    variants=None,
) -> list[Candidate]:

    if not GOOGLE_API_KEY:
        return []

    output = []

    streets = (
        variants
        or
        street_variants(
            parsed.street
        )
    )

    for street in streets[:5]:

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

            route = (
                google_component(
                    item,
                    "route",
                )
                or
                street
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
                    location[
                        "lat"
                    ]
                ),

                lon=float(
                    location[
                        "lng"
                    ]
                ),

                street=route,

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
                    if precision == "rooftop"
                    else
                    0.82
                ),

                query_street=street,
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                output.append(
                    candidate
                )

        if any(
            candidate.precision
            ==
            "rooftop"

            for candidate in output
        ):

            break

    return output


async def geocode_visicom(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    variants=None,
) -> list[Candidate]:

    if not VISICOM_KEY:
        return []

    output = []

    streets = (
        variants
        or
        street_variants(
            parsed.street
        )
    )

    for street in streets[:6]:

        for language, city in (

            (
                "uk",
                CITY_UA,
            ),

            (
                "ru",
                CITY_RU,
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

                text = " ".join(
                    flatten_strings(
                        properties
                    )
                )

                house = matching_house_from_text(
                    text,
                    parsed.house,
                )

                if not house:
                    continue

                geometry = (
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

                coordinates = (
                    geometry.get(
                        "coordinates"
                    )
                    or
                    []
                )

                if (
                    len(
                        coordinates
                    )
                    <
                    2
                ):
                    continue

                raw_street = properties.get(
                    "street"
                )

                names = (
                    flatten_strings(
                        raw_street
                    )

                    if isinstance(
                        raw_street,
                        (
                            dict,
                            list,
                        ),
                    )

                    else

                    []
                )

                street_name = (
                    names[0]
                    if names
                    else
                    str(
                        raw_street
                        or
                        street
                    )
                )

                candidate = Candidate(

                    source="visicom",

                    lat=float(
                        coordinates[1]
                    ),

                    lon=float(
                        coordinates[0]
                    ),

                    street=street_name,

                    house=house,

                    label=str(
                        properties.get(
                            "name"
                        )
                        or
                        text[:500]
                    ),

                    precision="address",

                    confidence=0.97,

                    query_street=street,
                )

                if valid_candidate(
                    parsed,
                    candidate,
                ):

                    output.append(
                        candidate
                    )

            if output:
                return output

    return output


async def geocode_mapbox(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    variants=None,
) -> list[Candidate]:

    if not MAPBOX_TOKEN:
        return []

    output = []

    streets = (
        variants
        or
        street_variants(
            parsed.street
        )
    )

    for street in streets[:6]:

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

            match_code = (
                properties.get(
                    "match_code"
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

            address_context = (
                context.get(
                    "address"
                )
                or
                {}
            )

            street_context = (
                context.get(
                    "street"
                )
                or
                {}
            )

            house = str(

                address_context.get(
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

            lat = None
            lon = None

            for point in (
                coordinate_info.get(
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

                    lat = point.get(
                        "latitude"
                    )

                    lon = point.get(
                        "longitude"
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

                if (
                    len(
                        coordinates
                    )
                    >=
                    2
                ):

                    lon = coordinates[0]
                    lat = coordinates[1]

            if (
                lat is None
                or
                lon is None
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

            candidate = Candidate(

                source="mapbox",

                lat=float(
                    lat
                ),

                lon=float(
                    lon
                ),

                street=str(
                    street_context.get(
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

            if valid_candidate(
                parsed,
                candidate,
            ):

                output.append(
                    candidate
                )

        if output:
            break

    return output


async def geocode_osm(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    variants=None,
) -> list[Candidate]:

    output = []

    headers = {

        "User-Agent":
            "Metka-Kryvyi-Rih/10.0",

        "Accept-Language":
            "uk,ru;q=0.9",
    }

    streets = (
        variants
        or
        street_variants(
            parsed.street
        )
    )

    for street in streets[:2]:

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

                confidence=0.86,

                query_street=street,
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                output.append(
                    candidate
                )

        if output:
            break

        await asyncio.sleep(
            1.05
        )

    return output


async def geocode_overpass(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    variants=None,
) -> list[Candidate]:

    bbox = (
        f"{LAT_MIN},"
        f"{LON_MIN},"
        f"{LAT_MAX},"
        f"{LON_MAX}"
    )

    house_regex = re.escape(
        parsed.house
    )

    query = f"""
[out:json][timeout:8];

(
  node["addr:housenumber"~"^{house_regex}$",i]({bbox});
  way["addr:housenumber"~"^{house_regex}$",i]({bbox});
  relation["addr:housenumber"~"^{house_regex}$",i]({bbox});
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
                "Metka-Kryvyi-Rih/10.0"
        },

    ) as response:

        if response.status != 200:
            return []

        data = await response.json(
            content_type=None
        )

    streets = (
        variants
        or
        street_variants(
            parsed.street
        )
    )

    output = []

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

        if not same_house(
            house,
            parsed.house,
        ):
            continue

        similarity = max(

            (
                street_similarity(
                    variant,
                    street,
                )
                for variant in streets
            ),

            default=0,
        )

        if similarity < 0.50:
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

        building = (

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

            street=street,

            house=house,

            label=(
                f"{street} "
                f"{house}"
            ),

            precision=(
                "building"
                if building
                else
                "address"
            ),

            confidence=(
                0.96
                if building
                else
                0.88
            ),

            query_street=street,
        )

        if valid_candidate(
            parsed,
            candidate,
        ):

            output.append(
                candidate
            )

    return output


def parse_json_object(
    text: str,
) -> dict[str, Any] | None:

    try:

        result = json.loads(
            (
                text
                or
                ""
            ).strip()
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
        text
        or
        "",
        flags=re.S,
    )

    if not match:
        return None

    try:

        result = json.loads(
            match.group(0)
        )

        return (
            result
            if isinstance(
                result,
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
) -> Candidate | None:

    if (
        not ai_client
        or
        not ranked
    ):
        return None

    shortlist = ranked[:10]

    data = [

        {
            "index":
                index,

            "source":
                candidate.source,

            "lat":
                candidate.lat,

            "lon":
                candidate.lon,

            "street":
                candidate.street,

            "house":
                candidate.house,

            "precision":
                candidate.precision,

            "label":
                candidate.label,

            "score":
                round(
                    candidate.score,
                    2,
                ),
        }

        for index, candidate in enumerate(
            shortlist
        )
    ]

    prompt = f"""
Ты проверяешь адрес только в Кривом Роге.

Uklon — основной источник,
но его можно исправить,
если несколько независимых карт
согласованно показывают другую точку.

Нельзя придумывать координаты.
Выбери только index из списка.

Номер дома обязан совпадать.

Учитывай русский/украинский вариант,
опечатки и переименования улиц.

Если уверенности нет:
found=false.

Адрес:
{parsed.street} {parsed.house}

Кандидаты:
{json.dumps(data, ensure_ascii=False)}

Ответ только JSON:

{{
    "found": true,
    "index": 0,
    "confidence": 0.95
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

        if not result.get(
            "found"
        ):
            return None

        if (
            float(
                result.get(
                    "confidence",
                    0,
                )
            )
            <
            0.62
        ):
            return None

        index = int(
            result.get(
                "index",
                -1,
            )
        )

        if (
            0
            <=
            index
            <
            len(
                shortlist
            )
        ):

            candidate = shortlist[
                index
            ]

            if valid_candidate(
                parsed,
                candidate,
            ):

                return candidate

    except Exception as error:

        log.warning(
            "AI choose failed: %s",
            error,
        )

    return None


async def ai_street_variants(
    street: str,
) -> list[str]:

    if not ai_client:
        return []

    prompt = f"""
Для поиска адреса в Кривом Роге
дай до 6 вариантов ЭТОЙ ЖЕ улицы:

- исправление опечатки;
- русский вариант;
- украинский вариант;
- старое название;
- новое название.

Не придумывай переименование,
если не уверен.

Улица:
{street!r}

Ответ только JSON:

{{
    "variants": [
        "вариант"
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

                output.append(
                    value.strip()
                )

        return output[:6]

    except Exception as error:

        log.warning(
            "AI street variants failed: %s",
            error,
        )

        return []


async def search_primary(
    application: Application,
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
    variants=None,
) -> list[Candidate]:

    uklon = application.bot_data.get(
        "uklon"
    )

    tasks = [

        safe_provider(
            "google_places",
            geocode_google_places(
                session,
                parsed,
                variants,
            ),
        ),

        safe_provider(
            "google",
            geocode_google(
                session,
                parsed,
                variants,
            ),
        ),

        safe_provider(
            "visicom",
            geocode_visicom(
                session,
                parsed,
                variants,
            ),
        ),

        safe_provider(
            "mapbox",
            geocode_mapbox(
                session,
                parsed,
                variants,
            ),
        ),
    ]

    if uklon:

        tasks.insert(
            0,

            safe_provider(
                "uklon",
                uklon.search(
                    parsed,
                    variants,
                ),
            ),
        )

    groups = await asyncio.gather(
        *tasks
    )

    return [

        candidate

        for group in groups

        for candidate in group
    ]


async def resolve_address(
    application: Application,
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
):

    learned = get_learned(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    candidates = await search_primary(

        application,

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

    if ranked:

        ai_best = await ai_choose(
            parsed,
            ranked,
        )

        if ai_best:

            return (
                ai_best,
                ranked,
            )

    deep_results = await asyncio.gather(

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

    if ranked:

        ai_best = await ai_choose(
            parsed,
            ranked,
        )

        if ai_best:

            return (
                ai_best,
                ranked,
            )

    ai_variants = await ai_street_variants(
        parsed.street
    )

    if ai_variants:

        merged = []

        seen = set()

        for value in [

            *street_variants(
                parsed.street
            ),

            *ai_variants,
        ]:

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

        extra = await search_primary(

            application,

            session,

            parsed,

            merged[:8],
        )

        candidates.extend(
            extra
        )

        ranked = rank_candidates(
            parsed,
            candidates,
        )

        chosen = deterministic_choice(
            parsed,
            ranked,
        )

        if not chosen:

            chosen = await ai_choose(
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

    return (
        None,
        ranked,
    )


memory_cache = {}
inflight = {}

pending_results = {}
awaiting_correction = {}


async def resolve_cached(
    application: Application,
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
):

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

            application,

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

    return (
        "https://www.google.com/"
        "maps/search/"
        "?api=1&query="
        +
        quote(
            (
                f"{parsed.street} "
                f"{parsed.house}, "
                f"{CITY_RU}"
            )
        )
    )


def extract_coords_from_text(
    text: str,
):

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

        if in_city(
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
):

    direct = extract_coords_from_text(
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

            direct = extract_coords_from_text(
                str(
                    response.url
                )
            )

            if direct:
                return direct

            html = await response.text()

            return extract_coords_from_text(
                html[:600000]
            )

    except Exception as error:

        log.warning(
            "Google Maps link error: %s",
            error,
        )

        return None


def result_keyboard(
    token: str,
    best: Candidate,
):

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
                callback_data=f"ok:{token}",
            ),

            InlineKeyboardButton(
                "🎯 Уточнить координаты",
                callback_data=f"fix:{token}",
            ),
        ],
    ])


def not_found_keyboard(
    token: str,
    parsed: ParsedAddress,
):

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
                callback_data=f"fix:{token}",
            )
        ],
    ])


async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.message:

        await update.message.reply_text(
            "Отправь улицу и номер дома.\n"
            "Например: Лермонтова 25"
        )


async def status_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    uklon = context.application.bot_data.get(
        "uklon"
    )

    await update.message.reply_text(

        "Uklon: "
        +
        (
            "✅"
            if uklon
            else
            "—"
        )

        +

        "\nGoogle: "
        +
        (
            "✅"
            if GOOGLE_API_KEY
            else
            "—"
        )

        +

        "\nVisicom: "
        +
        (
            "✅"
            if VISICOM_KEY
            else
            "—"
        )

        +

        "\nMapbox: "
        +
        (
            "✅"
            if MAPBOX_TOKEN
            else
            "—"
        )

        +

        "\nOSM: ✅"

        +

        "\nИИ: "
        +
        (
            "✅"
            if ai_client
            else
            "—"
        )
    )


def cleanup_pending():

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


async def save_manual(
    update: Update,
    pending: PendingResult,
    lat: float,
    lon: float,
):

    for candidate in pending.candidates:

        dist = distance_coords(

            lat,
            lon,

            candidate.lat,
            candidate.lon,
        )

        if dist <= 55:

            update_provider_stat(
                candidate.source,
                True,
            )

        elif dist >= 140:

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


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

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

            await save_manual(

                update,

                pending,

                coords[0],
                coords[1],
            )

            return

        if "http" in text.lower():

            await update.message.reply_text(
                "❌ Не смог вытащить координаты.\n\n"
                "В Google Maps зажми точный дом → "
                "Поделиться → Копировать ссылку → "
                "отправь сюда."
            )

            return

    parsed = parse_address(
        text
    )

    if not parsed:
        return

    best, ranked = await resolve_cached(

        context.application,

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

                "Открой карту и сохрани точную "
                "точку — после этого бот её "
                "запомнит."
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

            f"Нажми кнопку ниже 👇"
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
):

    query = update.callback_query

    if (
        not query
        or
        not update.effective_user
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

    if (
        update.effective_user.id
        !=
        pending.owner_id
    ):

        await query.answer(
            "Изменить может автор запроса",
            show_alert=True,
        )

        return

    if action == "ok":

        if not pending.best:

            await query.answer(
                "Сначала укажи точку",
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

                    "3. Нажми «Поделиться» → "
                    "«Копировать ссылку».\n"

                    "4. Отправь ссылку сюда.\n\n"

                    "После этого бот запомнит "
                    "точку."
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


async def handle_location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

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

    if not in_city(
        lat,
        lon,
    ):

        await update.message.reply_text(
            "❌ Точка вне Кривого Рога"
        )

        return

    awaiting_correction.pop(
        key,
        None,
    )

    await save_manual(
        update,
        pending,
        lat,
        lon,
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    log.exception(
        "Telegram error",
        exc_info=context.error,
    )


async def post_init(
    application: Application,
):

    application.bot_data[
        "http"
    ] = aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT
    )

    if (
        UKLON_ENABLED
        and
        async_playwright is not None
    ):

        uklon = UklonBrowser()

        try:

            await uklon.start()

            application.bot_data[
                "uklon"
            ] = uklon

        except Exception as error:

            log.warning(
                "Uklon browser start failed: %s",
                error,
            )

    log.info(
        "Started. "
        "Uklon=%s "
        "Google=%s "
        "Visicom=%s "
        "Mapbox=%s "
        "AI=%s",

        bool(
            application.bot_data.get(
                "uklon"
            )
        ),

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
    )


async def post_shutdown(
    application: Application,
):

    uklon = application.bot_data.get(
        "uklon"
    )

    if uklon:

        await uklon.stop()

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


def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Не задан BOT_TOKEN"
        )

    init_db()

    if (
        UKLON_ENABLED
        and
        async_playwright is None
    ):

        log.warning(
            "playwright не установлен — "
            "Uklon отключён"
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
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
