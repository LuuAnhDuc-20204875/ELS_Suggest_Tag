#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from typing import Any, Dict, Tuple

from pymongo import MongoClient


DEFAULT_MONGO_URI = os.getenv("MONGO_URI", "").strip()
DEFAULT_DB = "TAG_SEARCH"
DEFAULT_COLL = "TAG_SEARCH_DATA"


def vn_remove_tone(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--mongo-uri", default=DEFAULT_MONGO_URI)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--collection", default=DEFAULT_COLL)
    p.add_argument("--out", default="tag_stats_with_id.json")
    p.add_argument("--limit", type=int, default=0)
    return p.parse_args()


def main():
    args = parse_args()
    client = MongoClient(args.mongo_uri)
    col = client[args.db][args.collection]

    # Bạn sửa field name tag_id theo đúng data thực tế:
    # ví dụ: "tag_id", "key_id", "id", ...
    TAG_ID_FIELD = "tag_id"
    KEY_NAME_FIELD = "key_name"

    cursor = col.find(
        {KEY_NAME_FIELD: {"$exists": True, "$nin": [None, ""]}},
        {"_id": 0, TAG_ID_FIELD: 1, KEY_NAME_FIELD: 1},
        no_cursor_timeout=True
    )

    # group theo (key_name_norm, tag_id, key_name_display)
    counts: Dict[Tuple[str, str, str], int] = {}

    for doc in cursor:
        key_name = (doc.get(KEY_NAME_FIELD) or "").strip()
        if not key_name:
            continue
        tag_id = doc.get(TAG_ID_FIELD)
        if tag_id is None:
            # nếu doc không có tag_id thì bỏ qua
            continue
        tag_id = str(tag_id)

        key_norm = vn_remove_tone(key_name)

        k = (key_norm, tag_id, key_name)
        counts[k] = counts.get(k, 0) + 1

    cursor.close()

    items = []
    for (key_norm, tag_id, key_name), c in counts.items():
        items.append({
            "tag_id": tag_id,
            "key_name": key_name,
            "key_name_norm": key_norm,
            "count": c
        })

    # sort: count desc
    items.sort(key=lambda x: (-x["count"], x["key_name_norm"], x["tag_id"]))

    if args.limit and args.limit > 0:
        items = items[:args.limit]

    payload = {
        "db": args.db,
        "collection": args.collection,
        "total": len(items),
        "items": items
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"OK: wrote {len(items)} items -> {args.out}")


if __name__ == "__main__":
    main()