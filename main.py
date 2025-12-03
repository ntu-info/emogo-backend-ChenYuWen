from fastapi import FastAPI, UploadFile, File, Form, Request
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from datetime import datetime, timedelta, timezone
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
import json
import csv
from io import StringIO, BytesIO
import zipfile

# ======================================================
# 時區處理（UTC → 台灣）
# ======================================================
def to_tw(ts_str):
    try:
        clean = ts_str.replace("Z", "").split(".")[0]
        ts = datetime.fromisoformat(clean)
        ts = ts.replace(tzinfo=timezone.utc)
        ts_tw = ts.astimezone(timezone(timedelta(hours=8)))
        return ts_tw.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return ts_str

# ======================================================
# MongoDB 設定
# ======================================================
MONGODB_URI = "mongodb+srv://tren:psychinfo@cluster0.5igl1b7.mongodb.net/?retryWrites=true&w=majority"
DB_NAME = "EmogoBackend"

app = FastAPI()
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 連線 MongoDB
@app.on_event("startup")
async def startup_db_client():
    app.mongodb_client = AsyncIOMotorClient(MONGODB_URI)
    app.mongodb = app.mongodb_client[DB_NAME]
    print("✅ Connected to MongoDB")

@app.on_event("shutdown")
async def shutdown_db_client():
    app.mongodb_client.close()
    print("❎ MongoDB connection closed")

# ======================================================
# Pydantic Models
# ======================================================
class Sentiment(BaseModel):
    user_id: str
    score: int
    timestamp: str

class GPS(BaseModel):
    user_id: str
    lat: float
    lng: float
    timestamp: str

# ======================================================
# Dashboard
# ======================================================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    vlogs = await app.mongodb["vlogs"].find({}, {"video": 0}).to_list(9999)

    for v in vlogs:
        v["timestamp"] = to_tw(v["timestamp"])

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "vlogs": vlogs}
    )

# ======================================================
# 1. 上傳 Vlog — Binary 儲存
# ======================================================
@app.post("/upload_vlog")
async def upload_vlog(user_id: str = Form(...), file: UploadFile = File(...)):
    video_bytes = await file.read()

    vlog_record = {
        "user_id": user_id,
        "video": video_bytes,
        "timestamp": datetime.utcnow().isoformat()
    }

    result = await app.mongodb["vlogs"].insert_one(vlog_record)

    return {"status": "ok", "vlog_id": str(result.inserted_id)}

# ======================================================
# 2. 上傳 Sentiment
# ======================================================
@app.post("/upload_sentiment")
async def upload_sentiment(data: Sentiment):
    await app.mongodb["sentiments"].insert_one(data.dict())
    return {"status": "ok"}

# ======================================================
# 3. 上傳 GPS
# ======================================================
@app.post("/upload_gps")
async def upload_gps(data: GPS):
    await app.mongodb["gps"].insert_one(data.dict())
    return {"status": "ok"}

# ======================================================
# 4. 匯出 JSON
# ======================================================
@app.get("/export")
async def export_data():

    vlogs = await app.mongodb["vlogs"].find({}, {"video": 0, "_id": 0}).to_list(9999)
    sentiments = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(9999)
    gps = await app.mongodb["gps"].find({}, {"_id": 0}).to_list(9999)

    for v in vlogs: v["timestamp"] = to_tw(v["timestamp"])
    for s in sentiments: s["timestamp"] = to_tw(s["timestamp"])
    for g in gps:
        g["timestamp"] = to_tw(g["timestamp"])
        g["lat"] = round(float(g["lat"]), 4)
        g["lng"] = round(float(g["lng"]), 4)

    data = {
        "sentiments": sentiments,
        "gps": gps,
        "vlogs": vlogs
    }

    return Response(
        content=json.dumps(data, indent=4, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=Emogo_export.json"}
    )

# ======================================================
# 5. 單筆影片下載（Binary）
# ======================================================
@app.get("/download_video")
async def download_video(vlog_id: str):

    vlog = await app.mongodb["vlogs"].find_one({"_id": ObjectId(vlog_id)})

    if not vlog:
        return {"error": "Video not found"}

    video_bytes = vlog["video"]

    return Response(
        content=video_bytes,
        media_type="video/mp4",
        headers={"Content-Disposition": "attachment; filename=video.mp4"}
    )

# ======================================================
# 6. 所有影片 ZIP
# ======================================================
@app.get("/export_videos_zip")
async def export_videos_zip():
    vlogs = await app.mongodb["vlogs"].find({}).to_list(9999)

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for item in vlogs:
            vid = item["video"]
            filename = f"{item['user_id']}_{item['_id']}.mp4"
            zipf.writestr(filename, vid)

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=all_videos.zip"}
    )

# ======================================================
# 7. CSV 匯出（Sentiments / GPS / ALL）
# ======================================================
@app.get("/export_sentiments_csv")
async def export_sentiments_csv():
    data = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(9999)

    for d in data:
        d["timestamp"] = to_tw(d["timestamp"])

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["timestamp", "user_id", "score"])
    writer.writeheader()
    writer.writerows(data)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sentiments.csv"}
    )

@app.get("/export_gps_csv")
async def export_gps_csv():

    data = await app.mongodb["gps"].find({}, {"_id": 0}).to_list(9999)

    for d in data:
        d["timestamp"] = to_tw(d["timestamp"])
        d["lat"] = round(float(d["lat"]), 4)
        d["lng"] = round(float(d["lng"]), 4)

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["timestamp", "user_id", "lat", "lng"])
    writer.writeheader()
    writer.writerows(data)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gps.csv"}
    )

@app.get("/export_csv_all")
async def export_csv_all():

    sentiments = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(9999)
    gps = await app.mongodb["gps"].find({}, {"_id": 0}).to_list(9999)

    merged = {}

    for s in sentiments:
        ts = to_tw(s["timestamp"])
        merged.setdefault(ts, {
            "timestamp": ts,
            "user_id": s["user_id"],
            "sentiment": s["score"],
            "lat": "",
            "lng": ""
        })

    for g in gps:
        ts = to_tw(g["timestamp"])
        merged.setdefault(ts, {
            "timestamp": ts,
            "user_id": g["user_id"],
            "sentiment": "",
            "lat": round(float(g["lat"]), 4),
            "lng": round(float(g["lng"]), 4)
        })

        merged[ts]["lat"] = round(float(g["lat"]), 4)
        merged[ts]["lng"] = round(float(g["lng"]), 4)

    rows = sorted(merged.values(), key=lambda x: x["timestamp"])

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["timestamp", "user_id", "sentiment", "lat", "lng"])
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=Emogo_export.csv"}
    )
