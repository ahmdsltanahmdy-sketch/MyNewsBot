import time
import re
import os
import requests
import json
import threading
import difflib
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
import google.generativeai as genai
from flask import Flask

# ---------------------------------------------------------
# وب‌سرور زنده نگه‌داشتن ربات در Render / Replit
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running at peak performance!", 200

@app.route('/health')
def health():
    return "Healthy", 200

# ---------------------------------------------------------
# تنظیمات اولیه و خواندن کلیدها
# ---------------------------------------------------------
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "8903869878:AAGWo00OXfJYszdgJ-L4odB2d5Ug4phJK0I").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "AQ.Ab8RN6LoUe_micSnpWDgAzhuJU4UPWaBISmYXgq7KH2jZ7ZW1A").strip()

# ادمین اولیه (سازنده ربات) - امکان مدیریت سایر ادمین‌ها در پنل
INITIAL_ADMIN_ID = 123456789  # <--- آی‌دی عددی تلگرام خودتان را اینجا بگذارید

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Gemini Init Warning: {e}")

http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

DB_FILE = "seen_posts.txt"
CHANNELS_FILE = "channels.txt"
CONFIG_FILE = "config.json"

DEFAULT_CHANNELS = [
    "Tasnimnews", "FarsNewsInt", "IRNA_1313", "ISNAFA", 
    "KhabarOnline_ir", "Mehrnews", "YjcNewsChannel", 
    "mashreghnews_channel", "Jahan_Fouri", "jamarannews"
]

user_states = {}
selected_channels_for_bulk_delete = {}
recent_posts_history = []
db_lock = threading.Lock()

def load_config():
    default_config = {
        "bot_active": True,
        "target_channel_id": "-1002038404831",
        "target_channel_username": "Rallyir",
        "channel_signature": "🆔 @Rallyir",
        "blacklist": ["تبلیغات", "تخفیف ویژه"],
        "golden_keywords": [],
        "golden_keywords_active": False,
        "fuzzy_check_active": True,
        "fuzzy_threshold": 70,  # درصد حساسیت تشابه
        "quiet_hours_active": False,
        "quiet_start_hour": 0,
        "quiet_end_hour": 6,
        "check_interval": 10,
        "admin_ids": [INITIAL_ADMIN_ID]
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
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Config Save Error: {e}")

config_db = load_config()

def is_admin(user_id):
    """بررسی دسترسی ادمین"""
    admins = config_db.get("admin_ids", [])
    return (user_id in admins) or (user_id == INITIAL_ADMIN_ID)

def load_seen_posts():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen_post(post_id):
    with db_lock:
        try:
            with open(DB_FILE, "a") as f:
                f.write(f"{post_id}\n")
        except Exception:
            pass

def clear_seen_posts():
    with db_lock:
        try:
            open(DB_FILE, "w").close()
            seen_post_ids.clear()
            return True
        except Exception:
            return False

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            channels = [line.strip() for line in f if line.strip()]
            if channels:
                return channels[:20]
    return DEFAULT_CHANNELS.copy()

def save_channels(channels):
    try:
        with open(CHANNELS_FILE, "w") as f:
            for ch in channels[:20]:
                f.write(f"{ch}\n")
    except Exception:
        pass

seen_post_ids = load_seen_posts()
target_channels = load_channels()
last_update_id = 0

# ---------------------------------------------------------
# پردازش متون، حذف لینک‌ها، ایموجی‌ها و فضاهای اضافه
# ---------------------------------------------------------
def remove_emojis(text):
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U00010000-\U0010FFFF"
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002700-\U000027BF"
        "\U00002600-\U000026FF"
        "]+", flags=re.UNICODE
    )
    return emoji_pattern.sub("", text)

def clean_extra_spaces(text):
    """پاکسازی کامل فضاهای خالی و خطوط متوالی اضافه"""
    if not text:
        return ""
    # تبدیل چند فاصله افقی متوالی به یک فاصله
    text = re.sub(r'[ \t]+', ' ', text)
    # تبدیل چند خط خالی متوالی به حداکثر ۲ انتر (یک خط خالی)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()

