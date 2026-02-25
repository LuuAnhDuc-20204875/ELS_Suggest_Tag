#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import time
import json
import unicodedata
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import requests
from pymongo import MongoClient


# ====== CONFIG ======
MONGO_URI = os.getenv("MONGO_URI", "mongodb://myuser_duc:Anhduc14062002%40%23%24@123.24.206.25:27017/?authSource=admin")
MONGO_DB = os.getenv("MONGO_DB", "TAG_SEARCH")
MONGO_COLL = os.getenv("MONGO_COLL", "TAG_SEARCH_DATA")

ES_BASE_URL = os.getenv("ES_BASE_URL", "http://localhost:9200").rstrip("/")
ES_INDEX = os.getenv("ES_INDEX", "tag_data_v1")

BULK_CHUNK = int(os.getenv("BULK_CHUNK", "2000"))
HTTP_TIMEOUT = float(os.getenv("ES_TIMEOUT", "30"))


def vn_remove_tone(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def to_iso(dt_val: Any) -> Optional[str]:
    # pymongo trả datetime object cho created_at/updated_at
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        # luôn chuyển UTC ISO
        return dt_val.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def iter_docs(col, updated_after: Optional[datetime] = None) -> Iterable[Dict[str, Any]]:
    q = {}
    if updated_after is not None:
        q = {"updated_at": {"$gte": updated_after}}
    cur = col.find(q, no_cursor_timeout=True)
    for d in cur:
        yield d
    cur.close()


def transform(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert Mongo doc -> ES _source
    - _id bỏ qua
    - tag_id dùng làm ES _id (ổn định)
    - add key_name_norm, key_name_norm_full
    """
    tag_id = doc.get("tag_id")
    key_name = (doc.get("key_name") or "").strip()

    out: Dict[str, Any] = {}

    # fields numeric/date/text
    out["tag_id"] = int(tag_id) if tag_id is not None else None
    out["created_at"] = to_iso(doc.get("created_at"))
    out["updated_at"] = to_iso(doc.get("updated_at"))

    for f in ["key_cate_id","key_cate_lq","key_cb_id","key_city_id","key_qh_id","key_time"]:
        v = doc.get(f)
        out[f] = int(v) if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()) else (v or 0)

    for f in ["key_desc","key_h1","key_key","key_lq","key_name","key_tit"]:
        v = doc.get(f)
        out[f] = "" if v is None else str(v)

    # Norm fields for suggest/search
    kn_norm = vn_remove_tone(key_name)
    out["key_name_norm"] = kn_norm
    out["key_name_norm_full"] = kn_norm  # full token field

    return out


def bulk_upsert(docs: List[Dict[str, Any]]) -> None:
    if not docs:
        return
    url = f"{ES_BASE_URL}/_bulk"
    headers = {"Content-Type": "application/x-ndjson"}
    lines = []

    for doc in docs:
        tag_id = doc.get("tag_id")
        if tag_id is None:
            continue
        doc_id = str(tag_id)

        lines.append(json.dumps({"update": {"_index": ES_INDEX, "_id": doc_id}}, ensure_ascii=False))
        lines.append(json.dumps({"doc": doc, "doc_as_upsert": True}, ensure_ascii=False))

    data = "\n".join(lines) + "\n"
    r = requests.post(url, headers=headers, data=data.encode("utf-8"), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    resp = r.json()
    if resp.get("errors"):
        errors = [x for x in resp.get("items", []) if "error" in list(x.values())[0]]
        print("Bulk errors sample:", json.dumps(errors[:3], ensure_ascii=False))
        raise SystemExit(2)


def main():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][MONGO_COLL]

    # full sync (lần đầu): không filter updated_after
    # nếu muốn incremental: set UPDATED_AFTER_ISO env, ví dụ 2026-02-20T00:00:00Z
    updated_after_iso = os.getenv("UPDATED_AFTER_ISO", "").strip()
    updated_after = None
    if updated_after_iso:
        updated_after = datetime.fromisoformat(updated_after_iso.replace("Z", "+00:00"))

    batch: List[Dict[str, Any]] = []
    total = 0
    t0 = time.time()

    for mongo_doc in iter_docs(col, updated_after=updated_after):
        src = transform(mongo_doc)
        batch.append(src)
        if len(batch) >= BULK_CHUNK:
            bulk_upsert(batch)
            total += len(batch)
            print(f"Upserted {total} docs...")
            batch.clear()

    if batch:
        bulk_upsert(batch)
        total += len(batch)

    # refresh
    requests.post(f"{ES_BASE_URL}/{ES_INDEX}/_refresh", timeout=HTTP_TIMEOUT).raise_for_status()

    print(f"DONE. Total upserted: {total}. Took {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()