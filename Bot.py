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
from difflib import SequenceMatcher
from typing import Any, Optional

import aiohttp

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

DB_PATH = os.getenv("DB_PATH", "metka.sqlite3").strip()

CITY_UA = "Кривий Ріг"
CITY_RU = "Кривой Рог"
COUNTRY_UA = "Україна"
COUNTRY_RU = "Украина"

LAT_MIN = 47.65
LAT_MAX = 48.20
LON_MIN = 32.75
LON_MAX = 33.80

HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=10,
    connect=3,
    sock_read=8,
)

AI_TIMEOUT = 8
CACHE_TTL = 24 * 60 * 60
CONSENSUS_METERS = 80.0
MAX_AI_CANDIDATES = 12

logging.basicConfig(
    level=getattr(
        logging,
        os.getenv("LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    ),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("metka")

ai_client: Optional[AsyncOpenAI] = None

if OPENAI_API_KEY and AsyncOpenAI is not None:
    ai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# STREET ALIASES
# ============================================================

STREET_ALIASES: dict[str, list[str]] = {
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
# DATA MODELS
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

    score: float = 0.0


@dataclass(slots=True)
class PendingResult:
    owner_id: int
    chat_id: int

    parsed: ParsedAddress

    best: Candidate

    candidates: list[Candidate]


# ============================================================
# NORMALIZATION / PARSING
# ============================================================

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

    # /11 .11 -11 считаем квартирой
    r"(?:"
    r"\s*[/.-]\s*"
    r"\d{1,6}"
    r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"
    r")?"

    # кв. 11
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


def normalize_text(text: str) -> str:

    text = unicodedata.normalize(
        "NFKC",
        text or "",
    )

    text = text.lower()

    text = (
        text
        .replace("ё", "е")
        .replace("’", "'")
        .replace("`", "'")
    )

    text = re.sub(
        r"[^0-9a-zа-яіїєґ'\-\s]+",
        " ",
        text,
        flags=re.I,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def normalize_house(text: str) -> str:

    text = unicodedata.normalize(
        "NFKC",
        text or "",
    )

    text = text.translate(
        CYR_LOOKALIKES
    )

    text = text.replace(
        " ",
        "",
    )

    return (
        text
        .upper()
        .replace("Ё", "Е")
    )


def street_core(text: str) -> str:

    words = []

    for word in normalize_text(
        text
    ).split():

        word = word.strip(
            ".-"
        )

        if (
            word
            and
            word not in STREET_PREFIXES
        ):

            words.append(
                word
            )

    return " ".join(
        words
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
        normalize_house(a)
        ==
        normalize_house(b)
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

    if len(original) > 120:
        return None

    low = original.lower()

    if (
        "http://" in low
        or
        "https://" in low
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


def street_variants(
    street: str,
) -> list[str]:

    base = street_core(
        street
    )

    variants = [
        street
    ]

    for canonical, aliases in STREET_ALIASES.items():

        family = {
            street_core(canonical),
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

    out = []

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

            out.append(
                value
            )

    return out[:6]


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

    if not aa or not bb:
        return 0.0

    if aa == bb:
        return 1.0

    sequence = SequenceMatcher(
        None,
        aa,
        bb,
    ).ratio()

    aset = set(
        aa.split()
    )

    bset = set(
        bb.split()
    )

    jaccard = (
        len(
            aset & bset
        )
        /
        max(
            1,
            len(
                aset | bset
            ),
        )
    )

    return max(
        sequence,
        jaccard,
    )


# ============================================================
# DATABASE / LEARNING
# ============================================================

def db() -> sqlite3.Connection:

    connection = sqlite3.connect(
        DB_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_db() -> None:

    parent = os.path.dirname(
        os.path.abspath(
            DB_PATH
        )
    )

    if parent:
        os.makedirs(
            parent,
            exist_ok=True,
        )

    with db() as conn:

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS confirmed_addresses (
                query_key TEXT PRIMARY KEY,

                original_query TEXT NOT NULL,
                street TEXT NOT NULL,
                house TEXT NOT NULL,

                lat REAL NOT NULL,
                lon REAL NOT NULL,

                label TEXT,
                source TEXT,

                confirmations INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS provider_stats (
                provider TEXT PRIMARY KEY,

                good INTEGER NOT NULL DEFAULT 0,
                bad INTEGER NOT NULL DEFAULT 0,

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


def save_learned_address(
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
                    confirmed_addresses.confirmations + 1,

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


def get_learned_address(
    parsed: ParsedAddress,
) -> Optional[Candidate]:

    with db() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM confirmed_addresses
            WHERE query_key = ?
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

        street=row["street"],

        house=row["house"],

        label=(
            row["label"]
            or
            parsed.original
        ),

        precision="user_confirmed",

        confidence=0.999,

        score=10000.0,
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

    base = {
        "visicom": 1.12,
        "google": 1.10,
        "mapbox": 1.03,
        "overpass": 1.01,
        "osm": 0.98,
        "learned": 1.50,
    }.get(
        provider,
        1.0,
    )

    with db() as conn:

        row = conn.execute(
            """
            SELECT good,bad
            FROM provider_stats
            WHERE provider = ?
            """,

            (
                provider,
            ),
        ).fetchone()

    if not row:
        return base

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
        base
        *
        (
            0.85
            +
            0.30
            *
            ratio
        )
    )


# ============================================================
# GEOMETRY / SCORING
# ============================================================

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


def distance_m(
    a: Candidate,
    b: Candidate,
) -> float:

    return distance_coords(
        a.lat,
        a.lon,
        b.lat,
        b.lon,
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

    if not same_house(
        parsed.house,
        candidate.house,
    ):
        return False

    similarity = street_similarity(
        parsed.street,

        (
            candidate.street
            or
            candidate.label
        ),
    )

    if similarity < 0.48:
        return False

    # Запрещаем точки уровня улицы/города
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

    if not valid_candidate(
        parsed,
        candidate,
    ):
        return -1000.0

    if candidate.source == "learned":
        return 10000.0

    provider_score = {
        "visicom": 120,
        "google": 115,
        "mapbox": 108,
        "overpass": 104,
        "osm": 98,
    }.get(
        candidate.source,
        80,
    )

    precision_score = {
        "user_confirmed": 100,
        "rooftop": 40,
        "building": 36,
        "parcel": 32,
        "point": 30,
        "address": 26,
        "interpolated": 8,
        "approximate": 3,
    }.get(
        candidate.precision,
        0,
    )

    similarity = street_similarity(
        parsed.street,

        (
            candidate.street
            or
            candidate.label
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
                other.source
                !=
                candidate.source

                and

                distance_m(
                    candidate,
                    other,
                )
                <=
                CONSENSUS_METERS
            )
        }

        candidate.score += min(
            45,
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

    out = []

    for candidate in good:

        duplicate = any(

            existing.source
            ==
            candidate.source

            and

            distance_m(
                existing,
                candidate,
            )
            <
            8

            for existing in out
        )

        if not duplicate:

            out.append(
                candidate
            )

    return out


# ============================================================
# HTTP
# ============================================================

async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
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


# ============================================================
# VISICOM
# ============================================================

def find_house_in_text(
    text: str,
    wanted: str,
) -> str:

    if not text:
        return ""

    values = re.findall(
        r"(?<!\d)"
        r"("
        r"\d{1,4}"
        r"\s*"
        r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{0,2}"
        r")"
        r"(?!\d)",

        text,
    )

    for value in values:

        if same_house(
            value,
            wanted,
        ):
            return wanted

    return ""


async def geocode_visicom(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    if not VISICOM_KEY:
        return []

    out = []

    for street in street_variants(
        parsed.street
    )[:4]:

        searches = [
            (
                "ru",
                CITY_RU,
                COUNTRY_RU,
            ),

            (
                "uk",
                CITY_UA,
                COUNTRY_UA,
            ),
        ]

        for language, city, country in searches:

            query = (
                f"{city}, "
                f"{street} "
                f"{parsed.house}, "
                f"{country}"
            )

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
                        query,

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

                categories = str(
                    properties.get(
                        "categories"
                    )
                    or
                    ""
                )

                if "adr_address" not in categories:
                    continue

                centroid = (
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
                    centroid.get(
                        "coordinates"
                    )
                    or
                    []
                )

                if len(
                    coordinates
                ) < 2:
                    continue

                text_parts = []

                for key in (
                    "name",
                    "address",
                    "street",
                    "description",
                ):

                    value = properties.get(
                        key
                    )

                    if isinstance(
                        value,
                        str,
                    ):

                        text_parts.append(
                            value
                        )

                    elif isinstance(
                        value,
                        dict,
                    ):

                        for nested in value.values():

                            if isinstance(
                                nested,
                                str,
                            ):

                                text_parts.append(
                                    nested
                                )

                label = " ".join(
                    text_parts
                )

                house = find_house_in_text(
                    label,
                    parsed.house,
                )

                if not house:
                    continue

                candidate = Candidate(
                    source="visicom",

                    lat=float(
                        coordinates[1]
                    ),

                    lon=float(
                        coordinates[0]
                    ),

                    street=str(
                        properties.get(
                            "street"
                        )
                        or
                        street
                    ),

                    house=house,

                    label=(
                        label
                        or
                        f"{street} {house}"
                    ),

                    precision="address",

                    confidence=0.98,
                )

                if valid_candidate(
                    parsed,
                    candidate,
                ):

                    out.append(
                        candidate
                    )

        if out:
            break

    return out


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

    out = []

    for street in street_variants(
        parsed.street
    )[:4]:

        query = (
            f"{street} "
            f"{parsed.house}, "
            f"{CITY_UA}, "
            f"{COUNTRY_UA}"
        )

        data = await get_json(
            session,

            (
                "https://maps.googleapis.com/"
                "maps/api/geocode/json"
            ),

            params={
                "address":
                    query,

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
                "results",
                [],
            )
            or
            []
        ):

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

            candidate = Candidate(
                source="google",

                lat=float(
                    location["lat"]
                ),

                lon=float(
                    location["lng"]
                ),

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

                    if precision == "rooftop"

                    else

                    0.82
                ),
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                out.append(
                    candidate
                )

        if out:
            break

    return out


# ============================================================
# MAPBOX
# ============================================================

async def geocode_mapbox(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    if not MAPBOX_TOKEN:
        return []

    out = []

    for street in street_variants(
        parsed.street
    )[:4]:

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
            ) < 2:
                continue

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

            match_code = (
                properties.get(
                    "match_code"
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

            coordinate_data = (
                properties.get(
                    "coordinates"
                )
                or
                {}
            )

            accuracy = str(
                coordinate_data.get(
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

            precision = {
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

                    if feature_type == "address"

                    else

                    "unknown"
                ),
            )

            confidence_map = {
                "exact": 0.99,
                "high": 0.93,
                "medium": 0.80,
                "low": 0.62,
            }

            confidence = confidence_map.get(
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

                lat=float(
                    coordinates[1]
                ),

                lon=float(
                    coordinates[0]
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
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                out.append(
                    candidate
                )

        if out:
            break

    return out


# ============================================================
# OSM NOMINATIM
# ============================================================

async def geocode_osm(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    out = []

    headers = {
        "User-Agent":
            "Metka-Kryvyi-Rih/6.0",

        "Accept-Language":
            "uk,ru;q=0.9",
    }

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

        for item in data or []:

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
            )

            if valid_candidate(
                parsed,
                candidate,
            ):

                out.append(
                    candidate
                )

        if out:
            break

        await asyncio.sleep(
            1.05
        )

    return out


# ============================================================
# OSM OVERPASS
# ============================================================

async def geocode_overpass(
    session: aiohttp.ClientSession,
    parsed: ParsedAddress,
) -> list[Candidate]:

    digits = re.match(
        r"\d{1,4}",
        parsed.house,
    )

    if not digits:
        return []

    bbox = (
        f"{LAT_MIN},"
        f"{LON_MIN},"
        f"{LAT_MAX},"
        f"{LON_MAX}"
    )

    prefix = re.escape(
        digits.group(0)
    )

    query = f"""
[out:json][timeout:8];

(
  node["addr:housenumber"~"^{prefix}",i]({bbox});
  way["addr:housenumber"~"^{prefix}",i]({bbox});
  relation["addr:housenumber"~"^{prefix}",i]({bbox});
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
                "Metka-Kryvyi-Rih/6.0"
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

    out = []

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

        if street_similarity(
            parsed.street,
            street,
        ) < 0.48:
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

            street=street,
            house=house,

            label=(
                f"{street} "
                f"{house}"
            ).strip(),

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

                0.88
            ),
        )

        if valid_candidate(
            parsed,
            candidate,
        ):

            out.append(
                candidate
            )

    return out


# ============================================================
# AI VERIFIER
# ============================================================

def candidate_payload(
    candidate: Candidate,
    index: int,
) -> dict[str, Any]:

    return {
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
    }


async def ai_choose(
    parsed: ParsedAddress,
    candidates: list[Candidate],
) -> Optional[Candidate]:

    if (
        not ai_client
        or
        not candidates
    ):
        return None

    pool = candidates[
        :MAX_AI_CANDIDATES
    ]

    payload = [
        candidate_payload(
            candidate,
            index,
        )

        for index, candidate in enumerate(
            pool
        )
    ]

    prompt = f"""
Ты проверяешь адреса ТОЛЬКО в городе Кривой Рог / Кривий Ріг, Украина.

Искомый адрес:
улица={parsed.street!r}
дом={parsed.house!r}

Ниже только реальные кандидаты, найденные геокодерами.

Выбери наиболее вероятный именно ДОМ.

Правила:
1. Не придумывай и не изменяй координаты.
2. Можно выбрать только index из списка.
3. Номер дома должен совпадать.
4. Учитывай русский/украинский язык, старые/новые названия и небольшие опечатки.
5. rooftop/building/parcel/point лучше street/interpolated/approximate.
6. Согласие независимых провайдеров в пределах примерно 20-80 м — сильный плюс.
7. Если надёжного кандидата нет, found=false.

Кандидаты:
{json.dumps(payload, ensure_ascii=False)}

Ответ только JSON без markdown:

{{
    "found": true,
    "index": 0,
    "confidence": 0.95,
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

        text = (
            response.output_text
            or
            ""
        ).strip()

        match = re.search(
            r"\{.*\}",
            text,
            flags=re.S,
        )

        if not match:
            return None

        result = json.loads(
            match.group(0)
        )

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
                0,
            )
        )

        if not (
            0
            <=
            index
            <
            len(pool)
        ):
            return None

        if confidence < 0.60:
            return None

        chosen = pool[
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
            "AI verifier failed: %s",
            error,
        )

        return None


# ============================================================
# RESOLUTION
# ============================================================

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


async def resolve_address(
    parsed: ParsedAddress,
) -> tuple[
    Optional[Candidate],
    list[Candidate],
]:

    learned = get_learned_address(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    async with aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT
    ) as session:

        main_groups = await asyncio.gather(

            safe_provider(
                "visicom",
                geocode_visicom(
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

        candidates = [
            candidate

            for group in main_groups

            for candidate in group
        ]

        ranked = rank_candidates(
            parsed,
            candidates,
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

            best = ranked[0]

            if (
                best.source == "visicom"
                or
                best.precision in {
                    "rooftop",
                    "building",
                    "parcel",
                    "point",
                }
            ):

                return (
                    best,
                    ranked,
                )

            if any(

                other.source
                !=
                best.source

                and

                distance_m(
                    best,
                    other,
                )
                <=
                CONSENSUS_METERS

                for other in ranked[1:]
            ):

                return (
                    best,
                    ranked,
                )

        # ----------------------------------------------------
        # OSM Nominatim
        # ----------------------------------------------------

        osm_results = await safe_provider(
            "osm",

            geocode_osm(
                session,
                parsed,
            ),
        )

        candidates.extend(
            osm_results
        )

        ranked = rank_candidates(
            parsed,
            candidates,
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

            best = ranked[0]

            return (
                best,
                ranked,
            )

        # ----------------------------------------------------
        # Overpass
        # ----------------------------------------------------

        overpass_results = await safe_provider(
            "overpass",

            geocode_overpass(
                session,
                parsed,
            ),
        )

        candidates.extend(
            overpass_results
        )

        ranked = rank_candidates(
            parsed,
            candidates,
        )

        if ranked:

            ai_best = await ai_choose(
                parsed,
                ranked,
            )

            return (
                ai_best
                or
                ranked[0],

                ranked,
            )

    return (
        None,
        [],
    )


# ============================================================
# CACHE
# ============================================================

_cache: dict[
    str,
    tuple[
        float,
        Candidate,
        list[Candidate],
    ],
] = {}


_inflight: dict[
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
    parsed: ParsedAddress,
) -> tuple[
    Optional[Candidate],
    list[Candidate],
]:

    key = address_key(
        parsed
    )

    learned = get_learned_address(
        parsed
    )

    if learned:

        return (
            learned,
            [learned],
        )

    cached = _cache.get(
        key
    )

    now = time.time()

    if cached:

        if (
            now
            -
            cached[0]
            <=
            CACHE_TTL
        ):

            return (
                cached[1],
                cached[2],
            )

    if key in _inflight:

        return await _inflight[
            key
        ]

    async def worker():

        best, ranked = await resolve_address(
            parsed
        )

        if (
            best
            and
            best.source != "mapbox"
        ):

            _cache[key] = (
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

    _inflight[key] = task

    try:

        return await task

    finally:

        _inflight.pop(
            key,
            None,
        )


# ============================================================
# TELEGRAM UI
# ============================================================

def maps_url(
    lat: float,
    lon: float,
) -> str:

    return (
        "https://www.google.com/maps/search/"
        "?api=1&query="
        f"{lat:.7f},"
        f"{lon:.7f}"
    )


def source_name(
    source: str,
) -> str:

    return {
        "learned":
            "Исправлено вручную",

        "visicom":
            "Visicom",

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


async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not update.message:
        return

    await update.message.reply_text(
        "Отправь улицу и номер дома.\n"
        "Например: Лермонтова 25"
    )


async def status_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not update.message:
        return

    await update.message.reply_text(
        "Состояние:\n"

        f"Visicom: "
        f"{'✅' if VISICOM_KEY else '—'}\n"

        f"Google: "
        f"{'✅' if GOOGLE_API_KEY else '—'}\n"

        f"Mapbox: "
        f"{'✅' if MAPBOX_TOKEN else '—'}\n"

        "OpenStreetMap: ✅\n"

        f"ИИ: "
        f"{'✅' if ai_client else '—'}"
    )


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

    parsed = parse_address(
        update.message.text
    )

    if not parsed:
        return

    best, candidates = await resolve_cached(
        parsed
    )

    if not best:

        await update.message.reply_text(
            "❌ Не удалось точно найти дом:\n"
            f"{parsed.original}"
        )

        return

    user = update.effective_user

    if not user:
        return

    token = uuid.uuid4().hex[
        :12
    ]

    pending_results[token] = PendingResult(
        owner_id=user.id,

        chat_id=update.message.chat_id,

        parsed=parsed,

        best=best,

        candidates=candidates,
    )

    text = (
        f"📍  Улица: "
        f"{parsed.original}\n"

        f"🏙  Кривой Рог\n\n"

        f"Источник: "
        f"{source_name(best.source)}\n"

        f"Нажми кнопку ниже 👇"
    )

    await update.message.reply_text(
        text,

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

    if not query:
        return

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
        user.id
        !=
        pending.owner_id
    ):

        await query.answer(
            "Исправить метку может автор запроса.",
            show_alert=True,
        )

        return

    await query.answer()

    # ========================================================
    # ПОДТВЕРЖДЕНИЕ
    # ========================================================

    if action == "ok":

        save_learned_address(
            pending.parsed,

            pending.best.lat,
            pending.best.lon,

            pending.best.label,

            pending.best.source,
        )

        update_provider_stat(
            pending.best.source,
            True,
        )

        _cache.pop(
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
                    f"и сохранена."
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
    # ИСПРАВЛЕНИЕ
    # ========================================================

    if action == "fix":

        if not query.message:
            return

        awaiting_correction[
            (
                pending.chat_id,
                user.id,
            )
        ] = pending

        await query.message.reply_text(
            "🎯 Отправь правильную геолокацию.\n\n"
            "Нажми 📎 → Геопозиция → "
            "выбери правильный дом на карте "
            "и отправь точку."
        )


async def handle_location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
        update.message.chat_id,
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
            "вне допустимой области "
            "Кривого Рога."
        )

        return

    awaiting_correction.pop(
        key,
        None,
    )

    # ========================================================
    # ОБУЧАЕМ НАДЁЖНОСТЬ ПРОВАЙДЕРОВ
    # ========================================================

    for candidate in pending.candidates:

        if candidate.source in {
            "learned",
            "user",
            "user_correction",
        }:
            continue

        distance = distance_coords(
            lat,
            lon,
            candidate.lat,
            candidate.lon,
        )

        # очень близко к правильной точке
        if distance <= 60:

            update_provider_stat(
                candidate.source,
                True,
            )

        # явно ошибся
        elif distance >= 150:

            update_provider_stat(
                candidate.source,
                False,
            )

    # ========================================================
    # СОХРАНЯЕМ ИСПРАВЛЕННУЮ ТОЧКУ
    # ========================================================

    save_learned_address(
        pending.parsed,

        lat,
        lon,

        (
            f"{pending.parsed.street} "
            f"{pending.parsed.house}"
        ),

        "user_correction",
    )

    _cache.pop(
        address_key(
            pending.parsed
        ),
        None,
    )

    await update.message.reply_text(

        (
            "✅ Координаты сохранены.\n\n"

            f"📍 "
            f"{pending.parsed.original}\n\n"

            "В следующий раз бот сразу "
            "использует эту правильную точку."
        ),

        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📍 Открыть исправленную метку ↗",

                    url=maps_url(
                        lat,
                        lon,
                    ),
                )
            ]
        ]),

        disable_web_page_preview=True,
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
# START
# ============================================================

def validate_config() -> None:

    if not BOT_TOKEN:

        raise RuntimeError(
            "Не задан BOT_TOKEN"
        )

    if not any((
        VISICOM_KEY,
        GOOGLE_API_KEY,
        MAPBOX_TOKEN,
    )):

        log.warning(
            "Не задан ни один платный геокодер. "
            "Остаются только OSM / Overpass."
        )


def main() -> None:

    validate_config()

    init_db()

    app = (
        Application
        .builder()
        .token(
            BOT_TOKEN
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
        "Bot started | "
        "Visicom=%s "
        "Google=%s "
        "Mapbox=%s "
        "OSM=yes "
        "AI=%s "
        "model=%s "
        "DB=%s",

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
