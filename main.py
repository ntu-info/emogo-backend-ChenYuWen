from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from datetime import datetime, timedelta, timezone
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates
import json
import csv
from io import StringIO, BytesIO
import zipfile
import os

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
    except Exception:
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
    # 把 video 排除掉，只拿 metadata（含 _id）
    vlogs = await app.mongodb["vlogs"].find({}, {"video": 0}).to_list(9999)

    for v in vlogs:
        if "timestamp" in v:
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
        "video": video_bytes,            # ⭐ Binary 存進 MongoDB
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

    for v in vlogs:
        if "timestamp" in v:
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

    return Response(
        content=json.dumps(data, indent=4, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=Emogo_export.json"}
    )

# ======================================================
# 5. 單筆影片下載（Binary + 舊版 fallback）
# ======================================================
@app.get("/download_video")
async def download_video(vlog_id: str):

    try:
        oid = ObjectId(vlog_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid vlog_id")

    vlog = await app.mongodb["vlogs"].find_one(
        {"_id": oid},
        {"video": 1, "filename": 1, "user_id": 1}
    )

    if not vlog:
        raise HTTPException(status_code=404, detail="Video not found")

    # 新版：有 video（Binary）就直接回傳
    if "video" in vlog and vlog["video"] is not None:
        video_bytes = bytes(vlog["video"])
        filename = f"{vlog.get('user_id', 'video')}_{vlog_id}.mp4"
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    # 舊版：只有 filename，試著從檔案系統讀（本機開發用）
    if "filename" in vlog:
        path = vlog["filename"]
        if os.path.exists(path):
            return FileResponse(
                path,
                media_type="video/mp4",
                filename=os.path.basename(path)
            )
        else:
            raise HTTPException(
                status_code=404,
                detail="Video file path stored in DB but file not found on server"
            )

    # 兩種都沒有
    raise HTTPException(status_code=500, detail="No video data stored for this vlog")

# ======================================================
# 6. 所有影片 ZIP（只打包有 video 的）
# ======================================================
@app.get("/export_videos_zip")
async def export_videos_zip():
    # 只選有 video 欄位的紀錄，避免舊紀錄炸掉
    vlogs = await app.mongodb["vlogs"].find(
        {"video": {"$exists": True}}
    ).to_list(9999)

    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for item in vlogs:
            video_bytes = bytes(item["video"])
            filename = f"{item.get('user_id', 'user')}_{item['_id']}.mp4"
            zipf.writestr(filename, video_bytes)

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

# ======================================================
# 8. 清理舊 vlog（沒有 video 的舊格式）
# ======================================================
@app.delete("/cleanup_old_vlogs")
async def cleanup_old_vlogs():
    result = await app.mongodb["vlogs"].delete_many({
        "video": {"$exists": False}
    })

    return {
        "status": "ok",
        "deleted_count": result.deleted_count,
        "message": f"Deleted {result.deleted_count} old vlog records"
    }
