import os
import random
import string
import secrets
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from bson import ObjectId
from pymongo import MongoClient
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI, Request, Form, Response, HTTPException, status, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl
from itsdangerous import URLSafeTimedSerializer

# ========================================================
# কনফিগারেশন
# ========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEFAULT_MONGO = "mongodb+srv://MovieLinkbd:MovieLinkbd@cluster0.cmx4zn5.mongodb.net/smart_shortener?retryWrites=true&w=majority"
raw_mongo = os.getenv("MONGO_URI", "").strip()

if raw_mongo and (raw_mongo.startswith("mongodb://") or raw_mongo.startswith("mongodb+srv://")):
    MONGO_URI = raw_mongo
else:
    MONGO_URI = DEFAULT_MONGO

SECRET_KEY = os.getenv("SECRET_KEY", "smart_secret_link_key_2026").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
PORT = int(os.getenv("PORT", "8000"))

# অ্যাডমিন আইডি
admin_raw = os.getenv("ADMIN_IDS", "5370676246")
ADMIN_IDS = [int(x.strip()) for x in admin_raw.split(",") if x.strip().isdigit()]

step_signer = URLSafeTimedSerializer(SECRET_KEY, salt="step-clearance")
admin_signer = URLSafeTimedSerializer(SECRET_KEY, salt="admin-session")

# হাই-পারফরম্যান্স ডাটাবেজ কানেকশন পুল
client = AsyncIOMotorClient(MONGO_URI, maxPoolSize=50, minPoolSize=10, serverSelectionTimeoutMS=5000)
db = client.get_default_database()

sync_client = MongoClient(MONGO_URI, maxPoolSize=50, minPoolSize=10, serverSelectionTimeoutMS=5000)
sync_db = sync_client.get_default_database()

templates = Jinja2Templates(directory="templates")

# সিঙ্ক্রোনাস কোড জেনারেটর
def generate_unique_code_sync(length=6):
    chars = string.ascii_letters + string.digits
    for _ in range(15):
        code = "".join(secrets.choice(chars) for _ in range(length))
        try:
            if not sync_db.links.find_one({"short_code": code}):
                return code
        except Exception:
            return code
    return "".join(secrets.choice(chars) for _ in range(length + 2))

async def resolve_weighted_direct_link():
    try:
        links = await db.direct_links.find({"status": "Active"}).to_list(100)
        if not links:
            return None
        weights = [max(1, l.get("weight", 1)) for l in links]
        chosen = random.choices(links, weights=weights, k=1)[0]
        await db.direct_links.update_one({"_id": chosen["_id"]}, {"$inc": {"clicks": 1}})
        return chosen["url"]
    except Exception:
        return None

# 🌟 ইন্টারনাল সেলফ-পিং ইঞ্জিন (Render স্লিপ মোড চিরতরে বন্ধ রাখার জন্য)
def keep_alive_self_ping():
    time.sleep(30)  # সার্ভার সম্পূর্ণ চালু হতে ৩০ সেকেন্ড বিরতি
    print("🚀 Internal Keep-Alive Self-Ping Engine Activated!")
    while True:
        try:
            # প্রতি ৯ মিনিট পর পর নিজের /health লিংকে পিং পাঠাবে
            target_url = f"{BASE_URL.rstrip('/')}/health"
            res = requests.get(target_url, timeout=10)
            print(f"💓 Self-Ping Sent to keep server awake. Status: {res.status_code}")
        except Exception as e:
            print(f"Self-ping notice: {e}")
        time.sleep(9 * 60)  # ৯ মিনিট পর পর ঘুরবে

