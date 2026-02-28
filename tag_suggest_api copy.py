#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import unicodedata
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import requests
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel


# ================== CONFIG ==================
ES_BASE_URL = os.getenv("ES_BASE_URL", "http://localhost:9200").rstrip("/")
SUGGEST_INDEX = os.getenv("SUGGEST_INDEX", "tag_data_v1").strip()
HTTP_TIMEOUT = float(os.getenv("ES_TIMEOUT", "5"))

FALLBACK_HOT_SIZE = int(os.getenv("FALLBACK_HOT_SIZE", "10"))
MIN_ACCEPT_SCORE = float(os.getenv("MIN_ACCEPT_SCORE", "0.8"))

CATEGORY_JSON_PATH = os.getenv(
    "CATEGORY_JSON_PATH",
    "/root/AI_365/TAG_SEARCH/api-base365.CategoryJob.json"
).strip()

CITY_JSON_PATH = os.getenv(
    "CITY_JSON_PATH",
    "/root/AI_365/TAG_SEARCH/tinh_thanh.json"
).strip()

CATEGORY_MIN_SCORE = float(os.getenv("CATEGORY_MIN_SCORE", "2.8"))
CATEGORY_SEARCH_SIZE = int(os.getenv("CATEGORY_SEARCH_SIZE", "20"))
CATEGORY_MAX_ALIASES_PER_CAT = int(os.getenv("CATEGORY_MAX_ALIASES_PER_CAT", "80"))

DEBUG_MODE = int(os.getenv("DEBUG_MODE", "0") or 0)

app = FastAPI(title="TAG Suggest API", version="4.3.0")


# ================== SPECIAL TECH TOKENS ==================
SHORT_VALID_TOKENS = {
    "c", "r", "go", "hr", "qa", "qc", "ui", "ux", "it",
    "seo", "php", "sql", "js", "ts", "ai", "bi"
}

SPECIAL_QUERY_VARIANTS: Dict[str, List[str]] = {
    "php": ["php"],
    "csharp": ["csharp", "c sharp", "c"],
    "cpp": ["cpp", "cplusplus", "c plus plus", "c"],
    "dotnet": ["dotnet", "net"],
    "aspnet": ["aspnet", "dotnet", "net"],
    "nodejs": ["nodejs", "node js", "node"],
    "javascript": ["javascript", "js"],
    "typescript": ["typescript", "ts"],
    "golang": ["golang", "go"],
    "reactjs": ["reactjs", "react js", "react"],
    "vuejs": ["vuejs", "vue js", "vue"],
}

SPECIAL_CANONICAL_MAP = {
    "c#": "csharp",
    "c sharp": "csharp",
    "csharp": "csharp",

    "c++": "cpp",
    "c plus plus": "cpp",
    "cplusplus": "cpp",
    "cpp": "cpp",

    ".net": "dotnet",
    "net": "dotnet",
    "dotnet": "dotnet",

    "asp.net": "aspnet",
    "asp net": "aspnet",
    "aspnet": "aspnet",

    "node.js": "nodejs",
    "node js": "nodejs",
    "nodejs": "nodejs",

    "js": "javascript",
    "javascript": "javascript",

    "ts": "typescript",
    "typescript": "typescript",

    "go": "golang",
    "golang": "golang",
}


# ================== UTILS ==================
def vn_remove_tone(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_special_text(s: str) -> str:
    """
    Chuẩn hóa keyword đặc biệt trước khi cleanup/query:
    - C# -> csharp
    - C++ -> cpp
    - .NET -> dotnet
    - ASP.NET -> aspnet
    - Node.js -> nodejs
    - JS -> javascript
    - TS -> typescript
    - Go -> golang
    """
    s = (s or "").strip().lower()

    s = s.replace("node.js", " nodejs ")
    s = s.replace("node js", " nodejs ")

    s = s.replace("asp.net", " aspnet ")
    s = s.replace("asp net", " aspnet ")

    s = s.replace(".net", " dotnet ")

    s = s.replace("c#", " csharp ")
    s = s.replace("c++", " cpp ")

    s = re.sub(r"\bjs\b", " javascript ", s)
    s = re.sub(r"\bts\b", " typescript ", s)

    s = re.sub(r"\bgo\b", " golang ", s)

    s = re.sub(r"\s+", " ", s).strip()
    s = vn_remove_tone(s)
    return s


def normalize_doc_for_compare(s: str) -> str:
    """
    Chuẩn hóa document để so khớp tốt hơn với token đặc biệt.
    """
    s = (s or "").strip().lower()
    s = vn_remove_tone(s)

    s = s.replace("c#", " csharp ")
    s = s.replace("c++", " cpp ")
    s = s.replace(".net", " dotnet ")
    s = s.replace("asp.net", " aspnet ")
    s = s.replace("asp net", " aspnet ")
    s = s.replace("node.js", " nodejs ")
    s = s.replace("node js", " nodejs ")

    s = re.sub(r"\bjs\b", " javascript ", s)
    s = re.sub(r"\bts\b", " typescript ", s)

    s = re.sub(r"\s+", " ", s).strip()
    return s


def _to_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


def _to_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v).strip()


def _to_bool(v: Any) -> bool:
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def _contains_phrase(q_norm: str, phrase_norm: str) -> bool:
    """
    Match theo word boundary bằng khoảng trắng.
    """
    if not q_norm or not phrase_norm:
        return False
    return f" {phrase_norm} " in f" {q_norm} "


