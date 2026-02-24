#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel


ES_BASE_URL = os.getenv("ES_BASE_URL", "http://localhost:9200").rstrip("/")
SUGGEST_INDEX = os.getenv("SUGGEST_INDEX", "tag_suggest_v3").strip()  # đổi sang tag_suggest_v3 nếu có tag_id
HTTP_TIMEOUT = float(os.getenv("ES_TIMEOUT", "5"))

app = FastAPI(title="TAG Suggest API", version="1.0.1")


class SuggestItem(BaseModel):
    # để OPTIONAL để không bị 500 nếu index chưa có tag_id
    tag_id: Optional[str] = None
    key_name: str
    count: int


class SuggestResp(BaseModel):
    q: str
    size: int
    items: List[SuggestItem]


def build_query(q: str, size: int) -> Dict[str, Any]:
    q = (q or "").strip()

    # trả hot tags nếu q rỗng
    if not q:
        return {
            "size": size,
            "_source": ["tag_id", "key_name", "count"],
            "query": {"match_all": {}},
            "sort": [{"count": "desc"}]
        }

    # autocomplete nhiều từ: match_bool_prefix
    return {
        "size": size,
        "_source": ["tag_id", "key_name", "count"],
        "query": {
            "function_score": {
                "query": {
                    "match_bool_prefix": {
                        # index của bạn phải có key_name_norm (v2/v3 đều có)
                        "key_name_norm": {"query": q}
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
        # ES hỗ trợ GET với body, nhưng để chắc chắn tương thích hơn, dùng POST cũng OK
        r = requests.post(url, json=body, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Elasticsearch error: {e}")

    hits = data.get("hits", {}).get("hits", [])
    items: List[SuggestItem] = []

    for h in hits:
        src = h.get("_source") or {}
        key_name = src.get("key_name")
        if not key_name:
            continue

        tag_id = src.get("tag_id")
        count = int(src.get("count") or 0)

        items.append(SuggestItem(
            tag_id=str(tag_id) if tag_id is not None else None,
            key_name=str(key_name),
            count=count
        ))

    return SuggestResp(q=q, size=size, items=items)