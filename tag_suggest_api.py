#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import unicodedata
from typing import List, Dict, Any, Optional, Tuple

import requests
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel


# ================== CONFIG ==================
ES_BASE_URL = os.getenv("ES_BASE_URL", "http://localhost:9200").rstrip("/")
SUGGEST_INDEX = os.getenv("SUGGEST_INDEX", "tag_data_v1").strip()
HTTP_TIMEOUT = float(os.getenv("ES_TIMEOUT", "5"))

FALLBACK_HOT_SIZE = int(os.getenv("FALLBACK_HOT_SIZE", "10"))
MIN_ACCEPT_SCORE = float(os.getenv("MIN_ACCEPT_SCORE", "0.3"))

app = FastAPI(title="TAG Suggest API", version="1.0.9")


# ================== UTILS ==================
def vn_remove_tone(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_ascii_query(s: str) -> bool:
    return all(ord(ch) < 128 for ch in s)


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


def es_search(body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ES_BASE_URL}/{SUGGEST_INDEX}/_search"
    r = requests.post(url, json=body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ================== MODELS ==================
class SuggestItem(BaseModel):
    tag_id: Optional[str] = None
    key_name: str
    key_city_id: int = 0
    key_qh_id: int = 0
    key_cb_id: int = 0
    key_time: int = 0


class SuggestResp(BaseModel):
    q: str
    size: int
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

        item = SuggestItem(
            tag_id=str(tag_id) if tag_id is not None else None,
            key_name=str(key_name),
            key_city_id=_to_int(src.get("key_city_id"), 0),
            key_qh_id=_to_int(src.get("key_qh_id"), 0),
            key_cb_id=_to_int(src.get("key_cb_id"), 0),
            key_time=_to_int(src.get("key_time"), 0),
        )

        out.append({
            "item": item,
            "score": score,
            "key_name_norm_full": str(src.get("key_name_norm_full") or src.get("key_name_norm") or "").strip()
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
    # 0 trước, khác 0 sau
    city_not_zero = 0 if it.key_city_id == 0 else 1
    qh_not_zero = 0 if it.key_qh_id == 0 else 1
    cb_not_zero = 0 if it.key_cb_id == 0 else 1
    return (city_not_zero, qh_not_zero, cb_not_zero, tag_id_sort_key(it.tag_id))


def sort_within_keyname_runs(items: List[SuggestItem]) -> List[SuggestItem]:
    """
    GIỮ NGUYÊN thứ tự ưu tiên 'sát nhất' theo ES (tức là thứ tự items hiện tại),
    CHỈ sort trong từng CỤM LIÊN TIẾP có cùng key_name theo:
      key_city_id==0, key_qh_id==0, key_cb_id==0, tag_id asc
    """
    if not items:
        return items

    out: List[SuggestItem] = []
    i = 0
    n = len(items)
    while i < n:
        j = i + 1
        while j < n and items[j].key_name == items[i].key_name:
            j += 1

        run = items[i:j]
        if len(run) > 1:
            run.sort(key=group_sort_key)
        out.extend(run)
        i = j

    return out


def is_relevant(tokens: List[str], doc_norm_full: str) -> bool:
    if not doc_norm_full:
        return False
    for t in tokens:
        if not t:
            continue
        if t in doc_norm_full:
            return True
        if len(t) >= 4 and t[:3] in doc_norm_full:
            return True
    return False


def filter_results(parsed: List[Dict[str, Any]], q_norm: str, tokens: List[str], size: int) -> List[SuggestItem]:
    filtered: List[SuggestItem] = []
    for x in parsed:
        if x["score"] < MIN_ACCEPT_SCORE:
            continue
        if is_ascii_query(q_norm):
            if not is_relevant(tokens, x["key_name_norm_full"]):
                continue
        filtered.append(x["item"])

    # IMPORTANT: giữ đúng thứ tự ES theo key_name, chỉ sort trong các run
    filtered = sort_within_keyname_runs(filtered)
    return filtered[:size]


# ================== QUERIES ==================
def _source_fields() -> List[str]:
    return [
        "tag_id", "key_name",
        "key_city_id", "key_qh_id", "key_cb_id",
        "key_time",
        "key_name_norm_full", "key_name_norm",
    ]

def build_query_autocomplete(q_norm: str, size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": [
            "tag_id", "key_name",
            "key_city_id", "key_qh_id", "key_cb_id",
            "key_time",
            "key_name_norm_full", "key_name_norm",
        ],
        "query": {
            "bool": {
                "should": [
                    # 1) SÁT NHẤT: đúng nguyên cụm
                    {"match_phrase": {"key_name_norm_full": {"query": q_norm, "boost": 50}}},

                    # 2) GÕ DẦN THEO CỤM (ưu tiên field full, không ngram)
                    {"match_phrase_prefix": {"key_name_norm_full": {"query": q_norm, "boost": 20}}},

                    # 3) Bool-prefix trên field full (nhiều từ, từ cuối là prefix)
                    {"match_bool_prefix": {"key_name_norm_full": {"query": q_norm, "boost": 12}}},

                    # 4) Cuối cùng mới fallback sang edge_ngram để autocomplete mượt
                    {"match_bool_prefix": {"key_name_norm": {"query": q_norm, "boost": 3}}},
                ],
                "minimum_should_match": 1
            }
        }
    }


def build_query_fuzzy_phrase(q_norm: str, size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": _source_fields(),
        "query": {
            "function_score": {
                "query": {
                    "multi_match": {
                        "query": q_norm,
                        "fields": ["key_name_norm_full"],
                        "type": "best_fields",
                        "operator": "or",
                        "fuzziness": "AUTO",
                        "prefix_length": 1,
                        "max_expansions": 80,
                        "minimum_should_match": "1<75%"
                    }
                },
                "field_value_factor": {
                    "field": "key_time",
                    "modifier": "log1p",
                    "factor": 1.0,
                    "missing": 0
                },
                "boost_mode": "sum",
                "score_mode": "sum"
            }
        }
    }


def build_query_fuzzy_tokens(q_norm: str, size: int) -> Dict[str, Any]:
    tokens = [t for t in q_norm.split(" ") if t][:4]
    should = []
    for i, t in enumerate(tokens):
        boost = 3.0 if i == len(tokens) - 1 else 2.0
        should.append({
            "match": {
                "key_name_norm_full": {
                    "query": t,
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                    "max_expansions": 100,
                    "boost": boost
                }
            }
        })

    return {
        "size": size,
        "_source": _source_fields(),
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "should": should,
                        "minimum_should_match": 1
                    }
                },
                "field_value_factor": {
                    "field": "key_time",
                    "modifier": "log1p",
                    "factor": 1.0,
                    "missing": 0
                },
                "boost_mode": "sum",
                "score_mode": "sum"
            }
        }
    }

