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

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running at peak performance!", 200

@app.route('/health')
def health():
    return "Healthy", 200

CONFIG_FILE = "config.json"
DB_FILE = "seen_posts.txt"
CHANNELS_FILE = "channels.txt"
TARGETS_FILE = "target_channels.json"

INITIAL_ADMIN_ID = 97241647

DEFAULT_CHANNELS = [
    "Tasnimnews", "FarsNewsInt", "IRNA_1313", "ISNAFA", 
    "KhabarOnline_ir", "Mehrnews", "YjcNewsChannel", 
    "mashreghnews_channel", "Jahan_Fouri", "jamarannews"
]

DEFAULT_TARGETS = [
    {"chat_id": "-1002038404831", "username": "Rallyir"}
]

def load_config():
    default_config = {
        "bot_active": True,
        "bot_token": (os.environ.get("BOT_TOKEN") or "8903869878:AAGWo00OXfJYszdgJ-L4odB2d5Ug4phJK0I").strip(),
        "gemini_api_key": (os.environ.get("GEMINI_API_KEY") or "").strip(),
        "channel_signature": "🆔 @Rallyir",
        "blacklist": ["تبلیغات", "تخفیف ویژه"],
        "golden_keywords": [],
        "golden_keywords_active": False,
        "fuzzy_check_active": True,
        "fuzzy_threshold": 70,
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
    if INITIAL_ADMIN_ID not in default_config.get("admin_ids", []):
        default_config.setdefault("admin_ids", []).append(INITIAL_ADMIN_ID)
    return default_config

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Config Save Error: {e}")

config_db = load_config()

BOT_TOKEN = config_db.get("bot_token", "")
GEMINI_API_KEY = config_db.get("gemini_api_key", "")

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Gemini Init Warning: {e}")

http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

user_states = {}
selected_channels_for_bulk_delete = {}
recent_posts_history = []
last_processed_news_log = []
db_lock = threading.Lock()

def load_target_channels():
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if data:
                    return data[:5]
            except Exception:
                pass
    return DEFAULT_TARGETS.copy()

def save_target_channels(targets):
    try:
        with open(TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(targets[:5], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Targets Save Error: {e}")

target_destinations = load_target_channels()

def is_admin(user_id):
    admins = config_db.get("admin_ids", [])
    return (user_id in admins) or (user_id == INITIAL_ADMIN_ID)

def load_seen_posts():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen_post(post_id):
    with db_lock:
        try:
            with open(DB_FILE, "a", encoding="utf-8") as f:
                f.write(f"{post_id}\n")
        except Exception:
            pass

def clear_seen_posts():
    with db_lock:
        try:
            open(DB_FILE, "w", encoding="utf-8").close()
            seen_post_ids.clear()
            return True
        except Exception:
            return False

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            channels = [line.strip() for line in f if line.strip()]
            if channels:
                return channels[:20]
    return DEFAULT_CHANNELS.copy()

def save_channels(channels):
    try:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            for ch in channels[:20]:
                f.write(f"{ch}\n")
    except Exception:
        pass

seen_post_ids = load_seen_posts()
target_channels = load_channels()
last_update_id = 0

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
    if not text:
        return ""
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()

def sanitize_all_links(text):
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
    if not config_db.get("golden_keywords_active", False):
        return True
    keywords = config_db.get("golden_keywords", [])
    if not keywords:
        return True
    return any(word.lower() in text.lower() for word in keywords if word)

def is_fuzzy_duplicate(new_text):
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
    default_uname = target_destinations[0]["username"] if target_destinations else "Rallyir"
    sig = config_db.get("channel_signature", f"🆔 @{default_uname}")
    body = "\n\n".join(lines)
    return clean_extra_spaces(f"{body}\n\n#خبر\n\n{sig}")

def rewrite_with_ai(raw_text):
    if not raw_text or len(raw_text.strip()) < 15 or check_blacklist(raw_text):
        return None

    if not check_golden_keywords(raw_text):
        return None

    if is_fuzzy_duplicate(raw_text):
        return None

    key = config_db.get("gemini_api_key", "").strip()
    if not key:
        return clean_fallback(raw_text)

    try:
        genai.configure(api_key=key)
    except Exception:
        pass

    prompt = (
        "تو یک ویرایشگر خبر حرفه‌ای هستی. متن زیر را بازنویسی کن.\n\n"
        "قوانین:\n"
        "۱. تمام لینک‌ها، هایپرلینک‌ها و آیدی‌ها (@) را حتماً حذف کن.\n"
        "۲. خط اول را یک تیتر دقیق بگذار.\n"
        "۳. از هیچ ایموجی در متن خبر استفاده نکن.\n"
        "۴. در انتها ۲ تا ۴ هشتگ خبری تخصصی بگذار.\n\n"
        f"متن خبر:\n{raw_text}"
    )

    for model_name in ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash', 'gemini-1.5-flash']:
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

            default_uname = target_destinations[0]["username"] if target_destinations else "Rallyir"
            sig = config_db.get("channel_signature", f"🆔 @{default_uname}")
            
            clean_res = clean_extra_spaces(f"{formatted_body}\n\n{sig}")
            recent_posts_history.append(sanitize_all_links(raw_text))
            if len(recent_posts_history) > 30:
                recent_posts_history.pop(0)

            return clean_res
        except Exception:
            continue

    return clean_fallback(raw_text)

def send_telegram_post_to_all(text, source_url=None):
    token = config_db.get("bot_token", BOT_TOKEN).strip()
    if not token or not target_destinations:
        return False

    success_any = False
    for dest in target_destinations:
        target_id = dest["chat_id"]
        target_uname = dest["username"]

        keyboard = []
        links_row = []
        if source_url:
            links_row.append({"text": "🔗 منبع خبر", "url": source_url})
        links_row.append({"text": "📢 عضویت در کانال", "url": f"https://t.me/{target_uname}"})
        keyboard.append(links_row)
        
        send_url = f"https://api.telegram.org/bot{token}/sendMessage"
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
                success_any = True
                continue
                
            plain_text = text.replace("<b>", "").replace("</b>", "").replace("📌 ", "")
            payload["text"] = plain_text
            payload.pop("parse_mode", None)
            res_retry = http_session.post(send_url, json=payload, timeout=5)
            if res_retry.status_code == 200:
                success_any = True
        except Exception:
            pass
    return success_any

def get_main_panel_keyboard():
    status_icon = "🟢" if config_db.get("bot_active", True) else "🔴"
    status_text = "روشن / خاموش کردن ربات" if config_db.get("bot_active", True) else "روشن / خاموش کردن ربات"
    interval = config_db.get("check_interval", 10)
    
    golden_st = "🟢" if config_db.get("golden_keywords_active", False) else "🔴"
    fuzzy_st = "🟢" if config_db.get("fuzzy_check_active", True) else "🔴"
    quiet_st = "🟢" if config_db.get("quiet_hours_active", False) else "🔴"

    return {
        "inline_keyboard": [
            [
                {"text": f"{status_icon} {status_text}", "callback_data": "toggle_bot_status"}
            ],
            [
                {"text": "📋 لیست مبدا", "callback_data": "panel_list"},
                {"text": "➕ افزودن مبدا", "callback_data": "panel_add_prompt"}
            ],
            [
                {"text": "🗑 حذف تکی مبدا", "callback_data": "panel_delete_menu"},
                {"text": "☑️ حذف گروهی مبدا", "callback_data": "panel_bulk_delete_menu"}
            ],
            [
                {"text": "🎯 مدیریت مقصدهای ارسال", "callback_data": "panel_targets_menu"},
                {"text": "🔑 تنظیم توکن و API", "callback_data": "panel_tokens_menu"}
            ],
            [
                {"text": "✏️ ویرایش امضا", "callback_data": "panel_sig_prompt"},
                {"text": f"⏱ فاصله: {interval}s", "callback_data": "panel_interval_menu"}
            ],
            [
                {"text": "🚫 کلمات سیاه", "callback_data": "panel_blacklist_menu"},
                {"text": f"{golden_st} کلیدواژه طلایی", "callback_data": "panel_golden_menu"}
            ],
            [
                {"text": f"{fuzzy_st} سیستم ضدتکرار", "callback_data": "panel_fuzzy_menu"},
                {"text": f"{quiet_st} ساعت خاموشی", "callback_data": "panel_quiet_menu"}
            ],
            [
                {"text": "🤖 تست سلامت AI", "callback_data": "test_ai_health"},
                {"text": "🔍 گزارش ۵ خبر آخر", "callback_data": "show_recent_news_log"}
            ],
            [
                {"text": "⚡️ پاکسازی کش", "callback_data": "clear_ram_cache"},
                {"text": "👑 ادمین‌ها", "callback_data": "panel_admins_menu"}
            ],
            [
                {"text": "💾 بک‌آپ و ریست", "callback_data": "panel_backup_reset_menu"},
                {"text": "🚀 ارسال پست دستی", "callback_data": "panel_force_post_prompt"}
            ],
            [
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

def get_targets_keyboard():
    keyboard = []
    if len(target_destinations) < 5:
        keyboard.append([{"text": "➕ افزودن کانال مقصد جدید (حداکثر ۵تا)", "callback_data": "add_target_prompt"}])
    for idx, dest in enumerate(target_destinations):
        keyboard.append([{"text": f"❌ حذف مقصد: @{dest['username']}", "callback_data": f"del_target_{idx}"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_tokens_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🤖 تغییر توکن ربات (Bot Token)", "callback_data": "set_bot_token_prompt"}],
            [{"text": "💎 تغییر کلید جمنای (Gemini API Key)", "callback_data": "set_gemini_key_prompt"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
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
                {"text": "📥 دانلود بک‌آپ", "callback_data": "download_config_file"},
                {"text": "📥 دانلود دیتابیس", "callback_data": "download_db_file"}
            ],
            [{"text": "🧹 ریست حافظه اخبار دیده‌شده", "callback_data": "reset_db_confirm_prompt"}],
            [{"text": "🔄 بازگشت به تنظیمات کارخانه", "callback_data": "factory_reset_confirm_prompt"}],
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
    confirm_text = f"🗑 تأیید و حذف انتخاب‌شده‌ها ({count})" if count > 0 else "🗑 هیچ کانالی انتخاب نشده"
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

def fast_panel_listener():
    global last_update_id, target_channels, user_states, config_db, selected_channels_for_bulk_delete, recent_posts_history, target_destinations, last_processed_news_log
    print("Bot fast listener is online...")
    
    while True:
        try:
            token = config_db.get("bot_token", "").strip()
            if not token:
                time.sleep(3)
                continue

            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 5}
            
            res = http_session.get(url, params=params, timeout=8)
            if res.status_code != 200:
                time.sleep(2)
                continue
                
            data = res.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb.get("id")
                        action = cb.get("data")
                        msg = cb.get("message", {})
                        chat_id = msg.get("chat", {}).get("id")
                        from_user_id = cb.get("from", {}).get("id")

                        if not is_admin(from_user_id):
                            try:
                                http_session.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⛔️ شما دسترسی ادمین ندارید.", "show_alert": True}, timeout=2)
                            except Exception:
                                pass
                            continue

                        try:
                            http_session.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=2)
                        except Exception:
                            pass

                        if action == "panel_targets_menu":
                            targets_info = "\n".join([f"• `{d['chat_id']}` (@{d['username']})" for d in target_destinations])
                            reply = "🎯 **مدیریت کانال‌های مقصد (حداکثر ۵تا)**\n\n" + "---" + f"\nکانال‌های فعلی:\n{targets_info}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_targets_keyboard()
                            }, timeout=3)

                        elif action == "add_target_prompt":
                            if len(target_destinations) >= 5:
                                reply = "⚠️ ظرفیت کانال‌های مقصد تکمیل است (حداکثر ۵ کانال)."
                                http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                    "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_targets_keyboard()
                                }, timeout=3)
                            else:
                                user_states[chat_id] = "WAITING_FOR_NEW_TARGET"
                                reply = "🎯 **افزودن کانال مقصد جدید**\n\n" + "---" + "\nاطلاعات را به فرمت زیر بفرستید:\n`آیدی_عددی یوزرنیم`\n\nمثال:\n`-100123456789 mynewchannel`"
                                http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                    "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                                }, timeout=3)

                        elif action.startswith("del_target_"):
                            idx = int(action.replace("del_target_", ""))
                            if 0 <= idx < len(target_destinations) and len(target_destinations) > 1:
                                removed = target_destinations.pop(idx)
                                save_target_channels(target_destinations)
                                reply = f"❌ کانال مقصد @{removed['username']} حذف شد."
                            else:
                                reply = "⚠️ حداقل باید یک کانال مقصد وجود داشته باشد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_targets_keyboard()
                            }, timeout=3)

                        elif action == "panel_tokens_menu":
                            cur_token = config_db.get("bot_token", "")
                            cur_key = config_db.get("gemini_api_key", "")
                            masked_token = cur_token[:6] + "..." if len(cur_token) > 6 else "تنظیم‌نشده"
                            masked_key = cur_key[:6] + "..." if len(cur_key) > 6 else "تنظیم‌نشده"
                            reply = "🔑 **تنظیمات توکن و API کلیدها**\n\n" + "---" + f"\n• توکن ربات: `{masked_token}`\n• کلید جمنای: `{masked_key}`"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_tokens_keyboard()
                            }, timeout=3)

                        elif action == "set_bot_token_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_BOT_TOKEN"
                            reply = "🤖 **تغییر توکن ربات**\n\n" + "---" + "\nتوکن جدید ربات (Bot Token) را ارسال کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "set_gemini_key_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_GEMINI_KEY"
                            reply = "💎 **تغییر کلید جمنای**\n\n" + "---" + "\nکلید جدید جمنای (Gemini API Key) را ارسال کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "factory_reset_confirm_prompt":
                            if os.path.exists(CONFIG_FILE):
                                os.remove(CONFIG_FILE)
                            if os.path.exists(TARGETS_FILE):
                                os.remove(TARGETS_FILE)
                            if os.path.exists(CHANNELS_FILE):
                                os.remove(CHANNELS_FILE)
                            config_db = load_config()
                            global target_destinations, target_channels
                            target_destinations = load_target_channels()
                            target_channels = load_channels()
                            reply = "🔄 **بازگشت به تنظیمات کارخانه**\n\n" + "---" + "\nتمامی تنظیمات با موفقیت به حالت اولیه برگشتند."
                            http_session.post(f"https://api.telegram.org/bot{config_db.get('bot_token')}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "test_ai_health":
                            test_status = "❌ خطا در اتصال به Gemini"
                            for mdl in ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash']:
                                try:
                                    key = config_db.get("gemini_api_key", "").strip()
                                    if key:
                                        genai.configure(api_key=key)
                                    ai_model = genai.GenerativeModel(mdl)
                                    resp = ai_model.generate_content("سلام، تست اتصال ربات است.", request_options={"timeout": 5})
                                    if resp and resp.text:
                                        test_status = f"✅ اتصال برقرار است ({mdl}).\nپاسخ: {resp.text.strip()[:60]}..."
                                        break
                                except Exception as e2:
                                    test_status = f"❌ خطای هوش مصنوعی: {str(e2)[:80]}"
                            
                            reply = "🤖 **تست سلامت هوش مصنوعی**\n\n" + "---" + f"\n{test_status}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "show_recent_news_log":
                            if last_processed_news_log:
                                log_text = "\n\n".join([f"📌 {item}" for item in last_processed_news_log[-5:]])
                            else:
                                log_text = "هنوز خبری در این چرخه ثبت نشده است."
                            
                            reply = "🔍 **آخرین اخبار پردازش‌شده**\n\n" + "---" + f"\n{log_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "clear_ram_cache":
                            recent_posts_history.clear()
                            reply = "⚡️ **پاکسازی حافظه کش**\n\n" + "---" + "\nحافظه موقت پاک‌سازی و سرعت بهینه شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action in ["panel_main", "cancel_action"]:
                            user_states[chat_id] = None
                            selected_channels_for_bulk_delete[chat_id] = set()
                            st_text = "🟢 **فعال**" if config_db.get("bot_active", True) else "🔴 **غیرفعال (متوقف)**"
                            reply = "❌ **عملیات لغو شد**\n\n" + "---" + f"\nبه پنل اصلی بازگشتید.\nوضعیت ربات: {st_text}" if action == "cancel_action" else f"🛠 **پنل مدیریت ربات خبری**\n\n" + "---" + f"\nوضعیت سیستم: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "toggle_bot_status":
                            config_db["bot_active"] = not config_db.get("bot_active", True)
                            save_config(config_db)
                            st_text = "🟢 **روشن و فعال شد**" if config_db["bot_active"] else "🔴 **متوقف شد**"
                            reply = f"⚙️ **تغییر وضعیت ربات**\n\n" + "---" + f"\nوضعیت جدید: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_fuzzy_menu":
                            reply = f"🔍 **تنظیمات عدم تکرار هوشمند**\n\n" + "---" + f"\nحساسیت فعلی: **{config_db.get('fuzzy_threshold', 70)}%**"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_fuzzy_keyboard()
                            }, timeout=3)

                        elif action == "toggle_fuzzy_status":
                            config_db["fuzzy_check_active"] = not config_db.get("fuzzy_check_active", True)
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت سیستم ضدتکرار به‌روز شد.", "reply_markup": get_fuzzy_keyboard()
                            }, timeout=3)

                        elif action.startswith("set_fuzzy_"):
                            val = int(action.replace("set_fuzzy_", ""))
                            config_db["fuzzy_threshold"] = val
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ حساسیت روی {val}% تنظیم شد.", "reply_markup": get_fuzzy_keyboard()
                            }, timeout=3)

                        elif action == "panel_quiet_menu":
                            sh = config_db.get("quiet_start_hour", 0)
                            eh = config_db.get("quiet_end_hour", 6)
                            reply = f"🌙 **تنظیمات خاموشی شبانه**\n\n" + "---" + f"\nبازه فعلی: از ساعت **{sh:02d}:00** تا **{eh:02d}:00**"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "toggle_quiet_status":
                            config_db["quiet_hours_active"] = not config_db.get("quiet_hours_active", False)
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت خاموشی شبانه به‌روز شد.", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "set_quiet_start_prompt":
                            reply = "⏰ **ساعت شروع خاموشی**\n\n" + "---" + "\nساعت مورد نظر را انتخاب کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_hour_picker_keyboard("qstart")
                            }, timeout=3)

                        elif action.startswith("qstart_"):
                            h = int(action.replace("qstart_", ""))
                            config_db["quiet_start_hour"] = h
                            save_config(config_db)
                            reply = f"✅ ساعت شروع روی **{h:02d}:00** تنظیم شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "set_quiet_end_prompt":
                            reply = "⏰ **ساعت پایان خاموشی**\n\n" + "---" + "\nساعت مورد نظر را انتخاب کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_hour_picker_keyboard("qend")
                            }, timeout=3)

                        elif action.startswith("qend_"):
                            h = int(action.replace("qend_", ""))
                            config_db["quiet_end_hour"] = h
                            save_config(config_db)
                            reply = f"✅ ساعت پایان روی **{h:02d}:00** تنظیم شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_quiet_keyboard()
                            }, timeout=3)

                        elif action == "panel_backup_reset_menu":
                            reply = "💾 **مدیریت دیتابیس و پشتیبان‌گیری**\n\n" + "---" + "\nگزینه مورد نظر را انتخاب کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_backup_reset_keyboard()
                            }, timeout=3)

                        elif action == "download_config_file":
                            if os.path.exists(CONFIG_FILE):
                                with open(CONFIG_FILE, "rb") as f:
                                    http_session.post(f"https://api.telegram.org/bot{token}/sendDocument", data={"chat_id": chat_id}, files={"document": f}, timeout=10)

                        elif action == "download_db_file":
                            if os.path.exists(DB_FILE):
                                with open(DB_FILE, "rb") as f:
                                    http_session.post(f"https://api.telegram.org/bot{token}/sendDocument", data={"chat_id": chat_id}, files={"document": f}, timeout=10)

                        elif action == "reset_db_confirm_prompt":
                            clear_seen_posts()
                            reply = "🧹 **پاکسازی حافظه اخبار**\n\n" + "---" + "\nحافظه اخبار دیده‌شده خالی شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_admins_menu":
                            admins = config_db.get("admin_ids", [])
                            adm_list = "\n".join([f"• `{a}`" for a in admins])
                            reply = f"👑 **مدیریت ادمین‌ها**\n\n" + "---" + f"\nلیست ادمین‌های فعلی:\n{adm_list}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        elif action == "add_admin_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_ADMIN"
                            reply = "👑 **افزودن ادمین جدید**\n\n" + "---" + "\nشناسه عددی (Chat ID) ادمین جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_admin_"):
                            adm_id = int(action.replace("del_admin_", ""))
                            admins = config_db.get("admin_ids", [])
                            if adm_id in admins and adm_id != INITIAL_ADMIN_ID:
                                admins.remove(adm_id)
                                save_config(config_db)
                                reply = "❌ ادمین مورد نظر حذف شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        elif action == "panel_golden_menu":
                            gk = config_db.get("golden_keywords", [])
                            gk_text = "\n".join([f"• `{w}`" for w in gk]) if gk else "خالی"
                            st = "🟢 فعال" if config_db.get("golden_keywords_active", False) else "🔴 غیرفعال"
                            reply = f"🎯 **کلیدواژه‌های طلایی**\nوضعیت: {st}\n\n" + "---" + f"\nکلمات فعلی:\n{gk_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif action == "toggle_golden_status":
                            config_db["golden_keywords_active"] = not config_db.get("golden_keywords_active", False)
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت فیلتر کلیدواژه‌های طلایی تغییر کرد.", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif action == "add_golden_word_prompt":
                            user_states[chat_id] = "WAITING_FOR_GOLDEN_WORD"
                            reply = "🎯 **افزودن کلمه طلایی**\n\n" + "---" + "\nکلمه یا عبارت مورد نظر را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_gk_idx_"):
                            idx = int(action.replace("del_gk_idx_", ""))
                            gk = config_db.get("golden_keywords", [])
                            if 0 <= idx < len(gk):
                                gk.pop(idx)
                                save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ کلمه طلایی حذف شد.", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif action == "panel_list":
                            reply = f"📋 **لیست کانال‌های مبدا ({len(target_channels)}/20)**\n\n" + "---" + "\n" + "\n".join([f"{idx}. `{c}`" for idx, c in enumerate(target_channels, 1)])
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_bulk_delete_menu":
                            selected_channels_for_bulk_delete[chat_id] = set()
                            reply = "☑️ **مدیریت و حذف دسته‌جمعی مبدا**\n\n" + "---" + "\nروی کانال‌های مورد نظر کلیک کنید تا انتخاب شوند:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
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
                            try:
                                http_session.post(f"https://api.telegram.org/bot{token}/editMessageReplyMarkup", json={
                                    "chat_id": chat_id, "message_id": msg.get("message_id"), "reply_markup": get_bulk_delete_keyboard(chat_id)
                                }, timeout=3)
                            except Exception:
                                pass

                        elif action == "confirm_bulk_delete":
                            selected = selected_channels_for_bulk_delete.get(chat_id, set())
                            if selected:
                                for ch in list(selected):
                                    if ch in target_channels:
                                        target_channels.remove(ch)
                                save_channels(target_channels)
                                selected_channels_for_bulk_delete[chat_id] = set()
                                reply = "✅ کانال‌های مبدا انتخاب‌شده حذف شدند."
                            else:
                                reply = "⚠️ هیچ کانالی انتخاب نشده است."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_add_prompt":
                            if len(target_channels) >= 20:
                                reply = "⚠️ ظرفیت کانال‌های مبدا تکمیل است."
                            else:
                                user_states[chat_id] = "WAITING_FOR_CHANNEL_NAME"
                                reply = "📥 **افزودن کانال مبدا**\n\n" + "---" + "\nآیدی کانال مبدا جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_delete_menu":
                            reply = "🗑 **حذف تکی کانال مبدا**\n\n" + "---" + "\nروی کانال مورد نظر برای حذف کلیک کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_delete_channels_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_ch_"):
                            ch_to_del = action.replace("del_ch_", "")
                            if ch_to_del in target_channels:
                                target_channels.remove(ch_to_del)
                                save_channels(target_channels)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ کانال {ch_to_del} حذف شد.", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_interval_menu":
                            reply = f"⏱ **تنظیم فاصله بررسی**\n\n" + "---" + f"\nمقدار فعلی: {config_db.get('check_interval', 10)} ثانیه"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_interval_keyboard()
                            }, timeout=3)

                        elif action.startswith("set_interval_"):
                            sec = int(action.replace("set_interval_", ""))
                            config_db["check_interval"] = sec
                            save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ فاصله بررسی روی {sec} ثانیه تنظیم شد.", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_sig_prompt":
                            user_states[chat_id] = "WAITING_FOR_SIGNATURE"
                            reply = "✏️ **ویرایش امضا**\n\n" + "---" + "\nمتن امضای جدید پست‌ها را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_blacklist_menu":
                            bl = config_db.get("blacklist", [])
                            bl_text = "\n".join([f"• `{w}`" for w in bl]) if bl else "خالی"
                            reply = f"🚫 **مدیریت لیست سیاه**\n\n" + "---" + f"\nکلمات فیلتر شده:\n{bl_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif action == "add_bl_word_prompt":
                            user_states[chat_id] = "WAITING_FOR_BLACKLIST_WORD"
                            reply = "✏️ **افزودن کلمه به لیست سیاه**\n\n" + "---" + "\nکلمه جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_bl_idx_"):
                            idx = int(action.replace("del_bl_idx_", ""))
                            bl = config_db.get("blacklist", [])
                            if 0 <= idx < len(bl):
                                bl.pop(idx)
                                save_config(config_db)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ کلمه از لیست سیاه حذف شد.", "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif action == "panel_force_post_prompt":
                            user_states[chat_id] = "WAITING_FOR_FORCE_POST"
                            reply = "🚀 **ارسال پست دستی**\n\n" + "---" + "\nمتن خبر دلخواه خود را بفرستید تا فوراً ارسال شود:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_status":
                            st_icon = "🟢 روشن" if config_db.get("bot_active", True) else "🔴 خاموش"
                            reply = f"📊 **آمار و وضعیت کامل سیستم**\n\n" + "---" + f"\n• وضعیت ربات: {st_icon}\n• کانال‌های مقصد فعال: {len(target_destinations)}/5\n• کانال‌های مبدا: {len(target_channels)}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                    elif "message" in update:
                        message = update["message"]
                        chat_id = message.get("chat", {}).get("id")
                        from_user_id = message.get("from", {}).get("id")
                        text = message.get("text", "").strip()

                        if not text or not is_admin(from_user_id):
                            continue

                        token = config_db.get("bot_token", "").strip()

                        if text in ["لغو", "/cancel", "انصراف", "کنسل"]:
                            user_states[chat_id] = None
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "❌ **عملیات لغو شد.**", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
                            continue

                        state = user_states.get(chat_id)

                        if state == "WAITING_FOR_NEW_TARGET":
                            user_states[chat_id] = None
                            parts = text.split()
                            if len(parts) >= 2:
                                new_id = parts[0].strip()
                                new_un = parts[1].replace("@", "").strip()
                                if len(target_destinations) < 5:
                                    target_destinations.append({"chat_id": new_id, "username": new_un})
                                    save_target_channels(target_destinations)
                                    reply = f"✅ کانال مقصد جدید (@{new_un}) افزوده شد."
                                else:
                                    reply = "⚠️ ظرفیت کانال‌های مقصد تکمیل است (حداکثر ۵ کانال)."
                            else:
                                reply = "❌ فرمت اشتباه است. لطفا به شکل `آیدی_عددی یوزرنیم` بفرستید."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_NEW_BOT_TOKEN":
                            user_states[chat_id] = None
                            new_t = text.strip()
                            if ":" in new_t and len(new_t) > 15:
                                config_db["bot_token"] = new_t
                                save_config(config_db)
                                reply = "✅ توکن جدید ربات ثبت شد."
                            else:
                                reply = "❌ توکن نامعتبر است."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_NEW_GEMINI_KEY":
                            user_states[chat_id] = None
                            new_k = text.strip()
                            if len(new_k) > 10:
                                config_db["gemini_api_key"] = new_k
                                save_config(config_db)
                                reply = "✅ کلید جمنای با موفقیت ثبت شد."
                            else:
                                reply = "❌ کلید API نامعتبر است."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_NEW_ADMIN":
                            user_states[chat_id] = None
                            try:
                                new_adm = int(text.strip())
                                admins = config_db.get("admin_ids", [])
                                if new_adm not in admins:
                                    admins.append(new_adm)
                                    save_config(config_db)
                                    reply = "✅ ادمین جدید افزوده شد."
                                else:
                                    reply = "⚠️ قبلاً اضافه شده است."
                            except Exception:
                                reply = "❌ شناسه نامعتبر."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_CHANNEL_NAME":
                            user_states[chat_id] = None
                            new_ch = text.replace("@", "").replace("https://t.me/", "").strip()
                            if new_ch and new_ch not in target_channels:
                                target_channels.append(new_ch)
                                save_channels(target_channels)
                                reply = f"✅ کانال مبدا {new_ch} اضافه شد."
                            else:
                                reply = "❌ نامعتبر یا تکراری."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_GOLDEN_WORD":
                            user_states[chat_id] = None
                            word = text.strip()
                            if word:
                                config_db.setdefault("golden_keywords", []).append(word)
                                save_config(config_db)
                                reply = "✅ کلمه طلایی افزوده شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_golden_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_SIGNATURE":
                            user_states[chat_id] = None
                            config_db["channel_signature"] = text
                            save_config(config_db)
                            reply = "✅ امضا ثبت شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_BLACKLIST_WORD":
                            user_states[chat_id] = None
                            word = text.strip()
                            if word:
                                config_db.setdefault("blacklist", []).append(word)
                                save_config(config_db)
                                reply = "✅ کلمه فیلتر شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_FORCE_POST":
                            user_states[chat_id] = None
                            default_uname = target_destinations[0]["username"] if target_destinations else "Rallyir"
                            sig = config_db.get("channel_signature", f"🆔 @{default_uname}")
                            clean_text = sanitize_all_links(text)
                            post_text = clean_extra_spaces(f"📌 <b>{clean_text[:40]}...</b>\n\n{clean_text}\n\n#خبر_فوری\n\n{sig}")
                            
                            success = send_telegram_post_to_all(post_text)
                            reply = "🚀 **پست به تمامی کانال‌های مقصد ارسال شد.**" if success else "❌ خطا."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif text.lower() in ["/start", "start", "راهنما", "پنل"]:
                            user_states[chat_id] = None
                            reply = "🛠 **پنل مدیریت ربات خبری**"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
        except Exception:
            time.sleep(1)

def process_single_channel(channel):
    global last_processed_news_log
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
                        success = send_telegram_post_to_all(final_text, source_post_url)
                        if success:
                            seen_post_ids.add(post_id)
                            save_seen_post(post_id)
                            first_line = final_text.splitlines()[0].replace("<b>", "").replace("</b>", "").replace("📌 ", "")
                            last_processed_news_log.append(f"[{channel}] {first_line}")
                            if len(last_processed_news_log) > 5:
                                last_processed_news_log.pop(0)
                else:
                    seen_post_ids.add(post_id)
                    save_seen_post(post_id)
    except Exception:
        pass

def fetch_news_loop():
    with ThreadPoolExecutor(max_workers=5) as executor:
        while True:
            try:
                if config_db.get("bot_active", True):
                    interval = config_db.get("check_interval", 10)
                    active_channels = list(target_channels)
                    executor.map(process_single_channel, active_channels)
                    time.sleep(interval)
                else:
                    time.sleep(3)
            except Exception:
                time.sleep(3)

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
