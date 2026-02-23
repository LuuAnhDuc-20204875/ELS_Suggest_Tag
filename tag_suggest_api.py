#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from typing import List, Dict, Any
import requests
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel


ES_BASE_URL = os.getenv("ES_BASE_URL", "http://localhost:9200").rstrip("/")
SUGGEST_INDEX = os.getenv("SUGGEST_INDEX", "tag_suggest_v2")
HTTP_TIMEOUT = float(os.getenv("ES_TIMEOUT", "5"))


app = FastAPI(title="TAG Suggest API", version="1.0.0")


class SuggestItem(BaseModel):
    key_name: str
    count: int


class SuggestResp(BaseModel):
    q: str
    size: int
    items: List[SuggestItem]


def build_query(q: str, size: int) -> Dict[str, Any]:
    q = (q or "").strip()
    if not q:
        # nếu rỗng, trả top theo count
        return {
            "size": size,
            "_source": ["key_name", "count"],
            "query": {"match_all": {}},
            "sort": [{"count": "desc"}]
        }

    return {
    "size": size,
    "_source": ["key_name", "count"],
    "query": {
        "function_score": {
        "query": {
            "match_bool_prefix": {
            "key_name_norm": {
                "query": q
            }
            }
        },
        "field_value_factor": {
            "field": "count",
            "modifier": "log1p",
            "factor": 1.2
        },
        "boost_mode": "sum",
        "score_mode": "sum"
        }
    }
    }


@app.get("/suggest", response_model=SuggestResp)
def suggest(
    q: str = Query("", description="User typed prefix"),
    size: int = Query(10, ge=1, le=50)
):
    body = build_query(q, size)
    url = f"{ES_BASE_URL}/{SUGGEST_INDEX}/_search"

    try:
        r = requests.get(url, json=body, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Elasticsearch error: {e}")

    hits = data.get("hits", {}).get("hits", [])
    items = []
    for h in hits:
        src = h.get("_source") or {}
        kn = src.get("key_name")
        if not kn:
            continue
        items.append(SuggestItem(key_name=kn, count=int(src.get("count") or 0)))

    return SuggestResp(q=q, size=size, items=items)