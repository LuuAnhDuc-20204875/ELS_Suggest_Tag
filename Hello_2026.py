from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

from pymongo import MongoClient
from pymongo.errors import PyMongoError

DEFAULT_MONGO_URI = os.getenv("MONGO_URI", "")

DB_NAME = "TAG_SEARCH"
COLL_NAME = "TAG_SEARCH_DATA"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract `key_name` from MongoDB collection.")
    p.add_argument("--mongo-uri", default=DEFAULT_MONGO_URI, help="MongoDB URI (or set MONGO_URI env).")
    p.add_argument("--db", default=DB_NAME, help="Database name. Default: TAG_SEARCH")
    p.add_argument("--collection", default=COLL_NAME, help="Collection name. Default: TAG_SEARCH_DATA")

    # mode
    p.add_argument(
        "--mode",
        choices=["unique", "all"],
        default="unique",
        help="unique: distinct key_name; all: fetch all key_name (may contain duplicates). Default: unique",
    )

    p.add_argument("--limit", type=int, default=0, help="Limit number of results (0 = no limit).")
    p.add_argument(
        "--query",
        default="",
        help='Optional Mongo filter as JSON string. Example: \'{"source":"fb"}\'',
    )
    p.add_argument(
        "--include-empty",
        action="store_true",
        help="Include empty string key_name. By default empty/None are filtered out.",
    )

    p.add_argument(
        "--out",
        default="",
        help="Output file path. Supports .jsonl or .csv. If empty, print to stdout as one per line.",
    )
    return p.parse_args()


def build_base_filter(extra_query: Optional[Dict[str, Any]], include_empty: bool) -> Dict[str, Any]:
    # Filter out missing/null/empty by default
    base: Dict[str, Any] = {"key_name": {"$exists": True}}
    if include_empty:
        # allow empty string, but still filter None
        base["key_name"]["$ne"] = None
    else:
        base["key_name"]["$nin"] = [None, ""]
    if extra_query:
        # Merge extra query with base using $and to be safe
        return {"$and": [base, extra_query]}
    return base


def parse_json_query(s: str) -> Optional[Dict[str, Any]]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        if not isinstance(obj, dict):
            raise ValueError("Query JSON must be an object/dict.")
        return obj
    except Exception as e:
        raise ValueError(f"Invalid --query JSON: {e}") from e


def iter_all_key_names(col, mongo_filter: Dict[str, Any], limit: int) -> Iterable[str]:
    cursor = col.find(mongo_filter, {"_id": 0, "key_name": 1})
    if limit and limit > 0:
        cursor = cursor.limit(limit)
    for doc in cursor:
        v = doc.get("key_name")
        if v is None:
            continue
        # Convert non-string values to string (optional safety)
        yield v if isinstance(v, str) else str(v)


def get_unique_key_names(col, mongo_filter: Dict[str, Any], limit: int) -> List[str]:
    # distinct returns list
    vals = col.distinct("key_name", filter=mongo_filter)
    # Clean/normalize
    out: List[str] = []
    for v in vals:
        if v is None:
            continue
        out.append(v if isinstance(v, str) else str(v))
    out.sort()
    if limit and limit > 0:
        out = out[:limit]
    return out


def write_jsonl(path: str, values: Iterable[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for v in values:
            f.write(json.dumps({"key_name": v}, ensure_ascii=False) + "\n")


def write_csv(path: str, values: Iterable[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key_name"])
        for v in values:
            w.writerow([v])


def main() -> int:
    args = parse_args()

    try:
        extra_query = parse_json_query(args.query)
        mongo_filter = build_base_filter(extra_query, include_empty=args.include_empty)

        client = MongoClient(args.mongo_uri)
        db = client[args.db]
        col = db[args.collection]

        if args.mode == "unique":
            values: Iterable[str] = get_unique_key_names(col, mongo_filter, args.limit)
        else:
            values = iter_all_key_names(col, mongo_filter, args.limit)

        out_path = (args.out or "").strip()
        if not out_path:
            # stdout: one per line
            for v in values:
                print(v)
            return 0

        lower = out_path.lower()
        if lower.endswith(".jsonl"):
            write_jsonl(out_path, values)
        elif lower.endswith(".csv"):
            write_csv(out_path, values)
        else:
            # default: plain text
            with open(out_path, "w", encoding="utf-8") as f:
                for v in values:
                    f.write(v + "\n")

        print(f"OK: wrote output to {out_path}")
        return 0

    except (PyMongoError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())