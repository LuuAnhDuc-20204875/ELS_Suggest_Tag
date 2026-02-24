#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import time
from typing import Dict, Any, List
import requests


def bulk_index(es_base: str, index: str, docs: List[Dict[str, Any]], chunk_size: int = 2000):
    es_base = es_base.rstrip("/")
    url = f"{es_base}/_bulk"
    headers = {"Content-Type": "application/x-ndjson"}

    for i in range(0, len(docs), chunk_size):
        chunk = docs[i:i + chunk_size]
        lines = []
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        for d in chunk:
            tag_id = d.get("tag_id")
            key_name = (d.get("key_name") or "").strip()
            key_norm = (d.get("key_name_norm") or "").strip()
            if not tag_id or not key_name or not key_norm:
                continue

            count = int(d.get("count") or 0)

            # _id theo key_norm + tag_id để tránh đè mất trường hợp 1 norm có nhiều tag_id
            doc_id = f"{key_norm}__{tag_id}"

            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}, ensure_ascii=False))
            lines.append(json.dumps({
                "tag_id": str(tag_id),
                "key_name": key_name,
                "key_name_norm": key_norm,
                "count": count,
                "updated_at": now_iso
            }, ensure_ascii=False))

        if not lines:
            continue

        data = "\n".join(lines) + "\n"
        r = requests.post(url, headers=headers, data=data.encode("utf-8"), timeout=60)
        r.raise_for_status()
        resp = r.json()
        if resp.get("errors"):
            errors = [x for x in resp.get("items", []) if "error" in list(x.values())[0]]
            print("Bulk had errors. Sample:", json.dumps(errors[:3], ensure_ascii=False))
            raise SystemExit(2)

        print(f"Indexed batch {i//chunk_size + 1}: {len(chunk)} docs")

    requests.post(f"{es_base}/{index}/_refresh", timeout=60).raise_for_status()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", required=True, help="Input JSON (tag_stats_with_id.json)")
    p.add_argument("--es", default="http://localhost:9200")
    p.add_argument("--index", default="tag_suggest_v3")
    p.add_argument("--chunk", type=int, default=2000)
    args = p.parse_args()

    with open(args.inp, "r", encoding="utf-8") as f:
        payload = json.load(f)

    items = payload.get("items")
    if not isinstance(items, list):
        raise SystemExit("Invalid input JSON: expected { items: [...] }")

    bulk_index(args.es, args.index, items, chunk_size=args.chunk)
    print("DONE")


if __name__ == "__main__":
    main()