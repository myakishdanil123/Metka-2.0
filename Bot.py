import os
import re
import json
import math
import time
import sqlite3
import asyncio
import logging
import unicodedata
from dataclasses import dataclass, asdict
from typing import Any, Optional

import aiohttp

# OpenAI is optional. The geocoder must keep working even if the package
# is not installed or the API is temporarily unavailable.
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
VISICOM_KEY = os.getenv("VISICOM_KEY", "").strip()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6").strip()

# Mapbox Temporary responses must not be persistently cached.
# Set 1 only if your Mapbox account/request mode allows permanent storage.
MAPBOX_PERMANENT = os.getenv("MAPBOX_PERMANENT", "0").strip() == "1"

DB_PATH = os.getenv("DB_PATH", "bot_learning.sqlite3").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

CITY_UA = "Кривий Ріг"
CITY_RU = "Кривой Рог"
COUNTRY_UA = "Україна"
COUNTRY_RU = "Украина"
COUNTRY_CODE = "UA"

# Approximate city bounds. Used only as a sanity check, not as the geocoder itself.
LAT_MIN = 47.65
LAT_MAX = 48.20
LON_MIN = 32.75
LON_MAX = 33.80
CITY_CENTER = (47.9105, 33.3918)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=7.0, connect=2.5)
AI_TIMEOUT_SECONDS = 8.0
MAX_PROVIDER_RESULTS = 5
MAX_AI_CANDIDATES = 12
MIN_FINAL_SCORE = 35.0
MIN_AI_CONFIDENCE = 0.56
CACHE_TTL_SECONDS = 24 * 3600

