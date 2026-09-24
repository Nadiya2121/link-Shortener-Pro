import time
import threading
import telebot
from telebot import types
from datetime import datetime, timezone
from bson import ObjectId

ALBUM_CACHE = {}
ALBUM_LOCK = threading.Lock()

def init_bot(app, sync_db, config):
    bot = telebot.TeleBot(config["BOT_TOKEN"], parse_mode="HTML", threaded=True)

    def get_settings():
        settings = sync_db.settings.find_one({"type": "global"})
        if not settings:
            return {
                "base_url": config["BASE_URL"],
                "channel_id": "",
                "auto_delete_minutes": 10,
                "protect_content": True
            }
        return {
            "base_url": settings.get("base_url") or config["BASE_URL"],
            "channel_id": settings.get("channel_id", ""),
            "auto_delete_minutes": settings.get("auto_delete_minutes", 10),
            "protect_content": settings.get("protect_content", True)
        }

    # ইনবক্স থেকে স্বয়ংক্রিয় ফাইল মুছে দেওয়ার ব্যাকগ্রাউন্ড থ্রেড
    def schedule_auto_delete(chat_id, message_id, minutes):
        if minutes <= 0:
            return

        def delete_worker():
            time.sleep(minutes * 60)
            try:
                bot.delete_message(chat_id=chat_id, message_id=message_id)
                notice = bot.send_message(
                    chat_id, 
                    "⚠️ <b>সময় শেষ! আপনার ফাইলটি চ্যাট থেকে মুছে ফেলা হয়েছে।</b>\nপ্রয়োজনে পুনরায় শর্ট লিংক ভিজিট করে ফাইল আনলক করুন।"
                )
                time.sleep(45)
                bot.delete_message(chat_id=chat_id, message_id=notice.message_id)
            except Exception:
                pass

        threading.Thread(target=delete_worker, daemon=True).start()

    # অ্যালবাম বা ব্যাচ ফাইল প্রসেসিং
    def flush_album(media_group_id, chat_id):
        time.sleep(2.0)
        with ALBUM_LOCK:
            batch = ALBUM_CACHE.pop(media_group_id, None)
        if not batch or not batch.get("items"):
            return

        album_doc = {
            "media_group_id": media_group_id,
            "items": batch["items"],
            "chat_id": chat_id,
            "created_at": datetime.now(timezone.utc)
        }
        res = sync_db.albums.insert_one(album_doc)
        album_id = str(res.inserted_id)

        from app import generate_unique_code_sync
        code = generate_unique_code_sync()
        settings = get_settings()

        link_doc = {
            "short_code": code,
            "destination": album_id,
            "destination_type": "telegram_album",
            "metadata": {"total_items": len(batch["items"]), "name": f"Batch Media ({len(batch['items'])} files)"},
            "created_by": f"tg_{chat_id}",
            "created_at": datetime.now(timezone.utc),
            "status": "Active",
            "protect_content": settings["protect_content"],
            "clicks": 0,
            "step_views": 0,
            "final_clicks": 0
        }
        sync_db.links.insert_one(link_doc)

        short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
        send_creation_response(chat_id, f"Batch Collection ({len(batch['items'])} Files)", short_url, code, settings["protect_content"])

    # লিংক তৈরি শেষে অ্যাকশন বাটন সহ মেসেজ প্রদান
    def send_creation_response(chat_id, title, short_url, code, protect_status):
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_channel = types.InlineKeyboardButton("📢 Post to Channel", callback_data=f"post_{code}")
        btn_protect = types.InlineKeyboardButton(f"🛡️ Protect: {'ON' if protect_status else 'OFF'}", callback_data=f"tog_{code}")
        btn_visit = types.InlineKeyboardButton("🌐 Open Link", url=short_url)
        btn_share = types.InlineKeyboardButton("🔗 Share Link", switch_inline_query=short_url)
        markup.add(btn_channel, btn_protect)
        markup.add(btn_visit, btn_share)

        bot.send_message(
            chat_id,
            f"✅ <b>Smart Gateway Formed!</b>\n\n"
            f"📌 <b>Content:</b> {title}\n"
            f"🔗 <b>Short Link:</b> <code>{short_url}</code>\n\n"
            f"<i>নিচের বাটন চেপে সরাসরি চ্যানেলে পোস্ট করতে পারেন অথবা ফরওয়ার্ড প্রোটেকশন পরিবর্তন করতে পারেন।</i>",
            reply_markup=markup
        )

    # /start কমান্ড হ্যান্ডলার
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        chat_id = message.chat.id
        text = message.text or ""

        # যদি ইউজার আনলক শেষে ফাইল রিসিভ করতে আসে
        if len(text.split()) > 1 and text.split()[1].startswith("unlock_"):
            code = text.split()[1].replace("unlock_", "")
            link = sync_db.links.find_one({"short_code": code})
            if not link:
                bot.send_message(chat_id, "❌ <b>দুঃখিত! ফাইলটি পাওয়া যায়নি বা মেয়াদ শেষ হয়ে গেছে।</b>")
                return

            settings = get_settings()
            del_min = settings["auto_delete_minutes"]
            is_protected = link.get("protect_content", settings["protect_content"])
            timer_note = f"\n\n⏳ <i>সতর্কতা: এই ফাইলটি {del_min} মিনিট পর স্বয়ংক্রিয়ভাবে মুছে যাবে!</i>" if del_min > 0 else ""

            dtype = link.get("destination_type")

            # সিঙ্গেল ফাইল ডেলিভারি
            if dtype == "telegram_file":
                meta = link.get("metadata", {})
                caption = f"🎉 <b>আপনার আনলককৃত কন্টেন্ট:</b>\n📌 {meta.get('name', 'File')}{timer_note}"
                sent_msg = bot.send_video(
                    chat_id=chat_id,
                    video=meta["file_id"],
                    protect_content=is_protected,
                    caption=caption
                ) if meta.get("file_type") == "video" else bot.send_document(
                    chat_id=chat_id,
                    document=meta["file_id"],
                    protect_content=is_protected,
                    caption=caption
                )
                if del_min > 0:
                    schedule_auto_delete(chat_id, sent_msg.message_id, del_min)

            # অ্যালবাম ডেলিভারি
            elif dtype == "telegram_album":
                album = sync_db.albums.find_one({"_id": ObjectId(link["destination"])})
                if album and album.get("items"):
                    media_arr = []
                    for idx, it in enumerate(album["items"]):
                        cap = f"🎉 Batch Content ({idx+1}/{len(album['items'])}){timer_note}" if idx == 0 else ""
                        media_arr.append(
                            types.InputMediaPhoto(it["file_id"], caption=cap) if it["file_type"] == "photo"
                            else types.InputMediaVideo(it["file_id"], caption=cap)
                        )
                    sent_msgs = bot.send_media_group(chat_id=chat_id, media=media_arr, protect_content=is_protected)
                    if del_min > 0:
                        for m in sent_msgs:
                            schedule_auto_delete(chat_id, m.message_id, del_min)
            return

        bot.send_message(
            chat_id,
            "👋 <b>স্বাগতম Smart Link Shortener বটে!</b>\n\n"
            "যে কোনো বড় লিংক, ফাইল, ভিডিও অথবা একাধিক ছবি পাঠান। বট তাৎক্ষণিক একটি সুরক্ষিত মাল্টি-স্টেপ শর্ট লিংক তৈরি করে দেবে।"
        )

    # কলব্যাক কুয়েরি (Post to Channel এবং Protect টগল)
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callbacks(call):
        data = call.data
        chat_id = call.message.chat.id
        settings = get_settings()

        # চ্যানেলে অটো-পোস্ট করা
        if data.startswith("post_"):
            code = data.replace("post_", "")
            link = sync_db.links.find_one({"short_code": code})
            if not link:
                bot.answer_callback_query(call.id, "Link not found!", show_alert=True)
                return

            channel_target = settings.get("channel_id")
            if not channel_target:
                bot.answer_callback_query(call.id, "⚠️ অ্যাডমিন প্যানেলে চ্যানেল আইডি সেট করা হয়নি!", show_alert=True)
                return

            short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
            title = link.get("metadata", {}).get("name", "Exclusive Premium Content")

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("📥 Download / Watch (HD)", url=short_url))

            try:
                bot.send_message(
                    channel_target,
                    f"🎬 <b>{title}</b>\n\n"
                    f"⚡ সম্পূর্ণ ফ্রিতে দেখতে বা ডাউনলোড করতে নিচের বাটনে ক্লিক করুন 👇",
                    reply_markup=markup
                )
                bot.answer_callback_query(call.id, "🎉 চ্যানেলে সফলভাবে পোস্ট হয়েছে!", show_alert=True)
            except Exception as e:
                bot.answer_callback_query(call.id, f"পোস্ট ব্যর্থ: {str(e)}", show_alert=True)

        # ফরওয়ার্ড প্রটেক্ট টগল করা
        elif data.startswith("tog_"):
            code = data.replace("tog_", "")
            link = sync_db.links.find_one({"short_code": code})
            if link:
                new_state = not link.get("protect_content", True)
                sync_db.links.update_one({"_id": link["_id"]}, {"$set": {"protect_content": new_state}})
                short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
                title = link.get("metadata", {}).get("name", "Content")
                
                # বাটন টেক্সট আপডেট
                markup = types.InlineKeyboardMarkup(row_width=2)
                btn_channel = types.InlineKeyboardButton("📢 Post to Channel", callback_data=f"post_{code}")
                btn_protect = types.InlineKeyboardButton(f"🛡️ Protect: {'ON' if new_state else 'OFF'}", callback_data=f"tog_{code}")
                btn_visit = types.InlineKeyboardButton("🌐 Open Link", url=short_url)
                btn_share = types.InlineKeyboardButton("🔗 Share Link", switch_inline_query=short_url)
                markup.add(btn_channel, btn_protect)
                markup.add(btn_visit, btn_share)

                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
                bot.answer_callback_query(call.id, f"Protection is now {'ON' if new_state else 'OFF'}")

    # ফাইল, ভিডিও ও ফটো ইনপুট
    @bot.message_handler(content_types=['document', 'video', 'photo', 'audio'])
    def handle_media(message):
        chat_id = message.chat.id
        media_group_id = message.media_group_id

        # অ্যালবাম ডিটেকশন
        if media_group_id:
            file_id = ""
            ftype = "photo"
            if message.photo:
                file_id = message.photo[-1].file_id
                ftype = "photo"
            elif message.video:
                file_id = message.video.file_id
                ftype = "video"
            elif message.document:
                file_id = message.document.file_id
                ftype = "document"

            with ALBUM_LOCK:
                if media_group_id not in ALBUM_CACHE:
                    ALBUM_CACHE[media_group_id] = {"items": [], "chat_id": chat_id}
                    threading.Thread(target=flush_album, args=(media_group_id, chat_id), daemon=True).start()
                ALBUM_CACHE[media_group_id]["items"].append({"file_id": file_id, "file_type": ftype})
            return

        # সিঙ্গেল মিডিয়া
        file_id = ""
        file_type = "file"
        file_name = "Direct File Asset"

        if message.video:
            file_id = message.video.file_id
            file_type = "video"
            file_name = message.video.file_name or "video.mp4"
        elif message.document:
            file_id = message.document.file_id
            file_type = "document"
            file_name = message.document.file_name or "document.bin"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_type = "photo"
            file_name = "photo.jpg"

        from app import generate_unique_code_sync
        code = generate_unique_code_sync()
        settings = get_settings()

        link_doc = {
            "short_code": code,
            "destination": file_id,
            "destination_type": "telegram_file",
            "metadata": {"file_id": file_id, "file_type": file_type, "name": file_name},
            "created_by": f"tg_{chat_id}",
            "created_at": datetime.now(timezone.utc),
            "status": "Active",
            "protect_content": settings["protect_content"],
            "clicks": 0,
            "step_views": 0,
            "final_clicks": 0
        }
        sync_db.links.insert_one(link_doc)

        short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
        send_creation_response(chat_id, file_name, short_url, code, settings["protect_content"])

    # সাধারণ টেক্সট URL শর্ট করা
    @bot.message_handler(func=lambda msg: msg.text and msg.text.startswith(("http://", "https://")))
    def handle_url(message):
        chat_id = message.chat.id
        from app import generate_unique_code_sync
        code = generate_unique_code_sync()
        settings = get_settings()

        link_doc = {
            "short_code": code,
            "destination": message.text.strip(),
            "destination_type": "url",
            "metadata": {"name": message.text.strip()[:40]},
            "created_by": f"tg_{chat_id}",
            "created_at": datetime.now(timezone.utc),
            "status": "Active",
            "protect_content": False,
            "clicks": 0,
            "step_views": 0,
            "final_clicks": 0
        }
        sync_db.links.insert_one(link_doc)

        short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
        send_creation_response(chat_id, "Standard Web URL", short_url, code, False)

    # ব্যাকগ্রাউন্ড পোলিং থ্রেড
    def run_polling():
        try:
            bot.remove_webhook()
        except Exception:
            pass
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=20)
            except Exception:
                time.sleep(3)

    threading.Thread(target=run_polling, daemon=True).start()
    print("🤖 Telegram Bot Ingress Engine Started!")