bot_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_instance
    try:
        await db.links.create_index("short_code", unique=True)
        await db.links.create_index("created_at")
        await db.steps.create_index("order")
        await db.direct_links.create_index("status")
        await db.clicks.create_index("timestamp")

        if not await db.settings.find_one({"type": "global"}):
            await db.settings.insert_one({
                "type": "global",
                "site_name": "Pom Pom Links",
                "base_url": BASE_URL,
                "channel_id": "",
                "auto_delete_minutes": 10,
                "protect_content": True,
                "public_shortener": True,
                "wait_seconds": 7,
                "native_ad_top": "",
                "native_ad_bottom": "",
                "auto_scroll_enabled": True
            })

        if await db.steps.count_documents({}) == 0:
            await db.steps.insert_many([
                {"name": "Security Check", "title": "Verifying Link Gateway", "description": "Please wait while we verify destination security.", "timer": 7, "button_text": "Continue", "status": True, "order": 1},
                {"name": "Final Clearance", "title": "Unlocking Requested Content", "description": "Your requested destination is ready. Click below to proceed.", "timer": 5, "button_text": "Get Link / Download", "status": True, "order": 2}
            ])
        print("✅ Database & Settings Ready!")
    except Exception as e:
        print(f"Startup Warning: {e}")

    # ব্যাকগ্রাউন্ড সেলফ-পিং চালু করা
    threading.Thread(target=keep_alive_self_ping, daemon=True).start()

    # টেলিগ্রাম বট ইনিট
    if BOT_TOKEN:
        try:
            import bot
            bot_instance = bot.init_bot(app, sync_db, {
                "BOT_TOKEN": BOT_TOKEN,
                "BASE_URL": BASE_URL,
                "ADMIN_IDS": ADMIN_IDS
            })
        except Exception as e:
            print(f"Bot init error: {e}")
    yield

app = FastAPI(lifespan=lifespan)

# ফাস্ট টেলিগ্রাম Webhook গেটওয়ে
@app.post("/api/telegram/webhook")
async def telegram_webhook_handler(request: Request):
    if bot_instance:
        update_json = await request.json()
        import telebot
        update = telebot.types.Update.de_json(update_json)
        threading.Thread(target=bot_instance.process_new_updates, args=([update],), daemon=True).start()
    return Response(status_code=200)

async def check_admin_session(request: Request):
    cookie = request.cookies.get("admin_token")
    if not cookie:
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})
    try:
        admin_signer.loads(cookie, max_age=86400 * 7)
    except Exception:
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})
    return True

# -----------------------------------------------------------------------------
# পাবলিক ও মাল্টি-স্টেপ রুট
# -----------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}

@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    settings = await db.settings.find_one({"type": "global"})
    return templates.TemplateResponse("index.html", {
        "request": request,
        "mode": "home",
        "site_name": settings.get("site_name", "Pom Pom Links") if settings else "Pom Pom Links",
        "public_enabled": settings.get("public_shortener", True) if settings else True
    })

class ShortenReq(BaseModel):
    url: HttpUrl

@app.post("/api/create")
async def api_create(req: ShortenReq):
    settings = await db.settings.find_one({"type": "global"})
    if settings and not settings.get("public_shortener", True):
        raise HTTPException(status_code=403, detail="Public link creation is paused.")

    code = generate_unique_code_sync()
    base = (settings.get("base_url") if settings and settings.get("base_url") else BASE_URL).rstrip("/")

    await db.links.insert_one({
        "short_code": code,
        "destination": str(req.url),
        "destination_type": "url",
        "metadata": {"name": str(req.url)[:35]},
        "created_by": "public_web",
        "created_at": datetime.now(timezone.utc),
        "status": "Active",
        "protect_content": False,
        "clicks": 0,
        "step_views": 0,
        "final_clicks": 0
    })

    return {"status": "success", "short_url": f"{base}/s/{code}"}