def build_query_hot(size: int) -> Dict[str, Any]:
    return {
        "size": size,
        "_source": ["tag_id", "key_name", "key_city_id", "key_qh_id", "key_cb_id", "key_time"],
        "query": {"match_all": {}},
        "sort": [{"tag_id": "asc"}]
    }


# ================== ENDPOINT ==================
@app.get("/suggest", response_model=SuggestResp)
def suggest(
    q: str = Query("", description="User typed prefix"),
    size: int = Query(10, ge=1, le=50),
    fallback: int = Query(1, ge=0, le=1, description="0 = allow empty, 1 = never empty")
):
    q_raw = (q or "").strip()
    q_norm = vn_remove_tone(q_raw)
    tokens = [t for t in q_norm.split(" ") if t]

    try:
        if not q_norm:
            data = es_search(build_query_hot(size))
            parsed = parse_items_with_score(data)
            items = [x["item"] for x in parsed]
            items = sort_within_keyname_runs(items)
            return SuggestResp(q=q_raw, size=size, items=items[:size])

        # tier 1
        data1 = es_search(build_query_autocomplete(q_norm, size))
        parsed1 = parse_items_with_score(data1)
        items1 = [x["item"] for x in parsed1]
        if items1:
            items1 = sort_within_keyname_runs(items1)
            return SuggestResp(q=q_raw, size=size, items=items1[:size])

        # tier 2A
        data2a = es_search(build_query_fuzzy_phrase(q_norm, size))
        parsed2a = parse_items_with_score(data2a)
        items2a = filter_results(parsed2a, q_norm, tokens, size)
        if items2a:
            return SuggestResp(q=q_raw, size=size, items=items2a)

        # tier 2B
        data2b = es_search(build_query_fuzzy_tokens(q_norm, size))
        parsed2b = parse_items_with_score(data2b)
        items2b = filter_results(parsed2b, q_norm, tokens, size)
        if items2b:
            return SuggestResp(q=q_raw, size=size, items=items2b)

        # tier 3
        if fallback == 1:
            data3 = es_search(build_query_hot(min(size, FALLBACK_HOT_SIZE)))
            parsed3 = parse_items_with_score(data3)
            items3 = [x["item"] for x in parsed3]
            items3 = sort_within_keyname_runs(items3)
            return SuggestResp(q=q_raw, size=size, items=items3[:size])

        return SuggestResp(q=q_raw, size=size, items=[])

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Elasticsearch error: {e}")