import time
import re
import os
import requests
import json
import threading
import html
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup

# ---------------------------------------------------------
# ۱. وب‌سرور نیتیو و سبک برای زنده نگه‌داشتن Render
# ---------------------------------------------------------
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Bot is online and running FREE!")

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthServer)
    server.serve_forever()

# ---------------------------------------------------------
# ۲. تنظیمات اصلی و فایل‌های ذخیره‌سازی
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MY_CHANNEL = "@Rallyir"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

DB_FILE = "seen_posts.txt"
CHANNELS_FILE = "channels.txt"
CONFIG_FILE = "config.json"

DEFAULT_CHANNELS = [
    "JahanTasnim", "sepahnewsir403", "FarsNewsInt", "mizanplus",
    "jamarannews", "Jahan_Fouri", "Tasnimnews", "IRNA_1313", 
    "YjcNewsChannel", "isna94"
]

recent_news_summaries = []
today_top_headlines = []
user_states = {}
selected_channels_for_bulk_delete = {}
db_lock = threading.Lock()

def load_config():
    default_config = {
        "bot_active": True,
        "channel_signature": f"🆔 {MY_CHANNEL}",
        "blacklist": ["تبلیغات", "تخفیف ویژه"],
        "replacements": {
            "میزان در شبکه‌های اجتماعی": ""
        },
        "check_interval": 10
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                default_config.update(data)
            except Exception:
                pass
    return default_config

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config_db = load_config()

def load_seen_posts():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen_post(post_id):
    with db_lock:
        with open(DB_FILE, "a", encoding="utf-8") as f:
            f.write(f"{post_id}\n")

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            channels = [line.strip() for line in f if line.strip()]
            if channels:
                return channels[:20]
    return DEFAULT_CHANNELS.copy()

def save_channels(channels):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        for ch in channels[:20]:
            f.write(f"{ch}\n")

seen_post_ids = load_seen_posts()
target_channels = load_channels()

# ---------------------------------------------------------
# ۳. پاک‌سازی لینک‌ها، ایموجی‌ها و جایگزینی سفارشی کلمات
# ---------------------------------------------------------
def sanitize_all_links(text):
    if not text:
        return ""
    
    replacements = config_db.get("replacements", {})
    for old_word, new_word in replacements.items():
        if old_word:
            text = re.sub(re.escape(old_word), new_word, text, flags=re.IGNORECASE)

    text = re.sub(r"https?://\S+|www\.\S+|t\.me/\S+", "", text)
    text = re.sub(r"@[A-Za-z0-9_]+", "", text)
    
    emoji_pattern = re.compile(
        "["
        "\U00010000-\U0010FFFF"
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002600-\U000027BF"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "\u2702-\u27B0"
        "\u24C2-\u1F251"
        "]+", flags=re.UNICODE
    )
    text = emoji_pattern.sub(r'', text)
    return "\n".join([line.strip() for line in text.splitlines() if line.strip()]).strip()

def check_blacklist(text):
    for word in config_db.get("blacklist", []):
        if word and word.lower() in text.lower():
            return True
    return False

def clean_fallback(text):
    clean_t = sanitize_all_links(text)
    lines = clean_t.splitlines()
    if not lines:
        return ""
    
    lines[0] = f"📌 <b>{lines[0]}</b>"
    sig = config_db.get("channel_signature", f"🆔 {MY_CHANNEL}")
    return "\n\n".join(lines) + f"\n\n#خبر\n\n{sig}"

# ---------------------------------------------------------
# ۴. ارتباط با API هوش مصنوعی Gemini
# ---------------------------------------------------------
def call_gemini_api(prompt_text):
    if not GEMINI_API_KEY:
        return None

    models = ["gemini-1.5-flash", "gemini-1.5-pro"]
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
        try:
            res = http_session.post(url, json=payload, timeout=8)
            if res.status_code == 200:
                data = res.json()
                text_response = data['candidates'][0]['content']['parts'][0]['text']
                if text_response:
                    return text_response.strip()
        except Exception:
            continue
    return None

def summarize_text_with_ai(raw_text):
    clean_text = sanitize_all_links(raw_text)
    if not clean_text or len(clean_text) < 15:
        return "❌ متن ارسالی برای خلاصه‌سازی بسیار کوتاه است."

    prompt = (
        "وظیفه: خلاصه‌سازی خبر.\n"
        "دستورالعمل:\n"
        "۱. خبر زیر را خوانده و فقط در ۲ تا ۳ جمله کوتاه، روان و مفید خلاصه کن.\n"
        "۲. خط اول حتما یک تیتر کوتاه و جذاب باشد.\n"
        "۳. تمام لینک‌ها، تبلیغات، آیدی‌ها و ایموجی‌ها را حذف کن.\n"
        "۴. عین متن اصلی را کپی نکن، بلکه آن را فشرده کن.\n\n"
        f"متن خبر:\n{clean_text}"
    )

    ai_result = call_gemini_api(prompt)
    if ai_result:
        ai_result = sanitize_all_links(ai_result)
        lines = [line.strip() for line in ai_result.splitlines() if line.strip()]
        if lines:
            headline = lines[0].replace('<b>','').replace('</b>','')
            body = "\n\n".join(lines[1:]) if len(lines) > 1 else ""
            sig = config_db.get("channel_signature", f"🆔 {MY_CHANNEL}")
            output = f"📝 <b>خلاصه خبر (هوش مصنوعی):</b>\n\n📌 <b>{headline}</b>\n\n{body}"
            return output.strip() + f"\n\n{sig}"

    return "📝 <b>خلاصه خبر (پشتیبان):</b>\n\n" + clean_fallback(clean_text)

def rewrite_with_ai(raw_text):
    global recent_news_summaries, today_top_headlines

    clean_raw = sanitize_all_links(raw_text)
    if not clean_raw or len(clean_raw) < 15 or check_blacklist(clean_raw):
        return None

    recent_context = "\n---\n".join(recent_news_summaries[-15:]) if recent_news_summaries else "هیچ خبری ثبت نشده است."

    prompt = (
        "تو یک ویرایشگر خبر حرفه‌ای هستی. وظیفه داری متن زیر را بازنویسی کنی.\n\n"
        "قوانین مهم:\n"
        "۱. ابتدا بررسی کن آیا این خبر موضوع کاملاً مشابهی با اخبار اخیر دارد یا خیر. اگر تکراری است فقط کلمه 'DUPLICATE' را بنویس.\n"
        "۲. اگر خبر تبلیغاتی است فقط کلمه 'SKIP' را بنویس.\n"
        "۳. اگر جدید است، آن را بازنویسی کن:\n"
        "   - تمام لینک‌ها و آیدی‌ها را حذف کن.\n"
        "   - خط اول را یک تیتر جذاب قرار بده.\n"
        "   - هیچ ایموجی استفاده نکن.\n"
        "۴. در انتهای متن ۲ تا ۴ هشتگ تخصصی قرار بده.\n\n"
        f"اخبار اخیر:\n{recent_context}\n\n"
        f"متن جدید:\n{clean_raw}"
    )

    ai_result = call_gemini_api(prompt)
    if ai_result:
        if "DUPLICATE" in ai_result or "SKIP" in ai_result:
            return None

        result = sanitize_all_links(ai_result)
        lines = [line.strip() for line in result.splitlines() if line.strip()]
        if lines:
            headline = lines[0].replace("<b>", "").replace("</b>", "")
            lines[0] = f"📌 <b>{headline}</b>"
            formatted_body = "\n\n".join(lines)

            recent_news_summaries.append(clean_raw[:100])
            if len(recent_news_summaries) > 30:
                recent_news_summaries.pop(0)

            today_top_headlines.append(headline)
            sig = config_db.get("channel_signature", f"🆔 {MY_CHANNEL}")
            return formatted_body + f"\n\n{sig}"

    return clean_fallback(clean_raw)

# ---------------------------------------------------------
# ۵. تابع ارسال فوق‌العاده مقاوم به تلگرام
# ---------------------------------------------------------
def send_telegram_post(text, source_url=None):
    keyboard = []
    links_row = []
    if source_url:
        links_row.append({"text": "🔗 منبع خبر", "url": source_url})
    links_row.append({"text": "📢 عضویت در کانال", "url": f"https://t.me/{MY_CHANNEL.replace('@', '')}"})
    keyboard.append(links_row)
    
    send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    payload_html = {
        "chat_id": MY_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard}
    }
    
    try:
        res = http_session.post(send_url, json=payload_html, timeout=8)
        print(f"📢 [POST RESULT] Status: {res.status_code}, Body: {res.text}", flush=True)
        if res.status_code == 200:
            return True, "ارسال موفق با HTML"
        
        # در صورت بروز خطای تگ‌های HTML، متن ساده ارسال می‌شود
        plain_text = re.sub(r'<[^>]+>', '', text)
        payload_plain = {
            "chat_id": MY_CHANNEL,
            "text": plain_text,
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard}
        }
        res_plain = http_session.post(send_url, json=payload_plain, timeout=8)
        if res_plain.status_code == 200:
            return True, "ارسال موفق (متن ساده)"
            
        return False, f"ارور تلگرام ({res.status_code}): {res.text}"
    except Exception as e:
        return False, f"خطای شبکه: {str(e)}"