def sanitize_all_links(text):
    """حذف کامل تمام آدرس‌های اینترنتی، لینک‌ها و آیدی‌ها"""
    if not text:
        return ""
    text = re.sub(r'<a\s+[^>]*href=["\'][^"\']*["\'][^>]*>(.*?)</a>', r'\1', text, flags=re.IGNORECASE)
    text = re.sub(r'<a\b[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</a>', '', text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+|www\.\S+|t\.me/\S+", "", text)
    text = re.sub(r"@[A-Za-z0-9_]+", "", text)
    text = remove_emojis(text)
    return clean_extra_spaces(text)

def check_blacklist(text):
    for word in config_db.get("blacklist", []):
        if word and word.lower() in text.lower():
            return True
    return False

def check_golden_keywords(text):
    """بررسی فیلتر کلمات طلایی"""
    if not config_db.get("golden_keywords_active", False):
        return True
    keywords = config_db.get("golden_keywords", [])
    if not keywords:
        return True
    return any(word.lower() in text.lower() for word in keywords if word)

def is_fuzzy_duplicate(new_text):
    """تشخیص شباهت متنی با حساسیت متغیر تنظیم‌شده"""
    if not config_db.get("fuzzy_check_active", True):
        return False
    threshold = config_db.get("fuzzy_threshold", 70) / 100.0
    clean_new = sanitize_all_links(new_text)
    for past_text in recent_posts_history[-20:]:
        similarity = difflib.SequenceMatcher(None, clean_new, past_text).ratio()
        if similarity >= threshold:
            return True
    return False

def is_in_quiet_hours():
    """بررسی وضعیت خاموشی خودکار (ساعت رسمی ایران)"""
    if not config_db.get("quiet_hours_active", False):
        return False
    
    ir_time = datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)
    curr_hour = ir_time.hour
    
    start = config_db.get("quiet_start_hour", 0)
    end = config_db.get("quiet_end_hour", 6)
    
    if start <= end:
        return start <= curr_hour < end
    else:
        return curr_hour >= start or curr_hour < end

def clean_fallback(text):
    clean_t = sanitize_all_links(text)
    lines = [l.strip() for l in clean_t.splitlines() if l.strip()]
    if not lines:
        return ""
    lines[0] = f"📌 <b>{lines[0]}</b>"
    sig = config_db.get("channel_signature", f"🆔 @{config_db.get('target_channel_username', 'Rallyir')}")
    body = "\n\n".join(lines)
    return clean_extra_spaces(f"{body}\n\n#خبر\n\n{sig}")

def rewrite_with_ai(raw_text):
    if not raw_text or len(raw_text.strip()) < 15 or check_blacklist(raw_text):
        return None

    if not check_golden_keywords(raw_text):
        return None

    if is_fuzzy_duplicate(raw_text):
        return None

    prompt = (
        "تو یک ویرایشگر خبر حرفه‌ای هستی. متن زیر را بازنویسی کن.\n\n"
        "قوانین:\n"
        "۱. تمام لینک‌ها، هایپرلینک‌ها و آیدی‌ها (@) را حتماً حذف کن.\n"
        "۲. خط اول را یک تیتر دقیق بگذار.\n"
        "۳. از هیچ ایموجی در متن خبر استفاده نکن.\n"
        "۴. در انتها ۲ تا ۴ هشتگ خبری تخصصی بگذار.\n\n"
        f"متن خبر:\n{raw_text}"
    )

    for model_name in ['gemini-1.5-flash', 'gemini-pro']:
        try:
            ai_model = genai.GenerativeModel(model_name)
            response = ai_model.generate_content(prompt, request_options={"timeout": 4})
            result = response.text.strip()

            result = sanitize_all_links(result)
            lines = [line.strip() for line in result.splitlines() if line.strip()]
            if not lines:
                return None

            headline = lines[0].replace("<b>", "").replace("</b>", "")
            lines[0] = f"📌 <b>{headline}</b>"
            formatted_body = "\n\n".join(lines)

            sig = config_db.get("channel_signature", f"🆔 @{config_db.get('target_channel_username', 'Rallyir')}")
            
            recent_posts_history.append(sanitize_all_links(raw_text))
            if len(recent_posts_history) > 30:
                recent_posts_history.pop(0)

            return clean_extra_spaces(f"{formatted_body}\n\n{sig}")
        except Exception:
            continue

    return clean_fallback(raw_text)

