import time
import re
import os
import requests
import threading
import queue
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

INITIAL_ADMIN_ID = 97241647

DEFAULT_CHANNELS = [
    "iribnews", 
    "sepahnewsir403", 
    "IRNA_1313", 
    "JahanTasnim", 
    "ClashReport", 
    "TasnimNews", 
    "FarsNewsInt", 
    "mizanplus"
]

DEFAULT_TARGETS = [
    {"chat_id": "-1002038404831", "username": "Rallyir"}
]

config_db = {
    "bot_active": True,
    "bot_token": (os.environ.get("BOT_TOKEN") or "8903869878:AAGWo00OXfJYszdgJ-L4odB2d5Ug4phJK0I").strip(),
    "gemini_api_key": (os.environ.get("GEMINI_API_KEY") or "").strip(),
    "channel_signature": "🆔 @Rallyir",
    "blacklist": ["تبلیغات", "تخفیف ویژه"],
    "categories": {
        "سیاسی": ["دولت", "مجلس", "رهبر", "وزیر", "انتخابات"],
        "ورزشی": ["فوتبال", "استقلال", "پرسپولیس", "تیم ملی", "لیگ"],
        "فناوری": ["هوش مصنوعی", "گوشی", "اینترنت", "تکنولوژی", "آپدیت"]
    },
    "categories_active": True,
    "target_language": "fa", 
    "auto_translate_active": True, # قابلیت روشن/خاموش کردن ترجمه خودکار
    "allowed_languages": {"en": True, "ar": True, "he": True, "fr": True, "tr": True}, 
    "translation_tag_active": True, 
    "fuzzy_check_active": True,
    "fuzzy_threshold": 70,
    "quiet_hours_active": False,
    "quiet_start_hour": 0,
    "quiet_end_hour": 6,
    "check_interval": 10,
    "queue_schedule_active": True,
    "queue_interval": 5,
    "admin_ids": [INITIAL_ADMIN_ID]
}

target_channels = DEFAULT_CHANNELS.copy()
target_destinations = DEFAULT_TARGETS.copy()
seen_post_ids = set()
news_queue = queue.Queue()

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
channel_health_status = {}
db_lock = threading.Lock()
last_update_id = 0

def is_admin(user_id):
    admins = config_db.get("admin_ids", [])
    return (user_id in admins) or (user_id == INITIAL_ADMIN_ID)

def clear_seen_posts():
    with db_lock:
        try:
            seen_post_ids.clear()
            return True
        except Exception:
            return False

def detect_language(text):
    if not text:
        return None
    persian_chars = len(re.findall(r'[\u0600-\u06FF]', text))
    eng_chars = len(re.findall(r'[a-zA-Z]', text))
    hebrew_chars = len(re.findall(r'[\u0590-\u05FF]', text))
    arabic_pure_chars = len(re.findall(r'[\u0600-\u06FF]', text))
    
    if persian_chars > 20 and persian_chars >= eng_chars:
        return None

    if hebrew_chars > 5:
        return "he"
    elif eng_chars > 20 and eng_chars > persian_chars:
        return "en"
    elif arabic_pure_chars > 20 and persian_chars < 5:
        return "ar"
    return None

def translate_text(text):
    if not text or not config_db.get("auto_translate_active", True):
        src = detect_language(text)
        return text, src
    
    source_lang = detect_language(text)
    if not source_lang:
        return text, None 

    allowed_langs = config_db.get("allowed_languages", {})
    if not allowed_langs.get(source_lang, True):
        return text, source_lang 

    target_lang = config_db.get("target_language", "fa")

    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text
        }
        response = http_session.get(url, params=params, timeout=4)
        if response.status_code == 200:
            res_json = response.json()
            translated_sentence = "".join([item[0] for item in res_json[0] if item[0]])
            if translated_sentence:
                return translated_sentence, source_lang
    except Exception:
        pass
    return text, source_lang

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

