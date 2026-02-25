# TAG_SEARCH – Tag Suggest API (MongoDB → Elasticsearch) + Tag Data API

Dự án này cung cấp 2 API chính để phục vụ **gợi ý tìm kiếm theo tag (autocomplete / fuzzy)** và **upsert dữ liệu tag** vào Elasticsearch.

- **Elasticsearch index dùng cho suggest**: `tag_data_v1` (đồng bộ từ MongoDB)
- **Suggest API**: trả danh sách gợi ý theo `key_name` (ưu tiên “sát” nhất), kèm `tag_id` và các metadata địa lý.
- **Tag Data API**: insert/update (upsert) document vào `tag_data_v1` có **API key bảo vệ** và **check trùng**.

---

## 1) Kiến trúc & Luồng dữ liệu

### 1.1. Nguồn dữ liệu (MongoDB)
Collection: `TAG_SEARCH.TAG_SEARCH_DATA`

Ví dụ document:
```json
{
  "tag_id": 3283,
  "created_at": "2026-02-12T03:36:49Z",
  "updated_at": "2026-02-12T03:36:49Z",
  "key_city_id": 0,
  "key_qh_id": 0,
  "key_cb_id": 0,
  "key_name": "kế toán thanh toán",
  "key_desc": "...",
  "key_tit": "..."
}
1.2. Elasticsearch

Index chính:

tag_data_v1: lưu full fields + 2 field normalize để search:

key_name_norm (edge_ngram) → autocomplete prefix

key_name_norm_full (standard) → fuzzy/phrase-level

1.3. API

tag_suggest_api.py (port 7015): API gợi ý tìm kiếm theo tag

tag_data_api.py (port 7016): API upsert tag_data_v1 (có API key + check trùng)

2) Yêu cầu hệ thống

Ubuntu 22.04 (hoặc Linux tương đương)

Python 3.10+

Elasticsearch 7.17.x (hoặc tương đương)

PM2 (để chạy service bền bỉ)

3) Cài đặt & Setup
3.1. Tạo virtualenv & cài dependencies

Tại thư mục dự án (ví dụ /root/AI_365/TAG_SEARCH):

python3 -m venv venv
source venv/bin/activate

pip install -U fastapi uvicorn requests pydantic
3.2. (Khuyến nghị) Kiểm tra Elasticsearch đang chạy
curl -s http://localhost:9200
4) Tạo index Elasticsearch tag_data_v1

Nếu bạn đã tạo rồi thì bỏ qua bước này.

curl -X PUT "http://localhost:9200/tag_data_v1" -H "Content-Type: application/json" -d '{
  "settings": {
    "analysis": {
      "filter": {
        "vn_edge": { "type": "edge_ngram", "min_gram": 1, "max_gram": 20 }
      },
      "analyzer": {
        "norm_index": {
          "type": "custom",
          "tokenizer": "standard",
          "filter": ["lowercase", "vn_edge"]
        },
        "norm_search": {
          "type": "custom",
          "tokenizer": "standard",
          "filter": ["lowercase"]
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "tag_id":        { "type": "long" },
      "created_at":    { "type": "date" },
      "updated_at":    { "type": "date" },

      "key_cate_id":   { "type": "long" },
      "key_cate_lq":   { "type": "long" },
      "key_cb_id":     { "type": "long" },
      "key_city_id":   { "type": "long" },
      "key_qh_id":     { "type": "long" },
      "key_time":      { "type": "long" },

      "key_desc":      { "type": "text" },
      "key_h1":        { "type": "text" },
      "key_key":       { "type": "text" },
      "key_lq":        { "type": "text" },

      "key_name":      { "type": "text", "fields": { "raw": { "type": "keyword", "ignore_above": 256 } } },
      "key_tit":       { "type": "text", "fields": { "raw": { "type": "keyword", "ignore_above": 512 } } },

      "key_name_norm": {
        "type": "text",
        "analyzer": "norm_index",
        "search_analyzer": "norm_search"
      },
      "key_name_norm_full": {
        "type": "text",
        "analyzer": "standard"
      }
    }
  }
}'
5) Đồng bộ dữ liệu MongoDB → Elasticsearch

Nếu bạn có script đồng bộ (ví dụ sync_mongo_to_es_tag_data_v1.py), chạy:

export MONGO_URI="mongodb://..."
export ES_BASE_URL="http://localhost:9200"
export ES_INDEX="tag_data_v1"

python3 sync_mongo_to_es_tag_data_v1.py

Kiểm tra số lượng document:

curl -s "http://localhost:9200/tag_data_v1/_count?pretty"
6) Chạy Suggest API (tag_suggest_api.py)
6.1 Chạy trực tiếp (test nhanh)
export ES_BASE_URL="http://localhost:9200"
export SUGGEST_INDEX="tag_data_v1"

uvicorn tag_suggest_api:app --host 0.0.0.0 --port 7015

Test:

curl "http://127.0.0.1:7015/suggest?q=ke%20toan&size=10"
6.2 Chạy bằng PM2 (production)
cd /root/AI_365/TAG_SEARCH

ES_BASE_URL="http://localhost:9200" SUGGEST_INDEX="tag_data_v1" \
pm2 start ./venv/bin/python3 --name TAG_SUGGEST_API -- \
  -m uvicorn tag_suggest_api:app --host 0.0.0.0 --port 7015

pm2 save
pm2 logs TAG_SUGGEST_API
7) Chạy Tag Data API (tag_data_api.py) – Upsert + API Key + Check trùng
7.1 Chạy trực tiếp
export ES_BASE_URL="http://localhost:9200"
export ES_INDEX="tag_data_v1"
export TAGDATA_API_KEY="Hungha@#$TAGDATA_2026"   # đổi theo bạn

uvicorn tag_data_api:app --host 0.0.0.0 --port 7016
7.2 Chạy bằng PM2
cd /root/AI_365/TAG_SEARCH

ES_BASE_URL="http://localhost:9200" \
ES_INDEX="tag_data_v1" \
TAGDATA_API_KEY="Hungha@#$TAGDATA_2026" \
pm2 start ./venv/bin/python3 --name TAG_DATA_API -- \
  -m uvicorn tag_data_api:app --host 0.0.0.0 --port 7016

pm2 save
pm2 logs TAG_DATA_API
8) Hướng dẫn test bằng Postman
8.1 Test Suggest API

Method: GET

URL: http://<IP_SERVER>:7015/suggest

Params:

q: ví dụ ke toan

size: ví dụ 10

Ví dụ:

GET http://43.239.223.57:7015/suggest?q=ke%20toan&size=10
8.2 Test Tag Data API (Upsert 1 item)

Method: POST

URL: http://<IP_SERVER>:7016/tag/upsert?allow_duplicate=0

Header:

X-API-KEY: <TAGDATA_API_KEY>

Body JSON:

{
  "tag_id": 3283,
  "created_at": "2026-02-12T03:36:49Z",
  "updated_at": "2026-02-12T03:36:49Z",
  "key_cate_id": 0,
  "key_cate_lq": 1,
  "key_cb_id": 0,
  "key_city_id": 0,
  "key_qh_id": 0,
  "key_time": 1533625562,
  "key_desc": "Tìm việc làm kế toán thanh toán...",
  "key_h1": "",
  "key_key": "việc làm kế toán thanh toán...",
  "key_lq": "",
  "key_name": "kế toán thanh toán",
  "key_tit": "Việc làm Kế toán Thanh toán – Lương cao & Cơ hội thăng tiến"
}

Nếu trùng (cùng key_name + key_city_id + key_qh_id + key_cb_id) và allow_duplicate=0 → API trả 409.

9) Quy tắc sắp xếp trong Suggest API

Ưu tiên “sát theo key_name” (dựa theo score từ Elasticsearch).

Nếu có nhiều bản ghi cùng key_name (đặc biệt do nhiều tag_id), API chỉ sort nội bộ trong cụm đó:

key_city_id == 0 trước

key_qh_id == 0 trước

key_cb_id == 0 trước

tag_id tăng dần

10) Các lệnh PM2 hay dùng
pm2 status
pm2 logs TAG_SUGGEST_API
pm2 logs TAG_DATA_API

pm2 restart TAG_SUGGEST_API
pm2 restart TAG_DATA_API

pm2 stop TAG_SUGGEST_API
pm2 stop TAG_DATA_API

pm2 delete TAG_SUGGEST_API
pm2 delete TAG_DATA_API

pm2 save
pm2 startup
# chạy tiếp lệnh sudo mà pm2 in ra
pm2 save
11) Mở port firewall (nếu gọi từ máy ngoài)
ufw allow 7015/tcp
ufw allow 7016/tcp
ufw status
12) Troubleshooting nhanh
12.1 404 index không tồn tại

Kiểm tra index:

curl -s "http://localhost:9200/_cat/indices/tag_data_v1?v"
12.2 400 do sort field không tồn tại

Kiểm tra mapping:

curl -s "http://localhost:9200/tag_data_v1/_mapping?pretty" | head -n 80
12.3 API trả rỗng

Kiểm tra dữ liệu đã sync:

curl -s "http://localhost:9200/tag_data_v1/_count?pretty"