@app.get("/s/{code}", response_class=HTMLResponse)
async def short_gateway(code: str, request: Request):
    link = await db.links.find_one({"short_code": code})
    if not link or link.get("status") != "Active":
        return templates.TemplateResponse("index.html", {"request": request, "mode": "error", "message": "Link not found or disabled."}, status_code=404)

    await db.links.update_one({"_id": link["_id"]}, {"$inc": {"clicks": 1}})
    await db.clicks.insert_one({"short_code": code, "timestamp": datetime.now(timezone.utc)})

    steps = await db.steps.find({"status": True}).sort("order", 1).to_list(100)
    if not steps:
        return await finalize_redirect(link, request)

    token = step_signer.dumps({"code": code, "step": 0})
    return RedirectResponse(f"/s/{code}/step/0?auth={token}", status_code=303)

@app.get("/s/{code}/step/{step_idx}", response_class=HTMLResponse)
async def process_step(code: str, step_idx: int, auth: str, request: Request):
    try:
        data = step_signer.loads(auth, max_age=1800)
        if data.get("code") != code or data.get("step") != step_idx:
            raise Exception()
    except Exception:
        return templates.TemplateResponse("index.html", {"request": request, "mode": "error", "message": "Invalid or expired step session."}, status_code=403)

    link = await db.links.find_one({"short_code": code})
    steps = await db.steps.find({"status": True}).sort("order", 1).to_list(100)

    if step_idx >= len(steps):
        final_token = step_signer.dumps({"code": code, "step": len(steps)})
        return RedirectResponse(f"/s/{code}/final?auth={final_token}", status_code=303)

    current_step = steps[step_idx]
    await db.links.update_one({"_id": link["_id"]}, {"$inc": {"step_views": 1}})

    direct_url = await resolve_weighted_direct_link()
    next_token = step_signer.dumps({"code": code, "step": step_idx + 1})
    is_last = (step_idx + 1) >= len(steps)
    next_url = f"/s/{code}/final?auth={next_token}" if is_last else f"/s/{code}/step/{step_idx + 1}?auth={next_token}"

    settings = await db.settings.find_one({"type": "global"}) or {}

    return templates.TemplateResponse("index.html", {
        "request": request,
        "mode": "step",
        "step": current_step,
        "step_num": step_idx + 1,
        "total_steps": len(steps),
        "next_url": next_url,
        "direct_url": direct_url or "",
        "native_ad_top": settings.get("native_ad_top", ""),
        "native_ad_bottom": settings.get("native_ad_bottom", ""),
        "auto_scroll_enabled": settings.get("auto_scroll_enabled", True)
    })

@app.get("/s/{code}/final", response_class=HTMLResponse)
async def final_dispatch(code: str, auth: str, request: Request):
    steps = await db.steps.find({"status": True}).sort("order", 1).to_list(100)
    try:
        data = step_signer.loads(auth, max_age=1800)
        if data.get("code") != code or data.get("step") != len(steps):
            raise Exception()
    except Exception:
        return templates.TemplateResponse("index.html", {"request": request, "mode": "error", "message": "Step sequence validation error."}, status_code=403)

    link = await db.links.find_one({"short_code": code})
    await db.links.update_one({"_id": link["_id"]}, {"$inc": {"final_clicks": 1}})
    return await finalize_redirect(link, request)

async def finalize_redirect(link: dict, request: Request):
    dtype = link.get("destination_type", "url")
    if dtype == "url":
        return RedirectResponse(link["destination"], status_code=302)

    bot_user = "TelegramBot"
    if BOT_TOKEN:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=4.0) as cl:
                r = await cl.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
                bot_user = r.json().get("result", {}).get("username", bot_user)
        except Exception:
            pass

    deep_link = f"https://t.me/{bot_user}?start=unlock_{link['short_code']}"
    return templates.TemplateResponse("index.html", {
        "request": request,
        "mode": "final",
        "deep_link": deep_link,
        "content_name": link.get("metadata", {}).get("name", "Telegram Exclusive Content")
    })

# -----------------------------------------------------------------------------
# অ্যাডমিন প্যানেল
# -----------------------------------------------------------------------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_screen(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request, "mode": "login"})

