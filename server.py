import base64
import html
import logging
import os
import re
import threading
import time
import zlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from flask import Flask, Response, request
import json
import requests

from easynews_client import EasynewsClient, EasynewsError, SearchItem


APP = Flask(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)
_CLIENT: Optional[EasynewsClient] = None
_CLIENT_LOCK = threading.Lock()
_CLIENT_LOGIN_TTL = 600  # seconds
_CLIENT_LAST_LOGIN: float = 0.0


def _load_dotenv():
    path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
    except Exception:
        pass


_load_dotenv()

API_KEY = os.environ.get("NEWZNAB_APIKEY", "testkey")
EZ_USER = os.environ.get("EASYNEWS_USER")
EZ_PASS = os.environ.get("EASYNEWS_PASS")


def require_apikey() -> bool:
    key = request.args.get("apikey") or request.headers.get("X-Api-Key")
    return (API_KEY is None) or (key == API_KEY)


def client() -> EasynewsClient:
    if not EZ_USER or not EZ_PASS:
        raise RuntimeError("Set EASYNEWS_USER and EASYNEWS_PASS environment variables")
    global _CLIENT, _CLIENT_LAST_LOGIN
    with _CLIENT_LOCK:
        now = time.time()
        if _CLIENT is None:
            _CLIENT = EasynewsClient(EZ_USER, EZ_PASS)
            _CLIENT.login()
            _CLIENT_LAST_LOGIN = now
        elif now - _CLIENT_LAST_LOGIN > _CLIENT_LOGIN_TTL:
            try:
                _CLIENT.login()
            except EasynewsError:
                _CLIENT = EasynewsClient(EZ_USER, EZ_PASS)
                _CLIENT.login()
            _CLIENT_LAST_LOGIN = time.time()
        return _CLIENT


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def encode_id(item: dict) -> str:
    # Pack info needed to build NZB for a single selection and preserve title for filename
    if item.get("items"):
        payload = {
            "items": [
                {
                    "hash": child.get("hash"),
                    "filename": child.get("filename"),
                    "ext": child.get("ext"),
                    "sig": child.get("sig"),
                    "posted": child.get("posted"),
                    "poster": child.get("poster"),
                    "groups": child.get("groups"),
                    "size": child.get("size"),
                    "title": child.get("title"),
                    "language": child.get("language"),
                }
                for child in item.get("items", [])
            ],
            "title": item.get("title"),
            "posted": item.get("posted"),
            "poster": item.get("poster"),
            "groups": item.get("groups"),
            "size": item.get("size"),
            "language": item.get("language"),
        }
    else:
        payload = {
            "hash": item.get("hash"),
            "filename": item.get("filename"),
            "ext": item.get("ext"),
            "sig": item.get("sig"),
            "title": item.get("title"),
            "posted": item.get("posted"),
            "poster": item.get("poster"),
            "groups": item.get("groups"),
            "size": item.get("size"),
            "language": item.get("language"),
        }
    if item.get("sample"):
        payload["sample"] = True
    packed = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    raw = base64.urlsafe_b64encode(zlib.compress(packed)).decode().rstrip("=")
    return f"z.{raw}"


def decode_id(enc: str) -> dict:
    if enc.startswith("z."):
        pad = "=" * (-(len(enc) - 2) % 4)
        raw = base64.urlsafe_b64decode(enc[2:] + pad)
        return json.loads(zlib.decompress(raw).decode())
    pad = "=" * (-len(enc) % 4)
    raw = base64.urlsafe_b64decode(enc + pad).decode()
    return json.loads(raw)


def to_search_item(d: dict) -> SearchItem:
    return SearchItem(
        id=None,
        hash=d["hash"],
        filename=d["filename"],
        ext=d["ext"],
        sig=d.get("sig"),
        type="VIDEO",
        raw={},
    )


def _posted_epoch(*items: dict) -> Optional[int]:
    epochs: List[int] = []
    for item in items:
        posted = _coerce_datetime(item.get("posted"))
        if posted is not None:
            epochs.append(int(posted.timestamp()))
    if not epochs:
        return None
    return max(epochs)


def _normalize_nzb_dates(content: bytes, posted_epoch: Optional[int]) -> bytes:
    if posted_epoch is None:
        return content.replace(b'date=""', b'date="0"')
    epoch = str(posted_epoch).encode("ascii")
    return re.sub(
        rb'(<file\b[^>]*\bdate=")([^"]*)(")',
        rb"\g<1>" + epoch + rb"\3",
        content,
    )


def to_search_items(d: dict) -> List[SearchItem]:
    entries = d.get("items")
    if isinstance(entries, list) and entries:
        return [to_search_item(entry) for entry in entries]
    return [to_search_item(d)]


_TITLE_PARENS_RE = re.compile(r"\(([^()]*)\)")


def _normalize_title(raw: str) -> str:
    text = html.unescape(raw or "").strip()
    if not text:
        return text
    matches = _TITLE_PARENS_RE.findall(text)
    for candidate in reversed(matches):
        cleaned = candidate.strip()
        if cleaned:
            return cleaned
    return text


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            try:
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


_ALLOWED_VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".ts",
    ".mov",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".flv",
    ".webm",
}
_ALLOWED_EBOOK_EXTENSIONS = {
    ".epub",
    ".mobi",
    ".azw3",
    ".pdf",
}
_ALLOWED_AUDIOBOOK_EXTENSIONS = {
    ".mp3",
    ".m4b",
}
_ALLOWED_BOOK_ARCHIVE_EXTENSIONS = {
    ".rar",
    ".zip",
    ".7z",
}
_ALLOWED_BOOK_EXTENSIONS = (
    _ALLOWED_EBOOK_EXTENSIONS
    | _ALLOWED_AUDIOBOOK_EXTENSIONS
    | _ALLOWED_BOOK_ARCHIVE_EXTENSIONS
)

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "of",
    "in",
    "for",
    "on",
}

