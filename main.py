from fastapi import FastAPI, UploadFile, File, Form, Request
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
import base64
import os
import time
import shutil
import json
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
import csv
from fastapi.responses import StreamingResponse
from io import StringIO, BytesIO
import zipfile

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

@app.get("/")
async def home():
    return {"message": "EmoGo Backend is running!"}

# ---- 1. Upload Vlog (影片) ----
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
        "timestamp": datetime.utcnow().isoformat()
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

# ---- 4. Export Everything ----
@app.get("/export")
async def export_data():

    # --- 1. 取出資料 ---
    vlogs = await app.mongodb["vlogs"].find({}, {"_id": 0}).to_list(length=9999)
    sentiments = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(length=9999)
    gps = await app.mongodb["gps"].find({}, {"_id": 0}).to_list(length=9999)

    # --- 2. 統一格式 ---

    # 處理 vlog timestamp
    for v in vlogs:
        v["timestamp"] = v["timestamp"].replace("T", " ").split(".")[0]

    # 處理 sentiment timestamp
    for s in sentiments:
        s["timestamp"] = s["timestamp"].replace("T", " ").split(".")[0]

    # 處理 gps timestamp + 小數點
    for g in gps:
        g["timestamp"] = g["timestamp"].replace("T", " ").split(".")[0]
        g["lat"] = round(float(g["lat"]), 4)
        g["lng"] = round(float(g["lng"]), 4)

    # --- 3. 整理成匯出格式 ---
    data = {
        "sentiments": sentiments,
        "gps": gps,
        "vlogs": vlogs
    }

    # --- 4. 美化 JSON ---
    pretty_json = json.dumps(data, indent=4, ensure_ascii=False)

    return Response(
        content=pretty_json,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=Emogo_export.json"}
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    # 從 MongoDB 抓所有 vlog 紀錄
    vlogs = await app.mongodb["vlogs"].find({}, {"_id": 0}).to_list(length=9999)

    # 回傳前面建好的 dashboard.html
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "vlogs": vlogs
        }
    )

@app.get("/download_video")
async def download_video(path: str):
    """
    path 直接用資料庫裡存的 filename 欄位（如 uploads/vivian_1764432214.mp4）
    dashboard.html 也已經用這個當 query string 了。
    """
    if not os.path.exists(path):
        return {"error": "File not found"}

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=os.path.basename(path)  # 下載時顯示的檔名
    )

@app.get("/export_sentiments_csv")
async def export_sentiments_csv():
    data = await app.mongodb["sentiments"].find({}, {"_id": 0}).to_list(9999)

    # format timestamps
    for d in data:
        d["timestamp"] = d["timestamp"].replace("T", " ").split(".")[0]

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

    # format gps values & timestamp
    for d in data:
        d["lat"] = round(float(d["lat"]), 4)
        d["lng"] = round(float(d["lng"]), 4)
        d["timestamp"] = d["timestamp"].replace("T", " ").split(".")[0]

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

    # ---- 合併 sentiments ----
    for s in sentiments:
        ts_raw = s["timestamp"]
        ts = ts_raw.replace("T", " ").split(".")[0]   # 轉成好看格式

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

    # ---- 合併 GPS ----
    for g in gps:
        ts_raw = g["timestamp"]
        ts = ts_raw.replace("T", " ").split(".")[0]

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