# ---------------------------------------------------------
# ۶. کیبوردهای پنل مدیریت
# ---------------------------------------------------------
def get_main_panel_keyboard():
    status_icon = "🟢" if config_db.get("bot_active", True) else "🔴"
    status_text = "خاموش کردن ربات" if config_db.get("bot_active", True) else "روشن کردن ربات"
    interval = config_db.get("check_interval", 10)
    return {
        "inline_keyboard": [
            [
                {"text": f"{status_icon} {status_text}", "callback_data": "toggle_bot_status"},
                {"text": "📋 لیست کانال‌ها", "callback_data": "panel_list"}
            ],
            [
                {"text": "⚡️ ارسال پست دستی", "callback_data": "panel_force_post_prompt"},
                {"text": "🔄 جایگزینی کلمات", "callback_data": "panel_replacements_menu"}
            ],
            [
                {"text": "➕ افزودن کانال", "callback_data": "panel_add_prompt"},
                {"text": "🗑 حذف تکی کانال", "callback_data": "panel_delete_menu"}
            ],
            [
                {"text": "☑️ حذف دسته‌ای کانال‌ها", "callback_data": "panel_bulk_delete_menu"},
                {"text": "✏️ ویرایش امضای کانال", "callback_data": "panel_sig_prompt"}
            ],
            [
                {"text": "🚫 مدیریت کلمات کلیدی", "callback_data": "panel_blacklist_menu"},
                {"text": f"⏱ فاصله بررسی: {interval} ثانیه", "callback_data": "panel_interval_menu"}
            ],
            [
                {"text": "📝 خلاصه‌سازی خبر", "callback_data": "panel_summarize_prompt"},
                {"text": "📊 آمار و وضعیت کلی", "callback_data": "panel_status"}
            ]
        ]
    }

