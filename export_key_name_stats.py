#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List

from pymongo import MongoClient


DEFAULT_MONGO_URI = os.getenv("MONGO_URI", "").strip()
DEFAULT_DB = "TAG_SEARCH"
DEFAULT_COLL = "TAG_SEARCH_DATA"


def normalize_key_name(s: str) -> str:
    """
    Normalize để dùng cho search/suggest:
    - strip
    - collapse spaces
    - lowercase
    (Bạn có thể mở rộng: bỏ dấu tiếng Việt, bỏ ký tự đặc biệt...)
    """
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def parse_args():
    p = argparse.ArgumentParser(description="Export key_name stats (count duplicates) to JSON.")
    p.add_argument("--mongo-uri", default=DEFAULT_MONGO_URI)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--collection", default=DEFAULT_COLL)

    p.add_argument("--out", default="key_name_stats.json", help="Output JSON file path")
    p.add_argument("--min-count", type=int, default=1, help="Only keep items with count >= min-count")
    p.add_argument("--only-duplicates", action="store_true", help="Only keep duplicated key_name (count >= 2)")
    p.add_argument("--limit", type=int, default=0, help="Limit number of items (0 = no limit)")
    p.add_argument("--include-empty", action="store_true", help="Include empty key_name")
    p.add_argument("--use-normalized", action="store_true",
                   help="Group by normalized key_name (recommended for suggestions)")

    return p.parse_args()


def main():
    args = parse_args()

    client = MongoClient(args.mongo_uri)
    col = client[args.db][args.collection]

    # filter out null/empty by default
    match_filter: Dict[str, Any] = {"key_name": {"$exists": True}}
    if args.include_empty:
        match_filter["key_name"]["$ne"] = None
    else:
        match_filter["key_name"]["$nin"] = [None, ""]

    # Choose grouping key
    if args.use_normalized:
        # group by normalized key_name (lowercase, collapse spaces)
        # Mongo aggregation for normalize (basic): $toLower + $trim + $replaceAll loop is limited
        # => easiest: pull raw key_name then normalize in Python via streaming
        cursor = col.find(match_filter, {"_id": 0, "key_name": 1})
        counts: Dict[str, int] = {}
        for doc in cursor:
            raw = doc.get("key_name")
            if raw is None:
                continue
            raw_str = raw if isinstance(raw, str) else str(raw)
            k = normalize_key_name(raw_str)
            if (not args.include_empty) and (k == ""):
                continue
            counts[k] = counts.get(k, 0) + 1

        items = [{"key_name": k, "count": c} for k, c in counts.items()]
        items.sort(key=lambda x: (-x["count"], x["key_name"]))
    else:
        # group by raw key_name directly in MongoDB
        pipeline: List[Dict[str, Any]] = [
            {"$match": match_filter},
            {"$group": {"_id": "$key_name", "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
        items = []
        for x in col.aggregate(pipeline, allowDiskUse=True):
            k = x["_id"]
            k_str = k if isinstance(k, str) else str(k)
            items.append({"key_name": k_str, "count": int(x["count"])})

    # apply filters
    min_count = max(1, args.min_count)
    if args.only_duplicates:
        min_count = max(min_count, 2)

    items = [it for it in items if it["count"] >= min_count]

    if args.limit and args.limit > 0:
        items = items[: args.limit]

    payload = {
        "db": args.db,
        "collection": args.collection,
        "field": "key_name",
        "grouping": "normalized" if args.use_normalized else "raw",
        "total_unique": len(items),
        "items": items,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"OK: wrote {len(items)} items to {args.out}")


if __name__ == "__main__":
    main()