# ---------------------------------------------------------
# ارسال پیام به تلگرام
# ---------------------------------------------------------
def send_telegram_post(text, source_url=None):
    if not BOT_TOKEN:
        return False

    target_id = config_db.get("target_channel_id", "-1002038404831")
    target_uname = config_db.get("target_channel_username", "Rallyir")

    keyboard = []
    links_row = []
    if source_url:
        links_row.append({"text": "🔗 منبع خبر", "url": source_url})
    links_row.append({"text": "📢 عضویت در کانال", "url": f"https://t.me/{target_uname}"})
    keyboard.append(links_row)
    
    send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard}
    }
    
    try:
        res = http_session.post(send_url, json=payload, timeout=5)
        if res.status_code == 200:
            return True
            
        plain_text = text.replace("<b>", "").replace("</b>", "").replace("📌 ", "")
        payload["text"] = plain_text
        payload.pop("parse_mode", None)
        res_retry = http_session.post(send_url, json=payload, timeout=5)
        return res_retry.status_code == 200
    except Exception:
        return False

# ---------------------------------------------------------
# کیبوردهای کامل پنل مدیریت
# ---------------------------------------------------------
def get_main_panel_keyboard():
    status_icon = "🟢" if config_db.get("bot_active", True) else "🔴"
    status_text = "خاموش کردن ربات" if config_db.get("bot_active", True) else "روشن کردن ربات"
    interval = config_db.get("check_interval", 10)
    
    golden_st = "🟢" if config_db.get("golden_keywords_active", False) else "🔴"
    fuzzy_st = "🟢" if config_db.get("fuzzy_check_active", True) else "🔴"
    quiet_st = "🟢" if config_db.get("quiet_hours_active", False) else "🔴"

    return {
        "inline_keyboard": [
            [
                {"text": f"{status_icon} {status_text}", "callback_data": "toggle_bot_status"},
                {"text": "📋 لیست کانال‌ها", "callback_data": "panel_list"}
            ],
            [
                {"text": "➕ افزودن کانال", "callback_data": "panel_add_prompt"},
                {"text": "🗑 حذف تکی کانال", "callback_data": "panel_delete_menu"}
            ],
            [
                {"text": "☑️ حذف دسته‌ای", "callback_data": "panel_bulk_delete_menu"},
                {"text": "✏️ ویرایش امضا", "callback_data": "panel_sig_prompt"}
            ],
            [
                {"text": f"⏱ فاصله بررسی: {interval}s", "callback_data": "panel_interval_menu"},
                {"text": "🚫 کلمات سیاه (فیلتر)", "callback_data": "panel_blacklist_menu"}
            ],
            [
                {"text": f"{golden_st} کلیدواژه‌های طلایی", "callback_data": "panel_golden_menu"},
                {"text": f"{fuzzy_st} تنظیمات عدم تکرار", "callback_data": "panel_fuzzy_menu"}
            ],
            [
                {"text": f"{quiet_st} ساعت خاموشی", "callback_data": "panel_quiet_menu"},
                {"text": "🎯 تغییر کانال مقصد", "callback_data": "panel_target_channel_prompt"}
            ],
            [
                {"text": "👑 مدیریت ادمین‌ها", "callback_data": "panel_admins_menu"},
                {"text": "💾 دیتابیس و ریست", "callback_data": "panel_backup_reset_menu"}
            ],
            [
                {"text": "⚡️ ارسال پست دستی", "callback_data": "panel_force_post_prompt"},
                {"text": "📊 آمار و وضعیت کامل", "callback_data": "panel_status"}
            ]
        ]
    }