def get_replacements_keyboard():
    keyboard = []
    keyboard.append([{"text": "➕ افزودن قانون جایگزینی جدید", "callback_data": "add_replacement_prompt"}])
    reps = config_db.get("replacements", {})
    for idx, (old_w, new_w) in enumerate(reps.items()):
        disp_new = f"«{new_w}»" if new_w else "🗑 (حذف کاملاً)"
        keyboard.append([{"text": f"❌ حذف: «{old_w}» ➔ {disp_new}", "callback_data": f"del_rep_idx_{idx}"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_bulk_delete_keyboard(chat_id):
    keyboard = []
    selected = selected_channels_for_bulk_delete.get(chat_id, set())
    for ch in list(target_channels):
        icon = "☑️" if ch in selected else "⬜️"
        keyboard.append([{"text": f"{icon} {ch}", "callback_data": f"toggle_bulk_{ch}"}])
    count = len(selected)
    confirm_text = f"🗑 تأیید و حذف موارد انتخاب‌شده ({count})" if count > 0 else "🗑 هیچ کانالی انتخاب نشده"
    keyboard.append([{"text": confirm_text, "callback_data": "confirm_bulk_delete"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_delete_channels_keyboard():
    keyboard = []
    for ch in list(target_channels):
        keyboard.append([{"text": f"❌ حذف {ch}", "callback_data": f"del_ch_{ch}"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_blacklist_keyboard():
    keyboard = []
    keyboard.append([{"text": "➕ افزودن کلمه جدید", "callback_data": "add_bl_word_prompt"}])
    bl = config_db.get("blacklist", [])
    for idx, word in enumerate(bl):
        keyboard.append([{"text": f"❌ حذف «{word}»", "callback_data": f"del_bl_idx_{idx}"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_interval_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "⚡️ ۲ ثانیه", "callback_data": "set_interval_2"},
                {"text": "⏱ ۵ ثانیه", "callback_data": "set_interval_5"}
            ],
            [
                {"text": "⏳ ۱۰ ثانیه (توصیه‌شده)", "callback_data": "set_interval_10"},
                {"text": "🕰 ۳۰ ثانیه", "callback_data": "set_interval_30"}
            ],
            [
                {"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}
            ]
        ]
    }

def get_cancel_keyboard():
    return {"inline_keyboard": [[{"text": "🚫 لغو عملیات", "callback_data": "cancel_action"}]]}

# ---------------------------------------------------------
# ۷. شنونده سریع دستورات پنل مدیریت تلگرام
# ---------------------------------------------------------
def fast_panel_listener():
    last_update_id = 0
    print("🚀 [LOG] Listener started! Waiting for messages...", flush=True)
    
    try:
        http_session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
        print("🚀 [LOG] Webhook deleted.", flush=True)
    except Exception as e:
        print(f"⚠️ [LOG] Delete Webhook error: {e}", flush=True)

    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=3"
            response = http_session.get(url, timeout=6)
            res = response.json()
            
            if not res.get("ok"):
                print(f"❌ [TELEGRAM ERROR] getUpdates failed: {res}", flush=True)
                time.sleep(3)
                continue

            updates = res.get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                print(f"📥 [LOG] New Update: {update}", flush=True)
                
                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb.get("id")
                    action = cb.get("data")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")

                    http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=3)

                    if action in ["panel_main", "cancel_action"]:
                        user_states[chat_id] = None
                        selected_channels_for_bulk_delete[chat_id] = set()
                        st_text = "🟢 **فعال**" if config_db.get("bot_active", True) else "🔴 **غیرفعال**"
                        reply = "❌ **عملیات لغو شد.**" if action == "cancel_action" else f"🛠 **پنل مدیریت ربات خبری**\n\nوضعیت: {st_text}"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "toggle_bot_status":
                        config_db["bot_active"] = not config_db.get("bot_active", True)
                        save_config(config_db)
                        st_text = "🟢 **روشن شد**" if config_db["bot_active"] else "🔴 **متوقف شد**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": f"وضعیت ربات: {st_text}", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_list":
                        reply = f"📋 **لیست کانال‌های مبدا ({len(target_channels)}/20):**\n\n"
                        reply += "\n".join([f"{idx}. `{c}`" for idx, c in enumerate(target_channels, 1)])
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_replacements_menu":
                        reps = config_db.get("replacements", {})
                        rep_text = "\n".join([f"• `{k}` ➔ `{v if v else '[حذف کامل]'}`" for k, v in reps.items()]) if reps else "هیچ قانون جایگزینی وجود ندارد."
                        reply = f"🔄 **مدیریت جایگزینی کلمات/جملات:**\n\n{rep_text}"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_replacements_keyboard()
                        }, timeout=3)

                    elif action == "add_replacement_prompt":
                        user_states[chat_id] = "WAITING_FOR_REPLACEMENT_RULE"
                        reply = (
                            "✏️ **لطفاً کلمه یا جمله جایگزین را به این صورت ارسال کنید:**\n\n"
                            "`عبارت قدیمی -> عبارت جدید`\n\n"
                            "*مثال برای جایگزینی:* `خبرگزاری میزان -> خبرگزاری ما`\n"
                            "*مثال برای حذف کامل:* `میزان در شبکه‌های اجتماعی -> `"
                        )
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                        }, timeout=3)

                    elif action.startswith("del_rep_idx_"):
                        idx = int(action.replace("del_rep_idx_", ""))
                        reps = config_db.get("replacements", {})
                        keys = list(reps.keys())
                        if 0 <= idx < len(keys):
                            key_to_del = keys[idx]
                            del config_db["replacements"][key_to_del]
                            save_config(config_db)
                            reply = f"❌ قانون مربوط به `{key_to_del}` حذف شد."
                        else:
                            reply = "⚠️ قبلاً حذف شده است."
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_replacements_keyboard()
                        }, timeout=3)

                    elif action == "panel_bulk_delete_menu":
                        selected_channels_for_bulk_delete[chat_id] = set()
                        reply = "☑️ **حذف دسته‌ای کانال‌ها**\n\nکانال‌های موردنظر را انتخاب و سپس حذف کنید:"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_bulk_delete_keyboard(chat_id)
                        }, timeout=3)

                    elif action.startswith("toggle_bulk_"):
                        ch = action.replace("toggle_bulk_", "")
                        if chat_id not in selected_channels_for_bulk_delete:
                            selected_channels_for_bulk_delete[chat_id] = set()
                        if ch in selected_channels_for_bulk_delete[chat_id]:
                            selected_channels_for_bulk_delete[chat_id].remove(ch)
                        else:
                            selected_channels_for_bulk_delete[chat_id].add(ch)
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup", json={
                            "chat_id": chat_id, "message_id": msg.get("message_id"), "reply_markup": get_bulk_delete_keyboard(chat_id)
                        }, timeout=3)

                    elif action == "confirm_bulk_delete":
                        selected = selected_channels_for_bulk_delete.get(chat_id, set())
                        if not selected:
                            reply = "⚠️ هیچ کانالی انتخاب نشده است."
                        else:
                            removed_list = list(selected)
                            for ch in removed_list:
                                if ch in target_channels:
                                    target_channels.remove(ch)
                            save_channels(target_channels)
                            selected_channels_for_bulk_delete[chat_id] = set()
                            reply = f"❌ **تعداد {len(removed_list)} کانال حذف شدند.**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_add_prompt":
                        if len(target_channels) >= 20:
                            reply = "⚠️ **ظرفیت تکمیل است!** ابتدا یک کانال را حذف کنید."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
                        else:
                            user_states[chat_id] = "WAITING_FOR_CHANNEL_NAME"
                            reply = "📥 **لطفاً آیدی کانال جدید را بفرستید:**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                    elif action == "panel_delete_menu":
                        reply = "🗑 **روی کانال موردنظر کلیک کنید:**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_delete_channels_keyboard()
                        }, timeout=3)

                    elif action.startswith("del_ch_"):
                        ch_to_del = action.replace("del_ch_", "")
                        if ch_to_del in target_channels:
                            target_channels.remove(ch_to_del)
                            save_channels(target_channels)
                            reply = f"❌ کانال `{ch_to_del}` حذف شد."
                        else:
                            reply = "⚠️ این کانال قبلاً حذف شده است."
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_interval_menu":
                        reply = f"⏱ **فاصله بررسی فعلی:** {config_db.get('check_interval', 10)} ثانیه"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_interval_keyboard()
                        }, timeout=3)

                    elif action.startswith("set_interval_"):
                        sec = int(action.replace("set_interval_", ""))
                        config_db["check_interval"] = sec
                        save_config(config_db)
                        reply = f"✅ **فاصله زمانی روی {sec} ثانیه تنظیم شد.**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_sig_prompt":
                        user_states[chat_id] = "WAITING_FOR_SIGNATURE"
                        curr_sig = config_db.get("channel_signature", f"🆔 {MY_CHANNEL}")
                        reply = f"✏️ **امضای فعلی:**\n`{curr_sig}`\n\nمتن امضای جدید را بفرستید:"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                        }, timeout=3)

                    elif action == "panel_blacklist_menu":
                        bl = config_db.get("blacklist", [])
                        bl_text = "\n".join([f"• `{w}`" for w in bl]) if bl else "هیچ کلمه‌ای فیلتر نشده است."
                        reply = f"🚫 **کلمات فیلتر شده:**\n\n{bl_text}"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                        }, timeout=3)

                    elif action == "add_bl_word_prompt":
                        user_states[chat_id] = "WAITING_FOR_BLACKLIST_WORD"
                        reply = "✏️ **کلمه جدید را فرستید:**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                        }, timeout=3)

                    elif action.startswith("del_bl_idx_"):
                        idx = int(action.replace("del_bl_idx_", ""))
                        bl = config_db.get("blacklist", [])
                        if 0 <= idx < len(bl):
                            removed_word = bl.pop(idx)
                            save_config(config_db)
                            reply = f"❌ کلمه `{removed_word}` حذف شد."
                        else:
                            reply = "⚠️ قبلاً حذف شده است."
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_force_post_prompt":
                        user_states[chat_id] = "WAITING_FOR_FORCE_POST"
                        reply = "⚡️ **خبر دستی خود را بفرستید:**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                        }, timeout=3)

                    elif action == "panel_summarize_prompt":
                        user_states[chat_id] = "WAITING_FOR_SUMMARIZE_TEXT"
                        reply = "📝 **لطفاً متن خبری که می‌خواهید خلاصه شود را ارسال کنید:**"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                        }, timeout=3)

                    elif action == "panel_status":
                        st_icon = "🟢 روشن" if config_db.get("bot_active", True) else "🔴 خاموش"
                        reply = (
                            f"📊 **گزارش و آمار**\n\n"
                            f"⚙️ **وضعیت:** {st_icon}\n"
                            f"⏱ **فاصله بررسی:** {config_db.get('check_interval', 10)} ثانیه\n"
                            f"📢 **کانال‌ها:** {len(target_channels)} از ۲۰\n"
                            f"✅ **اخبار ارسال‌شده:** {len(seen_post_ids)}\n"
                            f"🔄 **قوانین جایگزینی:** {len(config_db.get('replacements', {}))}\n"
                            f"🚫 **کلمات فیلتر:** {len(config_db.get('blacklist', []))}"
                        )
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                elif "message" in update:
                    message = update["message"]
                    chat_id = message.get("chat", {}).get("id")
                    text = message.get("text", "").strip()

                    if not text:
                        continue

                    if text in ["لغو", "/cancel", "انصراف"]:
                        user_states[chat_id] = None
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": "❌ **عملیات لغو شد.**", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)
                        continue

                    state = user_states.get(chat_id)

                    if state == "WAITING_FOR_REPLACEMENT_RULE":
                        user_states[chat_id] = None
                        if "->" in text:
                            parts = text.split("->")
                            old_word = parts[0].strip()
                            new_word = parts[1].strip() if len(parts) > 1 else ""
                            if old_word:
                                if "replacements" not in config_db:
                                    config_db["replacements"] = {}
                                config_db["replacements"][old_word] = new_word
                                save_config(config_db)
                                disp_new = f"`{new_word}`" if new_word else "_[حذف کاملاً]_"
                                reply = f"✅ قانون جدید ثبت شد:\n`{old_word}` ➔ {disp_new}"
                            else:
                                reply = "❌ عبارت قدیمی نباید خالی باشد."
                        else:
                            reply = "❌ فرمت نادرست است! حتماً از نماد `->` استفاده کنید."
                        
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_replacements_keyboard()
                        }, timeout=3)

                    elif state == "WAITING_FOR_SUMMARIZE_TEXT":
                        user_states[chat_id] = None
                        summary = summarize_text_with_ai(text)
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": summary, "parse_mode": "HTML", "reply_markup": get_main_panel_keyboard()
                        }, timeout=5)

                    elif state == "WAITING_FOR_CHANNEL_NAME":
                        user_states[chat_id] = None
                        new_ch = text.replace("@", "").replace("https://t.me/", "").strip()
                        if len(target_channels) >= 20:
                            reply = "⚠️ ظرفیت تکمیل است."
                        elif new_ch in target_channels:
                            reply = f"⚠️ کانال `{new_ch}` موجود است."
                        elif new_ch:
                            target_channels.append(new_ch)
                            save_channels(target_channels)
                            reply = f"✅ کانال `{new_ch}` اضافه شد."
                        else:
                            reply = "❌ آیدی نامعتبر."
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif state == "WAITING_FOR_SIGNATURE":
                        user_states[chat_id] = None
                        config_db["channel_signature"] = text
                        save_config(config_db)
                        reply = f"✅ **امضا ثبت شد:**\n{text}"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif state == "WAITING_FOR_BLACKLIST_WORD":
                        user_states[chat_id] = None
                        word = text.strip()
                        if word and word not in config_db.get("blacklist", []):
                            config_db["blacklist"].append(word)
                            save_config(config_db)
                            reply = f"✅ کلمه `{word}` اضافه شد."
                        else:
                            reply = "⚠️ نامعتبر یا تکراری."
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                        }, timeout=3)

                    elif state == "WAITING_FOR_FORCE_POST":
                        user_states[chat_id] = None
                        sig = config_db.get("channel_signature", f"🆔 {MY_CHANNEL}")
                        clean_t = sanitize_all_links(text)
                        post_text = f"📌 <b>{clean_t[:40]}</b>\n\n{clean_t}\n\n#خبر_فوری\n\n{sig}"
                        
                        success, details = send_telegram_post(post_text)
                        if success:
                            reply = f"🚀 **پست فرستاده شد.**\n({details})"
                        else:
                            reply = f"❌ **خطا در ارسال به کانال:**\n`{details}`"
                        
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif text.lower() in ["/start", "start", "راهنما", "پنل"]:
                        user_states[chat_id] = None
                        st_text = "🟢 **فعال**" if config_db.get("bot_active", True) else "🔴 **غیرفعال**"
                        reply = f"🛠 **پنل مدیریت ربات خبری**\n\nوضعیت: {st_text}"
                        res_send = http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)
                        print(f"📌 [LOG] /start status: {res_send.status_code}", flush=True)

        except Exception as e:
            print(f"❌ [EXCEPTION] {e}", flush=True)
            time.sleep(2)

