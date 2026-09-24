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

def init_bot(app, sync_db, config):
    bot = telebot.TeleBot(config["BOT_TOKEN"], parse_mode="HTML", threaded=True)

    def get_settings():
        settings = sync_db.settings.find_one({"type": "global"})
        if not settings:
            return {
                "base_url": config["BASE_URL"],
                "channel_id": "",
                "auto_delete_minutes": 10,
                "protect_content": True,
                "tutorial_url": ""
            }
        return {
            "base_url": settings.get("base_url") or config["BASE_URL"],
            "channel_id": settings.get("channel_id", ""),
            "auto_delete_minutes": settings.get("auto_delete_minutes", 10),
            "protect_content": settings.get("protect_content", True),
            "tutorial_url": settings.get("tutorial_url", "")
        }

    # অন্যের নাম/লিংক মুছে ফেলার স্মার্ট ক্লিনার
    def clean_brand_text(text):
        if not text:
            return "Exclusive Content"
        text = re.sub(r'@\w+', '', text)
        text = re.sub(r'https?://\S+|www\.\S+|t\.me/\S+', '', text)
        text = text.replace("_", " ").replace("[", "").replace("]", "")
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 2 else "Exclusive Content"

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
        btn_channel = types.InlineKeyboardButton("📢 Post to Channel", callback_data=f"postinit_{code}")
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
            short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
            send_creation_response(chat_id, f"{clean_batch_name} ({len(items)} Files)", short_url, code, settings["protect_content"])

    # ফাইল আসা শেষ হলে Add More / Done বাটন দেখানো
    def show_batch_controls(chat_id):
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

    # 🌟 ১. /start কমান্ড ও ইউজার ট্র্যাকিং
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        chat_id = message.chat.id
        from_user = message.from_user
        text = message.text or ""

        # ডেটাবেজে ইউজার ট্র্যাক রাখা (ব্রডকাস্টের জন্য)
        sync_db.bot_users.update_one(
            {"user_id": from_user.id},
            {"$set": {
                "name": from_user.first_name,
                "username": from_user.username,
                "last_active": datetime.now(timezone.utc)
            }},
            upsert=True
        )

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
                        except Exception as e:
                            print(f"File delivery error: {e}")
            return

        bot.send_message(
            chat_id,
            "👋 <b>স্বাগতম Smart Link Shortener বটে!</b>\n\n"
            "আপনি চাইলে একটি বা <b>একসাথে অনেকগুলো ভিডিও/ফাইল</b> ফরওয়ার্ড করতে পারেন। বট অন্যের সব নাম-লিংক মুছে ফেলে সম্পূর্ণ ক্লিন শর্ট লিংক তৈরি করে দেবে।"
        )

    # 🌟 ২. ইউজার স্ট্যাটাস দেখার অ্যাডমিন কমান্ড (/stats)
    @bot.message_handler(commands=['stats'])
    def handle_stats(message):
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

    # 🌟 ৩. টিউটোরিয়াল ভিডিওর লিংক সেট করার কমান্ড (/settutorial)
    @bot.message_handler(commands=['settutorial'])
    def set_tutorial_cmd(message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ নিয়ম: <code>/settutorial https://t.me/your_tutorial_link</code> এভাবে দিন।")
            return
        
        tut_url = parts[1].strip()
        sync_db.settings.update_one({"type": "global"}, {"$set": {"tutorial_url": tut_url}}, upsert=True)
        bot.send_message(message.chat.id, f"✅ <b>টিউটোরিয়াল বাটন লিংক সেট করা হয়েছে:</b>\n{tut_url}")

    # 🌟 ৪. সমস্ত ইউজারের কাছে মেসেজ পাঠানো (/broadcast)
    @bot.message_handler(commands=['broadcast'])
    def handle_broadcast(message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 and not message.reply_to_message:
            bot.send_message(message.chat.id, "❌ নিয়ম: <code>/broadcast আপনার মেসেজ এখানে লিখুন</code> (অথবা কোনো মেসেজে রিপ্লাই দিয়ে /broadcast লিখুন)")
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
                    time.sleep(0.05)  # টেলিগ্রাম রেট লিমিট প্রটেকশন
                except Exception:
                    failed += 1

            bot.send_message(
                message.chat.id,
                f"🎉 <b>ব্রডকাস্ট সম্পন্ন হয়েছে!</b>\n\n"
                f"✅ সফল: {success} জন\n"
                f"❌ ব্যর্থ (ব্লক করেছে): {failed} জন"
            )

        threading.Thread(target=broadcast_worker, daemon=True).start()

    # কলব্যাক কুয়েরি হ্যান্ডলার
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callbacks(call):
        data = call.data
        chat_id = call.message.chat.id
        settings = get_settings()

        # ১. চ্যানেলে পোস্ট অপশন পছন্দ করা
        if data.startswith("postinit_"):
            code = data.replace("postinit_", "")
            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_quick = types.InlineKeyboardButton("⚡ Quick Post", callback_data=f"qpost_{code}")
            btn_custom = types.InlineKeyboardButton("🎨 Custom Poster Post", callback_data=f"cpost_{code}")
            markup.add(btn_quick, btn_custom)

            bot.send_message(
                chat_id,
                "📢 <b>চ্যানেলে কীভাবে পোস্ট করতে চান?</b>\n\n"
                "• <b>Quick Post:</b> সাধারণ টেক্সট সহ সরাসরি পোস্ট।\n"
                "• <b>Custom Poster:</b> সুন্দর ছবি/পোস্টার এবং কাস্টম টাইটেল দিয়ে পোস্ট।",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id)

        # ২. কুইক পোস্ট (ডাবল বাটন: Download + How to Watch)
        elif data.startswith("qpost_"):
            code = data.replace("qpost_", "")
            link = sync_db.links.find_one({"short_code": code})
            channel_target = settings.get("channel_id")

            if not channel_target:
                bot.answer_callback_query(call.id, "⚠️ অ্যাডমিন প্যানেলে চ্যানেল আইডি দেওয়া নেই!", show_alert=True)
                return

            short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"
            title = clean_brand_text(link.get("metadata", {}).get("name", "Exclusive Video"))

            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_watch = types.InlineKeyboardButton("📥 Download / Watch", url=short_url)
            
            # টিউটোরিয়াল বাটন যুক্ত করা
            if settings.get("tutorial_url"):
                btn_tut = types.InlineKeyboardButton("❓ How to Watch?", url=settings["tutorial_url"])
                markup.add(btn_watch, btn_tut)
            else:
                markup.add(btn_watch)

            try:
                bot.send_message(channel_target, f"🎬 <b>{title}</b>\n\n⚡ সম্পূর্ণ ফ্রিতে ডাউনলোড করতে নিচের বাটনে চাপ দিন 👇", reply_markup=markup)
                bot.answer_callback_query(call.id, "🎉 চ্যানেলে পোস্ট হয়েছে!", show_alert=True)
            except Exception as e:
                bot.answer_callback_query(call.id, f"এরর: {str(e)}", show_alert=True)

        # ৩. কাস্টম পোস্টার পোস্ট শুরু
        elif data.startswith("cpost_"):
            code = data.replace("cpost_", "")
            CHANNEL_POST_STATE[chat_id] = {
                "step": "AWAIT_POSTER",
                "code": code
            }
            bot.send_message(chat_id, "📸 <b>চ্যানেলে যে পোস্টার/ছবিটি পাঠাতে চান, সেই ছবিটি সেন্ড করুন:</b>")
            bot.answer_callback_query(call.id)

        # ৪. ব্যাচে আরও ফাইল অ্যাড করা
        elif data == "batch_add_more":
            with BATCH_LOCK:
                if chat_id in USER_BATCHES:
                    USER_BATCHES[chat_id]["is_waiting_more"] = True
            bot.answer_callback_query(call.id, "➕ আরও ফাইল পাঠান...")
            bot.send_message(chat_id, "📥 <b>আরও যতগুলো ফাইল চান পাঠান। সব পাঠানো শেষ হলে নিচে Done চাপুন।</b>", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Done (Create Link)", callback_data="batch_done")))

        # ৫. ব্যাচ লিংক তৈরি সম্পন্ন করা
        elif data == "batch_done":
            bot.answer_callback_query(call.id, "⏳ লিংক তৈরি হচ্ছে...")
            bot.delete_message(chat_id, call.message.message_id)
            finalize_batch_link(chat_id)

        # ৬. ফরওয়ার্ড প্রোটেকশন টগল
        elif data.startswith("tog_"):
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

    # কাস্টম পোস্টার ছবি ও ক্যাপশন রিসিভার
    @bot.message_handler(content_types=['photo'])
    def handle_poster_photo(message):
        chat_id = message.chat.id
        if chat_id in CHANNEL_POST_STATE and CHANNEL_POST_STATE[chat_id].get("step") == "AWAIT_POSTER":
            CHANNEL_POST_STATE[chat_id]["photo_id"] = message.photo[-1].file_id
            CHANNEL_POST_STATE[chat_id]["step"] = "AWAIT_CAPTION"
            bot.send_message(chat_id, "✍️ <b>পোস্টের জন্য একটি আকর্ষণীয় টাইটেল/ক্যাপশন লিখে পাঠান (বা স্কিপ করতে /skip লিখুন):</b>")
            return

        handle_incoming_media(message)

    @bot.message_handler(func=lambda msg: msg.chat.id in CHANNEL_POST_STATE and CHANNEL_POST_STATE[msg.chat.id].get("step") == "AWAIT_CAPTION")
    def handle_custom_caption(message):
        chat_id = message.chat.id
        data = CHANNEL_POST_STATE.pop(chat_id)
        settings = get_settings()

        channel_target = settings.get("channel_id")
        if not channel_target:
            bot.send_message(chat_id, "⚠️ অ্যাডমিন প্যানেলে চ্যানেল আইডি দেওয়া নেই!")
            return

        code = data["code"]
        photo_id = data["photo_id"]
        short_url = f"{settings['base_url'].rstrip('/')}/s/{code}"

        caption_text = message.text.strip() if message.text != "/skip" else "🔥 New Exclusive Content Available Now!"

        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_watch = types.InlineKeyboardButton("📥 Download / Watch (HD)", url=short_url)
        
        # 🌟 টিউটোরিয়াল বাটন যুক্ত করা
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
            bot.send_message(chat_id, "🎉 <b>পোস্টার ও টিউটোরিয়াল বাটন সহ চ্যানেলে পোস্ট সফল হয়েছে!</b>")
        except Exception as e:
            bot.send_message(chat_id, f"❌ চ্যানেলে পোস্ট ব্যর্থ: {str(e)}")

    # মিডিয়া ফাইল হ্যান্ডলার
    @bot.message_handler(content_types=['document', 'video', 'audio'])
    def handle_incoming_media(message):
        chat_id = message.chat.id

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

            timer = threading.Timer(1.2, show_batch_controls, args=[chat_id])
            BATCH_TIMERS[chat_id] = timer
            timer.start()

    # টেক্সট URL শর্ট করা
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
