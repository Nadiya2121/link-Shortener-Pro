import time
import threading
import telebot
from telebot import types
from datetime import datetime, timezone
from bson import ObjectId

# ব্যাচ প্রসেসিং ট্র্যাকিং
USER_BATCHES = {}
BATCH_TIMERS = {}
BATCH_LOCK = threading.Lock()

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

    # ইনবক্স থেকে ফাইল অটোমেটিক ডিলিট করার ব্যাকগ্রাউন্ড টাস্ক
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

    # চূড়ান্ত ব্যাচ লিংক জেনারেট করার ইঞ্জিন
    def finalize_batch_link(chat_id):
        with BATCH_LOCK:
            batch = USER_BATCHES.pop(chat_id, None)
            if chat_id in BATCH_TIMERS:
                del BATCH_TIMERS[chat_id]

        if not batch or not batch.get("items"):
            return

        items = batch["items"]
        settings = get_settings()
        from app import generate_unique_code_sync
        code = generate_unique_code_sync()

        # যদি মাত্র একটি ফাইল থাকে তাহলে সিঙ্গেল ফাইল লিংক হবে
        if len(items) == 1:
            it = items[0]
            link_doc = {
                "short_code": code,
                "destination": it["file_id"],
                "destination_type": "telegram_file",
                "metadata": {"file_id": it["file_id"], "file_type": it["file_type"], "name": it["name"]},
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
            send_creation_response(chat_id, it["name"], short_url, code, settings["protect_content"])
        else:
            # একাধিক ফাইল থাকলে অ্যালবাম/ব্যাচ হিসেবে সেভ হবে
            album_doc = {
                "items": items,
                "chat_id": chat_id,
                "created_at": datetime.now(timezone.utc)
            }
            res = sync_db.albums.insert_one(album_doc)
            album_id = str(res.inserted_id)

            link_doc = {
                "short_code": code,
                "destination": album_id,
                "destination_type": "telegram_album",
                "metadata": {"total_items": len(items), "name": f"Batch Collection ({len(items)} Files)"},
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
            send_creation_response(chat_id, f"Batch Collection ({len(items)} Files)", short_url, code, settings["protect_content"])

    # ফাইলগুলো আসা শেষ হলে Add More / Done প্যানেল শো করা
    def show_batch_controls(chat_id):
        time.sleep(1.2)  # টেলিগ্রামের একসাথে পাঠানো মেসেজগুলো জমা হওয়ার ছোট বিরতি
        with BATCH_LOCK:
            batch = USER_BATCHES.get(chat_id)
            if not batch or batch.get("is_waiting_more"):
                return
            count = len(batch.get("items", []))

        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_add_more = types.InlineKeyboardButton("➕ Add More", callback_data="batch_add_more")
        btn_done = types.InlineKeyboardButton("✅ Done (Create Link)", callback_data="batch_done")
        markup.add(btn_add_more, btn_done)

        bot.send_message(
            chat_id,
            f"📦 <b>মোট ফাইল যুক্ত হয়েছে: {count} টি</b>\n\n"
            f"আরও ফাইল যোগ করতে চাইলে <b>Add More</b> চাপুন, অথবা লিংক তৈরি করতে <b>Done</b> চাপুন:",
            reply_markup=markup
        )

    # /start কমান্ড হ্যান্ডলার
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        chat_id = message.chat.id
        text = message.text or ""

        # যদি ইউজার শর্ট লিংক আনলক করে ফাইল নিতে আসে
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

            # ব্যাচ / অ্যালবাম ডেলিভারি (সবগুলো ফাইল একটার পর একটা পাঠানো)
            elif dtype == "telegram_album":
                album = sync_db.albums.find_one({"_id": ObjectId(link["destination"])})
                if album and album.get("items"):
                    bot.send_message(chat_id, f"📦 <b>আপনার প্যাকেজের মোট {len(album['items'])}টি ফাইল পাঠানো হচ্ছে...</b>")
                    for idx, it in enumerate(album["items"]):
                        caption = f"🎬 Part {idx+1}/{len(album['items'])}: {it.get('name', 'Media')}{timer_note}"
                        try:
                            if it["file_type"] == "video":
                                sent_msg = bot.send_video(chat_id, it["file_id"], protect_content=is_protected, caption=caption)
                            elif it["file_type"] == "photo":
                                sent_msg = bot.send_photo(chat_id, it["file_id"], protect_content=is_protected, caption=caption)
                            else:
                                sent_msg = bot.send_document(chat_id, it["file_id"], protect_content=is_protected, caption=caption)
                            
                            if del_min > 0:
                                schedule_auto_delete(chat_id, sent_msg.message_id, del_min)
                            time.sleep(0.3)
                        except Exception as e:
                            print(f"File send error: {e}")
            return

        bot.send_message(
            chat_id,
            "👋 <b>স্বাগতম Smart Link Shortener বটে!</b>\n\n"
            "আপনি চাইলে একটি বা <b>একসাথে অনেকগুলো ভিডিও/ফাইল</b> ফরওয়ার্ড করতে পারেন। বট স্বয়ংক্রিয়ভাবে সেগুলোকে একটি ব্যাচে নিয়ে সিঙ্গেল শর্ট লিংক তৈরি করে দেবে।"
        )

    # কলব্যাক কুয়েরি হ্যান্ডলার
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callbacks(call):
        data = call.data
        chat_id = call.message.chat.id
        settings = get_settings()

        # ব্যাচে আরও ফাইল অ্যাড করার অপশন
        if data == "batch_add_more":
            with BATCH_LOCK:
                if chat_id in USER_BATCHES:
                    USER_BATCHES[chat_id]["is_waiting_more"] = True
            bot.answer_callback_query(call.id, "➕ আরও ফাইল পাঠান...")
            bot.send_message(chat_id, "📥 <b>এখন আরও যতগুলো ভিডিও/ফাইল চান পাঠান। সব পাঠানো হলে নিচে Done চাপুন।</b>", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Done (Create Link)", callback_data="batch_done")))

        # ব্যাচ সম্পূর্ণ করে লিংক তৈরি করা
        elif data == "batch_done":
            bot.answer_callback_query(call.id, "⏳ লিংক তৈরি হচ্ছে...")
            bot.delete_message(chat_id, call.message.message_id)
            finalize_batch_link(chat_id)

        # চ্যানেলে অটো-পোস্ট করা
        elif data.startswith("post_"):
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

        # ফরওয়ার্ড প্রটেক্ট টগল
        elif data.startswith("tog_"):
            code = data.replace("tog_", "")
            link = sync_db.links.find_one({"short_code": code})
            if link:
                new_state = not link.get("protect_content", True)
                sync_db.links.update_one({"_id": link["_id"]}, {"$set": {"protect_content": new_state}})
                short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
                title = link.get("metadata", {}).get("name", "Content")

                markup = types.InlineKeyboardMarkup(row_width=2)
                btn_channel = types.InlineKeyboardButton("📢 Post to Channel", callback_data=f"post_{code}")
                btn_protect = types.InlineKeyboardButton(f"🛡️ Protect: {'ON' if new_state else 'OFF'}", callback_data=f"tog_{code}")
                btn_visit = types.InlineKeyboardButton("🌐 Open Link", url=short_url)
                btn_share = types.InlineKeyboardButton("🔗 Share Link", switch_inline_query=short_url)
                markup.add(btn_channel, btn_protect)
                markup.add(btn_visit, btn_share)

                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
                bot.answer_callback_query(call.id, f"Protection is now {'ON' if new_state else 'OFF'}")

    # মিডিয়া ফাইল হ্যান্ডলার (ভিডিও, ডকুমেন্ট, ফটো)
    @bot.message_handler(content_types=['document', 'video', 'photo', 'audio'])
    def handle_incoming_media(message):
        chat_id = message.chat.id

        file_id = ""
        file_type = "file"
        file_name = "Media Asset"

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

        # ইউজার ব্যাচে ফাইল যুক্ত করা
        with BATCH_LOCK:
            if chat_id not in USER_BATCHES:
                USER_BATCHES[chat_id] = {"items": [], "is_waiting_more": False}
            
            USER_BATCHES[chat_id]["items"].append({
                "file_id": file_id,
                "file_type": file_type,
                "name": file_name
            })

            # যদি নতুন ফাইল পাঠানো হয়, তাহলে আগের টাইমার বাতিল করে নতুন করে কাউন্টডাউন
            if chat_id in BATCH_TIMERS:
                try:
                    BATCH_TIMERS[chat_id].cancel()
                except Exception:
                    pass

            # ১.২ সেকেন্ড অপেক্ষা করে বাটন শো করার থ্রেড
            timer = threading.Timer(1.2, show_batch_controls, args=[chat_id])
            BATCH_TIMERS[chat_id] = timer
            timer.start()

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
