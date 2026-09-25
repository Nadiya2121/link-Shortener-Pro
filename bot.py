import time
import re
import threading
import telebot
from telebot import types
from datetime import datetime, timezone
from bson import ObjectId

USER_BATCHES = {}
BATCH_TIMERS = {}
BATCH_LOCK = threading.Lock()
CHANNEL_POST_STATE = {}
FILE_CACHE = {}

def init_bot(app, sync_db, config):
    bot = telebot.TeleBot(config["BOT_TOKEN"], parse_mode="HTML", threaded=True)
    admin_ids = config.get("ADMIN_IDS", [5370676246])

    def is_admin(user_id):
        return user_id in admin_ids

    def get_settings():
        settings = sync_db.settings.find_one({"type": "global"})
        if not settings:
            return {
                "base_url": config["BASE_URL"],
                "auto_delete_minutes": 10,
                "protect_content": True,
                "tutorial_url": "",
                "public_shortener": True
            }
        return {
            "base_url": settings.get("base_url") or config["BASE_URL"],
            "auto_delete_minutes": settings.get("auto_delete_minutes", 10),
            "protect_content": settings.get("protect_content", True),
            "tutorial_url": settings.get("tutorial_url", ""),
            "public_shortener": settings.get("public_shortener", True)
        }

    def clean_brand_text(text):
        if not text:
            return "Exclusive Content"
        text = re.sub(r'@\w+', '', text)
        text = re.sub(r'https?://\S+|www\.\S+|t\.me/\S+', '', text)
        text = text.replace("_", " ").replace("[", "").replace("]", "")
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 2 else "Exclusive Content"

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

    # অ্যাডমিন এবং পাবলিকের জন্য আলাদা রেসপন্স বাটন
    def send_creation_response(chat_id, title, short_url, code, protect_status, user_is_admin):
        markup = types.InlineKeyboardMarkup(row_width=2)
        
        # 🌟 শুধুমাত্র অ্যাডমিনদের জন্য চ্যানেলে পোস্ট ও প্রটেকশন বাটন থাকবে
        if user_is_admin:
            btn_channel = types.InlineKeyboardButton("📢 Post to Channel", callback_data=f"postinit_{code}")
            btn_protect = types.InlineKeyboardButton(f"🛡️ Protect: {'ON' if protect_status else 'OFF'}", callback_data=f"tog_{code}")
            markup.add(btn_channel, btn_protect)

        btn_visit = types.InlineKeyboardButton("🌐 Open Link", url=short_url)
        btn_share = types.InlineKeyboardButton("🔗 Share Link", switch_inline_query=short_url)
        markup.add(btn_visit, btn_share)

        bot.send_message(
            chat_id,
            f"✅ <b>Smart Gateway Formed!</b>\n\n"
            f"📌 <b>Content:</b> {title}\n"
            f"🔗 <b>Short Link:</b> <code>{short_url}</code>\n\n"
            f"<i>লিংকটি বন্ধুদের সাথে শেয়ার করুন। লিংকে ক্লিক করলে স্বয়ংক্রিয়ভাবে কন্টেন্ট আনলক হয়ে যাবে।</i>",
            reply_markup=markup
        )

    def finalize_batch_link(chat_id, user_id):
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
        user_is_admin = is_admin(user_id)

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
            FILE_CACHE[code] = link_doc
            short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
            send_creation_response(chat_id, it["name"], short_url, code, settings["protect_content"], user_is_admin)
        else:
            album_doc = {
                "items": items,
                "chat_id": chat_id,
                "created_at": datetime.now(timezone.utc)
            }
            res = sync_db.albums.insert_one(album_doc)
            album_id = str(res.inserted_id)

            clean_batch_name = items[0]["name"] if items else "Exclusive Batch"
            link_doc = {
                "short_code": code,
                "destination": album_id,
                "destination_type": "telegram_album",
                "metadata": {"total_items": len(items), "name": f"{clean_batch_name} ({len(items)} Files)"},
                "created_by": f"tg_{chat_id}",
                "created_at": datetime.now(timezone.utc),
                "status": "Active",
                "protect_content": settings["protect_content"],
                "clicks": 0,
                "step_views": 0,
                "final_clicks": 0
            }
            sync_db.links.insert_one(link_doc)
            FILE_CACHE[code] = link_doc
            short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
            send_creation_response(chat_id, f"{clean_batch_name} ({len(items)} Files)", short_url, code, settings["protect_content"], user_is_admin)

    def show_batch_controls(chat_id, user_id):
        time.sleep(1.2)
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

    # /start হ্যান্ডলার
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        chat_id = message.chat.id
        from_user = message.from_user
        text = message.text or ""

        def track_user():
            sync_db.bot_users.update_one(
                {"user_id": from_user.id},
                {"$set": {
                    "name": from_user.first_name,
                    "username": from_user.username,
                    "last_active": datetime.now(timezone.utc)
                }},
                upsert=True
            )
        threading.Thread(target=track_user, daemon=True).start()

        if len(text.split()) > 1 and text.split()[1].startswith("unlock_"):
            code = text.split()[1].replace("unlock_", "")

            link = FILE_CACHE.get(code) or sync_db.links.find_one({"short_code": code})
            if not link:
                bot.send_message(chat_id, "❌ <b>দুঃখিত! ফাইলটি পাওয়া যায়নি বা মেয়াদ শেষ হয়ে গেছে।</b>")
                return

            settings = get_settings()
            del_min = settings["auto_delete_minutes"]
            is_protected = link.get("protect_content", settings["protect_content"])
            timer_note = f"\n\n⏳ <i>সতর্কতা: এই ফাইলটি {del_min} মিনিট পর স্বয়ংক্রিয়ভাবে মুছে যাবে!</i>" if del_min > 0 else ""

            dtype = link.get("destination_type")

            if dtype == "telegram_file":
                meta = link.get("metadata", {})
                clean_title = clean_brand_text(meta.get('name', 'File'))
                caption = f"🎉 <b>{clean_title}</b>{timer_note}"

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

            elif dtype == "telegram_album":
                album = sync_db.albums.find_one({"_id": ObjectId(link["destination"])})
                if album and album.get("items"):
                    bot.send_message(chat_id, f"📦 <b>আপনার প্যাকেজের মোট {len(album['items'])}টি ফাইল পাঠানো হচ্ছে...</b>")
                    for idx, it in enumerate(album["items"]):
                        clean_item_name = clean_brand_text(it.get('name', f'Part {idx+1}'))
                        caption = f"🎬 {clean_item_name}{timer_note}"
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
                        except Exception:
                            pass
            return

        bot.send_message(
            chat_id,
            "👋 <b>স্বাগতম Smart Link Shortener বটে!</b>\n\n"
            "আপনি যেকোনো সাধারণ ওয়েব লিঙ্ক, ভিডিও, ডকুমেন্ট বা ছবি পাঠিয়ে <b>সুরক্ষিত শর্ট লিঙ্ক তৈরি করতে পারেন।</b>"
        )

    # অ্যাডমিন কমান্ড: /stats
    @bot.message_handler(commands=['stats'])
    def handle_stats(message):
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "⛔ <b>দুঃখিত! এই কমান্ডটি শুধু অ্যাডমিনের জন্য সংরক্ষিত।</b>")
            return

        total_users = sync_db.bot_users.count_documents({})
        total_links = sync_db.links.count_documents({})
        total_albums = sync_db.albums.count_documents({})
        
        bot.send_message(
            message.chat.id,
            f"📊 <b>বট লাইভ অ্যানালিটিক্স:</b>\n\n"
            f"👥 মোট সক্রিয় ইউজার: <b>{total_users:,}</b> জন\n"
            f"🔗 মোট তৈরি করা লিংক: <b>{total_links:,}</b> টি\n"
            f"📦 মোট ব্যাচ অ্যালবাম: <b>{total_albums:,}</b> টি"
        )

    # অ্যাডমিন কমান্ড: /settutorial
    @bot.message_handler(commands=['settutorial'])
    def set_tutorial_cmd(message):
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "⛔ <b>দুঃখিত! এই কমান্ডটি শুধু অ্যাডমিনের জন্য সংরক্ষিত।</b>")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ নিয়ম: <code>/settutorial https://t.me/your_tutorial_link</code> এভাবে দিন।")
            return
        
        tut_url = parts[1].strip()
        sync_db.settings.update_one({"type": "global"}, {"$set": {"tutorial_url": tut_url}}, upsert=True)
        bot.send_message(message.chat.id, f"✅ <b>টিউটোরিয়াল বাটন লিংক সেট করা হয়েছে:</b>\n{tut_url}")

    # অ্যাডমিন কমান্ড: /broadcast
    @bot.message_handler(commands=['broadcast'])
    def handle_broadcast(message):
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "⛔ <b>দুঃখিত! এই কমান্ডটি শুধু অ্যাডমিনের জন্য সংরক্ষিত।</b>")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 and not message.reply_to_message:
            bot.send_message(message.chat.id, "❌ নিয়ম: <code>/broadcast আপনার মেসেজ এখানে লিখুন</code>")
            return

        broadcast_text = parts[1] if len(parts) > 1 else message.reply_to_message.text
        users = sync_db.bot_users.find({}, {"user_id": 1})
        all_users = list(users)
        
        bot.send_message(message.chat.id, f"⏳ মোট {len(all_users)} জন ইউজারের কাছে ব্রডকাস্ট পাঠানো শুরু হচ্ছে...")

        def broadcast_worker():
            success = 0
            failed = 0
            for u in all_users:
                try:
                    bot.send_message(u["user_id"], broadcast_text)
                    success += 1
                    time.sleep(0.04)
                except Exception:
                    failed += 1

            bot.send_message(message.chat.id, f"🎉 <b>ব্রডকাস্ট সম্পন্ন!</b>\n✅ সফল: {success} জন\n❌ ব্যর্থ: {failed} জন")

        threading.Thread(target=broadcast_worker, daemon=True).start()

    # কলব্যাক কুয়েরি হ্যান্ডলার
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callbacks(call):
        data = call.data
        chat_id = call.message.chat.id
        user_id = call.from_user.id
        settings = get_settings()

        if data.startswith("postinit_"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ আপনি অ্যাডমিন নন!", show_alert=True)
                return

            code = data.replace("postinit_", "")
            channels = list(sync_db.channels.find())

            if not channels:
                bot.send_message(chat_id, "⚠️ <b>কোনো চ্যানেল যুক্ত করা হয়নি!</b>\nআগে অ্যাডমিন প্যানেলে গিয়ে চ্যানেল অ্যাড করুন।")
                bot.answer_callback_query(call.id)
                return

            markup = types.InlineKeyboardMarkup(row_width=1)
            for ch in channels:
                markup.add(types.InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"selch_{ch['channel_id']}_{code}"))

            bot.send_message(chat_id, "🎯 <b>কোন চ্যানেলে পোস্ট করতে চান? চ্যানেল বেছে নিন:</b>", reply_markup=markup)
            bot.answer_callback_query(call.id)

        elif data.startswith("selch_"):
            parts = data.split("_")
            ch_target = parts[1]
            code = parts[2]

            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_quick = types.InlineKeyboardButton("⚡ Quick Post", callback_data=f"qpost_{ch_target}_{code}")
            btn_custom = types.InlineKeyboardButton("🎨 Custom Poster Post", callback_data=f"cpost_{ch_target}_{code}")
            markup.add(btn_quick, btn_custom)

            bot.send_message(chat_id, f"📢 <b>চ্যানেল: <code>{ch_target}</code></b>\nপোস্টের ধরন সিলেক্ট করুন:", reply_markup=markup)
            bot.answer_callback_query(call.id)

        elif data.startswith("qpost_"):
            if not is_admin(user_id):
                return

            parts = data.split("_")
            ch_target = parts[1]
            code = parts[2]

            link = sync_db.links.find_one({"short_code": code})
            short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
            title = clean_brand_text(link.get("metadata", {}).get("name", "Exclusive Video"))

            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_watch = types.InlineKeyboardButton("📥 Download / Watch", url=short_url)
            if settings.get("tutorial_url"):
                btn_tut = types.InlineKeyboardButton("❓ How to Watch?", url=settings["tutorial_url"])
                markup.add(btn_watch, btn_tut)
            else:
                markup.add(btn_watch)

            try:
                bot.send_message(ch_target, f"🎬 <b>{title}</b>\n\n⚡ সম্পূর্ণ ফ্রিতে ডাউনলোড করতে নিচের বাটনে চাপ দিন 👇", reply_markup=markup)
                bot.answer_callback_query(call.id, f"🎉 {ch_target} এ পোস্ট হয়েছে!", show_alert=True)
            except Exception as e:
                bot.answer_callback_query(call.id, f"এরর: {str(e)}", show_alert=True)

        elif data.startswith("cpost_"):
            if not is_admin(user_id):
                return

            parts = data.split("_")
            ch_target = parts[1]
            code = parts[2]

            CHANNEL_POST_STATE[chat_id] = {
                "step": "AWAIT_POSTER",
                "code": code,
                "channel_target": ch_target
            }
            bot.send_message(chat_id, f"📸 <b>{ch_target} এর জন্য পোস্টার/ছবিটি সেন্ড করুন:</b>")
            bot.answer_callback_query(call.id)

        elif data == "batch_add_more":
            with BATCH_LOCK:
                if chat_id in USER_BATCHES:
                    USER_BATCHES[chat_id]["is_waiting_more"] = True
            bot.answer_callback_query(call.id, "➕ আরও ফাইল পাঠান...")
            bot.send_message(chat_id, "📥 <b>আরও যতগুলো ফাইল চান পাঠান। সব পাঠানো শেষ হলে নিচে Done চাপুন।</b>", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Done (Create Link)", callback_data="batch_done")))

        elif data == "batch_done":
            bot.answer_callback_query(call.id, "⏳ লিংক তৈরি হচ্ছে...")
            bot.delete_message(chat_id, call.message.message_id)
            finalize_batch_link(chat_id, user_id)

        elif data.startswith("tog_"):
            if not is_admin(user_id):
                return
            code = data.replace("tog_", "")
            link = sync_db.links.find_one({"short_code": code})
            if link:
                new_state = not link.get("protect_content", True)
                sync_db.links.update_one({"_id": link["_id"]}, {"$set": {"protect_content": new_state}})
                short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
                title = clean_brand_text(link.get("metadata", {}).get("name", "Content"))

                markup = types.InlineKeyboardMarkup(row_width=2)
                btn_channel = types.InlineKeyboardButton("📢 Post to Channel", callback_data=f"postinit_{code}")
                btn_protect = types.InlineKeyboardButton(f"🛡️ Protect: {'ON' if new_state else 'OFF'}", callback_data=f"tog_{code}")
                btn_visit = types.InlineKeyboardButton("🌐 Open Link", url=short_url)
                btn_share = types.InlineKeyboardButton("🔗 Share Link", switch_inline_query=short_url)
                markup.add(btn_channel, btn_protect)
                markup.add(btn_visit, btn_share)

                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
                bot.answer_callback_query(call.id, f"Protection is now {'ON' if new_state else 'OFF'}")

    # পোস্টার ছবি রিসিভার
    @bot.message_handler(content_types=['photo'])
    def handle_poster_photo(message):
        chat_id = message.chat.id
        if chat_id in CHANNEL_POST_STATE and CHANNEL_POST_STATE[chat_id].get("step") == "AWAIT_POSTER":
            if not is_admin(message.from_user.id):
                return
            CHANNEL_POST_STATE[chat_id]["photo_id"] = message.photo[-1].file_id
            CHANNEL_POST_STATE[chat_id]["step"] = "AWAIT_CAPTION"
            bot.send_message(chat_id, "✍️ <b>পোস্টের জন্য আকর্ষণীয় ক্যাপশন লিখে পাঠান (বা স্কিপ করতে /skip লিখুন):</b>")
            return

        handle_incoming_media(message)

    @bot.message_handler(func=lambda msg: msg.chat.id in CHANNEL_POST_STATE and CHANNEL_POST_STATE[msg.chat.id].get("step") == "AWAIT_CAPTION")
    def handle_custom_caption(message):
        chat_id = message.chat.id
        if not is_admin(message.from_user.id):
            return

        data = CHANNEL_POST_STATE.pop(chat_id)
        settings = get_settings()

        channel_target = data["channel_target"]
        code = data["code"]
        photo_id = data["photo_id"]
        short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
        caption_text = message.text.strip() if message.text != "/skip" else "🔥 New Exclusive Content Available Now!"

        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_watch = types.InlineKeyboardButton("📥 Download / Watch (HD)", url=short_url)
        if settings.get("tutorial_url"):
            btn_tut = types.InlineKeyboardButton("❓ How to Watch?", url=settings["tutorial_url"])
            markup.add(btn_watch, btn_tut)
        else:
            markup.add(btn_watch)

        try:
            bot.send_photo(
                chat_id=channel_target,
                photo=photo_id,
                caption=f"🎬 <b>{caption_text}</b>\n\n⚡ সম্পূর্ণ ফ্রিতে দেখতে বা ডাউনলোড করতে নিচের বাটনে চাপ দিন 👇",
                reply_markup=markup
            )
            bot.send_message(chat_id, f"🎉 <b>{channel_target} চ্যানেলে পোস্টার সহ সফলভাবে পোস্ট হয়েছে!</b>")
        except Exception as e:
            bot.send_message(chat_id, f"❌ চ্যানেলে পোস্ট ব্যর্থ: {str(e)}")

    # 🌟 পাবলিক ও অ্যাডমিন সবার মিডিয়া ফাইল হ্যান্ডলার
    @bot.message_handler(content_types=['document', 'video', 'audio'])
    def handle_incoming_media(message):
        chat_id = message.chat.id
        from_user = message.from_user
        settings = get_settings()

        # যদি পাবলিক শর্টনার অফ থাকে এবং ইউজার অ্যাডমিন না হয়
        if not settings.get("public_shortener", True) and not is_admin(from_user.id):
            bot.send_message(chat_id, "⛔ <b>পাবলিক লিংক শর্টনার বর্তমানে বন্ধ রয়েছে।</b>")
            return

        file_id = ""
        file_type = "file"
        raw_name = "Media Asset"

        if message.video:
            file_id = message.video.file_id
            file_type = "video"
            raw_name = message.video.file_name or message.caption or "video.mp4"
        elif message.document:
            file_id = message.document.file_id
            file_type = "document"
            raw_name = message.document.file_name or message.caption or "document.bin"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_type = "photo"
            raw_name = message.caption or "photo.jpg"

        cleaned_name = clean_brand_text(raw_name)

        with BATCH_LOCK:
            if chat_id not in USER_BATCHES:
                USER_BATCHES[chat_id] = {"items": [], "is_waiting_more": False}
            
            USER_BATCHES[chat_id]["items"].append({
                "file_id": file_id,
                "file_type": file_type,
                "name": cleaned_name
            })

            if chat_id in BATCH_TIMERS:
                try:
                    BATCH_TIMERS[chat_id].cancel()
                except Exception:
                    pass

            timer = threading.Timer(1.2, show_batch_controls, args=[chat_id, from_user.id])
            BATCH_TIMERS[chat_id] = timer
            timer.start()

    # 🌟 পাবলিক ও অ্যাডমিন সবার টেক্সট URL শর্ট করা
    @bot.message_handler(func=lambda msg: msg.text and msg.text.startswith(("http://", "https://")))
    def handle_url(message):
        chat_id = message.chat.id
        from_user = message.from_user
        settings = get_settings()

        if not settings.get("public_shortener", True) and not is_admin(from_user.id):
            bot.send_message(chat_id, "⛔ <b>পাবলিক লিংক শর্টনার বর্তমানে বন্ধ রয়েছে।</b>")
            return

        from app import generate_unique_code_sync
        code = generate_unique_code_sync()
        user_is_admin = is_admin(from_user.id)

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
        FILE_CACHE[code] = link_doc

        short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
        send_creation_response(chat_id, "Standard Web URL", short_url, code, False, user_is_admin)

    def setup_webhook():
        time.sleep(2)
        webhook_url = f"{config['BASE_URL'].rstrip('/')}/api/telegram/webhook"
        try:
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url, drop_pending_updates=True)
            print(f"🚀 Telegram Webhook Successfully Linked to: {webhook_url}")
        except Exception as e:
            print(f"Webhook setup error: {e}")

    threading.Thread(target=setup_webhook, daemon=True).start()
    return bot