def get_cancel_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🚫 انصراف و بازگشت به پنل", "callback_data": "cancel_action"}]
        ]
    }

def get_fuzzy_keyboard():
    fuzzy_st = "🔴 غیرفعال" if not config_db.get("fuzzy_check_active", True) else "🟢 فعال"
    thresh = config_db.get("fuzzy_threshold", 70)
    return {
        "inline_keyboard": [
            [{"text": f"وضعیت سیستم: {fuzzy_st}", "callback_data": "toggle_fuzzy_status"}],
            [
                {"text": "50%", "callback_data": "set_fuzzy_50"},
                {"text": "60%", "callback_data": "set_fuzzy_60"},
                {"text": f"• {thresh}% •", "callback_data": "noop"},
                {"text": "80%", "callback_data": "set_fuzzy_80"},
                {"text": "90%", "callback_data": "set_fuzzy_90"}
            ],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_quiet_keyboard():
    quiet_st = "🔴 غیرفعال" if not config_db.get("quiet_hours_active", False) else "🟢 فعال"
    sh = config_db.get("quiet_start_hour", 0)
    eh = config_db.get("quiet_end_hour", 6)
    return {
        "inline_keyboard": [
            [{"text": f"وضعیت خاموشی: {quiet_st}", "callback_data": "toggle_quiet_status"}],
            [
                {"text": f"⏰ شروع: {sh:02d}:00", "callback_data": "set_quiet_start_prompt"},
                {"text": f"⏰ پایان: {eh:02d}:00", "callback_data": "set_quiet_end_prompt"}
            ],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_hour_picker_keyboard(action_prefix):
    keyboard = []
    row = []
    for h in range(24):
        row.append({"text": f"{h:02d}:00", "callback_data": f"{action_prefix}_{h}"})
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([{"text": "🚫 انصراف", "callback_data": "panel_quiet_menu"}])
    return {"inline_keyboard": keyboard}

def get_backup_reset_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود بک‌آپ (تنظیمات)", "callback_data": "download_config_file"},
                {"text": "📥 دانلود دیتابیس (اخبار)", "callback_data": "download_db_file"}
            ],
            [{"text": "🧹 ریست حافظه اخبار دیده‌شده", "callback_data": "reset_db_confirm_prompt"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_admins_keyboard():
    keyboard = []
    keyboard.append([{"text": "➕ افزودن ادمین جدید", "callback_data": "add_admin_prompt"}])
    admins = config_db.get("admin_ids", [])
    for adm in admins:
        if adm != INITIAL_ADMIN_ID:
            keyboard.append([{"text": f"❌ حذف ادمین {adm}", "callback_data": f"del_admin_{adm}"}])
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

def get_golden_keyboard():
    keyboard = []
    st = "🔴 غیرفعال (کلیک برای فعال‌سازی)" if not config_db.get("golden_keywords_active", False) else "🟢 فعال (کلیک برای غیرفعال‌سازی)"
    keyboard.append([{"text": f"وضعیت فیلتر: {st}", "callback_data": "toggle_golden_status"}])
    keyboard.append([{"text": "➕ افزودن کلمه طلایی", "callback_data": "add_golden_word_prompt"}])
    
    gk = config_db.get("golden_keywords", [])
    for idx, word in enumerate(gk):
        keyboard.append([{"text": f"❌ حذف «{word}»", "callback_data": f"del_gk_idx_{idx}"}])
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

# ---------------------------------------------------------
# شنونده پنل مدیریت
# ---------------------------------------------------------
def fast_panel_listener():
    global last_update_id, target_channels, user_states, config_db, selected_channels_for_bulk_delete
    print("Bot fast listener is online...")
    
    try:
        http_session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=5)
    except Exception:
        pass

    while True:
        try:
            if not BOT_TOKEN:
                time.sleep(3)
                continue

            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 5}
            
            res = http_session.get(url, params=params, timeout=8)
            if res.status_code != 200:
                time.sleep(2)
                continue
                
            data = res.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    
                    # پردازش Callback Query
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb.get("id")
                        action = cb.get("data")
                        msg = cb.get("message", {})
                        chat_id = msg.get("chat", {}).get("id")
                        from_user_id = cb.get("from", {}).get("id")

                        # قفل امنیت ادمین
                        if not is_admin(from_user_id):
                            try:
                                http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⛔️ شما دسترسی ادمین ندارید.", "show_alert": True}, timeout=2)
                            except Exception:
                                pass
                            continue

                        try:
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=2)
                        except Exception:
                            pass

                        if action in ["panel_main", "cancel_action"]:
                            user_states[chat_id] = None
                            selected_channels_for_bulk_delete[chat_id] = set()
                            st_text = "🟢 **فعال**" if config_db.get("bot_active", True) else "🔴 **غیرفعال (متوقف)**"
                            reply = "❌ **عملیات لغو شد و به پنل اصلی بازگشتید.**" if action == "cancel_action" else f"🛠 **پنل مدیریت ربات خبری**\n\nوضعیت جمع‌آوری اخبار: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "toggle_bot_status":
                            config_db["bot_active"] = not config_db.get("bot_active", True)
                            save_config(config_db)
                            st_text = "🟢 **روشن و فعال شد**" if config_db["bot_active"] else "🔴 **متوقف شد**"
                            reply = f"دستور اجرا شد. وضعیت ربات: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        # --- منوی عدم تکرار متون ---
                        elif action == "panel_fuzzy_menu":
                            reply = (
                                f"🔍 **تنظیمات عدم تکرار هوشمند**\n\n"
                                f"حساسیت فعلی: **{config_db.get('fuzzy_threshold', 70)}%**\n"
                                f"هرچه درصد را بالاتر ببرید، حساسیت کمتر شده و متون فقط در صورت شباهت بسیار زیاد فیلتر می‌شوند."
                            )
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_fuzzy_keyboard()
                            }, timeout=3)

                        elif action == "toggle_fuzzy_status":
                            config_db["fuzzy_check_active"] = not config_db.get("fuzzy_check_active", True)
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت بررسی شباهت تغییر کرد.", "reply_markup": get_fuzzy_keyboard()
                            }, timeout=3)

                        elif action.startswith("set_fuzzy_"):
                            val = int(action.replace("set_fuzzy_", ""))
                            config_db["fuzzy_threshold"] = val
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ حساسیت روی {val}% تنظیم شد.", "reply_markup": get_fuzzy_keyboard()
                            }, timeout=3)

                        # --- منوی ساعت خاموشی ---
                        elif action == "panel_quiet_menu":
                            sh = config_db.get("quiet_start_hour", 0)
                            eh = config_db.get("quiet_end_hour", 6)
                            reply = f"🌙 **تنظیمات خاموشی شبانه**\n\nبازه فعلی: از ساعت **{sh:02d}:00** تا **{eh:02d}:00**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "toggle_quiet_status":
                            config_db["quiet_hours_active"] = not config_db.get("quiet_hours_active", False)
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت خاموشی شبانه تغییر کرد.", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "set_quiet_start_prompt":
                            reply = "⏰ **ساعت شروع خاموشی را انتخاب کنید:**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_hour_picker_keyboard("qstart")
                            }, timeout=3)

                        elif action.startswith("qstart_"):
                            h = int(action.replace("qstart_", ""))
                            config_db["quiet_start_hour"] = h
                            save_config(config_db)
                            reply = f"✅ ساعت شروع روی **{h:02d}:00** تنظیم شد."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "set_quiet_end_prompt":
                            reply = "⏰ **ساعت پایان خاموشی را انتخاب کنید:**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_hour_picker_keyboard("qend")
                            }, timeout=3)

                        elif action.startswith("qend_"):
                            h = int(action.replace("qend_", ""))
                            config_db["quiet_end_hour"] = h
                            save_config(config_db)
                            reply = f"✅ ساعت پایان روی **{h:02d}:00** تنظیم شد."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        # --- مدیریت کانال مقصد ---
                        elif action == "panel_target_channel_prompt":
                            user_states[chat_id] = "WAITING_FOR_TARGET_CHANNEL"
                            curr_id = config_db.get("target_channel_id", "-1002038404831")
                            curr_un = config_db.get("target_channel_username", "Rallyir")
                            reply = (
                                f"🎯 **تغییر کانال مقصد**\n\n"
                                f"کانال فعلی: `{curr_id}` (@{curr_un})\n\n"
                                f"لطفاً اطلاعات کانال جدید را به فرمت زیر ارسال کنید:\n"
                                f"`آیدی_عددی یوزرنیم`\n\n"
                                f"مثال:\n`-100123456789 mychannel`"
                            )
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        # --- مدیریت دیتابیس و بک‌آپ ---
                        elif action == "panel_backup_reset_menu":
                            reply = "💾 **مدیریت دیتابیس و پشتیبان‌گیری**\n\nاز گزینه‌های زیر استفاده کنید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_backup_reset_keyboard()
                            }, timeout=3)

                        elif action == "download_config_file":
                            if os.path.exists(CONFIG_FILE):
                                with open(CONFIG_FILE, "rb") as f:
                                    http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument", data={"chat_id": chat_id}, files={"document": f}, timeout=10)
                            else:
                                http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "⚠️ فایل تنظیمات یافت نشد."}, timeout=3)

                        elif action == "download_db_file":
                            if os.path.exists(DB_FILE):
                                with open(DB_FILE, "rb") as f:
                                    http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument", data={"chat_id": chat_id}, files={"document": f}, timeout=10)
                            else:
                                http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "⚠️ فایل دیتابیس یافت نشد."}, timeout=3)

                        elif action == "reset_db_confirm_prompt":
                            if clear_seen_posts():
                                reply = "🧹 **حافظه اخبار دیده‌شده با موفقیت پاک‌سازی شد.**"
                            else:
                                reply = "❌ **خطا در پاک‌سازی حافظه.**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        # --- مدیریت ادمین‌ها ---
                        elif action == "panel_admins_menu":
                            admins = config_db.get("admin_ids", [])
                            adm_list = "\n".join([f"• `{a}`" for a in admins])
                            reply = f"👑 **لیست ادمین‌های ربات:**\n\n{adm_list}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        elif action == "add_admin_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_ADMIN"
                            reply = "👑 **شناسه عددی (Numeric User ID) ادمین جدید را ارسال کنید:**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_admin_"):
                            adm_id = int(action.replace("del_admin_", ""))
                            admins = config_db.get("admin_ids", [])
                            if adm_id in admins and adm_id != INITIAL_ADMIN_ID:
                                admins.remove(adm_id)
                                save_config(config_db)
                                reply = f"❌ ادمین `{adm_id}` حذف شد."
                            else:
                                reply = "⚠️ امکان حذف این ادمین وجود ندارد."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        # سایر گزینه‌های قبلی پنل
                        elif action == "panel_golden_menu":
                            gk = config_db.get("golden_keywords", [])
                            gk_text = "\n".join([f"• `{w}`" for w in gk]) if gk else "هیچ کلمه طلایی تعریف نشده است."
                            st = "🟢 فعال" if config_db.get("golden_keywords_active", False) else "🔴 غیرفعال"
                            reply = f"🎯 **مدیریت کلیدواژه‌های طلایی**\n\nوضعیت فیلتر: {st}\n\n**کلمات موجود:**\n{gk_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif action == "toggle_golden_status":
                            config_db["golden_keywords_active"] = not config_db.get("golden_keywords_active", False)
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت فیلتر کلمات طلایی تغییر کرد.", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif action == "add_golden_word_prompt":
                            user_states[chat_id] = "WAITING_FOR_GOLDEN_WORD"
                            reply = "🎯 **کلمه طلایی جدید را ارسال کنید:**"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_gk_idx_"):
                            idx = int(action.replace("del_gk_idx_", ""))
                            gk = config_db.get("golden_keywords", [])
                            if 0 <= idx < len(gk):
                                removed_word = gk.pop(idx)
                                save_config(config_db)
                                reply = f"❌ کلمه طلایی `{removed_word}` حذف شد."
                            else:
                                reply = "⚠️ قبلاً حذف شده است."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif action == "panel_list":
                            reply = f"📋 **لیست کانال‌های مبدا ({len(target_channels)}/20):**\n\n"
                            reply += "\n".join([f"{idx}. `{c}`" for idx, c in enumerate(target_channels, 1)])
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
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
                            reply = "🗑 **روی کانال موردنظر جهت حذف کلیک کنید:**"
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
                            curr_sig = config_db.get("channel_signature", f"🆔 @{config_db.get('target_channel_username', 'Rallyir')}")
                            reply = f"✏️ **امضای فعلی:**\n`{curr_sig}`\n\nمتن امضای جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_blacklist_menu":
                            bl = config_db.get("blacklist", [])
                            bl_text = "\n".join([f"• `{w}`" for w in bl]) if bl else "هیچ کلمه‌ای فیلتر نشده است."
                            reply = f"🚫 **کلمات فیلتر شده (لیست سیاه):**\n\n{bl_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif action == "add_bl_word_prompt":
                            user_states[chat_id] = "WAITING_FOR_BLACKLIST_WORD"
                            reply = "✏️ **کلمه جدید لیست سیاه را بفرستید:**"
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

                        elif action == "panel_status":
                            st_icon = "🟢 روشن" if config_db.get("bot_active", True) else "🔴 خاموش"
                            gold_icon = "🟢 فعال" if config_db.get("golden_keywords_active", False) else "🔴 غیرفعال"
                            fuzzy_icon = "🟢 فعال" if config_db.get("fuzzy_check_active", True) else "🔴 غیرفعال"
                            quiet_icon = "🟢 فعال" if config_db.get("quiet_hours_active", False) else "🔴 غیرفعال"

                            sh = config_db.get("quiet_start_hour", 0)
                            eh = config_db.get("quiet_end_hour", 6)

                            reply = (
                                f"📊 **گزارش و آمار جامع ربات**\n\n"
                                f"⚙️ **وضعیت کلی:** {st_icon}\n"
                                f"📢 **کانال مقصد:** `{config_db.get('target_channel_id')}` (@{config_db.get('target_channel_username')})\n"
                                f"⏱ **فاصله بررسی:** {config_db.get('check_interval', 10)} ثانیه\n"
                                f"📡 **کانال‌های ورودی:** {len(target_channels)} از ۲۰\n"
                                f"🎯 **فیلتر کلمات طلایی:** {gold_icon} ({len(config_db.get('golden_keywords', []))} کلمه)\n"
                                f"🔍 **عدم تکرار هوشمند:** {fuzzy_icon} (حساسیت {config_db.get('fuzzy_threshold', 70)}%)\n"
                                f"🌙 **خاموشی شبانه:** {quiet_icon} ({sh:02d}:00 تا {eh:02d}:00)\n"
                                f"👑 **تعداد ادمین‌ها:** {len(config_db.get('admin_ids', []))}\n"
                                f"✅ **اخبار ارسال‌شده:** {len(seen_post_ids)}\n"
                                f"🚫 **کلمات فیلتر سیاه:** {len(config_db.get('blacklist', []))}"
                            )
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                    # پردازش پیام‌های متنی
                    elif "message" in update:
                        message = update["message"]
                        chat_id = message.get("chat", {}).get("id")
                        from_user_id = message.get("from", {}).get("id")
                        text = message.get("text", "").strip()

                        if not text or not is_admin(from_user_id):
                            continue

                        if text in ["لغو", "/cancel", "انصراف", "کنسل"]:
                            user_states[chat_id] = None
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": "❌ **عملیات لغو شد و به پنل اصلی بازگشتید.**", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
                            continue

                        state = user_states.get(chat_id)

                        if state == "WAITING_FOR_TARGET_CHANNEL":
                            user_states[chat_id] = None
                            parts = text.split()
                            if len(parts) >= 2:
                                new_id = parts[0].strip()
                                new_un = parts[1].replace("@", "").strip()
                                config_db["target_channel_id"] = new_id
                                config_db["target_channel_username"] = new_un
                                save_config(config_db)
                                reply = f"✅ **کانال مقصد تغییر یافت:**\n`{new_id}` (@{new_un})"
                            else:
                                reply = "❌ فرمت ورودی اشتباه است. باید به صورت `آیدی یوزرنیم` بفرستید."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_NEW_ADMIN":
                            user_states[chat_id] = None
                            try:
                                new_adm = int(text.strip())
                                admins = config_db.get("admin_ids", [])
                                if new_adm not in admins:
                                    admins.append(new_adm)
                                    config_db["admin_ids"] = admins
                                    save_config(config_db)
                                    reply = f"✅ ادمین جدید `{new_adm}` اضافه شد."
                                else:
                                    reply = "⚠️ این ادمین قبلاً اضافه شده است."
                            except Exception:
                                reply = "❌ شناسه عددی نامعتبر است."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

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

                        elif state == "WAITING_FOR_GOLDEN_WORD":
                            user_states[chat_id] = None
                            word = text.strip()
                            if word and word not in config_db.get("golden_keywords", []):
                                if "golden_keywords" not in config_db:
                                    config_db["golden_keywords"] = []
                                config_db["golden_keywords"].append(word)
                                save_config(config_db)
                                reply = f"✅ کلمه طلایی `{word}` اضافه شد."
                            else:
                                reply = "⚠️ نامعتبر یا تکراری."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_golden_keyboard()
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
                            sig = config_db.get("channel_signature", f"🆔 @{config_db.get('target_channel_username', 'Rallyir')}")
                            clean_text = sanitize_all_links(text)
                            post_text = clean_extra_spaces(f"📌 <b>{clean_text[:40]}...</b>\n\n{clean_text}\n\n#خبر_فوری\n\n{sig}")
                            
                            success = send_telegram_post(post_text)
                            reply = "🚀 **پست با موفقیت در کانال منتشر شد.**" if success else "❌ خطا در ارسال پست."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif text.lower() in ["/start", "start", "راهنما", "پنل"]:
                            user_states[chat_id] = None
                            st_text = "🟢 **فعال**" if config_db.get("bot_active", True) else "🔴 **غیرفعال (متوقف)**"
                            reply = f"🛠 **پنل مدیریت ربات خبری**\n\nوضعیت جمع‌آوری اخبار: {st_text}\nکانال‌ها: {len(target_channels)} از ۲۰"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
        except Exception:
            time.sleep(1)

# ---------------------------------------------------------
# بررسی موازی کانال‌ها
# ---------------------------------------------------------
def process_single_channel(channel):
    if not config_db.get("bot_active", True) or channel not in target_channels:
        return

    if is_in_quiet_hours():
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
                        success = send_telegram_post(final_text, source_post_url)
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
# پینگ خودکار جهت جلوگیری از به خواب رفتن سرور Render
# ---------------------------------------------------------
def self_ping_loop():
    time.sleep(15)
    port = int(os.environ.get("PORT", 8080))
    health_url = f"http://127.0.0.1:{port}/health"
    print("🌐 Auto self-ping mechanism started...")
    
    while True:
        try:
            http_session.get(health_url, timeout=5)
        except Exception:
            pass
        time.sleep(240)

# ---------------------------------------------------------
# اجرا
# ---------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    
    bot_thread = threading.Thread(target=fast_panel_listener, daemon=True)
    bot_thread.start()

    news_thread = threading.Thread(target=fetch_news_loop, daemon=True)
    news_thread.start()

    ping_thread = threading.Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    print("🚀 Server starting on port:", port)
    app.run(host='0.0.0.0', port=port)