def detect_category_and_tags(text):
    if not config_db.get("categories_active", True):
        return "#خبر"
    
    tags = []
    categories = config_db.get("categories", {})
    for cat_name, keywords in categories.items():
        for kw in keywords:
            if kw and kw.lower() in text.lower():
                tags.append(f"#{cat_name}")
                break
    
    if not tags:
        tags.append("#خبر_فوری")
    return " ".join(tags[:3])

def clean_fallback(text, translated_from=None):
    clean_t = sanitize_all_links(text)
    if not clean_t:
        clean_t = text
    lines = [l.strip() for l in clean_t.splitlines() if l.strip()]
    if not lines:
        lines = [text]
    
    headline = lines[0][:50]
    lines[0] = f"📌 <b>{headline}</b>"
    
    default_uname = target_destinations[0]["username"] if target_destinations else "Rallyir"
    sig = config_db.get("channel_signature", f"🆔 @{default_uname}")
    cat_tags = detect_category_and_tags(text)
    
    trans_tag = ""
    if translated_from and config_db.get("translation_tag_active", True):
        lang_emojis = {"en": "🇬🇧", "ar": "🇸🇦", "he": "🇮🇱", "fr": "🇫🇷", "tr": "🇹🇷"}
        emoji_flag = lang_emojis.get(translated_from, "🌐")
        trans_tag = f"\n\n{emoji_flag}"

    body = "\n\n".join(lines)
    return clean_extra_spaces(f"{body}{trans_tag}\n\n{cat_tags}\n\n{sig}")

def rewrite_with_ai(raw_text, translated_from=None):
    if not raw_text or check_blacklist(raw_text):
        return clean_fallback(raw_text, translated_from)

    key = config_db.get("gemini_api_key", "").strip()
    if not key:
        return clean_fallback(raw_text, translated_from)

    try:
        genai.configure(api_key=key)
    except Exception:
        return clean_fallback(raw_text, translated_from)

    cat_tags = detect_category_and_tags(raw_text)
    prompt = (
        "تو یک ویرایشگر خبر حرفه‌ای هستی. متن زیر را بازنویسی کن.\n\n"
        "قوانین:\n"
        "۱. تمام لینک‌ها، هایپرلینک‌ها و آیدی‌ها (@) را حتماً حذف کن.\n"
        "۲. خط اول را یک تیتر دقیق بگذار.\n"
        "۳. از هیچ ایموجی در متن خبر استفاده نکن.\n\n"
        f"متن خبر:\n{raw_text}"
    )

    for model_name in ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash', 'gemini-1.5-flash']:
        try:
            ai_model = genai.GenerativeModel(model_name)
            response = ai_model.generate_content(prompt, request_options={"timeout": 5})
            result = response.text.strip()

            result = sanitize_all_links(result)
            lines = [line.strip() for line in result.splitlines() if line.strip()]
            if not lines:
                continue

            headline = lines[0].replace("<b>", "").replace("</b>", "")
            lines[0] = f"📌 <b>{headline}</b>"
            formatted_body = "\n\n".join(lines)

            default_uname = target_destinations[0]["username"] if target_destinations else "Rallyir"
            sig = config_db.get("channel_signature", f"🆔 @{default_uname}")
            
            trans_tag = ""
            if translated_from and config_db.get("translation_tag_active", True):
                lang_emojis = {"en": "🇬🇧", "ar": "🇸🇦", "he": "🇮🇱", "fr": "🇫🇷", "tr": "🇹🇷"}
                emoji_flag = lang_emojis.get(translated_from, "🌐")
                trans_tag = f"\n\n{emoji_flag}"

            clean_res = clean_extra_spaces(f"{formatted_body}{trans_tag}\n\n{cat_tags}\n\n{sig}")
            return clean_res
        except Exception:
            continue

    return clean_fallback(raw_text, translated_from)

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

