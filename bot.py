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
# وب‌سرور سبک برای زنده نگه‌داشتن Render
# ---------------------------------------------------------
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Bot is online and healthy!")

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthServer)
    server.serve_forever()

# ---------------------------------------------------------
# تنظیمات اصلی
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
user_states = {}
db_lock = threading.Lock()

def load_config():
    default_config = {
        "bot_active": True,
        "channel_signature": f"🆔 {MY_CHANNEL}",
        "blacklist": ["تبلیغات", "تخفیف ویژه"],
        "replacements": {"میزان در شبکه‌های اجتماعی": ""},
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
# پاک‌سازی متون، لینک‌ها، ایموجی‌ها و کلمات سفارشی
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
# فراخوانی REST API هماهنگ با Gemini
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

def rewrite_with_ai(raw_text):
    global recent_news_summaries

    clean_raw = sanitize_all_links(raw_text)
    if not clean_raw or len(clean_raw) < 15 or check_blacklist(clean_raw):
        return None

    recent_context = "\n---\n".join(recent_news_summaries[-15:]) if recent_news_summaries else "هیچ خبری ثبت نشده است."

    prompt = (
        "تو یک ویرایشگر خبر هستی. متن زیر را بازنویسی کن.\n"
        "اگر تکراری است 'DUPLICATE' و اگر تبلیغاتی است 'SKIP' بنویس.\n"
        "اگر جدید است، تمام لینک‌ها و ایموجی‌ها را حذف کن، خط اول تیتر بگذار و انتها ۲ هشتگ بنویس.\n\n"
        f"اخبار اخیر:\n{recent_context}\n\nمتن جدید:\n{clean_raw}"
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

            sig = config_db.get("channel_signature", f"🆔 {MY_CHANNEL}")
            return formatted_body + f"\n\n{sig}"

    return clean_fallback(clean_raw)

# ---------------------------------------------------------
# تابع ارسال مطمئن به تلگرام
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
            return True, "ارسال موفق"
        
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
# کیبورد پنل
# ---------------------------------------------------------
def get_main_panel_keyboard():
    status_icon = "🟢" if config_db.get("bot_active", True) else "🔴"
    status_text = "خاموش کردن" if config_db.get("bot_active", True) else "روشن کردن"
    return {
        "inline_keyboard": [
            [
                {"text": f"{status_icon} {status_text}", "callback_data": "toggle_bot_status"},
                {"text": "⚡️ ارسال پست دستی", "callback_data": "panel_force_post_prompt"}
            ],
            [
                {"text": "📋 لیست کانال‌ها", "callback_data": "panel_list"}
            ]
        ]
    }

def get_cancel_keyboard():
    return {"inline_keyboard": [[{"text": "🚫 لغو", "callback_data": "cancel_action"}]]}

# ---------------------------------------------------------
# دریافت دستورات تلگرام با ثبت کامل لاگ
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

                    if action == "panel_force_post_prompt":
                        user_states[chat_id] = "WAITING_FOR_FORCE_POST"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": "⚡️ **متن خبر دستی را بفرستید:**", "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                        }, timeout=3)

                    elif action == "toggle_bot_status":
                        config_db["bot_active"] = not config_db.get("bot_active", True)
                        save_config(config_db)
                        st_text = "🟢 روشن" if config_db["bot_active"] else "🔴 خاموش"
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": f"وضعیت ربات: {st_text}", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "panel_list":
                        reply = "📋 **کانال‌ها:**\n" + "\n".join([f"• `{c}`" for c in target_channels])
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                    elif action == "cancel_action":
                        user_states[chat_id] = None
                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, "text": "❌ لغو شد.", "reply_markup": get_main_panel_keyboard()
                        }, timeout=3)

                elif "message" in update:
                    message = update["message"]
                    chat_id = message.get("chat", {}).get("id")
                    text = message.get("text", "").strip()

                    if not text:
                        continue

                    state = user_states.get(chat_id)

                    if state == "WAITING_FOR_FORCE_POST":
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

                    elif text.lower() in ["/start", "start", "پنل"]:
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
# اسکراپ اخبار
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
# اجرای برنامه
# ---------------------------------------------------------
web_thread = threading.Thread(target=run_web_server, daemon=True)
web_thread.start()

news_thread = threading.Thread(target=fetch_news_loop, daemon=True)
news_thread.start()

fast_panel_listener()
