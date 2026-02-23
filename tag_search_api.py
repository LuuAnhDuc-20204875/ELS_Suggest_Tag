import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError


# ================== CONFIG ==================
MONGO_URI = os.getenv("MONGO_URI", "mongodb://myuser_duc:Anhduc14062002%40%23%24@123.24.206.25:27017/?authSource=admin")
DB_NAME = "TAG_SEARCH"
COLL_NAME = "TAG_SEARCH_DATA"

# Field dùng để upsert (bạn có thể đổi thành: "tag", "keyword", "_id", ...)
UPSERT_KEY = os.getenv("TAG_UPSERT_KEY", "tag_id")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
col = db[COLL_NAME]

app = FastAPI(title="TAG_SEARCH API", version="1.0.0")


# ================== MODELS ==================
class UpsertOneReq(BaseModel):
    # dữ liệu linh hoạt, không ép schema cứng
    data: Dict[str, Any] = Field(..., description="Bản ghi cần upsert")
    # cho phép override key upsert theo request (optional)
    upsert_key: Optional[str] = Field(None, description="Field dùng để upsert (mặc định lấy từ env TAG_UPSERT_KEY)")


class UpsertManyReq(BaseModel):
    items: List[Dict[str, Any]] = Field(..., min_items=1)
    upsert_key: Optional[str] = None
    ordered: bool = False  # bulk có thể chạy unordered cho nhanh


class UpdateReq(BaseModel):
    # filter theo Mongo query
    filter: Dict[str, Any] = Field(..., description="Mongo filter, ví dụ {'tag':'python'}")
    # update document theo Mongo update operator
    update: Dict[str, Any] = Field(
        ...,
        description="Mongo update, ví dụ {'$set': {'status': 1}} hoặc {'$inc': {'count': 1}}",
    )
    upsert: bool = False
    multi: bool = True  # True => update_many, False => update_one


class ApiResp(BaseModel):
    ok: bool
    message: str
    data: Optional[Dict[str, Any]] = None


def _now():
    return datetime.utcnow()


def _ensure_operator_update(update: Dict[str, Any]) -> Dict[str, Any]:
    """
    Nếu user gửi update dạng plain object (vd {"a":1}) thì tự convert thành {"$set": {...}}
    """
    if not update:
        raise HTTPException(status_code=400, detail="update is empty")

    has_operator = any(k.startswith("$") for k in update.keys())
    if has_operator:
        return update
    return {"$set": update}


# ================== ROUTES ==================
@app.get("/health", response_model=ApiResp)
def health():
    return ApiResp(ok=True, message="OK", data={"db": DB_NAME, "collection": COLL_NAME, "upsert_key": UPSERT_KEY})


@app.post("/tag-search/upsert", response_model=ApiResp)
def upsert_one(req: UpsertOneReq):
    try:
        upsert_key = (req.upsert_key or UPSERT_KEY).strip()
        if not upsert_key:
            raise HTTPException(status_code=400, detail="upsert_key is empty")

        if upsert_key not in req.data:
            raise HTTPException(status_code=400, detail=f"Missing upsert key '{upsert_key}' in data")

        key_val = req.data.get(upsert_key)

        now = _now()

        # $set: cập nhật các field + updated_at (KHÔNG set created_at)
        set_doc = dict(req.data)
        set_doc.pop("created_at", None)        # tránh conflict
        set_doc["updated_at"] = now

        # $setOnInsert: chỉ set created_at khi insert lần đầu
        created_at = req.data.get("created_at", now)

        update_doc = {
            "$set": set_doc,
            "$setOnInsert": {"created_at": created_at},
        }

        res = col.update_one({upsert_key: key_val}, update_doc, upsert=True)

        return ApiResp(
            ok=True,
            message="upsert_one success",
            data={
                "matched_count": res.matched_count,
                "modified_count": res.modified_count,
                "upserted_id": str(res.upserted_id) if res.upserted_id else None,
                "filter": {upsert_key: key_val},
            },
        )
    except HTTPException:
        raise
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Mongo error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/tag-search/upsert-many", response_model=ApiResp)
def upsert_many(req: UpsertManyReq):
    try:
        upsert_key = (req.upsert_key or UPSERT_KEY).strip()
        if not upsert_key:
            raise HTTPException(status_code=400, detail="upsert_key is empty")

        ops: List[UpdateOne] = []
        skipped = 0
        now = _now()

        for item in req.items:
            if upsert_key not in item:
                skipped += 1
                continue

            key_val = item.get(upsert_key)

            set_doc = dict(item)
            set_doc.pop("created_at", None)   # tránh conflict
            set_doc["updated_at"] = now

            created_at = item.get("created_at", now)

            ops.append(
                UpdateOne(
                    {upsert_key: key_val},
                    {"$set": set_doc, "$setOnInsert": {"created_at": created_at}},
                    upsert=True,
                )
            )

        if not ops:
            raise HTTPException(status_code=400, detail=f"No valid items containing '{upsert_key}'")

        bulk = col.bulk_write(ops, ordered=req.ordered)

        return ApiResp(
            ok=True,
            message="upsert_many success",
            data={
                "matched_count": bulk.matched_count,
                "modified_count": bulk.modified_count,
                "upserted_count": len(bulk.upserted_ids or {}),
                "skipped_items": skipped,
            },
        )
    except HTTPException:
        raise
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Mongo error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.patch("/tag-search/update", response_model=ApiResp)
def update_docs(req: UpdateReq):
    try:
        update_doc = _ensure_operator_update(req.update)

        # auto updated_at nếu có $set hoặc là update plain -> $set
        if "$set" not in update_doc:
            update_doc["$set"] = {}
        update_doc["$set"]["updated_at"] = _now()

        if req.multi:
            res = col.update_many(req.filter, update_doc, upsert=req.upsert)
        else:
            res = col.update_one(req.filter, update_doc, upsert=req.upsert)

        return ApiResp(
            ok=True,
            message="update success",
            data={
                "matched_count": res.matched_count,
                "modified_count": res.modified_count,
                "upserted_id": str(res.upserted_id) if getattr(res, "upserted_id", None) else None,
                "multi": req.multi,
                "upsert": req.upsert,
            },
        )
    except HTTPException:
        raise
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Mongo error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
