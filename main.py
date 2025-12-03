from fastapi import FastAPI, UploadFile, File, Form, Request
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
import base64
import os
import time
import shutil
import json
from datetime import datetime, timedelta, timezone
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
import csv
from fastapi.responses import StreamingResponse
from io import StringIO, BytesIO
import zipfile

# ------------------------------
#  時區轉換函式（UTC → 台灣 UTC+8）
# ------------------------------
def to_tw(ts_str):
    try:
        # 去掉 Z、小數部分
        clean = ts_str.replace("Z", "").split(".")[0]
        
        # 解析成 datetime（沒有時區）
        ts = datetime.fromisoformat(clean)

        # 「強制當成 UTC」
        ts = ts.replace(tzinfo=datetime.timezone.utc)

        # 換算成台灣 UTC+8
        ts_tw = ts.astimezone(datetime.timezone(timedelta(hours=8)))

        return ts_tw.strftime("%Y-%m-%d %H:%M:%S")

    except Exception as e:
        print("Time convert error:", e)
        return ts_str
    
# --------------------------
# MongoDB Configuration
# --------------------------

MONGODB_URI = "mongodb+srv://tren:psychinfo@cluster0.5igl1b7.mongodb.net/?retryWrites=true&w=majority"
DB_NAME = "EmogoBackend"   # <--- 你自己的 database 名字（Compass 會看到）

app = FastAPI()

templates = Jinja2Templates(directory="templates")
# Allow CORS (讓前端能連後端)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# Connect MongoDB on startup
# --------------------------

@app.on_event("startup")
async def startup_db_client():
    app.mongodb_client = AsyncIOMotorClient(MONGODB_URI)
    app.mongodb = app.mongodb_client[DB_NAME]
    print("✅ Connected to MongoDB")


@app.on_event("shutdown")
async def shutdown_db_client():
    app.mongodb_client.close()
    print("❎ MongoDB connection closed")

# --------------------------
# Pydantic Models
# --------------------------

class Sentiment(BaseModel):
    user_id: str
    score: int
    timestamp: str

class GPS(BaseModel):
    user_id: str
    lat: float
    lng: float
    timestamp: str

# --------------------------
# Endpoints
# --------------------------

# -------------------------- Endpoints --------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    vlogs = await app.mongodb["vlogs"].find({}, {"_id": 0}).to_list(9999)

    # 轉成台灣時間
    for v in vlogs:
        v["timestamp"] = to_tw(v["timestamp"])

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "vlogs": vlogs}
    )


# ---- 1. Upload Vlog ----
@app.post("/upload_vlog")
async def upload_vlog(user_id: str = Form(...), file: UploadFile = File(...)):
    UPLOAD_DIR = "uploads"
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    file_path = os.path.join(UPLOAD_DIR, f"{user_id}_{int(time.time())}.mp4")

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    vlog_record = {
        "user_id": user_id,
        "filename": file_path,
        "timestamp": datetime.utcnow().isoformat()   # 存 UTC → 下載時轉台灣時間
    }

    await app.mongodb["vlogs"].insert_one(vlog_record)

    return {"status": "ok", "path": file_path}


# ---- 2. Upload Sentiment ----
@app.post("/upload_sentiment")
async def upload_sentiment(data: Sentiment):
    await app.mongodb["sentiments"].insert_one(data.dict())
    return {"status": "ok"}


# ---- 3. Upload GPS ----
@app.post("/upload_gps")
async def upload_gps(data: GPS):
    await app.mongodb["gps"].insert_one(data.dict())
    return {"status": "ok"}


# ---- 4. Export Everything (JSON) ----
@app.get("/export")
async def export_data():
    vlogs = await app.mongodb["vlogs"].find({}, {"_id": 0}).to_list(9999)
    sentiments = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(9999)
    gps = await app.mongodb["gps"].find({}, {"_id": 0}).to_list(9999)

    # 轉成台灣時間
    for v in vlogs:
        v["timestamp"] = to_tw(v["timestamp"])

    for s in sentiments:
        s["timestamp"] = to_tw(s["timestamp"])

    for g in gps:
        g["timestamp"] = to_tw(g["timestamp"])
        g["lat"] = round(float(g["lat"]), 4)
        g["lng"] = round(float(g["lng"]), 4)

    data = {
        "sentiments": sentiments,
        "gps": gps,
        "vlogs": vlogs
    }

    pretty_json = json.dumps(data, indent=4, ensure_ascii=False)

    return Response(
        content=pretty_json,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=Emogo_export.json"}
    )


# ---- Dashboard ----
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    vlogs = await app.mongodb["vlogs"].find({}, {"_id": 0}).to_list(9999)

    for v in vlogs:
        v["timestamp"] = to_tw(v["timestamp"])

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "vlogs": vlogs}
    )


# ---- Download Single Video ----
@app.get("/download_video")
async def download_video(path: str):

    if not os.path.exists(path):
        return {"error": "File not found"}

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=os.path.basename(path)
    )


# ---- Export Sentiments CSV ----
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


# ---- Export GPS CSV ----
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


# ---- Export ALL CSV ----
@app.get("/export_csv_all")
async def export_csv_all():
    sentiments = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(9999)
    gps = await app.mongodb["gps"].find({}, {"_id": 0}).to_list(9999)

    merged = {}

    # Sentiments
    for s in sentiments:
        ts = to_tw(s["timestamp"])
        if ts not in merged:
            merged[ts] = {
                "timestamp": ts,
                "user_id": s["user_id"],
                "sentiment": s["score"],
                "lat": "",
                "lng": ""
            }
        else:
            merged[ts]["sentiment"] = s["score"]

    # GPS
    for g in gps:
        ts = to_tw(g["timestamp"])
        lat = round(float(g["lat"]), 4)
        lng = round(float(g["lng"]), 4)

        if ts not in merged:
            merged[ts] = {
                "timestamp": ts,
                "user_id": g["user_id"],
                "sentiment": "",
                "lat": lat,
                "lng": lng
            }
        else:
            merged[ts]["lat"] = lat
            merged[ts]["lng"] = lng

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


# ---- Export Videos ZIP ----
@app.get("/export_videos_zip")
async def export_videos_zip():
    vlogs = await app.mongodb["vlogs"].find({}, {"_id": 0}).to_list(9999)

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for item in vlogs:
            filepath = item["filename"]
            if os.path.exists(filepath):
                zipf.write(filepath, arcname=os.path.basename(filepath))

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=all_videos.zip"}
    )