def queue_worker():
    print("⏳ Queue worker thread is online...")
    while True:
        try:
            if not config_db.get("bot_active", True):
                time.sleep(2)
                continue
            
            is_scheduled = config_db.get("queue_schedule_active", True)
            interval = max(5, config_db.get("queue_interval", 5))

            if not news_queue.empty():
                item = news_queue.get()
                final_text = item["text"]
                source_url = item["url"]
                
                send_telegram_post_to_all(final_text, source_url)
                news_queue.task_done()
                
                if is_scheduled:
                    time.sleep(interval)
            else:
                time.sleep(1)
        except Exception:
            time.sleep(1)

def get_main_panel_keyboard():
    status_icon = "🟢" if config_db.get("bot_active", True) else "🔴"
    interval = config_db.get("check_interval", 10)
    queue_st = "🟢" if config_db.get("queue_schedule_active", True) else "🔴"
    queue_sec = config_db.get("queue_interval", 5)
    cat_st = "🟢" if config_db.get("categories_active", True) else "🔴"

    return {
        "inline_keyboard": [
            [{"text": f"{status_icon} روشن / خاموش ربات", "callback_data": "toggle_bot_status"}],
            [{"text": "🌐 تنظیمات ترجمه", "callback_data": "panel_translation_menu"}, {"text": f"{queue_st} صف انتشار ({queue_sec}s)", "callback_data": "panel_queue_menu"}],
            [{"text": f"{cat_st} دسته‌بندی موضوعی", "callback_data": "panel_categories_menu"}, {"text": "📡 سلامت مبدا", "callback_data": "panel_health_monitor"}],
            [{"text": "🎯 مقصدهای ارسال", "callback_data": "panel_targets_menu"}, {"text": f"📋 لیست مبدا ({len(target_channels)}/25)", "callback_data": "panel_list"}],
            [{"text": "➕ افزودن مبدا", "callback_data": "panel_add_prompt"}, {"text": "🗑 حذف تکی مبدا", "callback_data": "panel_delete_menu"}],
            [{"text": "☑️ حذف گروهی مبدا", "callback_data": "panel_bulk_delete_menu"}, {"text": "🔑 توکن و API", "callback_data": "panel_tokens_menu"}],
            [{"text": "✏️ ویرایش امضا", "callback_data": "panel_sig_prompt"}, {"text": f"⏱ فاصله: {interval}s", "callback_data": "panel_interval_menu"}],
            [{"text": "🚫 کلمات سیاه", "callback_data": "panel_blacklist_menu"}, {"text": "🤖 تست سلامت AI", "callback_data": "test_ai_health"}],
            [{"text": "🔍 گزارش ۵ خبر آخر", "callback_data": "show_recent_news_log"}, {"text": "⚡️ پاکسازی کش", "callback_data": "clear_ram_cache"}],
            [{"text": "👑 ادمین‌ها", "callback_data": "panel_admins_menu"}, {"text": "🚀 ارسال پست دستی", "callback_data": "panel_force_post_prompt"}],
            [{"text": "📊 آمار و وضعیت کامل", "callback_data": "panel_status"}]
        ]
    }

def get_cancel_keyboard():
    return {
        "inline_keyboard": [[{"text": "🚫 انصراف و بازگشت به پنل", "callback_data": "cancel_action"}]]
    }