def es_search(body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ES_BASE_URL}/{SUGGEST_INDEX}/_search"
    try:
        r = requests.post(url, json=body, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise RuntimeError(f"ES request failed: {e}") from e


# ================== QUERY CLEANUP ==================
REMOVE_PHRASES_HARD = [
    "viec lam",
    "tim viec",
    "tim viec lam",
    "tuyen dung",
    "tin tuyen dung",
    "cong viec",
    "nganh nghe",
    "nghe nghiep",
    "moi nhat",
    "cap nhat",
    "hom nay",
    "nam nay",
    "hot nhat",
]

REMOVE_PHRASES_SOFT = [
    "moi",
    "hot",
    "gap",
    "can gap",
    "di lam ngay",
    "thu nhap cao",
    "luong tot",
    "luong hap dan",
    "che do tot",
]

REMOVE_TOKENS_HARD = {
    "viec", "lam", "tim", "tuyen", "dung", "tin",
    "cong", "nghe", "nghiep", "nganh",
    "moi", "nhat", "cap", "hom", "nay",
    "nam", "hot", "gap",
}

SOFT_STOPWORDS = {
    "viec", "lam", "tim", "tuyen", "dung", "moi", "nhat", "hot",
    "luong", "cao", "cho", "va", "tai", "nhan", "vien",
    "chuyen", "ky", "truong", "phong", "nam", "gap",
    "can", "ngay", "tot", "hap", "dan", "cap", "nhat"
}

KEEP_AS_MODIFIERS = {
    "online", "remote", "part time", "full time", "tai nha",
    "thuc tap", "intern", "fresher", "moi ra truong",
    "khong can kinh nghiem", "khong yeu cau kinh nghiem",
    "tieng anh", "tieng trung", "tieng nhat", "ca dem", "xoay ca"
}


def _remove_years_and_numbers(s: str) -> str:
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _remove_phrases(s: str, phrases: List[str]) -> str:
    s2 = f" {s} "
    for p in phrases:
        pn = vn_remove_tone(p)
        if pn:
            s2 = s2.replace(f" {pn} ", " ")
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2


def _extract_modifiers(q_norm: str) -> List[str]:
    found: List[str] = []
    s = f" {q_norm} "
    for p in sorted(KEEP_AS_MODIFIERS, key=len, reverse=True):
        pn = vn_remove_tone(p)
        if f" {pn} " in s:
            found.append(pn)
    return found


def cleanup_query_for_search(q_norm: str) -> str:
    s = q_norm
    s = _remove_years_and_numbers(s)
    s = _remove_phrases(s, REMOVE_PHRASES_HARD)
    s = _remove_phrases(s, REMOVE_PHRASES_SOFT)

    tokens = []
    for t in s.split():
        if not t:
            continue
        if t in SHORT_VALID_TOKENS:
            tokens.append(t)
            continue
        if t in REMOVE_TOKENS_HARD:
            continue
        tokens.append(t)

    s = " ".join(tokens)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def meaningful_query_tokens(q_norm: str) -> List[str]:
    tokens = [t for t in q_norm.split() if t]
    out: List[str] = []
    for t in tokens:
        if t in SHORT_VALID_TOKENS:
            out.append(t)
            continue
        if t in SOFT_STOPWORDS:
            continue
        out.append(t)
    return out


def get_special_variants(q_core: str) -> List[str]:
    q = (q_core or "").strip().lower()
    if not q:
        return []

    canonical = SPECIAL_CANONICAL_MAP.get(q, q)
    variants = SPECIAL_QUERY_VARIANTS.get(canonical, [canonical])

    cleaned: List[str] = []
    seen = set()

    for v in variants:
        v2 = normalize_special_text(v)
        v2 = cleanup_query_for_search(v2)
        if not v2:
            continue
        if v2 not in seen:
            seen.add(v2)
            cleaned.append(v2)

    if q not in seen and q:
        cleaned.insert(0, q)

    return cleaned


# ================== LOCATION FILTER (for tag suggest only) ==================
STANDALONE_LOCATION_PHRASES = {
    "tinh", "tinh thanh", "thanh pho", "quan", "quan huyen", "phuong", "xa", "thi tran",
    "ha noi", "hn", "ho chi minh", "tp ho chi minh", "tphcm", "sai gon", "hcm",
    "da nang", "hai phong", "can tho",
    "an giang", "ba ria vung tau", "bac giang", "bac kan", "bac lieu", "bac ninh",
    "ben tre", "binh dinh", "binh duong", "binh phuoc", "binh thuan", "ca mau",
    "cao bang", "dak lak", "dak nong", "dien bien", "dong nai", "dong thap",
    "gia lai", "ha giang", "ha nam", "ha tinh", "hai duong", "hau giang",
    "hoa binh", "hung yen", "khanh hoa", "kien giang", "kon tum", "lai chau",
    "lam dong", "lang son", "lao cai", "long an", "nam dinh", "nghe an",
    "ninh binh", "ninh thuan", "phu tho", "phu yen", "quang binh", "quang nam",
    "quang ngai", "quang ninh", "quang tri", "soc trang", "son la", "tay ninh",
    "thai binh", "thai nguyen", "thanh hoa", "thua thien hue", "hue", "tien giang",
    "tra vinh", "tuyen quang", "vinh long", "vinh phuc", "yen bai",
}

LOCATION_STOP_TOKENS = {
    "viec", "lam", "tim", "tuyen", "dung", "moi", "nhat",
    "online", "remote", "part", "full", "time",
    "php", "sql", "seo", "qa", "qc", "ui", "ux", "hr", "it",
    "javascript", "typescript", "nodejs", "dotnet", "csharp", "cpp", "golang"
}


def _normalize_location_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("tp.", "tp ")
    s = s.replace("q.", "q ")
    s = s.replace("p.", "p ")
    s = s.replace("h.", "h ")
    s = s.replace("x.", "x ")
    s = re.sub(r"[,\.;:/\\\-\(\)\[\]]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_and_remove_locations(q_norm: str) -> Tuple[str, List[str]]:
    s = _normalize_location_text(q_norm)
    found: List[str] = []

    s2 = f" {s} "
    for loc in sorted(STANDALONE_LOCATION_PHRASES, key=len, reverse=True):
        locn = _normalize_location_text(vn_remove_tone(loc))
        needle = f" {locn} "
        if needle in s2:
            found.append(locn)
            s2 = s2.replace(needle, " ")
    s = re.sub(r"\s+", " ", s2).strip()

    tokens = [t for t in s.split() if t]
    out_tokens: List[str] = []
    i = 0
    n = len(tokens)

    multi_prefixes = {("thanh", "pho"), ("thi", "xa"), ("thi", "tran")}
    single_prefixes = {"tp", "tinh", "quan", "q", "huyen", "h", "tx", "tt", "phuong", "p", "xa", "x"}

    while i < n:
        matched_prefix = None
        prefix_len = 0

        if i + 1 < n and (tokens[i], tokens[i + 1]) in multi_prefixes:
            matched_prefix = f"{tokens[i]} {tokens[i + 1]}"
            prefix_len = 2
        elif tokens[i] in single_prefixes:
            matched_prefix = tokens[i]
            prefix_len = 1

        if matched_prefix:
            j = i + prefix_len
            loc_tokens = []
            while j < n and len(loc_tokens) < 4:
                tj = tokens[j]
                if tj in single_prefixes:
                    break
                if j + 1 < n and (tokens[j], tokens[j + 1]) in multi_prefixes:
                    break
                if tj in LOCATION_STOP_TOKENS:
                    break

                loc_tokens.append(tj)
                j += 1

                if len(loc_tokens) == 1 and re.fullmatch(r"\d+[a-z]?", loc_tokens[0]):
                    break

            if loc_tokens:
                found.append(f"{matched_prefix} {' '.join(loc_tokens)}".strip())
                i = j
                continue
            else:
                out_tokens.append(tokens[i])
                i += 1
                continue

        out_tokens.append(tokens[i])
        i += 1

    cleaned = " ".join(out_tokens)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    final_found = []
    seen = set()
    for x in found:
        x = re.sub(r"\s+", " ", x).strip()
        if x and x not in seen:
            seen.add(x)
            final_found.append(x)

    return cleaned, final_found


# ================== PRE-RESOLVE CITY (cit_id, cit_name) ==================
CITY_DB: List[Dict[str, Any]] = []
CITY_PHRASES: List[Tuple[str, int]] = []
CITY_ID_TO_NAME: Dict[int, str] = {}

CITY_ALIAS: Dict[str, int] = {
    "hn": 1, "ha noi": 1, "hanoi": 1,
    "hcm": 45, "tphcm": 45, "tp hcm": 45, "tp ho chi minh": 45, "ho chi minh": 45, "sai gon": 45, "saigon": 45,
    "da nang": 26, "danang": 26,
    "hai phong": 2, "haiphong": 2,
    "can tho": 48, "cantho": 48,
    "hue": 27,
}


def load_city_db(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

    items = []
    try:
        items = raw.get("data", {}).get("data", [])
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []

    out: List[Dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        cit_id = _to_int(row.get("cit_id"), 0)
        cit_name = _to_str(row.get("cit_name"))
        if cit_id <= 0 or not cit_name:
            continue
        out.append({"cit_id": cit_id, "cit_name": cit_name, "cit_name_norm": vn_remove_tone(cit_name)})
    return out


def build_city_phrases() -> List[Tuple[str, int]]:
    phrases: List[Tuple[str, int]] = []
    for row in CITY_DB:
        phrases.append((row["cit_name_norm"], int(row["cit_id"])))
    for k, v in CITY_ALIAS.items():
        phrases.append((vn_remove_tone(k), int(v)))

    phrases.sort(key=lambda x: len(x[0]), reverse=True)

    seen = set()
    out: List[Tuple[str, int]] = []
    for name_norm, cit_id in phrases:
        if not name_norm or name_norm in seen:
            continue
        seen.add(name_norm)
        out.append((name_norm, cit_id))
    return out


def detect_city_id(q_norm: str) -> int:
    if not q_norm:
        return 0
    for name_norm, cit_id in CITY_PHRASES:
        if _contains_phrase(q_norm, name_norm):
            return cit_id
    return 0


CITY_DB = load_city_db(CITY_JSON_PATH)
CITY_PHRASES = build_city_phrases()
CITY_ID_TO_NAME = {int(x["cit_id"]): str(x["cit_name"]) for x in CITY_DB if int(x["cit_id"]) > 0 and x.get("cit_name")}


# ================== PRE-RESOLVE CATEGORY (cat_id, cat_name) ==================
CAT_UT_PHRASES: List[Tuple[str, int]] = []     # from cat_ut (priority 1)
CAT_NAME_PHRASES: List[Tuple[str, int]] = []   # from cat_name_new/cat_name (priority 2)
CAT_ID_TO_NAME: Dict[int, str] = {}


def _split_cat_ut(v: Any) -> List[str]:
    """
    cat_ut keywords ngăn cách bằng dấu phẩy ","
    """
    if v is None:
        return []
    s = str(v).strip()
    if not s:
        return []
    parts = s.split(",")
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


def build_cat_phrase_tables(category_json_path: str) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]], Dict[int, str]]:
    p = Path(category_json_path)
    if not p.exists():
        return [], [], {}

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return [], [], {}
    except Exception:
        return [], [], {}

    ut_phrases: List[Tuple[str, int]] = []
    name_phrases: List[Tuple[str, int]] = []
    id2name: Dict[int, str] = {}

    # ===== helper lọc phrase rác =====
    def is_good_phrase(ph: str, source: str) -> bool:
        """
        source: 'ut' hoặc 'name'
        ph đã normalize + cleanup
        """
        if not ph:
            return False

        toks_all = [t for t in ph.split() if t]
        if not toks_all:
            return False

        # meaningful token: loại stopword mềm
        toks = [t for t in toks_all if t not in SOFT_STOPWORDS]
        if not toks:
            # nếu toàn stopword thì bỏ
            return False

        # 1 token:
        if len(toks) == 1:
            t = toks[0]

            # cho phép token special (php, qa, ui...) nếu bạn muốn map ngành theo token ngắn
            if t in SHORT_VALID_TOKENS:
                return True

            # token 1 mà ngắn quá => cực dễ match nhầm (vd: "game", "lap", "trinh")
            if len(t) < 5:
                return False

            return True

        # >=2 token:
        # nếu source là name (cat_name/cat_name_new) thì bắt buộc >=2 token meaningful
        # để tránh cat_name_new kiểu "Xây dựng", "Game" ... match bậy
        if source == "name":
            if len(toks) < 2:
                return False

            # nếu chỉ 2 token mà cả 2 đều ngắn <4 => bỏ
            if len(toks) == 2 and all(len(x) < 4 for x in toks):
                return False

        # không cho phrase quá ngắn tổng thể
        if len(ph) < 5:
            return False

        return True

    for row in raw:
        if not isinstance(row, dict):
            continue

        cat_id = _to_int(row.get("cat_id"), 0)
        if cat_id <= 0:
            continue

        cat_name = _to_str(row.get("cat_name"))
        cat_name_new = _to_str(row.get("cat_name_new"))

        if cat_name:
            id2name[cat_id] = cat_name

        # 1) cat_ut (PRIORITY)
        for ut in _split_cat_ut(row.get("cat_ut")):
            ut_norm = normalize_special_text(ut)
            ut_norm = cleanup_query_for_search(ut_norm)
            ut_norm = re.sub(r"\s+", " ", ut_norm).strip()

            if not is_good_phrase(ut_norm, source="ut"):
                continue

            ut_phrases.append((ut_norm, cat_id))

        # 2) fallback cat_name_new / cat_name (để bắt case "kinh doanh")
        for base in [cat_name_new, cat_name]:
            base = (base or "").strip()
            if not base:
                continue

            bn = normalize_special_text(base)
            bn = cleanup_query_for_search(bn)
            bn = re.sub(r"\s+", " ", bn).strip()

            if not is_good_phrase(bn, source="name"):
                continue

            name_phrases.append((bn, cat_id))

    # ưu tiên cụm dài trước
    ut_phrases.sort(key=lambda x: len(x[0]), reverse=True)
    name_phrases.sort(key=lambda x: len(x[0]), reverse=True)

    # dedupe phrase
    def dedupe(phs: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
        seen = set()
        out = []
        for ph, cid in phs:
            if not ph or ph in seen:
                continue
            seen.add(ph)
            out.append((ph, cid))
        return out

    return dedupe(ut_phrases), dedupe(name_phrases), id2name


def detect_cat_id(q_norm: str) -> int:
    if not q_norm:
        return 0
    q = re.sub(r"\s+", " ", q_norm).strip()
    q_tokens = [t for t in q.split() if t and t not in SOFT_STOPWORDS]
    q_tok_set = set(q_tokens)

    def ok_match(phrase_norm: str) -> bool:
        if not _contains_phrase(q, phrase_norm):
            return False
        ptoks = [t for t in phrase_norm.split() if t and t not in SOFT_STOPWORDS]
        if not ptoks:
            return False

        # Nếu query >= 2 token meaningful => phrase phải match >= 2 token meaningful
        if len(q_tokens) >= 2:
            overlap = len(set(ptoks) & q_tok_set)
            if overlap >= 2:
                return True
            # hoặc phrase bản thân dài >=2 token (cụm rõ ràng)
            if len(ptoks) >= 2:
                return True
            return False

        # query chỉ 1 token thì giữ logic cũ
        return True

    # 1) cat_ut ưu tiên
    for phrase_norm, cat_id in CAT_UT_PHRASES:
        if ok_match(phrase_norm):
            return int(cat_id)

    # 2) fallback name
    for phrase_norm, cat_id in CAT_NAME_PHRASES:
        if ok_match(phrase_norm):
            return int(cat_id)

    return 0


CAT_UT_PHRASES, CAT_NAME_PHRASES, CAT_ID_TO_NAME = build_cat_phrase_tables(CATEGORY_JSON_PATH)


# ================== CATEGORY INTENT (alias resolver for TAG fallback) ==================
# (giữ nguyên logic match category để boost tag suggest khi không pre-resolve)
CATEGORY_FIELDS = [
    "cat_name",
    "cat_title",
    "cat_tags",
    "cat_description",
    "cat_keyword",
    "cat_ut",
    "cat_name_new",
]

GENERIC_ALIAS_PHRASES = {
    "viec lam",
    "tim viec",
    "tim viec lam",
    "tuyen dung",
    "cong viec",
    "nghe nghiep",
    "luong cao",
    "hap dan",
    "viec",
    "lam",
}

CATEGORY_DB: List[Dict[str, Any]] = []


def _split_alias_text(v: Any) -> List[str]:
    if v is None:
        return []
    s = str(v).strip()
    if not s:
        return []
    parts = re.split(r"[,;\n\r|]+", s)
    return [p.strip() for p in parts if p and p.strip()]


def load_category_db(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
    except Exception:
        return []

    out: List[Dict[str, Any]] = []

    for row in raw:
        if not isinstance(row, dict):
            continue

        cat_id = _to_int(row.get("cat_id"), 0)
        cat_name = _to_str(row.get("cat_name"))
        cat_name_new = _to_str(row.get("cat_name_new"))
        canonical = cat_name_new or cat_name
        canonical_norm = normalize_special_text(canonical)

        cat_only = _to_bool(row.get("cat_only", 0))
        cat_required_keyword = _to_bool(row.get("cat_required_keyword", 0))

        if not canonical_norm:
            continue

        alias_scores: Dict[str, float] = {}

        for field in CATEGORY_FIELDS:
            val = row.get(field)

            for alias_raw in _split_alias_text(val):
                alias_norm = normalize_special_text(alias_raw)
                if not alias_norm:
                    continue

                alias_norm = _remove_years_and_numbers(alias_norm)
                alias_norm = re.sub(r"\s+", " ", alias_norm).strip()

                if not alias_norm:
                    continue
                if alias_norm in GENERIC_ALIAS_PHRASES:
                    continue
                if re.fullmatch(r"\d+", alias_norm):
                    continue
                if len(alias_norm) < 1:
                    continue

                alias_tokens = [t for t in alias_norm.split() if t]
                if not alias_tokens:
                    continue

                token_count = len(alias_tokens)
                longest_token = max((len(t) for t in alias_tokens), default=0)

                if token_count == 1 and longest_token <= 2 and alias_tokens[0] not in SHORT_VALID_TOKENS:
                    continue

                if field in ("cat_name", "cat_name_new"):
                    base = 3.2
                elif field == "cat_tags":
                    base = 2.8
                elif field in ("cat_keyword", "cat_ut"):
                    base = 2.2
                elif field == "cat_title":
                    base = 1.6
                else:
                    base = 1.1

                score = base + min(token_count * 0.22, 1.4)

                if field == "cat_description" and token_count >= 6:
                    score -= 0.4

                if cat_only:
                    score += 0.25

                old = alias_scores.get(alias_norm, 0.0)
                if score > old:
                    alias_scores[alias_norm] = score

        alias_scores[canonical_norm] = max(alias_scores.get(canonical_norm, 0.0), 4.0)

        for v in get_special_variants(canonical_norm):
            alias_scores[v] = max(alias_scores.get(v, 0.0), 3.2)

        top_aliases = sorted(alias_scores.items(), key=lambda x: (-x[1], len(x[0])))
        top_aliases = top_aliases[:CATEGORY_MAX_ALIASES_PER_CAT]

        out.append({
            "cat_id": cat_id,
            "canonical": canonical,
            "canonical_norm": canonical_norm,
            "aliases": dict(top_aliases),
            "cat_only": cat_only,
            "cat_required_keyword": cat_required_keyword,
        })

    return out


def find_best_category_match(q_core: str) -> Optional[Dict[str, Any]]:
    if not CATEGORY_DB:
        return None
    if not q_core:
        return None

    q_tokens = meaningful_query_tokens(q_core)
    if not q_tokens:
        q_tokens = [t for t in q_core.split() if t]
    if not q_tokens:
        return None

    if len(q_tokens) == 1 and len(q_tokens[0]) <= 2 and q_tokens[0] not in SHORT_VALID_TOKENS:
        return None

    best: Optional[Dict[str, Any]] = None
    q_phrase = f" {q_core} "
    q_token_set = set(q_tokens)

    for cat in CATEGORY_DB:
        best_score = 0.0
        best_alias = ""
        has_direct_keyword = False

        for alias_norm, weight in cat["aliases"].items():
            alias_tokens = [t for t in alias_norm.split() if t and (t not in SOFT_STOPWORDS or t in SHORT_VALID_TOKENS)]
            if not alias_tokens:
                alias_tokens = [t for t in alias_norm.split() if t]
            if not alias_tokens:
                continue

            alias_phrase = f" {alias_norm} "
            alias_set = set(alias_tokens)

            overlap = len(alias_set & q_token_set)
            coverage_alias = overlap / max(len(alias_set), 1)
            coverage_query = overlap / max(len(q_token_set), 1)
            score = 0.0

            if alias_phrase in q_phrase:
                score = weight + len(alias_tokens) * 1.35
                has_direct_keyword = True
            elif len(q_core) >= 2 and f" {q_core} " in alias_phrase:
                score = weight + min(len(q_token_set) * 0.9, 2.2)
                has_direct_keyword = True
            elif overlap > 0:
                if coverage_alias >= 0.75 or coverage_query >= 0.75:
                    score = weight + overlap * 0.8 + coverage_alias + coverage_query
                elif coverage_alias >= 0.6 and overlap >= 1:
                    score = weight + overlap * 0.65 + coverage_alias + coverage_query * 0.7

            if score == 0.0 and len(q_token_set) == 1:
                q_tok = next(iter(q_token_set))
                if q_tok in SHORT_VALID_TOKENS and q_tok in alias_set:
                    score = max(score, weight + 1.6)
                    has_direct_keyword = True
                elif len(q_tok) >= 5:
                    for a_tok in alias_tokens:
                        if a_tok == q_tok:
                            score = max(score, weight + 1.8)
                            has_direct_keyword = True
                            break
                        if a_tok.startswith(q_tok[:4]) and q_tok in a_tok:
                            score = max(score, weight + 0.5)

            if score > best_score:
                best_score = score
                best_alias = alias_norm

        if cat.get("cat_required_keyword") and not has_direct_keyword:
            continue

        if best_score >= CATEGORY_MIN_SCORE:
            cand = {
                "cat_id": int(cat.get("cat_id", 0)),
                "canonical": cat.get("canonical", ""),
                "canonical_norm": cat.get("canonical_norm", ""),
                "matched_alias": best_alias,
                "score": best_score,
            }

            if best is None or cand["score"] > best["score"]:
                best = cand
            elif best is not None and cand["score"] == best["score"] and len(cand["matched_alias"]) > len(best["matched_alias"]):
                best = cand

    return best


CATEGORY_DB = load_category_db(CATEGORY_JSON_PATH)


# ================== MODELS ==================
class SuggestItem(BaseModel):
    tag_id: Optional[str] = None
    key_name: str = ""
    key_name_norm_full: str = ""
    key_city_id: int = 0
    key_qh_id: int = 0
    key_cb_id: int = 0


class SuggestResp(BaseModel):
    q: str
    size: int
    cat_id: int = 0
    cat_name: str = ""
    cit_id: int = 0
    cit_name: str = ""
    items: List[SuggestItem]


# ================== PARSE + SORT ==================
def parse_items_with_score(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    hits = data.get("hits", {}).get("hits", [])
    out: List[Dict[str, Any]] = []

    for h in hits:
        src = h.get("_source") or {}
        key_name = src.get("key_name")
        if not key_name:
            continue

        tag_id = src.get("tag_id")
        score = float(h.get("_score") or 0.0)
        kn_full = str(src.get("key_name_norm_full") or src.get("key_name_norm") or "").strip()
        kn_full_norm = normalize_doc_for_compare(kn_full)

        item = SuggestItem(
            tag_id=str(tag_id) if tag_id is not None else None,
            key_name=str(key_name),
            key_name_norm_full=kn_full,
            key_city_id=_to_int(src.get("key_city_id"), 0),
            key_qh_id=_to_int(src.get("key_qh_id"), 0),
            key_cb_id=_to_int(src.get("key_cb_id"), 0),
        )

        out.append({
            "item": item,
            "score": score,
            "doc_norm_full": kn_full_norm
        })

    return out


def tag_id_sort_key(tag_id: Optional[str]) -> Tuple[int, str]:
    if tag_id is None:
        return (10**18, "")
    s = str(tag_id).strip()
    if s.isdigit():
        return (int(s), "")
    return (10**18 - 1, s)


def group_sort_key(it: SuggestItem) -> Tuple[int, int, int, Tuple[int, str]]:
    city_not_zero = 0 if it.key_city_id == 0 else 1
    qh_not_zero = 0 if it.key_qh_id == 0 else 1
    cb_not_zero = 0 if it.key_cb_id == 0 else 1
    return (city_not_zero, qh_not_zero, cb_not_zero, tag_id_sort_key(it.tag_id))


def dedupe_items(items: List[SuggestItem]) -> List[SuggestItem]:
    seen = set()
    out: List[SuggestItem] = []
    for it in items:
        key = (it.tag_id or "", it.key_name, it.key_city_id, it.key_qh_id, it.key_cb_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ================== RELEVANCE + RE-RANK ==================
def edit_distance_limited(a: str, b: str, max_dist: int = 2) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        row_min = curr[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
            curr.append(v)
            if v < row_min:
                row_min = v
        if row_min > max_dist:
            return max_dist + 1
        prev = curr
    return prev[-1]


def is_relevant(tokens: List[str], doc_norm_full: str) -> bool:
    if not doc_norm_full:
        return False

    doc_norm = normalize_doc_for_compare(doc_norm_full)
    doc_tokens = [t for t in doc_norm.split() if t]
    if not doc_tokens:
        return False
    doc_set = set(doc_tokens)

    q_tokens = [t for t in tokens if t and (t not in SOFT_STOPWORDS or t in SHORT_VALID_TOKENS)]
    if not q_tokens:
        q_tokens = [t for t in tokens if t]
    if not q_tokens:
        return False

    exact_matches = 0
    prefix_matches = 0

    for t in q_tokens:
        if t in doc_set:
            exact_matches += 1
            continue
        if t in SHORT_VALID_TOKENS:
            continue
        if len(t) >= 5:
            for d in doc_tokens:
                if d.startswith(t[:4]) and (t in d or d in t):
                    prefix_matches += 1
                    break

    if len(q_tokens) >= 2:
        if any(t in SHORT_VALID_TOKENS for t in q_tokens) and exact_matches >= 1:
            return True
        long_exact = sum(1 for t in q_tokens if len(t) >= 4 and t in doc_set)
        if long_exact >= 1:
            return True
        coverage = exact_matches / max(len(q_tokens), 1)
        return coverage >= 0.6

    t = q_tokens[0]
    if t in SHORT_VALID_TOKENS:
        return t in doc_set
    if len(t) >= 4 and t in doc_set:
        return True
    if len(t) >= 6 and prefix_matches >= 1:
        return True
    return False


def compute_rerank_score(item: SuggestItem, es_score: float, q_core: str, q_tokens: List[str]) -> float:
    doc = normalize_doc_for_compare(item.key_name_norm_full or "")
    if not doc:
        return -1e9

    doc_tokens = [t for t in doc.split() if t]
    if not doc_tokens:
        return -1e9
    doc_set = set(doc_tokens)

    q_core_norm = normalize_doc_for_compare(q_core)
    q_tokens2 = [t for t in q_tokens if t and (t not in SOFT_STOPWORDS or t in SHORT_VALID_TOKENS)]
    if not q_tokens2:
        q_tokens2 = [t for t in q_tokens if t]
    if not q_tokens2:
        return -1e9

    score = 0.0

    if q_core_norm and doc == q_core_norm:
        score += 220.0
    elif q_core_norm and f" {q_core_norm} " in f" {doc} ":
        score += 130.0

    exact_count = 0
    typo_near_count = 0.0

    for qt in q_tokens2:
        if qt in doc_set:
            exact_count += 1
            if qt in SHORT_VALID_TOKENS:
                score += 18.0
            continue
        if qt in SHORT_VALID_TOKENS:
            continue

        best_dist = 99
        for dt in doc_tokens:
            d = edit_distance_limited(qt, dt, max_dist=2)
            if d < best_dist:
                best_dist = d
                if best_dist == 0:
                    break

        if best_dist == 1:
            typo_near_count += 1.0
        elif best_dist == 2 and len(qt) >= 6:
            typo_near_count += 0.5

    score += exact_count * 35.0
    score += typo_near_count * 18.0

    coverage = exact_count / max(len(q_tokens2), 1)
    score += coverage * 30.0

    if len(q_tokens2) <= 2:
        extra_tokens = max(len(doc_tokens) - max(len(q_tokens2), 1), 0)
        score -= extra_tokens * 8.0

    if q_tokens2 and doc_tokens:
        q0 = q_tokens2[0]
        if q0 in SHORT_VALID_TOKENS:
            if q0 == doc_tokens[0]:
                score += 10.0
        else:
            d0 = edit_distance_limited(q0, doc_tokens[0], max_dist=2)
            if d0 == 0:
                score += 12.0
            elif d0 == 1:
                score += 5.0

    score += min(es_score, 20.0)

    if item.key_city_id == 0:
        score += 2.0
    if item.key_qh_id == 0:
        score += 1.0
    if item.key_cb_id == 0:
        score += 1.0

    return score


def rerank_items(parsed: List[Dict[str, Any]], q_core: str, q_tokens: List[str], size: int) -> List[SuggestItem]:
    candidates: List[Tuple[float, SuggestItem]] = []
    for x in parsed:
        item = x["item"]
        es_score = float(x.get("score") or 0.0)
        if not is_relevant(q_tokens, x.get("doc_norm_full", "")):
            continue
        rr = compute_rerank_score(item, es_score, q_core, q_tokens)
        candidates.append((rr, item))

    candidates.sort(key=lambda z: (-z[0], group_sort_key(z[1])))
    items = [it for _, it in candidates]
    items = dedupe_items(items)
    return items[:size]


# ================== QUERIES ==================
def _source_fields() -> List[str]:
    return [
        "tag_id",
        "key_name",
        "key_city_id", "key_qh_id", "key_cb_id",
        "key_name_norm_full", "key_name_norm",
    ]


def _multi_should_match_queries(variants: List[str], match_type: str = "exact_core") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()

    for v in variants:
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)

        token_count = len(v.split())
        short_penalty = 1.0
        if token_count == 1 and len(v) <= 3:
            short_penalty = 0.45

        if match_type == "exact_core":
            out.append({"term": {"key_name_norm_full": {"value": v, "boost": 180 * short_penalty}}})
            out.append({"match_phrase": {"key_name_norm_full": {"query": v, "boost": 140 * short_penalty}}})
            out.append({"match_phrase_prefix": {"key_name_norm_full": {"query": v, "boost": 40 * short_penalty}}})

        elif match_type == "autocomplete":
            out.append({"match_phrase": {"key_name_norm_full": {"query": v, "boost": 80 * short_penalty}}})
            out.append({"match_phrase_prefix": {"key_name_norm_full": {"query": v, "boost": 30 * short_penalty}}})
            out.append({"match_bool_prefix": {"key_name_norm_full": {"query": v, "boost": 18 * short_penalty}}})
            out.append({"match_bool_prefix": {"key_name_norm": {"query": v, "boost": 5 * short_penalty}}})

        elif match_type == "fuzzy_phrase":
            out.append({
                "multi_match": {
                    "query": v,
                    "fields": ["key_name_norm_full"],
                    "type": "best_fields",
                    "operator": "or",
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                    "max_expansions": 80,
                    "minimum_should_match": "1<75%"
                }
            })

        elif match_type == "fuzzy_tokens":
            toks = [t for t in v.split() if t][:4]
            for i, t in enumerate(toks):
                if t in SHORT_VALID_TOKENS:
                    out.append({"match": {"key_name_norm_full": {"query": t, "boost": 1.5 * short_penalty}}})
                else:
                    boost = 3.0 if i == len(toks) - 1 else 2.0
                    out.append({
                        "match": {
                            "key_name_norm_full": {
                                "query": t,
                                "fuzziness": "AUTO",
                                "prefix_length": 1,
                                "max_expansions": 100,
                                "boost": boost * short_penalty
                            }
                        }
                    })
    return out


def build_query_exact_core(q_variants: List[str], size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": _source_fields(),
        "query": {"bool": {"should": _multi_should_match_queries(q_variants, "exact_core"), "minimum_should_match": 1}}
    }


def build_query_autocomplete(q_variants: List[str], size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": _source_fields(),
        "query": {"bool": {"should": _multi_should_match_queries(q_variants, "autocomplete"), "minimum_should_match": 1}}
    }


def build_query_fuzzy_phrase(q_variants: List[str], size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": _source_fields(),
        "query": {"bool": {"should": _multi_should_match_queries(q_variants, "fuzzy_phrase"), "minimum_should_match": 1}}
    }


def build_query_fuzzy_tokens(q_variants: List[str], size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": _source_fields(),
        "query": {"bool": {"should": _multi_should_match_queries(q_variants, "fuzzy_tokens"), "minimum_should_match": 1}}
    }


def build_query_hot(size: int) -> Dict[str, Any]:
    return {"size": size, "_source": _source_fields(), "query": {"match_all": {}}, "sort": [{"tag_id": "asc"}]}


# ================== SPLIT QUERY INTENT ==================
def split_query_intent(q_raw: str) -> Dict[str, Any]:
    q_norm = normalize_special_text(q_raw)

    # chỉ remove location cho TAG suggest (pre-resolve city dùng q_norm_full)
    q_wo_locations, q_locations = extract_and_remove_locations(q_norm)

    q_modifiers = _extract_modifiers(q_wo_locations)
    q_core = cleanup_query_for_search(q_wo_locations)

    if not q_core:
        q_core = q_wo_locations or q_norm

    core_tokens = meaningful_query_tokens(q_core)
    if not core_tokens:
        core_tokens = [t for t in q_core.split() if t]

    q_variants = get_special_variants(q_core)
    if not q_variants:
        q_variants = [q_core]

    return {
        "q_raw": q_raw,
        "q_norm": q_norm,
        "q_core": q_core,
        "q_modifiers": q_modifiers,
        "q_locations": q_locations,
        "q_tokens": core_tokens,
        "q_variants": q_variants,
    }


# ================== ENDPOINT ==================
@app.get("/suggest", response_model=SuggestResp)
def suggest(
    q: str = Query("", description="User typed query"),
    size: int = Query(10, ge=1, le=50),
    fallback: int = Query(1, ge=0, le=1, description="0 = allow empty, 1 = never empty")
):
    q_raw = (q or "").strip()

    # ===== PRE-RESOLVE cat/city trên query normalize FULL =====
    q_norm_full = normalize_special_text(q_raw)

    cat_id = int(detect_cat_id(q_norm_full))
    cit_id = int(detect_city_id(q_norm_full))

    cat_name = CAT_ID_TO_NAME.get(cat_id, "") if cat_id > 0 else ""
    cit_name = CITY_ID_TO_NAME.get(cit_id, "") if cit_id > 0 else ""

    # Nếu detect được cat hoặc city => trả luôn placeholder tag_id=0
    if cat_id != 0 or cit_id != 0:
        return SuggestResp(
            q=q_raw,
            size=size,
            cat_id=cat_id,
            cat_name=cat_name,
            cit_id=cit_id,
            cit_name=cit_name,
            items=[SuggestItem(tag_id="0")]
        )

    # ===== Không detect được cat/city => mới suggest TAG =====
    ctx = split_query_intent(q_raw)
    q_norm = ctx["q_norm"]
    q_core = ctx["q_core"]
    q_tokens = ctx["q_tokens"]
    q_variants = ctx["q_variants"]

    try:
        if not q_norm:
            data = es_search(build_query_hot(size))
            parsed = parse_items_with_score(data)
            items = dedupe_items([x["item"] for x in parsed])[:size]
            return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=items)

        # Tier 1: exact core
        if q_core:
            data0 = es_search(build_query_exact_core(q_variants, max(size, 12)))
            parsed0 = parse_items_with_score(data0)
            items0 = rerank_items(parsed0, q_core, q_tokens, size)
            if items0:
                return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=items0)

        # Tier 2: autocomplete
        data1 = es_search(build_query_autocomplete(q_variants, max(size, 12)))
        parsed1 = parse_items_with_score(data1)
        items1 = rerank_items(parsed1, q_core, q_tokens, size)
        if items1:
            return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=items1)

        # Tier 3: fuzzy phrase
        data2a = es_search(build_query_fuzzy_phrase(q_variants, max(size, 12)))
        parsed2a = parse_items_with_score(data2a)
        parsed2a = [x for x in parsed2a if float(x.get("score") or 0.0) >= MIN_ACCEPT_SCORE]
        items2a = rerank_items(parsed2a, q_core, q_tokens, size)
        if items2a:
            return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=items2a)

        # Tier 4: fuzzy tokens
        data2b = es_search(build_query_fuzzy_tokens(q_variants, max(size, 12)))
        parsed2b = parse_items_with_score(data2b)
        parsed2b = [x for x in parsed2b if float(x.get("score") or 0.0) >= MIN_ACCEPT_SCORE]
        items2b = rerank_items(parsed2b, q_core, q_tokens, size)
        if items2b:
            return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=items2b)

        # Tier 5: fallback hot
        if fallback == 1:
            data3 = es_search(build_query_hot(min(size, FALLBACK_HOT_SIZE)))
            parsed3 = parse_items_with_score(data3)
            items3 = dedupe_items([x["item"] for x in parsed3])[:size]
            return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=items3)

        return SuggestResp(q=q_raw, size=size, cat_id=0, cat_name="", cit_id=0, cit_name="", items=[])

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Elasticsearch error: {e}")