# Optional trainer restriction. Example: TRAINER_IDS="123456,987654"
TRAINER_IDS = {
    int(x.strip())
    for x in os.getenv("TRAINER_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("address-bot")

openai_client: Any = None
if OPENAI_API_KEY and AsyncOpenAI is not None:
    try:
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        log.warning("OpenAI client disabled: %s", e)
elif OPENAI_API_KEY and AsyncOpenAI is None:
    log.warning("OPENAI_API_KEY is set, but package 'openai' is not installed. AI verifier disabled.")

# Small seed list. The database can learn more aliases with /alias.
SEED_ALIASES = {
    "одоєвського": ["одоевского", "одоевського", "одоєвского"],
    "одоевского": ["одоєвського", "одоевського", "одоєвского"],
    "волгоградська": ["волгоградская"],
    "волгоградская": ["волгоградська"],
    "дзержинського": ["дзержинского"],
    "дзержинского": ["дзержинського"],
}

# Address parser. For this bot, separators after the house number are treated
# as an apartment/flat number: 25/11, 25.11 and 25-11 -> house 25.
# A letter attached directly to the house number is preserved: 25А/11 -> 25А.
ADDRESS_RE = re.compile(
    r"(?iu)^\s*(?:вул(?:иця)?\.?|ул(?:ица)?\.?|просп(?:ект)?\.?|пр-т\.?|"
    r"пров(?:улок)?\.?|пер(?:еулок)?\.?|бул(?:ьвар)?\.?|б-р\.?|"
    r"пл(?:ощадь|оща)?\.?|шосе|шоссе)?\s*"
    r"(?P<street>[\wА-Яа-яЁёІіЇїЄєҐґ'’\-\.\s]{2,80}?)"
    r"\s*[,№#]?\s*(?P<house>\d{1,4}[А-Яа-яЁёІіЇїЄєҐґA-Za-z]{0,3})"
    r"(?:\s*(?:[/.-])\s*\d{1,6}[А-Яа-яЁёІіЇїЄєҐґA-Za-z]{0,3})?"
    r"(?:\s*,?\s*(?:кв(?:артира)?\.?|apt\.?|apartment)\s*№?\s*\d{1,6})?"
    r"\s*$"
)

# ============================================================
# DATA MODELS
# ============================================================
@dataclass
class Candidate:
    source: str
    lat: float
    lon: float
    display: str
    street: str = ""
    house: str = ""
    city: str = ""
    accuracy: str = ""
    provider_confidence: float = 0.0
    score: float = 0.0
    distance_to_cluster_m: float = 0.0

    def compact(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lat": round(self.lat, 7),
            "lon": round(self.lon, 7),
            "display": self.display[:220],
            "street": self.street[:100],
            "house": self.house[:30],
            "city": self.city[:80],
            "accuracy": self.accuracy[:60],
            "provider_confidence": round(self.provider_confidence, 3),
            "score": round(self.score, 2),
        }


@dataclass
class ParsedAddress:
    original: str
    street: str
    house: str


# ============================================================
# DATABASE / LEARNING
# ============================================================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS confirmed_addresses (
                query_key TEXT PRIMARY KEY,
                original_query TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                display TEXT,
                source TEXT,
                confirmations INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rejected_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key TEXT NOT NULL,
                lat REAL,
                lon REAL,
                source TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS street_aliases (
                alias TEXT PRIMARY KEY,
                canonical TEXT NOT NULL,
                confirmations INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS provider_stats (
                provider TEXT PRIMARY KEY,
                good INTEGER NOT NULL DEFAULT 0,
                bad INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key TEXT NOT NULL,
                original_query TEXT NOT NULL,
                chosen_source TEXT,
                lat REAL,
                lon REAL,
                score REAL,
                ai_confidence REAL,
                ai_reason TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.lower().replace("’", "'").replace("`", "'")
    s = s.replace("ё", "е")
    s = re.sub(r"[^0-9a-zа-яіїєґ'\-\s]", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_house(s: str) -> str:
    return re.sub(r"\s+", "", normalize_text(s)).replace("-", "/")


def query_key(street: str, house: str) -> str:
    return f"{canonical_street(street)}|{normalize_house(house)}"


def canonical_street(street: str) -> str:
    x = normalize_text(street)
    x = re.sub(
        r"^(вулиця|вул|улица|ул|проспект|просп|пр т|провулок|пров|переулок|пер|бульвар|бул|площа|площадь)\s+",
        "",
        x,
    )

    # DB-learned aliases override seeds.
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT canonical FROM street_aliases WHERE alias=?", (x,)
            ).fetchone()
            if row:
                return normalize_text(row["canonical"])
    except sqlite3.Error:
        pass

    for canonical, aliases in SEED_ALIASES.items():
        vals = {normalize_text(canonical), *(normalize_text(a) for a in aliases)}
        if x in vals:
            return normalize_text(canonical)
    return x


def provider_weight(provider: str) -> float:
    base = {
        "google": 1.12,
        "visicom": 1.10,
        "mapbox": 1.00,
        "osm": 0.96,
        "learned": 1.30,
    }.get(provider, 1.0)
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT good,bad FROM provider_stats WHERE provider=?", (provider,)
            ).fetchone()
        if not row:
            return base
        good, bad = int(row["good"]), int(row["bad"])
        # Smoothed multiplier 0.85..1.15
        ratio = (good + 3) / (good + bad + 6)
        return base * (0.85 + 0.30 * ratio)
    except sqlite3.Error:
        return base


def update_provider_stat(provider: str, good: bool) -> None:
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO provider_stats(provider,good,bad,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
                good=good+excluded.good,
                bad=bad+excluded.bad,
                updated_at=excluded.updated_at
            """,
            (provider, 1 if good else 0, 0 if good else 1, now),
        )


def get_learned(parsed: ParsedAddress) -> Optional[Candidate]:
    key = query_key(parsed.street, parsed.house)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM confirmed_addresses WHERE query_key=?", (key,)
        ).fetchone()
    if not row:
        return None
    return Candidate(
        source="learned",
        lat=float(row["lat"]),
        lon=float(row["lon"]),
        display=row["display"] or parsed.original,
        street=parsed.street,
        house=parsed.house,
        city=CITY_UA,
        accuracy="user_confirmed",
        provider_confidence=min(0.99, 0.78 + 0.04 * int(row["confirmations"])),
        score=120.0,
    )


def save_confirmed(parsed: ParsedAddress, c: Candidate) -> None:
    key = query_key(parsed.street, parsed.house)
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO confirmed_addresses(
                query_key,original_query,lat,lon,display,source,confirmations,updated_at
            ) VALUES(?,?,?,?,?,?,1,?)
            ON CONFLICT(query_key) DO UPDATE SET
                lat=excluded.lat,
                lon=excluded.lon,
                display=excluded.display,
                source=excluded.source,
                confirmations=confirmations+1,
                updated_at=excluded.updated_at
            """,
            (key, parsed.original, c.lat, c.lon, c.display, c.source, now),
        )
    update_provider_stat(c.source, True)


def save_rejected(parsed: ParsedAddress, c: Candidate) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO rejected_results(query_key,lat,lon,source,created_at) VALUES(?,?,?,?,?)",
            (query_key(parsed.street, parsed.house), c.lat, c.lon, c.source, int(time.time())),
        )
    update_provider_stat(c.source, False)


def save_alias(alias: str, canonical: str) -> None:
    a = normalize_text(alias)
    c = normalize_text(canonical)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO street_aliases(alias,canonical,confirmations,updated_at)
            VALUES(?,?,1,?)
            ON CONFLICT(alias) DO UPDATE SET
                canonical=excluded.canonical,
                confirmations=confirmations+1,
                updated_at=excluded.updated_at
            """,
            (a, c, int(time.time())),
        )


# ============================================================
# ADDRESS PARSING
# ============================================================
def parse_address(text: str) -> Optional[ParsedAddress]:
    if not text or len(text) > 120:
        return None
    x = unicodedata.normalize("NFKC", text).strip()

    # Reject obvious chatter / URLs / coordinates.
    if "http://" in x.lower() or "https://" in x.lower():
        return None
    if re.fullmatch(r"[\d\s.,+-]+", x):
        return None

    # Remove explicit city suffix if user wrote it.
    x = re.sub(
        r"(?iu),?\s*(?:м\.?\s*)?(?:кривий\s+ріг|кривой\s+рог)(?:,?\s*(?:україна|украина))?\s*$",
        "",
        x,
    ).strip(" ,")

    m = ADDRESS_RE.match(x)
    if not m:
        return None

    street = m.group("street").strip(" ,.-")
    house = m.group("house").strip()
    if len(normalize_text(street)) < 2:
        return None
    return ParsedAddress(original=text.strip(), street=street, house=house)


def query_variants(parsed: ParsedAddress) -> list[str]:
    street = parsed.street.strip()
    # parsed.house is already the building number only; apartment suffixes are stripped.
    house = parsed.house.strip()
    canonical = canonical_street(street)
    variants = [
        f"{street} {house}, {CITY_UA}, {COUNTRY_UA}",
        f"{street} {house}, {CITY_RU}, {COUNTRY_RU}",
    ]
    if canonical and canonical != normalize_text(street):
        variants += [
            f"{canonical} {house}, {CITY_UA}, {COUNTRY_UA}",
            f"{canonical} {house}, {CITY_RU}, {COUNTRY_RU}",
        ]
    # Preserve order, deduplicate normalized variants.
    out, seen = [], set()
    for q in variants:
        k = normalize_text(q)
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out[:4]


# ============================================================
# GEOMETRY / SCORING
# ============================================================
def in_city_bounds(lat: float, lon: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def token_similarity(a: str, b: str) -> float:
    aa = set(canonical_street(a).split())
    bb = set(canonical_street(b).split())
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def score_candidate(parsed: ParsedAddress, c: Candidate) -> float:
    if not in_city_bounds(c.lat, c.lon):
        return -1000.0

    score = 10.0
    wanted_house = normalize_house(parsed.house)
    got_house = normalize_house(c.house)
    if got_house:
        if got_house == wanted_house:
            score += 40
        elif got_house.split("/")[0] == wanted_house.split("/")[0]:
            score += 17
        else:
            score -= 35
    else:
        score -= 8

    sim = token_similarity(parsed.street, c.street or c.display)
    score += 32 * sim

    city_norm = normalize_text(c.city + " " + c.display)
    if "кривий ріг" in city_norm or "кривой рог" in city_norm:
        score += 15

    acc = normalize_text(c.accuracy)
    if any(x in acc for x in ("rooftop", "building", "house", "address", "точна", "точный")):
        score += 14
    if any(x in acc for x in ("interpol", "street", "approx")):
        score -= 4

    score += max(0.0, min(10.0, c.provider_confidence * 10.0))
    score *= provider_weight(c.source)
    return score


def add_consensus_bonus(candidates: list[Candidate]) -> None:
    for i, c in enumerate(candidates):
        neighbors = []
        for j, other in enumerate(candidates):
            if i == j or c.source == other.source:
                continue
            d = haversine_m(c.lat, c.lon, other.lat, other.lon)
            if d <= 60:
                neighbors.append(d)
        if neighbors:
            c.score += min(28.0, 9.0 * len(neighbors))
            c.distance_to_cluster_m = sum(neighbors) / len(neighbors)
        else:
            c.distance_to_cluster_m = 99999.0


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    # Keep nearby duplicates from the same provider from flooding AI.
    out: list[Candidate] = []
    for c in sorted(candidates, key=lambda x: x.score, reverse=True):
        if any(
            c.source == x.source and haversine_m(c.lat, c.lon, x.lat, x.lon) < 12
            for x in out
        ):
            continue
        out.append(c)
    return out


# ============================================================
# PROVIDERS
# ============================================================
async def get_json(session: aiohttp.ClientSession, url: str, *, params=None, headers=None) -> Any:
    async with session.get(url, params=params, headers=headers) as r:
        if r.status != 200:
            body = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {body[:180]}")
        return await r.json(content_type=None)


def component_google(result: dict, typ: str) -> str:
    for comp in result.get("address_components", []):
        if typ in comp.get("types", []):
            return comp.get("long_name", "")
    return ""


async def geocode_google(session: aiohttp.ClientSession, parsed: ParsedAddress) -> list[Candidate]:
    if not GOOGLE_API_KEY:
        return []
    out: list[Candidate] = []
    for q in query_variants(parsed)[:2]:
        data = await get_json(
            session,
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": q, "key": GOOGLE_API_KEY, "language": "uk", "region": "ua"},
        )
        for r in data.get("results", [])[:MAX_PROVIDER_RESULTS]:
            loc = r.get("geometry", {}).get("location", {})
            if "lat" not in loc or "lng" not in loc:
                continue
            c = Candidate(
                source="google",
                lat=float(loc["lat"]),
                lon=float(loc["lng"]),
                display=r.get("formatted_address", ""),
                street=component_google(r, "route"),
                house=component_google(r, "street_number"),
                city=(component_google(r, "locality") or component_google(r, "administrative_area_level_2")),
                accuracy=r.get("geometry", {}).get("location_type", ""),
                provider_confidence=0.95 if r.get("geometry", {}).get("location_type") == "ROOFTOP" else 0.78,
            )
            out.append(c)
        if out:
            break
    return out


async def geocode_osm(session: aiohttp.ClientSession, parsed: ParsedAddress) -> list[Candidate]:
    out: list[Candidate] = []
    headers = {"User-Agent": "KryvyiRihAddressBot/2.0 (telegram bot geocoder)"}
    for q in query_variants(parsed)[:2]:
        data = await get_json(
            session,
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": q,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": MAX_PROVIDER_RESULTS,
                "countrycodes": "ua",
                "viewbox": f"{LON_MIN},{LAT_MAX},{LON_MAX},{LAT_MIN}",
                "bounded": 1,
                "accept-language": "uk,ru",
            },
            headers=headers,
        )
        for r in data or []:
            a = r.get("address", {}) or {}
            out.append(
                Candidate(
                    source="osm",
                    lat=float(r["lat"]),
                    lon=float(r["lon"]),
                    display=r.get("display_name", ""),
                    street=a.get("road") or a.get("pedestrian") or a.get("residential") or "",
                    house=a.get("house_number", ""),
                    city=a.get("city") or a.get("town") or a.get("municipality") or "",
                    accuracy=r.get("addresstype") or r.get("type") or "",
                    provider_confidence=min(0.90, 0.55 + float(r.get("importance") or 0.0)),
                )
            )
        if out:
            break
    return out


async def geocode_mapbox(session: aiohttp.ClientSession, parsed: ParsedAddress) -> list[Candidate]:
    if not MAPBOX_TOKEN:
        return []
    params = {
        "address_number": parsed.house,
        "street": parsed.street,
        "place": CITY_UA,
        "country": "ua",
        "language": "uk,ru",
        "limit": MAX_PROVIDER_RESULTS,
        "autocomplete": "false",
        "access_token": MAPBOX_TOKEN,
    }
    if MAPBOX_PERMANENT:
        params["permanent"] = "true"

    data = await get_json(
        session,
        "https://api.mapbox.com/search/geocode/v6/forward",
        params=params,
    )
    out: list[Candidate] = []
    for f in data.get("features", [])[:MAX_PROVIDER_RESULTS]:
        props = f.get("properties", {}) or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        context = props.get("context", {}) or {}
        address_obj = context.get("address", {}) or {}
        street_obj = context.get("street", {}) or {}
        place_obj = context.get("place", {}) or {}
        match = props.get("match_code", {}) or {}
        conf_map = {"exact": 0.97, "high": 0.90, "medium": 0.76, "low": 0.58}
        conf = conf_map.get(str(match.get("confidence", "")).lower(), 0.70)
        out.append(
            Candidate(
                source="mapbox",
                lat=float(coords[1]),
                lon=float(coords[0]),
                display=(props.get("full_address") or props.get("name_preferred") or props.get("name") or ""),
                street=(street_obj.get("name") or props.get("name") or ""),
                house=(address_obj.get("address_number") or props.get("address") or parsed.house),
                city=place_obj.get("name") or "",
                accuracy=(props.get("coordinates", {}) or {}).get("accuracy", "address"),
                provider_confidence=conf,
            )
        )
    return out


async def geocode_visicom(session: aiohttp.ClientSession, parsed: ParsedAddress) -> list[Candidate]:
    if not VISICOM_KEY:
        return []
    out: list[Candidate] = []
    for q in query_variants(parsed)[:2]:
        data = await get_json(
            session,
            "https://api.visicom.ua/data-api/5.0/uk/geocode.json",
            params={
                "text": q,
                "limit": MAX_PROVIDER_RESULTS,
                "country": "UA",
                "key": VISICOM_KEY,
            },
        )
        features = data.get("features", []) if isinstance(data, dict) else []
        for f in features[:MAX_PROVIDER_RESULTS]:
            geo = f.get("geometry", {}) or {}
            coords = geo.get("coordinates") or []
            if len(coords) < 2:
                continue
            p = f.get("properties", {}) or {}
            # Visicom payloads may expose address fields under different nested objects.
            a = p.get("address", {}) if isinstance(p.get("address"), dict) else {}
            name = p.get("name") or p.get("description") or ""
            out.append(
                Candidate(
                    source="visicom",
                    lat=float(coords[1]),
                    lon=float(coords[0]),
                    display=p.get("description") or p.get("name") or json.dumps(p, ensure_ascii=False)[:220],
                    street=a.get("street") or p.get("street") or name,
                    house=str(a.get("house_number") or a.get("number") or p.get("house_number") or ""),
                    city=a.get("city") or p.get("city") or CITY_UA,
                    accuracy=str(p.get("type") or p.get("categories") or "address"),
                    provider_confidence=0.86,
                )
            )
        if out:
            break
    return out


async def safe_provider(name: str, coro) -> list[Candidate]:
    started = time.perf_counter()
    try:
        result = await coro
        log.info("%s: %d candidates in %.2fs", name, len(result), time.perf_counter() - started)
        return result
    except Exception as e:
        log.warning("%s failed in %.2fs: %s", name, time.perf_counter() - started, e)
        return []


async def collect_candidates(parsed: ParsedAddress) -> list[Candidate]:
    learned = get_learned(parsed)
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        results = await asyncio.gather(
            safe_provider("google", geocode_google(session, parsed)),
            safe_provider("visicom", geocode_visicom(session, parsed)),
            safe_provider("mapbox", geocode_mapbox(session, parsed)),
            safe_provider("osm", geocode_osm(session, parsed)),
        )
    candidates = [c for group in results for c in group]
    if learned:
        candidates.append(learned)

    for c in candidates:
        c.score = score_candidate(parsed, c)
    add_consensus_bonus(candidates)
    candidates = [c for c in candidates if c.score > -500 and in_city_bounds(c.lat, c.lon)]
    candidates = dedupe_candidates(candidates)
    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates[:MAX_AI_CANDIDATES]


# ============================================================
# AI VERIFIER
# ============================================================
def extract_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def ai_choose(parsed: ParsedAddress, candidates: list[Candidate]) -> Optional[dict[str, Any]]:
    if not openai_client or not candidates:
        return None

    payload = [c.compact() for c in candidates]
    instruction = f"""
You are the verification controller for a Telegram address geocoder dedicated ONLY to Kryvyi Rih, Ukraine.
The requested address is: street={parsed.street!r}, house={parsed.house!r}.

You are given candidate coordinates returned by Google, Visicom, Mapbox, OpenStreetMap and/or a user-confirmed local memory.
Your job is ONLY to choose among those candidates. NEVER invent coordinates, change coordinates, or return a candidate index outside the list.

Check all of these:
1. exact house number, including suffix/ корпус-like forms;
2. street identity despite Ukrainian/Russian spelling, transliteration and learned/old-name variants;
3. candidate belongs to Kryvyi Rih, Ukraine;
4. house/building/rooftop-level precision is stronger than street/interpolated precision;
5. agreement of independent providers within roughly 20-80 meters is strong evidence;
6. reject obvious outliers even if a provider score is high;
7. user-confirmed local memory is strong evidence, but still reject it if clearly impossible;
8. deterministic score is evidence, not an absolute instruction.

Return ONLY JSON, no markdown:
{{
  "found": true,
  "candidate_index": 0,
  "confidence": 0.0,
  "reason": "short Russian explanation",
  "house_match": true,
  "street_match": true,
  "provider_agreement": 0
}}

If no candidate is safe enough, return found=false and candidate_index=-1.
Candidates:
{json.dumps(payload, ensure_ascii=False)}
""".strip()

    try:
        response = await asyncio.wait_for(
            openai_client.responses.create(
                model=OPENAI_MODEL,
                input=instruction,
            ),
            timeout=AI_TIMEOUT_SECONDS,
        )
        obj = extract_json(response.output_text)
        if not obj:
            return None
        idx = int(obj.get("candidate_index", -1))
        conf = float(obj.get("confidence", 0.0))
        found = bool(obj.get("found", False))
        if found and not (0 <= idx < len(candidates)):
            return None
        if not 0.0 <= conf <= 1.0:
            return None
        obj["candidate_index"] = idx
        obj["confidence"] = conf
        return obj
    except Exception as e:
        log.warning("AI verifier failed: %s", e)
        return None


async def choose_best(parsed: ParsedAddress, candidates: list[Candidate]) -> tuple[Optional[Candidate], float, str]:
    if not candidates:
        return None, 0.0, "Нет кандидатов"

    deterministic = candidates[0]

    # If a user-confirmed result exists with strong confidence, no need to spend AI every time.
    if deterministic.source == "learned" and deterministic.score >= 110:
        return deterministic, deterministic.provider_confidence, "Ранее подтверждено пользователем"

    ai = await ai_choose(parsed, candidates)
    if ai and ai.get("found"):
        idx = ai["candidate_index"]
        chosen = candidates[idx]
        conf = ai["confidence"]
        # Final programmatic guard: AI can select only a real in-bounds candidate.
        if in_city_bounds(chosen.lat, chosen.lon) and conf >= MIN_AI_CONFIDENCE:
            return chosen, conf, str(ai.get("reason") or "ИИ проверил кандидатов")[:300]

    # Deterministic fallback if AI unavailable or uncertain.
    if deterministic.score >= MIN_FINAL_SCORE:
        # Translate score to a rough 0..1 confidence for display only.
        conf = max(0.45, min(0.95, 0.45 + deterministic.score / 180.0))
        return deterministic, conf, "Выбран по совпадению адреса и согласованию геокодеров"

    return None, 0.0, "Недостаточно надежное совпадение"


# ============================================================
# TELEGRAM STATE / CACHE
# ============================================================
# Fast in-memory cache. Never persists raw provider payloads.
memory_cache: dict[str, tuple[float, Candidate, float, str]] = {}
inflight: dict[str, asyncio.Task] = {}
last_result_by_user: dict[int, tuple[ParsedAddress, Candidate]] = {}


def can_train(user_id: int) -> bool:
    return not TRAINER_IDS or user_id in TRAINER_IDS


def cache_allowed(c: Candidate) -> bool:
    # Do not cache a Mapbox-derived result unless permanent storage is explicitly enabled.
    return c.source != "mapbox" or MAPBOX_PERMANENT


def log_decision(parsed: ParsedAddress, c: Candidate, confidence: float, reason: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO decision_log(
                    query_key,original_query,chosen_source,lat,lon,score,
                    ai_confidence,ai_reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    query_key(parsed.street, parsed.house), parsed.original, c.source,
                    c.lat, c.lon, c.score, confidence, reason[:500], int(time.time())
                ),
            )
    except sqlite3.Error as e:
        log.warning("Decision log failed: %s", e)


async def resolve_address(parsed: ParsedAddress) -> tuple[Optional[Candidate], float, str]:
    key = query_key(parsed.street, parsed.house)
    now = time.time()
    cached = memory_cache.get(key)
    if cached and now - cached[0] <= CACHE_TTL_SECONDS:
        return cached[1], cached[2], "Кэш: " + cached[3]

    if key in inflight:
        return await inflight[key]

    async def work():
        candidates = await collect_candidates(parsed)
        chosen, confidence, reason = await choose_best(parsed, candidates)
        if chosen:
            log_decision(parsed, chosen, confidence, reason)
            if cache_allowed(chosen):
                memory_cache[key] = (time.time(), chosen, confidence, reason)
        return chosen, confidence, reason

    task = asyncio.create_task(work())
    inflight[key] = task
    try:
        return await task
    finally:
        inflight.pop(key, None)


# ============================================================
# TELEGRAM HANDLERS
# ============================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришли адрес в Кривом Роге, например: Одоевского 45\n"
        "Можно писать квартиру через / . или -: Лермонтова 25/11 → ищу дом 25.\n"
        "Я проверю Google + Visicom + Mapbox + OpenStreetMap и выберу наиболее надежную точку.\n\n"
        "Обучение: /yes, /no, /teach, /alias"
    )


async def cache_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"В быстром кэше: {len(memory_cache)} адресов")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not can_train(update.effective_user.id):
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM provider_stats ORDER BY provider").fetchall()
        learned = conn.execute("SELECT COUNT(*) n FROM confirmed_addresses").fetchone()["n"]
        aliases = conn.execute("SELECT COUNT(*) n FROM street_aliases").fetchone()["n"]
    text = [f"Подтвержденных адресов: {learned}", f"Обученных алиасов: {aliases}"]
    for r in rows:
        text.append(f"{r['provider']}: ✅ {r['good']} / ❌ {r['bad']}")
    await update.message.reply_text("\n".join(text))


async def yes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not can_train(user.id):
        await update.message.reply_text("У тебя нет доступа к обучению бота.")
        return
    last = last_result_by_user.get(user.id)
    if not last:
        await update.message.reply_text("Сначала отправь адрес и получи точку.")
        return
    parsed, c = last
    save_confirmed(parsed, c)
    await update.message.reply_text("✅ Запомнил этот адрес как правильный. В следующий раз он получит больший приоритет.")


async def no_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not can_train(user.id):
        await update.message.reply_text("У тебя нет доступа к обучению бота.")
        return
    last = last_result_by_user.get(user.id)
    if not last:
        await update.message.reply_text("Сначала отправь адрес и получи точку.")
        return
    parsed, c = last
    save_rejected(parsed, c)
    memory_cache.pop(query_key(parsed.street, parsed.house), None)
    await update.message.reply_text(
        "❌ Пометил результат как неправильный.\n"
        "Чтобы записать правильную точку:\n"
        "/teach Улица 45 | 47.123456 | 33.123456"
    )


async def teach_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not can_train(user.id):
        await update.message.reply_text("У тебя нет доступа к обучению бота.")
        return
    raw = " ".join(context.args).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 3:
        await update.message.reply_text("Формат: /teach Одоевского 45 | 47.123456 | 33.123456")
        return
    parsed = parse_address(parts[0])
    if not parsed:
        await update.message.reply_text("Не понял адрес. Нужны улица и номер дома.")
        return
    try:
        lat, lon = float(parts[1].replace(",", ".")), float(parts[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Координаты должны быть числами.")
        return
    if not in_city_bounds(lat, lon):
        await update.message.reply_text("Эта точка вне допустимой области Кривого Рога. Не сохраняю.")
        return
    c = Candidate(
        source="learned",
        lat=lat,
        lon=lon,
        display=f"{parsed.street} {parsed.house}, {CITY_UA}",
        street=parsed.street,
        house=parsed.house,
        city=CITY_UA,
        accuracy="manual_ground_truth",
        provider_confidence=0.99,
        score=130.0,
    )
    save_confirmed(parsed, c)
    memory_cache[query_key(parsed.street, parsed.house)] = (time.time(), c, 0.99, "Ручное обучение")
    await update.message.reply_location(latitude=lat, longitude=lon)
    await update.message.reply_text("🧠 Сохранил правильную точку как обучающий пример.")


async def alias_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not can_train(user.id):
        await update.message.reply_text("У тебя нет доступа к обучению бота.")
        return
    raw = " ".join(context.args).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 2 or not all(parts):
        await update.message.reply_text("Формат: /alias старое название | новое название")
        return
    save_alias(parts[0], parts[1])
    # Also save reverse relation to make matching symmetric.
    save_alias(parts[1], parts[1])
    await update.message.reply_text(f"🧠 Запомнил: «{parts[0]}» = «{parts[1]}»")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    parsed = parse_address(update.message.text)
    if not parsed:
        return  # Important for groups: ignore normal chat.

    chosen, confidence, reason = await resolve_address(parsed)
    if not chosen:
        await update.message.reply_text(
            f"Не смог надежно подтвердить адрес: {parsed.street} {parsed.house}."
        )
        return

    user = update.effective_user
    if user:
        last_result_by_user[user.id] = (parsed, chosen)

    await update.message.reply_location(latitude=chosen.lat, longitude=chosen.lon)
    maps_url = f"https://www.google.com/maps?q={chosen.lat:.7f},{chosen.lon:.7f}"
    pct = int(round(confidence * 100))
    await update.message.reply_text(
        f"📍 {chosen.display or parsed.original}\n"
        f"Источник: {chosen.source}\n"
        f"Проверка: {pct}%\n"
        f"{reason}\n"
        f"{maps_url}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Telegram handler error", exc_info=context.error)


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not any((GOOGLE_API_KEY, VISICOM_KEY, MAPBOX_TOKEN)):
        missing.append("at least one geocoder key: GOOGLE_API_KEY / VISICOM_KEY / MAPBOX_TOKEN")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def main() -> None:
    validate_config()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cache", cache_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("yes", yes_cmd))
    app.add_handler(CommandHandler("no", no_cmd))
    app.add_handler(CommandHandler("teach", teach_cmd))
    app.add_handler(CommandHandler("alias", alias_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    log.info("Bot started. AI=%s model=%s", bool(openai_client), OPENAI_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