@app.post("/admin/login")
def do_login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = admin_signer.dumps({"user": username, "iat": time.time()})
        res = RedirectResponse("/admin", status_code=303)
        res.set_cookie("admin_token", token, max_age=86400 * 7, httponly=True)
        return res
    return RedirectResponse("/admin/login?error=1", status_code=303)

@app.get("/admin/logout")
def do_logout():
    res = RedirectResponse("/admin/login", status_code=303)
    res.delete_cookie("admin_token")
    return res

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, auth: bool = Depends(check_admin_session)):
    now = datetime.now(timezone.utc)
    start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start_yesterday = start_today - timedelta(days=1)

    total_links = await db.links.count_documents({})
    total_clicks = await db.clicks.count_documents({})
    today_clicks = await db.clicks.count_documents({"timestamp": {"$gte": start_today}})
    yesterday_clicks = await db.clicks.count_documents({"timestamp": {"$gte": start_yesterday, "$lt": start_today}})

    links = await db.links.find().sort("created_at", -1).limit(40).to_list(40)
    steps = await db.steps.find().sort("order", 1).to_list(50)
    direct_links = await db.direct_links.find().to_list(50)
    settings = await db.settings.find_one({"type": "global"})

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "mode": "dashboard",
        "stats": {
            "total_links": total_links,
            "total_clicks": total_clicks,
            "today_clicks": today_clicks,
            "yesterday_clicks": yesterday_clicks
        },
        "links": links,
        "steps": steps,
        "direct_links": direct_links,
        "settings": settings or {}
    })

@app.post("/api/admin/settings")
async def save_settings(
    site_name: str = Form(...),
    base_url: str = Form(""),
    channel_id: str = Form(""),
    auto_delete_minutes: int = Form(10),
    protect_content: bool = Form(False),
    public_shortener: bool = Form(False),
    auth: bool = Depends(check_admin_session)
):
    await db.settings.update_one(
        {"type": "global"},
        {"$set": {
            "site_name": site_name,
            "base_url": base_url.rstrip("/"),
            "channel_id": channel_id.strip(),
            "auto_delete_minutes": auto_delete_minutes,
            "protect_content": protect_content,
            "public_shortener": public_shortener
        }},
        upsert=True
    )
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/admin/save-native-ads")
async def save_native_ads(
    native_ad_top: str = Form(""),
    native_ad_bottom: str = Form(""),
    auto_scroll_enabled: bool = Form(False),
    auth: bool = Depends(check_admin_session)
):
    await db.settings.update_one(
        {"type": "global"},
        {"$set": {
            "native_ad_top": native_ad_top.strip(),
            "native_ad_bottom": native_ad_bottom.strip(),
            "auto_scroll_enabled": auto_scroll_enabled
        }},
        upsert=True
    )
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/admin/steps/add")
async def add_step(
    name: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    timer: int = Form(7),
    button_text: str = Form("Continue"),
    order: int = Form(1),
    auth: bool = Depends(check_admin_session)
):
    await db.steps.insert_one({
        "name": name, "title": title, "description": description,
        "timer": timer, "button_text": button_text, "order": order, "status": True
    })
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/admin/steps/delete/{sid}")
async def del_step(sid: str, auth: bool = Depends(check_admin_session)):
    await db.steps.delete_one({"_id": ObjectId(sid)})
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/admin/direct/add")
async def add_direct(
    name: str = Form(...),
    url: str = Form(...),
    weight: int = Form(1),
    auth: bool = Depends(check_admin_session)
):
    await db.direct_links.insert_one({"name": name, "url": url.strip(), "weight": weight, "clicks": 0, "status": "Active"})
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/admin/direct/delete/{did}")
async def del_direct(did: str, auth: bool = Depends(check_admin_session)):
    await db.direct_links.delete_one({"_id": ObjectId(did)})
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/admin/links/delete/{code}")
async def del_link(code: str, auth: bool = Depends(check_admin_session)):
    await db.links.delete_one({"short_code": code})
    return RedirectResponse("/admin", status_code=303)
