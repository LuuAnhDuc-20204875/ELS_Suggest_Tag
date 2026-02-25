#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tag Data API (Elasticsearch Upsert + Silent Dedup) - FULL

- Bảo vệ bằng API KEY qua header: X-API-KEY
- Đồng bộ full fields giống Mongo (upsert_full)
- Có check trùng "im lặng" (silent): nếu trùng key_name + (city/qh/cb) thì SKIP, không báo duplicate
- Không cần allow_duplicate
- Có bulk_upsert_full (silent dedup từng item)
- Tự tạo key_name_norm + key_name_norm_full từ key_name (bỏ dấu)

ENV:
  ES_BASE_URL=http://localhost:9200
  ES_INDEX=tag_data_v1
  TAGDATA_API_KEY=Hungha@#$TAGDATA_2026
  ES_TIMEOUT=10
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field


# ================== CONFIG ==================
ES_BASE_URL = os.getenv("ES_BASE_URL", "http://localhost:9200").rstrip("/")
ES_INDEX = os.getenv("ES_INDEX", "tag_data_v1").strip()
HTTP_TIMEOUT = float(os.getenv("ES_TIMEOUT", "10"))

# API KEY bảo vệ (NodeJS gửi header này)
API_KEY = os.getenv("TAGDATA_API_KEY", "").strip()
API_KEY_HEADER = "X-API-KEY"

app = FastAPI(title="Tag Data API (Silent Dedup Upsert)", version="2.1.0")


# ================== HELPERS ==================
def vn_remove_tone(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def to_iso(dt: Optional[Union[str, datetime]]) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(dt).strip()
    return s or None


def require_api_key(x_api_key: Optional[str]) -> None:
    # nếu API_KEY rỗng => không khóa
    if not API_KEY:
        return
    if (x_api_key or "").strip() != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def es_request(method: str, path: str, json_body: Optional[dict] = None) -> Dict[str, Any]:
    url = f"{ES_BASE_URL}/{path.lstrip('/')}"
    try:
        r = requests.request(method, url, json=json_body, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Elasticsearch error: {e}")


def es_bulk(ndjson: str) -> Dict[str, Any]:
    url = f"{ES_BASE_URL}/_bulk"
    try:
        r = requests.post(
            url,
            data=ndjson.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Elasticsearch bulk error: {e}")


# ================== INPUT MODELS (FULL FIELDS) ==================
class TagUpsertFullIn(BaseModel):
    tag_id: int = Field(..., description="Primary key tag_id")

    created_at: Optional[Union[str, datetime]] = None
    updated_at: Optional[Union[str, datetime]] = None

    key_cate_id: int = 0
    key_cate_lq: int = 0
    key_cb_id: int = 0
    key_city_id: int = 0
    key_qh_id: int = 0
    key_time: int = 0

    key_desc: str = ""
    key_h1: str = ""
    key_key: str = ""
    key_lq: str = ""
    key_name: str = ""
    key_tit: str = ""


class SimpleResp(BaseModel):
    ok: bool
    tag_id: int
    result: str  # created | updated | skipped_duplicate


class BulkResp(BaseModel):
    ok: bool
    total: int
    upserted: int
    skipped_duplicate: int


# ================== DEDUP (SILENT) ==================
def is_duplicate_full(item: TagUpsertFullIn) -> bool:
    """
    Trùng nếu tồn tại doc khác tag_id nhưng cùng:
      key_name.raw + key_city_id + key_qh_id + key_cb_id
    """
    key_name = (item.key_name or "").strip()
    if not key_name:
        return False

    body = {
        "size": 1,
        "_source": ["tag_id"],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"key_name.raw": key_name}},
                    {"term": {"key_city_id": int(item.key_city_id)}},
                    {"term": {"key_qh_id": int(item.key_qh_id)}},
                    {"term": {"key_cb_id": int(item.key_cb_id)}},
                ],
                "must_not": [
                    {"term": {"tag_id": int(item.tag_id)}}
                ],
            }
        },
    }

    data = es_request("POST", f"{ES_INDEX}/_search", body)
    hits = data.get("hits", {}).get("hits", [])
    return len(hits) > 0


def build_full_doc(item: TagUpsertFullIn) -> Dict[str, Any]:
    d = item.model_dump()

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    key_name = (d.get("key_name") or "").strip()
    key_norm = vn_remove_tone(key_name)

    d["created_at"] = to_iso(d.get("created_at"))
    d["updated_at"] = to_iso(d.get("updated_at")) or now_iso

    # thêm norm fields phục vụ suggest/search
    d["key_name_norm"] = key_norm
    d["key_name_norm_full"] = key_norm

    # đảm bảo key_name là string sạch
    d["key_name"] = key_name

    return d


# ================== ENDPOINTS ==================
@app.get("/health")
def health():
    return {"ok": True, "index": ES_INDEX}


@app.get("/tag/{tag_id}")
def get_tag(tag_id: int, x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER)):
    require_api_key(x_api_key)
    return es_request("GET", f"{ES_INDEX}/_doc/{tag_id}")


@app.post("/tag/upsert_full", response_model=SimpleResp)
def upsert_full(
    data: TagUpsertFullIn,
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
):
    """
    NodeJS gọi endpoint này để insert/update full fields.
    API tự check trùng và nếu trùng -> skip im lặng.
    """
    require_api_key(x_api_key)

    # 1) silent dedup
    if is_duplicate_full(data):
        return SimpleResp(ok=True, tag_id=int(data.tag_id), result="skipped_duplicate")

    # 2) upsert
    doc = build_full_doc(data)
    tag_id = int(doc["tag_id"])

    body = {"doc": doc, "doc_as_upsert": True}
    resp = es_request("POST", f"{ES_INDEX}/_update/{tag_id}", body)
    return SimpleResp(ok=True, tag_id=tag_id, result=resp.get("result", "updated"))


@app.post("/tag/bulk_upsert_full", response_model=BulkResp)
def bulk_upsert_full(
    items: List[TagUpsertFullIn],
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
):
    """
    Bulk upsert full fields.
    - Mỗi item tự silent dedup: nếu trùng -> skip
    - Các item còn lại bulk update/upsert
    """
    require_api_key(x_api_key)

    if not items:
        return BulkResp(ok=True, total=0, upserted=0, skipped_duplicate=0)

    to_upsert: List[TagUpsertFullIn] = []
    skipped = 0

    for it in items:
        if is_duplicate_full(it):
            skipped += 1
            continue
        to_upsert.append(it)

    # build NDJSON bulk update/upsert
    import json as _json

    lines: List[str] = []
    for it in to_upsert:
        doc = build_full_doc(it)
        tag_id = int(doc["tag_id"])
        lines.append(_json.dumps({"update": {"_index": ES_INDEX, "_id": str(tag_id)}}, ensure_ascii=False))
        lines.append(_json.dumps({"doc": doc, "doc_as_upsert": True}, ensure_ascii=False))

    if lines:
        resp = es_bulk("\n".join(lines) + "\n")
        if resp.get("errors"):
            errors = [x for x in resp.get("items", []) if "error" in list(x.values())[0]]
            raise HTTPException(status_code=500, detail={"bulk_errors_sample": errors[:3]})

        # refresh để search thấy ngay
        es_request("POST", f"{ES_INDEX}/_refresh")

    return BulkResp(ok=True, total=len(items), upserted=len(to_upsert), skipped_duplicate=skipped)