# ---------------------------------------------------------
# ۸. اسکراپ اخبار از کانال‌های مبدا
# ---------------------------------------------------------
def process_single_channel(channel):
    if not config_db.get("bot_active", True) or channel not in target_channels:
        return

    url = f"https://t.me/s/{channel}"
    try:
        res = http_session.get(url, timeout=4)
        soup = BeautifulSoup(res.text, "html.parser")
        posts = soup.find_all("div", class_="tgme_widget_message")
        
        if posts:
            last_post = posts[-1]
            post_id = last_post.get("data-post")
            
            if post_id and post_id not in seen_post_ids:
                text_div = last_post.find("div", class_="tgme_widget_message_text")
                if text_div:
                    raw_text = text_div.get_text(separator="\n")
                    final_text = rewrite_with_ai(raw_text)
                    source_post_url = f"https://t.me/{post_id}"

                    if final_text:
                        success, _ = send_telegram_post(final_text, source_post_url)
                        if success:
                            seen_post_ids.add(post_id)
                            save_seen_post(post_id)
                    else:
                        seen_post_ids.add(post_id)
                        save_seen_post(post_id)
    except Exception:
        pass

def fetch_news_loop():
    with ThreadPoolExecutor(max_workers=5) as executor:
        while True:
            if config_db.get("bot_active", True):
                interval = config_db.get("check_interval", 10)
                active_channels = list(target_channels)
                executor.map(process_single_channel, active_channels)
                time.sleep(interval)
            else:
                time.sleep(3)

# ---------------------------------------------------------
# ۹. اجرای همزمان بخش‌ها
# ---------------------------------------------------------
web_thread = threading.Thread(target=run_web_server, daemon=True)
web_thread.start()

news_thread = threading.Thread(target=fetch_news_loop, daemon=True)
news_thread.start()

fast_panel_listener()