def get_translation_keyboard():
    trans_act_st = "🟢 روشن" if config_db.get("auto_translate_active", True) else "🔴 خاموش"
    tag_st = "🟢 فعال" if config_db.get("translation_tag_active", True) else "🔴 غیرفعال"
    allowed = config_db.get("allowed_languages", {})
    en_st = "✅" if allowed.get("en", True) else "❌"
    ar_st = "✅" if allowed.get("ar", True) else "❌"
    he_st = "✅" if allowed.get("he", True) else "❌"
    fr_st = "✅" if allowed.get("fr", True) else "❌"
    tr_st = "✅" if allowed.get("tr", True) else "❌"

    return {
        "inline_keyboard": [
            [{"text": f"ترجمه خودکار: {trans_act_st}", "callback_data": "toggle_trans_active_status"}],
            [{"text": f"ایموجی پرچم در انتها: {tag_st}", "callback_data": "toggle_trans_tag"}],
            [{"text": f"{en_st} انگلیسی 🇬🇧", "callback_data": "toggle_lang_en"}, {"text": f"{ar_st} عربی 🇸🇦", "callback_data": "toggle_lang_ar"}],
            [{"text": f"{he_st} عبری 🇮🇱", "callback_data": "toggle_lang_he"}, {"text": f"{fr_st} فرانسوی 🇫🇷", "callback_data": "toggle_lang_fr"}],
            [{"text": f"{tr_st} ترکی 🇹🇷", "callback_data": "toggle_lang_tr"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_queue_keyboard():
    q_st = "🔴 غیرفعال" if not config_db.get("queue_schedule_active", True) else "🟢 فعال"
    q_sec = config_db.get("queue_interval", 5)
    return {
        "inline_keyboard": [
            [{"text": f"وضعیت زمان‌بندی: {q_st}", "callback_data": "toggle_queue_status"}],
            [{"text": "5 ثانیه", "callback_data": "set_q_5"}, {"text": "10 ثانیه", "callback_data": "set_q_10"}, {"text": f"• {q_sec}s •", "callback_data": "noop"}, {"text": "30 ثانیه", "callback_data": "set_q_30"}, {"text": "60 ثانیه", "callback_data": "set_q_60"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_categories_keyboard():
    cat_st = "🔴 غیرفعال" if not config_db.get("categories_active", True) else "🟢 فعال"
    return {
        "inline_keyboard": [
            [{"text": f"وضعیت دسته‌بندی هوشمند: {cat_st}", "callback_data": "toggle_cat_status"}],
            [{"text": "📋 مشاهده دسته‌ها و کلمات", "callback_data": "show_categories_list"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_targets_keyboard():
    keyboard = []
    if len(target_destinations) < 5:
        keyboard.append([{"text": "➕ افزودن مقصد جدید (حداکثر ۵تا)", "callback_data": "add_target_prompt"}])
    for idx, dest in enumerate(target_destinations):
        keyboard.append([{"text": f"❌ حذف مقصد: @{dest['username']}", "callback_data": f"del_target_{idx}"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_tokens_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🤖 تغییر توکن ربات", "callback_data": "set_bot_token_prompt"}],
            [{"text": "💎 تغییر کلید جمنای", "callback_data": "set_gemini_key_prompt"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
        ]
    }

def get_admins_keyboard():
    keyboard = [[{"text": "➕ افزودن ادمین جدید", "callback_data": "add_admin_prompt"}]]
    for adm in config_db.get("admin_ids", []):
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
    keyboard = [[{"text": "➕ افزودن کلمه جدید", "callback_data": "add_bl_word_prompt"}]]
    for idx, word in enumerate(config_db.get("blacklist", [])):
        keyboard.append([{"text": f"❌ حذف «{word}»", "callback_data": f"del_bl_idx_{idx}"}])
    keyboard.append([{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_interval_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "⚡️ ۲ ثانیه", "callback_data": "set_interval_2"}, {"text": "⏱ ۵ ثانیه", "callback_data": "set_interval_5"}],
            [{"text": "⏳ ۱۰ ثانیه (توصیه‌شده)", "callback_data": "set_interval_10"}, {"text": "🕰 ۳۰ ثانیه", "callback_data": "set_interval_30"}],
            [{"text": "🚫 انصراف و بازگشت", "callback_data": "cancel_action"}]
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

                        if action == "panel_translation_menu":
                            trans_st = "روشن" if config_db.get("auto_translate_active", True) else "خاموش"
                            tag_st = "فعال" if config_db.get("translation_tag_active", True) else "غیرفعال"
                            reply = f"🌐 **تنظیمات ترجمه و زبان‌ها**\n\n---" + f"\n• ترجمه خودکار: **{trans_st}**\n• برچسب پرچم: **{tag_st}**"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_translation_keyboard()
                            }, timeout=3)

                        elif action == "toggle_trans_active_status":
                            config_db["auto_translate_active"] = not config_db.get("auto_translate_active", True)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت ترجمه خودکار به‌روز شد.", "reply_markup": get_translation_keyboard()
                            }, timeout=3)

                        elif action == "toggle_trans_tag":
                            config_db["translation_tag_active"] = not config_db.get("translation_tag_active", True)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت برچسب پرچم به‌روز شد.", "reply_markup": get_translation_keyboard()
                            }, timeout=3)

                        elif action.startswith("toggle_lang_"):
                            lang = action.replace("toggle_lang_", "")
                            allowed = config_db.get("allowed_languages", {})
                            allowed[lang] = not allowed.get(lang, True)
                            config_db["allowed_languages"] = allowed
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ وضعیت زبان {lang.upper()} به‌روز شد.", "reply_markup": get_translation_keyboard()
                            }, timeout=3)

                        elif action == "panel_queue_menu":
                            q_st = "🟢 فعال" if config_db.get("queue_schedule_active", True) else "🔴 غیرفعال"
                            q_sec = config_db.get("queue_interval", 5)
                            reply = f"⏳ **سیستم صف‌بندی و انتشار زمان‌بندی‌شده**\n\n---" + f"\nوضعیت: {q_st}\nفاصله بین ارسال‌ها: **{q_sec} ثانیه**"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_queue_keyboard()
                            }, timeout=3)

                        elif action == "toggle_queue_status":
                            config_db["queue_schedule_active"] = not config_db.get("queue_schedule_active", True)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت صف‌بندی به‌روز شد.", "reply_markup": get_queue_keyboard()
                            }, timeout=3)

                        elif action.startswith("set_q_"):
                            sec = int(action.replace("set_q_", ""))
                            config_db["queue_interval"] = max(5, sec)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ فاصله انتشار روی {sec} ثانیه تنظیم شد.", "reply_markup": get_queue_keyboard()
                            }, timeout=3)

                        elif action == "panel_categories_menu":
                            cat_st = "🟢 فعال" if config_db.get("categories_active", True) else "🔴 غیرفعال"
                            reply = f"🎯 **مدیریت دسته‌بندی موضوعی هوشمند**\n\n---" + f"\nوضعیت: {cat_st}\nربات به طور خودکار اخبار را دسته‌بندی و هشتگ‌گذاری می‌کند."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_categories_keyboard()
                            }, timeout=3)

                        elif action == "toggle_cat_status":
                            config_db["categories_active"] = not config_db.get("categories_active", True)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": "✅ وضعیت دسته‌بندی هوشمند به‌روز شد.", "reply_markup": get_categories_keyboard()
                            }, timeout=3)

                        elif action == "show_categories_list":
                            cats = config_db.get("categories", {})
                            cat_text = ""
                            for c_name, kws in cats.items():
                                cat_text += f"• **{c_name}**: `{', '.join(kws)}`\n"
                            reply = f"📋 **لیست دسته‌ها و کلمات کلیدی**\n\n---" + f"\n{cat_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_categories_keyboard()
                            }, timeout=3)

                        elif action == "panel_health_monitor":
                            monitor_text = ""
                            for ch in target_channels:
                                st = channel_health_status.get(ch, "🟢 سالم و آنلاین")
                                monitor_text += f"• `{ch}`: {st}\n"
                            if not monitor_text:
                                monitor_text = "هنوز بررسی انجام نشده است."
                            reply = f"📡 **مانیتورینگ هوشمند کانال‌های مبدا**\n\n---" + f"\n{monitor_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_targets_menu":
                            targets_info = "\n".join([f"• `{d['chat_id']}` (@{d['username']})" for d in target_destinations])
                            reply = "🎯 **مدیریت کانال‌های مقصد (حداکثر ۵تا)**\n\n---" + f"\nکانال‌های فعلی:\n{targets_info}"
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
                                reply = "🎯 **افزودن کانال مقصد جدید**\n\n---" + "\nاطلاعات را به فرمت زیر بفرستید:\n`آیدی_عددی یوزرنیم`"
                                http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                    "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                                }, timeout=3)

                        elif action.startswith("del_target_"):
                            idx = int(action.replace("del_target_", ""))
                            if 0 <= idx < len(target_destinations) and len(target_destinations) > 1:
                                removed = target_destinations.pop(idx)
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
                            reply = "🔑 **تنظیمات توکن و API کلیدها**\n\n---" + f"\n• توکن ربات: `{masked_token}`\n• کلید جمنای: `{masked_key}`"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_tokens_keyboard()
                            }, timeout=3)

                        elif action == "set_bot_token_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_BOT_TOKEN"
                            reply = "🤖 **تغییر توکن ربات**\n\n---" + "\nتوکن جدید ربات (Bot Token) را ارسال کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "set_gemini_key_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_GEMINI_KEY"
                            reply = "💎 **تغییر کلید جمنای**\n\n---" + "\nکلید جدید جمنای (Gemini API Key) را ارسال کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
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
                            
                            reply = "🤖 **تست سلامت هوش مصنوعی**\n\n---" + f"\n{test_status}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "show_recent_news_log":
                            if last_processed_news_log:
                                log_text = "\n\n".join([f"📌 {item}" for item in last_processed_news_log[-5:]])
                            else:
                                log_text = "هنوز خبری در این چرخه ثبت نشده است."
                            
                            reply = "🔍 **آخرین اخبار پردازش‌شده**\n\n---" + f"\n{log_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "clear_ram_cache":
                            recent_posts_history.clear()
                            reply = "⚡️ **پاکسازی حافظه کش**\n\n---" + "\nحافظه موقت پاک‌سازی و سرعت بهینه شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action in ["panel_main", "cancel_action"]:
                            user_states[chat_id] = None
                            selected_channels_for_bulk_delete[chat_id] = set()
                            st_text = "🟢 **فعال**" if config_db.get("bot_active", True) else "🔴 **غیرفعال (متوقف)**"
                            reply = "❌ **عملیات لغو شد**\n\n---" + f"\nبه پنل اصلی بازگشتید.\nوضعیت ربات: {st_text}" if action == "cancel_action" else f"🛠 **پنل مدیریت ربات خبری**\n\n---" + f"\nوضعیت سیستم: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "toggle_bot_status":
                            config_db["bot_active"] = not config_db.get("bot_active", True)
                            st_text = "🟢 **روشن و فعال شد**" if config_db["bot_active"] else "🔴 **متوقف شد**"
                            reply = f"⚙️ **تغییر وضعیت ربات**\n\n---" + f"\nوضعیت جدید: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_admins_menu":
                            admins = config_db.get("admin_ids", [])
                            adm_list = "\n".join([f"• `{a}`" for a in admins])
                            reply = f"👑 **مدیریت ادمین‌ها**\n\n---" + f"\nلیست ادمین‌های فعلی:\n{adm_list}"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        elif action == "add_admin_prompt":
                            user_states[chat_id] = "WAITING_FOR_NEW_ADMIN"
                            reply = "👑 **افزودن ادمین جدید**\n\n---" + "\nشناسه عددی (Chat ID) ادمین جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_admin_"):
                            adm_id = int(action.replace("del_admin_", ""))
                            admins = config_db.get("admin_ids", [])
                            if adm_id in admins and adm_id != INITIAL_ADMIN_ID:
                                admins.remove(adm_id)
                                reply = "❌ ادمین مورد نظر حذف شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_admins_keyboard()
                            }, timeout=3)

                        elif action == "panel_list":
                            reply = f"📋 **لیست کانال‌های مبدا ({len(target_channels)}/25)**\n\n---" + "\n" + "\n".join([f"{idx}. `{c}`" for idx, c in enumerate(target_channels, 1)])
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_bulk_delete_menu":
                            selected_channels_for_bulk_delete[chat_id] = set()
                            reply = "☑️ **مدیریت و حذف دسته‌جمعی مبدا**\n\n---" + "\nروی کانال‌های مورد نظر کلیک کنید تا انتخاب شوند:"
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
                                selected_channels_for_bulk_delete[chat_id] = set()
                                reply = "✅ کانال‌های مبدا انتخاب‌شده حذف شدند."
                            else:
                                reply = "⚠️ هیچ کانالی انتخاب نشده است."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_add_prompt":
                            if len(target_channels) >= 25:
                                reply = "⚠️ ظرفیت کانال‌های مبدا تکمیل است (حداکثر 25 کانال)."
                            else:
                                user_states[chat_id] = "WAITING_FOR_CHANNEL_NAME"
                                reply = "📥 **افزودن کانال مبدا**\n\n---" + "\nآیدی کانال مبدا جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_delete_menu":
                            reply = "🗑 **حذف تکی کانال مبدا**\n\n---" + "\nروی کانال مورد نظر برای حذف کلیک کنید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_delete_channels_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_ch_"):
                            ch_to_del = action.replace("del_ch_", "")
                            if ch_to_del in target_channels:
                                target_channels.remove(ch_to_del)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ کانال {ch_to_del} حذف شد.", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_interval_menu":
                            reply = f"⏱ **تنظیم فاصله بررسی**\n\n---" + f"\nمقدار فعلی: {config_db.get('check_interval', 10)} ثانیه"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_interval_keyboard()
                            }, timeout=3)

                        elif action.startswith("set_interval_"):
                            sec = int(action.replace("set_interval_", ""))
                            config_db["check_interval"] = sec
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ فاصله بررسی روی {sec} ثانیه تنظیم شد.", "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_sig_prompt":
                            user_states[chat_id] = "WAITING_FOR_SIGNATURE"
                            reply = "✏️ **ویرایش امضا**\n\n---" + "\nمتن امضای جدید پست‌ها را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_blacklist_menu":
                            reply = f"🚫 **مدیریت لیست سیاه**\n\n---" + f"\nکلمات فیلتر شده فعلی"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif action == "add_bl_word_prompt":
                            user_states[chat_id] = "WAITING_FOR_BLACKLIST_WORD"
                            reply = "✏️ **افزودن کلمه به لیست سیاه**\n\n---" + "\nکلمه جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_bl_idx_"):
                            idx = int(action.replace("del_bl_idx_", ""))
                            bl = config_db.get("blacklist", [])
                            if 0 <= idx < len(bl):
                                bl.pop(idx)
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": f"✅ کلمه از لیست سیاه حذف شد.", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif action == "panel_force_post_prompt":
                            user_states[chat_id] = "WAITING_FOR_FORCE_POST"
                            reply = "🚀 **ارسال پست دستی**\n\n---" + "\nمتن خبر دلخواه خود را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_status":
                            st_icon = "🟢 روشن" if config_db.get("bot_active", True) else "🔴 خاموش"
                            reply = f"📊 **آمار و وضعیت کامل سیستم**\n\n---" + f"\n• وضعیت ربات: {st_icon}\n• کانال‌های مقصد فعال: {len(target_destinations)}/5\n• کانال‌های مبدا: {len(target_channels)}"
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
                                    reply = f"✅ کانال مقصد جدید (@{new_un}) افزوده شد."
                                else:
                                    reply = "⚠️ ظرفیت کانال‌های مقصد تکمیل است."
                            else:
                                reply = "❌ فرمت اشتباه است."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_NEW_BOT_TOKEN":
                            user_states[chat_id] = None
                            config_db["bot_token"] = text.strip()
                            reply = "✅ توکن جدید ربات ثبت شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_NEW_GEMINI_KEY":
                            user_states[chat_id] = None
                            config_db["gemini_api_key"] = text.strip()
                            reply = "✅ کلید جمنای ثبت شد."
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
                                if len(target_channels) < 25:
                                    target_channels.append(new_ch)
                                    reply = f"✅ کانال مبدا {new_ch} اضافه شد."
                                else:
                                    reply = "⚠️ ظرفیت کانال‌های مبدا تکمیل است (حداکثر 25 کانال)."
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
                                reply = "✅ کلمه طلایی افزوده شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_SIGNATURE":
                            user_states[chat_id] = None
                            config_db["channel_signature"] = text
                            reply = "✅ امضا ثبت شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_BLACKLIST_WORD":
                            user_states[chat_id] = None
                            word = text.strip()
                            if word:
                                config_db.setdefault("blacklist", []).append(word)
                                reply = "✅ کلمه فیلتر شد."
                            http_session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "parse_mode": "Markdown", "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_FORCE_POST":
                            user_states[chat_id] = None
                            try:
                                translated_text, src_lang = translate_text(text)
                                final_post = rewrite_with_ai(translated_text, translated_from=src_lang)
                                if not final_post:
                                    final_post = clean_fallback(text, translated_from=src_lang)
                                
                                news_queue.put({"text": final_post, "url": None})
                                reply = "🚀 **پست دستی با موفقیت به صف انتشار اضافه شد.**"
                            except Exception as e:
                                reply = f"🚀 **پست دستی ارسال شد.**"
                                clean_p = clean_fallback(text, translated_from=None)
                                news_queue.put({"text": clean_p, "url": None})

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
    global last_processed_news_log, channel_health_status
    if not config_db.get("bot_active", True) or channel not in target_channels:
        return

    if is_in_quiet_hours():
        return

    url = f"https://t.me/s/{channel}"
    try:
        res = http_session.get(url, timeout=5)
        if res.status_code != 200:
            channel_health_status[channel] = "🔴 خطا (در دسترس نیست)"
            return
        
        soup = BeautifulSoup(res.text, "html.parser")
        posts = soup.find_all("div", class_="tgme_widget_message")
        
        if posts:
            channel_health_status[channel] = "🟢 سالم و آنلاین"
            last_post = posts[-1]
            post_id = last_post.get("data-post")
            
            if post_id and post_id not in seen_post_ids:
                text_div = last_post.find("div", class_="tgme_widget_message_text")
                
                if text_div:
                    raw_text = text_div.get_text(separator="\n")
                    translated_text, src_lang = translate_text(raw_text)
                    final_text = rewrite_with_ai(translated_text, translated_from=src_lang)
                    if not final_text:
                        final_text = clean_fallback(raw_text, translated_from=src_lang)
                    
                    source_post_url = f"https://t.me/{post_id}"

                    if final_text:
                        seen_post_ids.add(post_id)
                        news_queue.put({"text": final_text, "url": source_post_url})
                        first_line = final_text.splitlines()[0].replace("<b>", "").replace("</b>", "").replace("📌 ", "")
                        last_processed_news_log.append(f"[{channel}] {first_line}")
                        if len(last_processed_news_log) > 5:
                            last_processed_news_log.pop(0)
                else:
                    seen_post_ids.add(post_id)
        else:
            channel_health_status[channel] = "🟡 بدون پست جدید"
    except Exception:
        channel_health_status[channel] = "🔴 خطای شبکه"

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
    port = int(os.environ.get("PORT", 10000))
    health_url = f"http://127.0.0.1:{port}/health"
    print("🌐 Auto self-ping mechanism started...")
    
    while True:
        try:
            http_session.get(health_url, timeout=5)
        except Exception:
            pass
        time.sleep(240)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    bot_thread = threading.Thread(target=fast_panel_listener, daemon=True)
    bot_thread.start()

    news_thread = threading.Thread(target=fetch_news_loop, daemon=True)
    news_thread.start()

    queue_thread = threading.Thread(target=queue_worker, daemon=True)
    queue_thread.start()

    ping_thread = threading.Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    print("🚀 Server starting on port:", port)
    app.run(host='0.0.0.0', port=port)
