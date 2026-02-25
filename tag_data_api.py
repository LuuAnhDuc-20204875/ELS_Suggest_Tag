#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

# API KEY bảo vệ
API_KEY = os.getenv("TAGDATA_API_KEY", "Hungha@#$TAGDATA_2026").strip()
API_KEY_HEADER = "X-API-KEY"  # NodeJS gửi header này

app = FastAPI(title="Tag Data API (ES Upsert + Dedup)", version="1.1.0")


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
    # nếu bạn chưa set TAGDATA_API_KEY thì coi như không khóa
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


# ================== MODELS ==================
class TagDataIn(BaseModel):
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


class UpsertResp(BaseModel):
    ok: bool
    tag_id: int
    result: str
    duplicated: bool = False
    duplicate_tag_ids: List[int] = []


class BulkUpsertResp(BaseModel):
    ok: bool
    total: int
    upserted: int
    duplicated: int
    duplicates: List[Dict[str, Any]]


# ================== DEDUP LOGIC ==================
def find_duplicates(item: TagDataIn) -> List[int]:
    """
    Check trùng theo:
      key_name.raw + key_city_id + key_qh_id + key_cb_id
    Trả về list tag_id khác với item.tag_id nếu thấy trùng.
    """
    key_name = (item.key_name or "").strip()
    if not key_name:
        return []

    # yêu cầu mapping có key_name.raw (keyword) -> bạn đã tạo trong mapping tag_data_v1
    body = {
        "size": 10,
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
                ]
            }
        }
    }

    data = es_request("POST", f"{ES_INDEX}/_search", body)
    hits = data.get("hits", {}).get("hits", [])
    dup_ids: List[int] = []
    for h in hits:
        src = h.get("_source") or {}
        tid = src.get("tag_id")
        if tid is None:
            continue
        try:
            dup_ids.append(int(tid))
        except Exception:
            continue
    return dup_ids


def build_doc(item: TagDataIn) -> Dict[str, Any]:
    d = item.model_dump()

    key_name = (d.get("key_name") or "").strip()
    key_norm = vn_remove_tone(key_name)

    d["created_at"] = to_iso(d.get("created_at"))
    d["updated_at"] = to_iso(d.get("updated_at")) or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # norm fields phục vụ search/suggest
    d["key_name_norm"] = key_norm
    d["key_name_norm_full"] = key_norm

    return d


# ================== ENDPOINTS ==================
@app.get("/health")
def health():
    return {"ok": True, "index": ES_INDEX}


@app.get("/tag/{tag_id}")
def get_tag(tag_id: int, x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER)):
    require_api_key(x_api_key)
    return es_request("GET", f"{ES_INDEX}/_doc/{tag_id}")


@app.post("/tag/upsert", response_model=UpsertResp)
def upsert_tag(
    data: TagDataIn,
    allow_duplicate: int = 0,  # 0 = chặn trùng, 1 = vẫn cho upsert
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
):
    require_api_key(x_api_key)

    # 1) check trùng
    dup_ids = find_duplicates(data)
    if dup_ids and allow_duplicate == 0:
        # chặn trùng
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Duplicate key_name with same (key_city_id,key_qh_id,key_cb_id)",
                "duplicate_tag_ids": dup_ids
            }
        )

    # 2) upsert
    doc = build_doc(data)
    tag_id = int(doc["tag_id"])

    body = {"doc": doc, "doc_as_upsert": True}
    resp = es_request("POST", f"{ES_INDEX}/_update/{tag_id}", body)
    result = resp.get("result", "updated")

    return UpsertResp(
        ok=True,
        tag_id=tag_id,
        result=result,
        duplicated=bool(dup_ids),
        duplicate_tag_ids=dup_ids
    )


@app.post("/tag/bulk_upsert", response_model=BulkUpsertResp)
def bulk_upsert(
    items: List[TagDataIn],
    allow_duplicate: int = 0,
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
):
    require_api_key(x_api_key)

    if not items:
        return BulkUpsertResp(ok=True, total=0, upserted=0, duplicated=0, duplicates=[])

    # bulk strategy:
    # - với từng item: check duplicate (nhẹ vì size nhỏ), nếu trùng và allow_duplicate=0 -> skip + ghi log duplicates
    # - upsert phần còn lại bằng _bulk
    duplicates_report: List[Dict[str, Any]] = []
    to_upsert: List[TagDataIn] = []

    for it in items:
        dup_ids = find_duplicates(it)
        if dup_ids and allow_duplicate == 0:
            duplicates_report.append({
                "tag_id": int(it.tag_id),
                "key_name": it.key_name,
                "key_city_id": int(it.key_city_id),
                "key_qh_id": int(it.key_qh_id),
                "key_cb_id": int(it.key_cb_id),
                "duplicate_tag_ids": dup_ids
            })
            continue
        to_upsert.append(it)

    # build ndjson for _bulk update/upsert
    import json as _json
    lines: List[str] = []
    for it in to_upsert:
        doc = build_doc(it)
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

    return BulkUpsertResp(
        ok=True,
        total=len(items),
        upserted=len(to_upsert),
        duplicated=len(duplicates_report),
        duplicates=duplicates_report
    )