_MIN_DURATION_SECONDS = 60
_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)
_QUALITY_RE = re.compile(r"(2160|1440|1080|720|480|360)\s*(p|i)?", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_SEASON_EP_RE = re.compile(
    r"(?:s(?P<season>\d{1,2})e(?P<episode>\d{1,2})|(?<!\d)(?P<season2>\d{1,2})x(?P<episode2>\d{1,2})(?!\d))",
    re.IGNORECASE,
)
# Anime detection patterns
_ANIME_BRACKET_GROUP_RE = re.compile(r"^\[([^\]]+)\]", re.IGNORECASE)

# Known fansub groups for anime detection
_KNOWN_FANSUB_GROUPS = {
    "subsplease",
    "erai-raws",
    "horriblesubs",
    "judas",
    "gjm",
    "commiesubs",
    "commie",
    "animekaizoku",
    "anime time",
    "asenshi",
    "damedesuyo",
    "gg",
    "fff",
    "underwater",
    "ember",
    "kametsu",
    "kawaiika",
    "mezashite",
    "reinforce",
    "senritsu",
    "vivid",
    "coalgirls",
    "utw",
    "thora",
    "ohys-raws",
    "leopard-raws",
    "asw",
    "mtbb",
    "anime-time",
}
_SANITIZE_SYMBOLS_RE = re.compile(r"[\.\-_:\s]+")
_AUDIOBOOK_TRACK_PREFIX_RE = re.compile(
    r"^\s*(?:(?:cd|disc|disk)\s*\d+[\s\._-]*)?(?:track\s*)?\d{1,4}[\s\._-]+",
    re.IGNORECASE,
)
_AUDIOBOOK_TRAILING_PART_RE = re.compile(
    r"[\s\._-]+(?:part|pt|track|chapter|ch)\s*\d{1,4}\s*$",
    re.IGNORECASE,
)
_AUDIOBOOK_TRAILING_NUMBER_RE = re.compile(r"[\s\._-]+\d{1,4}\s*$")
_NON_ALNUM_RE = re.compile(r"[^\w\sÀ-ÿ]")

# Newznab category constants
CATEGORY_MOVIES = 2000
CATEGORY_MOVIES_HD = 2030
CATEGORY_MOVIES_UHD = 2040
CATEGORY_AUDIO = 3000
CATEGORY_AUDIOBOOK_AUDIO = 3030
CATEGORY_TV = 5000
CATEGORY_TV_HD = 5030
CATEGORY_TV_UHD = 5040
CATEGORY_ANIME = 5070  # Anime as TV subcategory
CATEGORY_BOOKS = 7000
CATEGORY_EBOOK = 7010
CATEGORY_EBOOK_STANDARD = 7020
CATEGORY_AUDIOBOOK = 7040


def _parse_duration_seconds(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        if raw <= 0:
            return None
        return int(raw)
    text = str(raw).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    matched = False
    for label, multiplier in (("h", 3600), ("m", 60), ("s", 1)):
        for part in re.findall(rf"(\d+)\s*{label}", text):
            total += int(part) * multiplier
            matched = True
    if matched:
        return total
    if ":" in text:
        try:
            pieces = [int(p) for p in text.split(":")]
            if len(pieces) == 3:
                h, m, s = pieces
            elif len(pieces) == 2:
                h = 0
                m, s = pieces
            else:
                return None
            return h * 3600 + m * 60 + s
        except ValueError:
            return None
    return None


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    normalized = _TOKEN_SPLIT_RE.sub(" ", text.lower())
    tokens = [
        tok for tok in normalized.split() if len(tok) > 1 and tok not in _STOPWORDS
    ]
    return tokens


def _sanitize_phrase(text: str) -> str:
    if not text:
        return ""
    working = text.replace("&", " and ")
    working = _SANITIZE_SYMBOLS_RE.sub(" ", working)
    working = _NON_ALNUM_RE.sub("", working)
    return working.lower().strip()


def _normalize_ext(ext: Optional[str]) -> str:
    if not ext:
        return ""
    normalized = ext.strip().lower()
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def _split_categories(cat_param: str) -> Set[str]:
    return {part.strip() for part in cat_param.split(",") if part.strip()}


def _requested_book_kinds(cat_param: str) -> Set[str]:
    requested = _split_categories(cat_param)
    kinds: Set[str] = set()
    if requested & {"3000", "3030"}:
        kinds.add("audiobook")
    if "7000" in requested:
        kinds.update({"ebook", "audiobook"})
    if requested & {"7010", "7020", "8010"}:
        kinds.add("ebook")
    if "7040" in requested:
        kinds.add("audiobook")
    return kinds


def _preferred_book_category(cat_param: str) -> Dict[str, int]:
    requested = _split_categories(cat_param)
    preferred: Dict[str, int] = {}
    if requested & {"3000", "3030"}:
        preferred["audiobook"] = CATEGORY_AUDIOBOOK_AUDIO
    elif requested & {"7000", "7040"}:
        preferred["audiobook"] = CATEGORY_AUDIOBOOK
    if requested & {"7020", "8010"}:
        preferred["ebook"] = CATEGORY_EBOOK_STANDARD
    elif requested & {"7000", "7010"}:
        preferred["ebook"] = CATEGORY_EBOOK
    return preferred


def _joined_text(*values: Optional[Any]) -> str:
    parts: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(part) for part in value if part is not None)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def _extract_group_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return _joined_text(
        item.get("group"),
        item.get("groups"),
        item.get("grp"),
        item.get("newsgroup"),
        item.get("newsgroups"),
        item.get("category"),
        item.get("cat"),
    )


def _detect_book_category(title: str, ext: Optional[str], group_text: str = "") -> Optional[int]:
    normalized_ext = _normalize_ext(ext)
    haystack = _joined_text(title, group_text)
    if normalized_ext in _ALLOWED_AUDIOBOOK_EXTENSIONS:
        return CATEGORY_AUDIOBOOK
    if any(marker in haystack for marker in ("audiobook", "audio book", "spoken word")):
        return CATEGORY_AUDIOBOOK
    if normalized_ext in _ALLOWED_EBOOK_EXTENSIONS:
        return CATEGORY_EBOOK
    if any(marker in haystack for marker in ("ebook", "e-book", "ebooks", "e-books")):
        return CATEGORY_EBOOK
    if "book" in haystack and normalized_ext in _ALLOWED_BOOK_EXTENSIONS:
        return CATEGORY_BOOKS
    return None


def _detect_audiobook_quality(title: str, ext: Optional[str], group_text: str = "") -> str:
    normalized_ext = _normalize_ext(ext)
    haystack = _joined_text(title, group_text)
    if normalized_ext == ".m4b" or "m4b" in haystack:
        return "M4B"
    if normalized_ext == ".mp3" or "mp3" in haystack:
        return "MP3"
    return "MP3"


def _detect_language(title: str, group_text: str = "") -> Optional[str]:
    haystack = _joined_text(title, group_text)
    if any(
        marker in haystack
        for marker in (
            "german",
            "deutsch",
            "deutsche",
            "hoerbuch",
            "hoerbuecher",
            "hörbuch",
            "hörbücher",
            "ungekurzt",
            "ungekürzt",
            "mädchen",
            "maedchen",
        )
    ):
        return "de"
    if any(marker in haystack for marker in ("english", "englisch")):
        return "en"
    return None


def _audiobook_group_title(title: str) -> str:
    working = re.sub(r"\.(mp3|m4b|rar|zip|7z)$", "", title or "", flags=re.IGNORECASE)
    previous = None
    while previous != working:
        previous = working
        working = _AUDIOBOOK_TRACK_PREFIX_RE.sub("", working)
    working = _AUDIOBOOK_TRAILING_PART_RE.sub("", working)
    working = _AUDIOBOOK_TRAILING_NUMBER_RE.sub("", working)
    working = _SANITIZE_SYMBOLS_RE.sub(" ", working)
    working = re.sub(r"\s+", " ", working).strip()
    return working or title


def _audiobook_group_key(title: str) -> str:
    return _sanitize_phrase(_audiobook_group_title(title))


def _normalize_creator_initials(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        initials = [part for part in match.group(1).split() if part]
        surname = match.group(2)
        return f"{'.'.join(initials)}. {surname}"

    return re.sub(r"\b((?:[A-Z]\s+){2,})([A-Z][a-z]+)\b", repl, text)


def _space_creator_initials(text: str) -> str:
    spaced = re.sub(r"\b([A-Z])\.(?=\s*[A-Z])", r"\1 ", text)
    return re.sub(r"\s+", " ", spaced).strip()


def _clean_audiobook_query_title(query: str) -> str:
    working = query or ""
    working = re.sub(
        r"\b(?:mp3|m4b|audiobook|audio\s*book)\b",
        " ",
        working,
        flags=re.IGNORECASE,
    )
    working = re.sub(
        r"\b(?:book|volume|vol|part|pt)\s*\d+\b",
        " ",
        working,
        flags=re.IGNORECASE,
    )
    working = re.sub(r"[_:\s]+", " ", working)
    working = re.sub(r"\s+", " ", working).strip()
    working = _normalize_creator_initials(working)
    return working


def _book_search_queries(raw_query: str, args: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []

    def add(value: Optional[Any]) -> None:
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    add(raw_query)
    clean_query = _clean_audiobook_query_title(raw_query)
    add(clean_query)
    add(_space_creator_initials(clean_query))

    author = (
        args.get("author")
        or args.get("authorname")
        or args.get("authorName")
        or args.get("artist")
    )
    title = (
        args.get("book")
        or args.get("booktitle")
        or args.get("bookTitle")
        or args.get("title")
    )
    series = args.get("series") or args.get("seriesTitle") or args.get("seriesname")
    number = args.get("seriesnumber") or args.get("seriesNumber") or args.get("position")
    if author and title:
        add(f"{author} {title}")
        add(f"{title} {author}")
    if author and series:
        add(f"{author} {series}")
    if author and series and number:
        add(f"{author} {series} {number}")
        try:
            add(f"{author} {series} {int(str(number)):02d}")
        except ValueError:
            pass

    return candidates


def _group_audiobook_tracks(items: List[dict], query_title: str = "") -> List[dict]:
    grouped: Dict[str, List[dict]] = {}
    passthrough: List[dict] = []
    clean_query_title = _clean_audiobook_query_title(query_title)
    for item in items:
        if _detect_book_category(
            item.get("title", ""),
            item.get("ext"),
            item.get("groups", ""),
        ) != CATEGORY_AUDIOBOOK:
            passthrough.append(item)
            continue
        key = _audiobook_group_key(item.get("title", ""))
        if not key:
            passthrough.append(item)
            continue
        grouped.setdefault(key, []).append(item)

    out = list(passthrough)
    for group_items in grouped.values():
        group_items.sort(key=lambda item: item.get("title", ""))
        first = group_items[0]
        total_size = sum(int(item.get("size") or 0) for item in group_items)
        title = clean_query_title or _audiobook_group_title(first.get("title", "Audiobook"))
        quality = first.get("quality") or _detect_audiobook_quality(
            first.get("title", ""),
            first.get("ext"),
            first.get("groups", ""),
        )
        language = first.get("language") or _detect_language(
            first.get("title", ""),
            first.get("groups", ""),
        )
        out.append(
            {
                "hash": first.get("hash"),
                "filename": first.get("filename"),
                "ext": first.get("ext"),
                "sig": first.get("sig"),
                "size": total_size,
                "title": f"{title} {quality} ({len(group_items)} tracks)"
                if len(group_items) > 1
                else f"{title} {quality}",
                "poster": first.get("poster"),
                "posted": first.get("posted"),
                "duration": None,
                "duration_hms": None,
                "quality": quality,
                "language": language,
                "thumbnail": first.get("thumbnail"),
                "groups": first.get("groups"),
                "category_override": first.get("category_override"),
                "year": first.get("year"),
                "season": None,
                "episode": None,
                "items": group_items,
            }
        )
    out.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
    return out


def _search_profile(t: str, cat_param: str) -> Dict[str, Any]:
    book_kinds = _requested_book_kinds(cat_param)
    if book_kinds:
        allowed_extensions: Set[str] = set()
        file_types: List[str] = []
        if "ebook" in book_kinds:
            allowed_extensions.update(
                _ALLOWED_EBOOK_EXTENSIONS | _ALLOWED_BOOK_ARCHIVE_EXTENSIONS
            )
        if "audiobook" in book_kinds:
            allowed_extensions.update(
                _ALLOWED_AUDIOBOOK_EXTENSIONS | _ALLOWED_BOOK_ARCHIVE_EXTENSIONS
            )
            # EasyNews audiobook posts may be header/newsgroup based, and
            # fty[]=AUDIO is not proven to cover all audiobook releases.
        return {
            "kind": "books",
            "book_kinds": book_kinds,
            "preferred_categories": _preferred_book_category(cat_param),
            "allowed_extensions": allowed_extensions or _ALLOWED_BOOK_EXTENSIONS,
            "allowed_file_types": None,
            "file_types": file_types,
            "enforce_min_duration": False,
            "default_min_size_mb": 0,
        }
    return {
        "kind": "video",
        "book_kinds": set(),
        "preferred_categories": {},
        "allowed_extensions": _ALLOWED_VIDEO_EXTENSIONS,
        "allowed_file_types": {"VIDEO"},
        "file_types": ["VIDEO"],
        "enforce_min_duration": True,
        "default_min_size_mb": 100,
    }


def _is_flagged_item(
    item: Any,
    ext: str,
    duration_seconds: Optional[int],
    allowed_extensions: Optional[Set[str]] = None,
    allowed_file_types: Optional[Set[str]] = None,
    enforce_min_duration: bool = True,
) -> bool:
    passwd = False
    virus = False
    file_type = ""
    if isinstance(item, dict):
        passwd = bool(item.get("passwd") or item.get("password"))
        virus = bool(item.get("virus"))
        file_type = str(item.get("type") or item.get("file_type") or "").upper()
    if passwd or virus:
        return True
    if allowed_file_types is not None and file_type and file_type not in allowed_file_types:
        return True
    normalized_ext = _normalize_ext(ext)
    if allowed_extensions is not None and normalized_ext not in allowed_extensions:
        return True
    if (
        enforce_min_duration
        and duration_seconds is not None
        and duration_seconds < _MIN_DURATION_SECONDS
    ):
        return True
    return False


def _format_duration(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds <= 0:
        return None
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _extract_quality(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if "4k" in lowered:
            return "2160p"
        match = _QUALITY_RE.search(lowered)
        if match:
            value = match.group(1)
            suffix = match.group(2) or "p"
            return f"{value}{suffix.lower()}"
        if "uhd" in lowered:
            return "2160p"
        if "fhd" in lowered:
            return "1080p"
    return None


def _build_thumbnail_url(
    base: Optional[str], hash_id: Optional[str], slug: Optional[str]
) -> Optional[str]:
    if not base or not hash_id:
        return None
    base = base.rstrip("/") + "/"
    prefix = hash_id[:3]
    safe_slug = quote((slug or hash_id).replace("/", "_"))
    return f"{base}{prefix}/pr-{hash_id}.jpg/th-{safe_slug}.jpg"


def _easynews_details_url(title: str) -> str:
    return (
        "https://members.easynews.com/2.0/search/solr-search/"
        "?fly=2&sb=1&pno=1&pby=250&u=1&chxu=1&chxgx=1&st=basic"
        f"&gps={quote(title)}&vv=1&safeO=0&s1=relevance&s1d=-"
    )


def _format_size(size: Any) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        value = 0
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _details_html(release: dict) -> str:
    title = release.get("title") or release.get("filename") or "EasyNews release"
    entries = release.get("items")
    if not isinstance(entries, list) or not entries:
        entries = [release]
    posted_dt = _coerce_datetime(release.get("posted"))
    posted = posted_dt.strftime("%Y-%m-%d %H:%M:%S UTC") if posted_dt else ""
    language = release.get("language") or _detect_language(
        str(title),
        release.get("groups") or entries[0].get("groups") or "",
    )
    raw_groups = release.get("groups") or entries[0].get("groups") or ""
    group_rows = "".join(
        f"<li>{html.escape(group.strip())}</li>"
        for group in str(raw_groups).split(",")
        if group.strip()
    )
    file_rows = []
    for idx, entry in enumerate(entries, 1):
        entry_title = entry.get("title") or (
            f"{entry.get('filename', '')}{entry.get('ext', '')}"
        )
        entry_posted_dt = _coerce_datetime(entry.get("posted"))
        entry_posted = (
            entry_posted_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            if entry_posted_dt
            else ""
        )
        entry_language = entry.get("language") or _detect_language(
            str(entry_title or ""),
            entry.get("groups") or raw_groups,
        )
        file_rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{html.escape(str(entry_title or ''))}</td>"
            f"<td>{html.escape(str(entry.get('ext') or ''))}</td>"
            f"<td>{html.escape(_format_size(entry.get('size')))}</td>"
            f"<td>{html.escape(entry_posted)}</td>"
            f"<td>{html.escape(str(entry.get('poster') or ''))}</td>"
            f"<td>{html.escape(str(entry_language or ''))}</td>"
            "</tr>"
        )
    easynews_url = _easynews_details_url(str(title))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(str(title))}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#222}"
        "h1{font-size:22px;margin:0 0 16px}"
        ".meta{display:grid;grid-template-columns:max-content 1fr;gap:8px 16px;margin-bottom:20px}"
        ".label{font-weight:600;color:#555}"
        "table{border-collapse:collapse;width:100%;font-size:14px}"
        "th,td{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}"
        "th{background:#f5f5f5}"
        "ul{margin-top:0;padding-left:20px}"
        "a{color:#1769aa}"
        "</style></head><body>"
        f"<h1>{html.escape(str(title))}</h1>"
        "<div class='meta'>"
        "<div class='label'>Indexer</div><div>EasyNews Bridge</div>"
        f"<div class='label'>Files</div><div>{len(entries)}</div>"
        f"<div class='label'>Size</div><div>{html.escape(_format_size(release.get('size')))}</div>"
        f"<div class='label'>Posted</div><div>{html.escape(posted)}</div>"
        f"<div class='label'>Poster</div><div>{html.escape(str(release.get('poster') or ''))}</div>"
        f"<div class='label'>Language</div><div>{html.escape(str(language or ''))}</div>"
        f"<div class='label'>Groups</div><div><ul>{group_rows}</ul></div>"
        f"<div class='label'>EasyNews search</div><div><a href='{html.escape(easynews_url)}'>Open raw EasyNews search</a></div>"
        "</div>"
        "<table><thead><tr><th>#</th><th>File</th><th>Ext</th><th>Size</th><th>Posted</th><th>Poster</th><th>Language</th></tr></thead>"
        f"<tbody>{''.join(file_rows)}</tbody></table>"
        "</body></html>"
    )


def _extract_release_markers(
    text: str, quality_hint: Optional[str] = None
) -> Dict[str, Optional[Any]]:
    info: Dict[str, Optional[Any]] = {}
    if not text:
        return info
    season_match = _SEASON_EP_RE.search(text)
    if season_match:
        season = season_match.group("season") or season_match.group("season2")
        episode = season_match.group("episode") or season_match.group("episode2")
        if season:
            info["season"] = int(season)
        if episode:
            info["episode"] = int(episode)
    year_match = _YEAR_RE.search(text)
    if year_match:
        info["year"] = int(year_match.group(0))
    quality = quality_hint or _extract_quality(text)
    if quality:
        info["quality"] = quality
    return info


def _detect_anime(title: str) -> bool:
    """
    Detect anime releases using fansub indicators.

    Requirements (all must be true):
    1. Bracketed release group at start: [Group]
    2. Group must be in known fansub whitelist
    3. Episode-only numbering (- 01, Ep 01, Episode 01)
    4. NO traditional TV patterns (S01E01, 1x02)

    Returns: True if anime, False otherwise
    """
    # Guard: Exclude if traditional TV patterns exist
    if _SEASON_EP_RE.search(title):
        return False

    bracket_match = _ANIME_BRACKET_GROUP_RE.search(title)
    if not bracket_match:
        return False

    # This prevents [BBC], [PBS], [REPACK] from being detected as anime
    group_name = bracket_match.group(1).strip().lower()
    if group_name not in _KNOWN_FANSUB_GROUPS:
        return False

    # Remove bracketed group to avoid false matches
    title_without_group = title[bracket_match.end() :].strip()

    # Episode patterns: "- 01", "Ep 01", "Episode 01", "- 01v2", "-1090."
    # Support up to 4 digits for long-running anime (e.g., One Piece episode 1045)
    episode_patterns = [
        r"[\s\-_]+\d{1,4}(?:\s*v\d+)?[\s\-_\.\(\[]",  # " - 1090 " or "-1045." or "- 01v2 -"
        r"[\s\-_]Ep?\.?\s*\d{1,4}",  # "- Ep01" or " E1045"
        r"[\s\-_]Episode\s*\d{1,4}",  # "- Episode 01" or "- Episode 1045"
    ]

    has_episode = any(
        re.search(pattern, title_without_group, re.IGNORECASE)
        for pattern in episode_patterns
    )

    return has_episode


def _detect_category(title: str, metadata: Dict[str, Optional[Any]]) -> int:
    """
    Detect Newznab category based on filename and extracted metadata.

    Detection logic:
    1. Anime: bracketed fansub groups + episode-only patterns (PRIORITY)
    2. TV shows: presence of season/episode patterns (SxxExx or xxyy)
    3. Movies: presence of year, absence of TV patterns
    4. Resolution subcategories: 720p+ = HD, 2160p/4K/UHD = UHD (TV/Movies only)
    5. Default to generic categories if uncertain

    Args:
        title: The filename/title to analyze
        metadata: Dict with season, episode, year, quality keys

    Returns:
        Newznab category ID (int)
    """
    book_category = _detect_book_category(
        title,
        metadata.get("ext"),
        str(metadata.get("groups") or ""),
    )
    if book_category is not None:
        return book_category

    # Check for anime FIRST (priority detection)
    if _detect_anime(title):
        return CATEGORY_ANIME  # 5070 - No quality subcategories

    season = metadata.get("season")
    episode = metadata.get("episode")
    quality = metadata.get("quality")
    year = metadata.get("year")

    quality_lower = (quality or "").lower()
    is_uhd = False
    is_hd = False

    if quality_lower:
        # UHD: 2160p or higher, or contains 4k/uhd keywords
        if "2160" in quality_lower or "4k" in quality_lower or "uhd" in quality_lower:
            is_uhd = True
        # HD: 720p or 1080p
        elif "720" in quality_lower or "1080" in quality_lower:
            is_hd = True

    has_tv_pattern = season is not None or episode is not None

    if not has_tv_pattern:
        if _SEASON_EP_RE.search(title):
            has_tv_pattern = True

    if has_tv_pattern:
        if is_uhd:
            return CATEGORY_TV_UHD  # 5040
        elif is_hd:
            return CATEGORY_TV_HD  # 5030
        else:
            return CATEGORY_TV  # 5000

    # Movies typically have a year but no season/episode
    if year or (not has_tv_pattern):
        if is_uhd:
            return CATEGORY_MOVIES_UHD  # 2040
        elif is_hd:
            return CATEGORY_MOVIES_HD  # 2030
        else:
            return CATEGORY_MOVIES  # 2000

    # Default fallback to generic Movies
    return CATEGORY_MOVIES  # 2000


def _matches_strict(title: str, strict_phrase: Optional[str]) -> bool:
    if not strict_phrase:
        return True
    candidate = _sanitize_phrase(title)
    if not candidate:
        return False
    if candidate == strict_phrase:
        return True
    candidate_tokens = candidate.split()
    phrase_tokens = strict_phrase.split()
    if not phrase_tokens:
        return True
    for idx in range(0, max(1, len(candidate_tokens) - len(phrase_tokens) + 1)):
        if candidate_tokens[idx : idx + len(phrase_tokens)] == phrase_tokens:
            return True
    return False


def filter_and_map(
    json_data: dict,
    min_bytes: int,
    query_tokens: Optional[List[str]] = None,
    query_meta: Optional[Dict[str, Optional[Any]]] = None,
    strict_phrase: Optional[str] = None,
    strict_match: bool = False,
    allowed_extensions: Optional[Set[str]] = None,
    allowed_file_types: Optional[Set[str]] = None,
    enforce_min_duration: bool = True,
    book_kinds: Optional[Set[str]] = None,
    preferred_categories: Optional[Dict[str, int]] = None,
) -> List[dict]:
    token_set: Set[str] = set(query_tokens or [])
    wanted_book_kinds = set(book_kinds or [])
    category_preferences = preferred_categories or {}
    thumb_base = json_data.get("thumbURL") or json_data.get("thumbUrl")
    out: List[dict] = []
    for it in json_data.get("data", []):
        hash_id: Optional[str] = None
        subject: Optional[str] = None
        filename_no_ext: Optional[str] = None
        ext: Optional[str] = None
        size: Any = 0
        poster: Optional[str] = None
        posted_raw: Any = None
        sig: Optional[str] = None
        display_fn: Optional[str] = None
        extension_field: Optional[str] = None
        duration_raw: Any = None
        fullres: Optional[str] = None
        group_text = ""

        if isinstance(it, list):
            if len(it) >= 12:
                hash_id = it[0]
                subject = it[6]
                filename_no_ext = it[10]
                ext = it[11]
            if len(it) > 7:
                poster = it[7]
            if len(it) > 8:
                posted_raw = it[8]
            if len(it) > 14:
                duration_raw = it[14]
        elif isinstance(it, dict):
            hash_id = it.get("hash") or it.get("0") or it.get("id")
            subject = it.get("subject") or it.get("6")
            filename_no_ext = it.get("filename") or it.get("10")
            ext = it.get("ext") or it.get("11")
            size = it.get("size", 0)
            poster = it.get("poster") or it.get("7")
            posted_raw = it.get("timestamp") or it.get("ts") or it.get("dtime") or it.get("date") or it.get("12")
            sig = it.get("sig")
            display_fn = it.get("fn") or it.get("filename")
            extension_field = it.get("extension") or it.get("ext")
            duration_raw = it.get("14") or it.get("duration") or it.get("len")
            fullres = it.get("fullres") or it.get("resolution")
            group_text = _extract_group_text(it)

        if not hash_id or not ext:
            continue

        filename_no_ext = filename_no_ext or ""
        ext = _normalize_ext(ext or "")
        if extension_field and not ext:
            ext = _normalize_ext(extension_field)

        # Try to use numeric size if present; otherwise skip (can't verify <100MB rule)
        if not isinstance(size, int):
            try:
                size = int(size)
            except Exception:
                size = 0

        if size < min_bytes:
            continue

        duration_seconds = _parse_duration_seconds(duration_raw)

        if _is_flagged_item(
            it,
            ext,
            duration_seconds,
            allowed_extensions=allowed_extensions,
            allowed_file_types=allowed_file_types,
            enforce_min_duration=enforce_min_duration,
        ):
            continue

        title: Optional[str] = None
        if display_fn:
            cleaned = display_fn.strip()
            if cleaned:
                normalized = cleaned.replace(" - ", "-")
                parts = [segment for segment in normalized.split(" ") if segment]
                sanitized = ".".join(parts)
                ext_component = extension_field or ext or ""
                if ext_component and not ext_component.startswith("."):
                    ext_component = f".{ext_component}"
                title = f"{sanitized}{ext_component}" if ext_component else sanitized

        if not title:
            fallback = subject or f"{filename_no_ext}{ext}"
            title = _normalize_title(fallback)

        book_category = _detect_book_category(title, ext, group_text)
        if wanted_book_kinds:
            if book_category == CATEGORY_AUDIOBOOK:
                item_book_kind = "audiobook"
            elif book_category in {CATEGORY_EBOOK, CATEGORY_BOOKS}:
                item_book_kind = "ebook"
            else:
                continue
            if item_book_kind not in wanted_book_kinds:
                continue
            category_override = category_preferences.get(item_book_kind)
        else:
            category_override = None

        quality = _extract_quality(title, fullres)
        title_meta = _extract_release_markers(title, quality)
        if not quality and title_meta.get("quality"):
            quality = title_meta.get("quality")

        if strict_match and not _matches_strict(title, strict_phrase):
            continue

        if query_meta:
            q_year = query_meta.get("year")
            q_season = query_meta.get("season")
            q_episode = query_meta.get("episode")
            q_quality = query_meta.get("quality")
            t_year = title_meta.get("year")
            t_season = title_meta.get("season")
            t_episode = title_meta.get("episode")
            t_quality = quality or title_meta.get("quality")
            if q_year and t_year and q_year != t_year:
                continue
            if q_season and t_season and q_season != t_season:
                continue
            if q_episode and t_episode and q_episode != t_episode:
                continue
            if q_quality and t_quality and q_quality.lower() != t_quality.lower():
                continue

        if token_set:
            title_tokens = set(_tokenize(title))
            if not title_tokens or not token_set.issubset(title_tokens):
                continue

        duration_formatted = _format_duration(duration_seconds)
        thumbnail_url = _build_thumbnail_url(thumb_base, hash_id, filename_no_ext)
        year = title_meta.get("year")
        language = _detect_language(title, group_text)

        out.append(
            {
                "hash": hash_id,
                "filename": filename_no_ext,
                "ext": ext,
                "sig": sig,
                "size": size,
                "title": title,
                "poster": poster,
                "posted": posted_raw,
                "duration": duration_seconds,
                "duration_hms": duration_formatted,
                "quality": quality,
                "language": language,
                "thumbnail": thumbnail_url,
                "groups": group_text,
                "category_override": category_override,
                "year": year,
                "season": title_meta.get("season"),
                "episode": title_meta.get("episode"),
            }
        )
    return out


@APP.route("/api")
def api():
    request_attrs = dict(request.args)
    if "apikey" in request_attrs:
        request_attrs["apikey"] = "<redacted>"
    logger.info(
        "NEWZNAB request t=%s q=%s cat=%s attrs=%s",
        request.args.get("t"),
        request.args.get("q"),
        request.args.get("cat"),
        request_attrs,
    )

    if not require_apikey():
        return Response("Unauthorized", status=401)

    t = request.args.get("t", "caps")
    if t == "caps":
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<caps>"
            '<server version="0.1" title="Easynews Bridge"/>'
            '<limits max="100" default="100"/>'
            '<registration available="no" open="no"/>'
            "<searching>"
            '<search available="yes" supportedParams="q"/>'
            '<movie-search available="yes" supportedParams="q,year"/>'
            '<tv-search available="yes" supportedParams="q,season,ep"/>'
            "</searching>"
            "<categories>"
            '<category id="3000" name="Audio">'
            '<subcat id="3030" name="Audio/Audiobook"/>'
            "</category>"
            '<category id="2000" name="Movies">'
            '<subcat id="2030" name="Movies/HD"/>'
            '<subcat id="2040" name="Movies/UHD"/>'
            "</category>"
            '<category id="5000" name="TV">'
            '<subcat id="5030" name="TV/HD"/>'
            '<subcat id="5040" name="TV/UHD"/>'
            '<subcat id="5070" name="TV/Anime"/>'
            "</category>"
            '<category id="7000" name="Books">'
            '<subcat id="7010" name="Books/EBook"/>'
            '<subcat id="7020" name="Books/EBook"/>'
            '<subcat id="7040" name="Books/Audiobook"/>'
            "</category>"
            "</categories>"
            "</caps>"
        )
        return Response(xml, mimetype="application/xml")

    if t in ("search", "movie", "tvsearch"):
        base_query = (request.args.get("q") or "").strip()
        cat_param = request.args.get("cat") or ""
        season_param = request.args.get("season") or request.args.get("seasonnum")
        episode_param = (
            request.args.get("ep")
            or request.args.get("epnum")
            or request.args.get("episode")
        )
        year_param = request.args.get("year") or request.args.get("yr")
        season_int = _as_int(season_param)
        episode_int = _as_int(episode_param)
        year_int = _as_int(year_param)

        search_components: List[str] = []
        if base_query:
            search_components.append(base_query)

        if t == "movie":
            if year_int and str(year_int) not in base_query:
                search_components.append(str(year_int))
        elif t == "tvsearch":
            if season_int is not None and episode_int is not None:
                search_components.append(f"S{season_int:02}E{episode_int:02}")
            elif season_int is not None:
                search_components.append(f"S{season_int:02}")
            if year_int and str(year_int) not in base_query:
                search_components.append(str(year_int))

        search_label = " ".join(part for part in search_components if part).strip()
        raw_query = search_label or base_query
        q = raw_query.strip()
        fallback_query = False
        if (
            not q or q.lower() == "test"
        ):  # allow Prowlarr validation calls to receive data
            # Check if TV/Anime categories are requested
            tv_categories = {"5000", "5030", "5040"}
            anime_categories = {"5070"}
            requested_categories = _split_categories(cat_param)
            book_kinds = _requested_book_kinds(cat_param)
            wants_tv = t == "tvsearch" or bool(requested_categories & tv_categories)
            wants_anime = bool(requested_categories & anime_categories)
            # Use appropriate fallback query
            if "audiobook" in book_kinds:
                q = "audiobook"
            elif "ebook" in book_kinds:
                q = "ebook"
            elif wants_anime:
                q = "one piece"  # Anime fallback
            elif wants_tv:
                q = "breaking bad"  # TV fallback
            else:
                q = "matrix"  # Movie fallback
            fallback_query = True
        query_tokens = _tokenize(raw_query)
        query_meta = _extract_release_markers(raw_query)
        if year_int:
            query_meta["year"] = year_int
        if season_int is not None:
            query_meta["season"] = season_int
        if episode_int is not None:
            query_meta["episode"] = episode_int
        strict_param = request.args.get("strict")
        strict_requested = t in {"movie", "tvsearch"}
        if strict_param is not None:
            strict_requested = strict_param.strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        strict_phrase = _sanitize_phrase(raw_query) if strict_requested else None
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
        profile = _search_profile(t, cat_param)
        min_size_param = request.args.get("minsize")
        min_size_mb = profile["default_min_size_mb"]
        if min_size_param:
            try:
                min_size_mb = max(profile["default_min_size_mb"], int(min_size_param))
            except ValueError:
                min_size_mb = profile["default_min_size_mb"]
        min_bytes = min_size_mb * 1024 * 1024
        filter_query_tokens = (
            None if profile["kind"] == "books" else query_tokens
        )

        if fallback_query:
            # Check if TV/Anime categories are requested
            tv_categories = {"5000", "5030", "5040"}
            anime_categories = {"5070"}
            requested_categories = _split_categories(cat_param)
            book_kinds = _requested_book_kinds(cat_param)
            wants_tv = t == "tvsearch" or bool(requested_categories & tv_categories)
            wants_anime = bool(requested_categories & anime_categories)

            if "audiobook" in book_kinds:
                items = [
                    {
                        "hash": "SAMPLEHASH_AUDIOBOOK123",
                        "filename": "sample.audiobook",
                        "ext": ".m4b",
                        "sig": None,
                        "size": 50 * 1024 * 1024,
                        "title": "Sample Audiobook.m4b",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                        "groups": "alt.binaries.audiobooks",
                        "category_override": profile["preferred_categories"].get(
                            "audiobook"
                        ),
                    }
                ]
            elif "ebook" in book_kinds:
                items = [
                    {
                        "hash": "SAMPLEHASH_EBOOK123",
                        "filename": "sample.ebook",
                        "ext": ".epub",
                        "sig": None,
                        "size": 2 * 1024 * 1024,
                        "title": "Sample EBook.epub",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                        "groups": "alt.binaries.e-book",
                        "category_override": profile["preferred_categories"].get(
                            "ebook"
                        ),
                    }
                ]
            elif wants_anime:
                # Anime-appropriate fallback
                items = [
                    {
                        "hash": "SAMPLEHASH_ANIME123",
                        "filename": "sample.anime.series.01.720p.mkv",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 350 * 1024 * 1024,
                        "title": "[SampleSubs] Sample Anime Series - 01 [720p]",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
            elif wants_tv:
                # TV-appropriate fallback for Sonarr
                items = [
                    {
                        "hash": "SAMPLEHASH_TV123456",
                        "filename": "sample.tv.show.s01e01.1080p.mkv",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 800 * 1024 * 1024,
                        "title": "Sample TV Show S01E01 1080p",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
            else:
                # Movie fallback for Radarr
                items = [
                    {
                        "hash": "SAMPLEHASH1234567890",
                        "filename": "sample.matrix.clip",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 700 * 1024 * 1024,
                        "title": "Sample Matrix Clip",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
        else:
            c = client()
            search_queries = (
                _book_search_queries(q, dict(request.args))
                if profile["kind"] == "books"
                else [q]
            )
            items = []
            data = {"data": []}
            used_query = q
            for search_query in search_queries:
                # aim for maximum results per page
                data = c.search(
                    query=search_query,
                    file_type=None,
                    file_types=profile["file_types"],
                    per_page=250,
                    sort_field="relevance",
                    sort_dir="-",
                )
                if fallback_query:
                    mapped_items = filter_and_map(data, min_bytes=min_bytes)
                else:
                    mapped_items = filter_and_map(
                        data,
                        min_bytes=min_bytes,
                        query_tokens=filter_query_tokens,
                        query_meta=query_meta,
                        strict_phrase=strict_phrase,
                        strict_match=strict_requested,
                        allowed_extensions=profile["allowed_extensions"],
                        allowed_file_types=profile["allowed_file_types"],
                        enforce_min_duration=profile["enforce_min_duration"],
                        book_kinds=profile["book_kinds"],
                        preferred_categories=profile["preferred_categories"],
                    )
                logger.info(
                    "Mapped Easynews results: query=%r search_query=%r category_profile=%s returned=%s mapped=%s",
                    q,
                    search_query,
                    profile["kind"],
                    len(data.get("data", [])),
                    len(mapped_items),
                )
                if mapped_items or profile["kind"] != "books":
                    items = mapped_items
                    used_query = search_query
                    break
            if "audiobook" in profile["book_kinds"]:
                before_grouping = len(items)
                items = _group_audiobook_tracks(items, raw_query)
                logger.info(
                    "Grouped audiobook results: search_query=%r before=%s after=%s",
                    used_query,
                    before_grouping,
                    len(items),
                )

        # Trim by limit (handles fallback and real queries)
        items = items[offset : offset + limit]

        display_q = raw_query if raw_query else q
        chan_title = f"Results for {display_q}"
        now_dt = datetime.now(timezone.utc)
        channel_pub = now_dt.strftime("%a, %d %b %Y %H:%M:%S %z")

        header = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            "<channel>"
            f"<title>{xml_escape(chan_title)}</title>"
            f"<description>{xml_escape(chan_title)}</description>"
            f"<link>{request.url_root.rstrip('/')}/api</link>"
            f"<pubDate>{channel_pub}</pubDate>"
        )

        body_parts: List[str] = []
        for it in items:
            enc_id = encode_id(it)
            title = xml_escape(it["title"]) if it["title"] else "Untitled"
            detail_url = f"{request.url_root.rstrip('/')}/details?id={enc_id}"
            safe_detail_url = xml_escape(detail_url)
            link = f"{request.url_root.rstrip('/')}/api?t=get&id={enc_id}&apikey={request.args.get('apikey')}"
            safe_link = xml_escape(link)
            size = it["size"]
            guid = enc_id
            poster = it.get("poster")
            posted_dt = _coerce_datetime(it.get("posted")) or now_dt
            posted_str = posted_dt.strftime("%a, %d %b %Y %H:%M:%S %z")
            posted_epoch = str(int(posted_dt.timestamp()))
            duration_hms = it.get("duration_hms")
            quality = it.get("quality")
            language = it.get("language")
            thumb = it.get("thumbnail")
            year = it.get("year")
            season = it.get("season")
            episode = it.get("episode")
            ext = it.get("ext")
            groups = it.get("groups")

            title_text = it.get("title", "")
            title_metadata = {
                "season": season,
                "episode": episode,
                "year": year,
                "quality": quality,
                "ext": ext,
                "groups": groups,
            }
            category_id = it.get("category_override") or _detect_category(
                title_text, title_metadata
            )

            attr_parts = [
                f'<newznab:attr name="size" value="{size}"/>',
                f'<newznab:attr name="category" value="{category_id}"/>',
                f'<newznab:attr name="usenetdate" value="{posted_str}"/>',
                f'<newznab:attr name="posted" value="{posted_epoch}"/>',
                f'<newznab:attr name="comments" value="{safe_detail_url}"/>',
                f'<newznab:attr name="details" value="{safe_detail_url}"/>',
            ]
            if poster:
                attr_parts.append(
                    f'<newznab:attr name="poster" value="{xml_escape(poster)}"/>'
                )
            if quality:
                attr_parts.append(
                    f'<newznab:attr name="quality" value="{xml_escape(quality)}"/>'
                )
            if language:
                attr_parts.append(
                    f'<newznab:attr name="language" value="{xml_escape(language)}"/>'
                )
            if duration_hms:
                attr_parts.append(
                    f'<newznab:attr name="duration" value="{duration_hms}"/>'
                )
            if thumb:
                attr_parts.append(
                    f'<newznab:attr name="thumb" value="{xml_escape(thumb)}"/>'
                )
            if year:
                attr_parts.append(f'<newznab:attr name="year" value="{year}"/>')
            if season:
                attr_parts.append(f'<newznab:attr name="season" value="{season}"/>')
            if episode:
                attr_parts.append(f'<newznab:attr name="episode" value="{episode}"/>')
            attr_xml = "".join(attr_parts)
            item_xml = (
                f"<item>"
                f"<title>{title}</title>"
                f'<guid isPermaLink="false">{guid}</guid>'
                f"<link>{safe_link}</link>"
                f"<comments>{safe_detail_url}</comments>"
                f"<category>{category_id}</category>"
                f"<pubDate>{posted_str}</pubDate>"
                f"{attr_xml}"
                f'<enclosure url="{safe_link}" length="{size}" type="application/x-nzb"/>'
                f"</item>"
            )
            body_parts.append(item_xml)

        footer = "</channel></rss>"
        xml = header + "".join(body_parts) + footer
        return Response(xml, mimetype="application/rss+xml")

    if t in ("get", "getnzb"):
        enc_id = request.args.get("id")
        if not enc_id:
            return Response("Missing id", status=400)
        d = decode_id(enc_id)
        release_items = to_search_items(d)
        logger.info(
            "NEWZNAB get release title=%r grouped_items=%s id_length=%s",
            d.get("title") or d.get("filename"),
            len(release_items),
            len(enc_id),
        )
        if d.get("sample"):
            title = d.get("title", "Sample Item")
            safe_title = "sample"
            nzb_content = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
                '<file subject="Sample Matrix Clip" date="0" poster="sample@example.com">'
                "<groups><group>alt.binaries.sample</group></groups>"
                '<segments><segment bytes="1024" number="1">sample</segment></segments>'
                "</file></nzb>"
            ).encode("utf-8")
            resp = Response(nzb_content, mimetype="application/x-nzb")
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="{safe_title}.nzb"'
            )
            return resp
        try:
            c = client()
            payload = c.build_nzb_payload(release_items, name=d.get("title"))
            posted_epoch = _posted_epoch(d, *d.get("items", []))
            logger.info(
                "EasyNews NZB payload built title=%r selected_items=%s fields=%s posted_epoch=%s",
                d.get("title") or d.get("filename"),
                len(release_items),
                len(payload),
                posted_epoch,
            )
            content = c.download_nzb_content(payload)
            content = _normalize_nzb_dates(content, posted_epoch)
            logger.info(
                "EasyNews NZB downloaded title=%r bytes=%s",
                d.get("title") or d.get("filename"),
                len(content),
            )
        except EasynewsError as e:
            logger.warning(
                "EasyNews NZB download failed title=%r error=%s",
                d.get("title") or d.get("filename"),
                e,
            )
            return Response(f"Upstream error: {e}", status=502)
        except requests.exceptions.RequestException as e:
            logger.warning(
                "EasyNews NZB network failure title=%r error=%s",
                d.get("title") or d.get("filename"),
                e,
            )
            return Response(f"Upstream network error: {e}", status=502)
        # Name file as title.nzb
        title = d.get("title") or (d.get("filename", "download") + d.get("ext", ""))
        safe_title = (
            "".join(ch for ch in title if ch.isalnum() or ch in (" ", "-", "_", "."))[
                :200
            ].strip()
            or "download"
        )
        resp = Response(content, mimetype="application/x-nzb")
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_title}.nzb"'
        return resp

    return Response("Unsupported 't' parameter", status=400)


@APP.route("/details")
def details():
    enc_id = request.args.get("id")
    if not enc_id:
        return Response("Missing id", status=400)
    try:
        release = decode_id(enc_id)
    except Exception:
        return Response("Invalid id", status=400)
    return Response(_details_html(release), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    APP.run(host="0.0.0.0", port=port)
