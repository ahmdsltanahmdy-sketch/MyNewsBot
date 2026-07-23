import time
import re
import os
import requests
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
import google.generativeai as genai
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

# مقداردهی مستقیم برای تست نهایی
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8903869878:AAGWo00OXfJYszdgJ-L4odB2d5Ug4phJK0I")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6LoUe_micSnpWDgAzhuJU4UPWaBISmYXgq7KH2jZ7ZW1A")
MY_CHANNEL = "@Rallyir"

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

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

recent_news_summaries = []
user_states = {}
selected_channels_for_bulk_delete = {}
db_lock = threading.Lock()

def load_config():
    default_config = {
        "bot_active": True,
        "channel_signature": f"شناسه: {MY_CHANNEL}",
        "blacklist": ["تبلیغات", "تخفیف ویژه"],
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
        with open(DB_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen_post(post_id):
    with db_lock:
        with open(DB_FILE, "a") as f:
            f.write(f"{post_id}\n")

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            channels = [line.strip() for line in f if line.strip()]
            if channels:
                return channels[:20]
    return DEFAULT_CHANNELS.copy()

def save_channels(channels):
    with open(CHANNELS_FILE, "w") as f:
        for ch in channels[:20]:
            f.write(f"{ch}\n")

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

def sanitize_all_links(text):
    if not text:
        return ""
    text = re.sub(r'<a\s+[^>]*href=["\'][^"\']*["\'][^>]*>(.*?)</a>', r'\1', text, flags=re.IGNORECASE)
    text = re.sub(r'<a\b[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</a>', '', text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+|www\.\S+|t\.me/\S+", "", text)
    text = re.sub(r"@[A-Za-z0-9_]+", "", text)
    text = remove_emojis(text)
    return text.strip()

def check_blacklist(text):
    for word in config_db.get("blacklist", []):
        if word and word.lower() in text.lower():
            return True
    return False

def clean_fallback(text):
    clean_t = sanitize_all_links(text)
    lines = [re.sub(r"[ \t]+", " ", l.strip()) for l in clean_t.splitlines() if l.strip()]
    if not lines:
        return ""
    lines[0] = f"<b>{lines[0]}</b>"
    sig = config_db.get("channel_signature", f"شناسه: {MY_CHANNEL}")
    return "\n\n".join(lines) + f"\n\n#خبر\n\n{sig}"

def rewrite_with_ai(raw_text):
    global recent_news_summaries

    if not raw_text or len(raw_text.strip()) < 15 or check_blacklist(raw_text):
        return None

    recent_context = "\n---\n".join(recent_news_summaries[-15:]) if recent_news_summaries else "هیچ خبری ثبت نشده است."

    prompt = (
        "تو یک ویرایشگر خبر حرفه‌ای هستی. وظیفه داری متن زیر را بازنویسی کنی.\n\n"
        "قوانین مهم:\n"
        "۱. ابتدا بررسی کن آیا این خبر موضوع کاملاً مشابهی با یکی از «اخبار اخیر» زیر دارد یا خیر.\n"
        "   اگر تکراری است، فقط کلمه 'DUPLICATE' را بنویس.\n"
        "۲. اگر خبر کلاً تبلیغاتی است، فقط کلمه 'SKIP' را بنویس.\n"
        "۳. اگر خبر جدید است، آن را بازنویسی کن:\n"
        "   - تمام لینک‌ها (http/t.me)، کلمات هایپرشده و آیدی‌ها (@) را حتماً حذف کن.\n"
        "   - خط اول را یک تیتر دقیق و بدون ایموجی قرار بده.\n"
        "   - از هیچ ایموجی و نمادی در هیچ جای متن استفاده نکن.\n"
        "۴. تعیین دقیق هشتگ‌ها: در انتهای متن، بین ۲ تا ۴ هشتگ تخصصی بگذار.\n\n"
        f"اخبار اخیر منتشر شده:\n{recent_context}\n\n"
        f"متن خبر جدید برای بررسی:\n{raw_text}"
    )

    for model_name in ['gemini-1.5-flash', 'gemini-pro']:
        try:
            ai_model = genai.GenerativeModel(model_name)
            response = ai_model.generate_content(prompt, request_options={"timeout": 4})
            result = response.text.strip()
            
            if "DUPLICATE" in result or "SKIP" in result:
                return None

            result = sanitize_all_links(result)
            lines = [line.strip() for line in result.splitlines() if line.strip()]
            if not lines:
                return None

            headline = lines[0].replace("<b>", "").replace("</b>", "")
            lines[0] = f"<b>{headline}</b>"
            formatted_body = "\n\n".join(lines)

            recent_news_summaries.append(raw_text[:100])
            if len(recent_news_summaries) > 30:
                recent_news_summaries.pop(0)

            sig = config_db.get("channel_signature", f"شناسه: {MY_CHANNEL}")
            return formatted_body + f"\n\n{sig}"
        except Exception:
            continue

    return clean_fallback(raw_text)

def send_telegram_post(text, source_url=None):
    keyboard = []
    links_row = []
    if source_url:
        links_row.append({"text": "منبع خبر", "url": source_url})
    links_row.append({"text": "عضویت در کانال", "url": f"https://t.me/{MY_CHANNEL.replace('@', '')}"})
    keyboard.append(links_row)
    
    send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": MY_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard}
    }
    try:
        res = http_session.post(send_url, json=payload, timeout=4)
        return res.status_code == 200
    except Exception:
        return False

def get_main_panel_keyboard():
    status_text = "خاموش کردن ربات" if config_db.get("bot_active", True) else "روشن کردن ربات"
    interval = config_db.get("check_interval", 10)
    return {
        "inline_keyboard": [
            [
                {"text": status_text, "callback_data": "toggle_bot_status"},
                {"text": "لیست کانال‌ها", "callback_data": "panel_list"}
            ],
            [
                {"text": "افزودن کانال", "callback_data": "panel_add_prompt"},
                {"text": "حذف تکی کانال", "callback_data": "panel_delete_menu"}
            ],
            [
                {"text": "حذف دسته‌ای کانال‌ها", "callback_data": "panel_bulk_delete_menu"},
                {"text": "ویرایش امضای کانال", "callback_data": "panel_sig_prompt"}
            ],
            [
                {"text": f"فاصله بررسی: {interval} ثانیه", "callback_data": "panel_interval_menu"},
                {"text": "مدیریت کلمات کلیدی", "callback_data": "panel_blacklist_menu"}
            ],
            [
                {"text": "ارسال پست دستی", "callback_data": "panel_force_post_prompt"},
                {"text": "آمار و وضعیت", "callback_data": "panel_status"}
            ]
        ]
    }

def get_bulk_delete_keyboard(chat_id):
    keyboard = []
    selected = selected_channels_for_bulk_delete.get(chat_id, set())
    for ch in list(target_channels):
        icon = "[X]" if ch in selected else "[ ]"
        keyboard.append([{"text": f"{icon} {ch}", "callback_data": f"toggle_bulk_{ch}"}])
    count = len(selected)
    confirm_text = f"تأیید و حذف موارد انتخاب‌شده ({count})" if count > 0 else "هیچ کانالی انتخاب نشده"
    keyboard.append([{"text": confirm_text, "callback_data": "confirm_bulk_delete"}])
    keyboard.append([{"text": "انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_delete_channels_keyboard():
    keyboard = []
    for ch in list(target_channels):
        keyboard.append([{"text": f"حذف {ch}", "callback_data": f"del_ch_{ch}"}])
    keyboard.append([{"text": "انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_blacklist_keyboard():
    keyboard = []
    keyboard.append([{"text": "افزودن کلمه جدید", "callback_data": "add_bl_word_prompt"}])
    bl = config_db.get("blacklist", [])
    for idx, word in enumerate(bl):
        keyboard.append([{"text": f"حذف «{word}»", "callback_data": f"del_bl_idx_{idx}"}])
    keyboard.append([{"text": "انصراف و بازگشت", "callback_data": "cancel_action"}])
    return {"inline_keyboard": keyboard}

def get_interval_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "۲ ثانیه", "callback_data": "set_interval_2"},
                {"text": "۵ ثانیه", "callback_data": "set_interval_5"}
            ],
            [
                {"text": "۱۰ ثانیه (توصیه‌شده)", "callback_data": "set_interval_10"},
                {"text": "۳۰ ثانیه", "callback_data": "set_interval_30"}
            ],
            [
                {"text": "انصراف و بازگشت", "callback_data": "cancel_action"}
            ]
        ]
    }

def get_cancel_keyboard():
    return {"inline_keyboard": [[{"text": "لغو عملیات", "callback_data": "cancel_action"}]]}

def fast_panel_listener():
    global last_update_id, target_channels, user_states, config_db, selected_channels_for_bulk_delete
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=1"
            res = http_session.get(url, timeout=3).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    last_update_id = update["update_id"]
                    
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb.get("id")
                        action = cb.get("data")
                        msg = cb.get("message", {})
                        chat_id = msg.get("chat", {}).get("id")

                        http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=2)

                        if action in ["panel_main", "cancel_action"]:
                            user_states[chat_id] = None
                            selected_channels_for_bulk_delete[chat_id] = set()
                            st_text = "فعال" if config_db.get("bot_active", True) else "غیرفعال"
                            reply = "عملیات لغو شد." if action == "cancel_action" else f"پنل مدیریت ربات خبری\n\nوضعیت جمع‌آوری اخبار: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "toggle_bot_status":
                            config_db["bot_active"] = not config_db.get("bot_active", True)
                            save_config(config_db)
                            st_text = "روشن و فعال شد" if config_db["bot_active"] else "متوقف شد"
                            reply = f"دستور اجرا شد. وضعیت ربات: {st_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_list":
                            reply = f"لیست کانال‌های مبدا ({len(target_channels)}/20):\n\n"
                            reply += "\n".join([f"{idx}. {c}" for idx, c in enumerate(target_channels, 1)])
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_bulk_delete_menu":
                            selected_channels_for_bulk_delete[chat_id] = set()
                            reply = "حذف دسته‌ای کانال‌ها\n\nکانال‌های موردنظر را انتخاب و سپس حذف کنید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_bulk_delete_keyboard(chat_id)
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
                                reply = "هیچ کانالی انتخاب نشده است."
                            else:
                                removed_list = list(selected)
                                for ch in removed_list:
                                    if ch in target_channels:
                                        target_channels.remove(ch)
                                save_channels(target_channels)
                                selected_channels_for_bulk_delete[chat_id] = set()
                                reply = f"تعداد {len(removed_list)} کانال حذف شدند."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_add_prompt":
                            if len(target_channels) >= 20:
                                reply = "ظرفیت تکمیل است (حداکثر ۲۰ کانال)! ابتدا یک کانال را حذف کنید."
                                http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                    "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                                }, timeout=3)
                            else:
                                user_states[chat_id] = "WAITING_FOR_CHANNEL_NAME"
                                reply = "لطفاً آیدی کانال جدید را بفرستید:"
                                http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                    "chat_id": chat_id, "text": reply, "reply_markup": get_cancel_keyboard()
                                }, timeout=3)

                        elif action == "panel_delete_menu":
                            reply = "روی کانال موردنظر جهت حذف کلیک کنید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_delete_channels_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_ch_"):
                            ch_to_del = action.replace("del_ch_", "")
                            if ch_to_del in target_channels:
                                target_channels.remove(ch_to_del)
                                save_channels(target_channels)
                                reply = f"کانال {ch_to_del} حذف شد."
                            else:
                                reply = "این کانال قبلاً حذف شده است."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_interval_menu":
                            reply = f"فاصله بررسی فعلی: {config_db.get('check_interval', 10)} ثانیه"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_interval_keyboard()
                            }, timeout=3)

                        elif action.startswith("set_interval_"):
                            sec = int(action.replace("set_interval_", ""))
                            config_db["check_interval"] = sec
                            save_config(config_db)
                            reply = f"فاصله زمانی روی {sec} ثانیه تنظیم شد."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_sig_prompt":
                            user_states[chat_id] = "WAITING_FOR_SIGNATURE"
                            curr_sig = config_db.get("channel_signature", f"شناسه: {MY_CHANNEL}")
                            reply = f"امضای فعلی:\n{curr_sig}\n\nمتن امضای جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_blacklist_menu":
                            bl = config_db.get("blacklist", [])
                            bl_text = "\n".join([f"- {w}" for w in bl]) if bl else "هیچ کلمه‌ای فیلتر نشده است."
                            reply = f"کلمات فیلتر شده:\n\n{bl_text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif action == "add_bl_word_prompt":
                            user_states[chat_id] = "WAITING_FOR_BLACKLIST_WORD"
                            reply = "کلمه جدید را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action.startswith("del_bl_idx_"):
                            idx = int(action.replace("del_bl_idx_", ""))
                            bl = config_db.get("blacklist", [])
                            if 0 <= idx < len(bl):
                                removed_word = bl.pop(idx)
                                save_config(config_db)
                                reply = f"کلمه {removed_word} حذف شد."
                            else:
                                reply = "قبلاً حذف شده است."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif action == "panel_force_post_prompt":
                            user_states[chat_id] = "WAITING_FOR_FORCE_POST"
                            reply = "خبر دستی خود را بفرستید:"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_cancel_keyboard()
                            }, timeout=3)

                        elif action == "panel_status":
                            st_icon = "روشن" if config_db.get("bot_active", True) else "خاموش"
                            reply = (
                                f"گزارش و آمار ربات\n\n"
                                f"وضعیت: {st_icon}\n"
                                f"فاصله بررسی: {config_db.get('check_interval', 10)} ثانیه\n"
                                f"تعداد کانال‌ها: {len(target_channels)} از ۲۰\n"
                                f"اخبار ارسال‌شده: {len(seen_post_ids)}\n"
                                f"کلمات فیلتر: {len(config_db.get('blacklist', []))}"
                            )
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
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
                                "chat_id": chat_id, "text": "عملیات لغو شد.", "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
                            continue

                        state = user_states.get(chat_id)

                        if state == "WAITING_FOR_CHANNEL_NAME":
                            user_states[chat_id] = None
                            new_ch = text.replace("@", "").replace("https://t.me/", "").strip()
                            if len(target_channels) >= 20:
                                reply = "ظرفیت تکمیل است."
                            elif new_ch in target_channels:
                                reply = f"کانال {new_ch} قبلا وجود دارد."
                            elif new_ch:
                                target_channels.append(new_ch)
                                save_channels(target_channels)
                                reply = f"کانال {new_ch} اضافه شد."
                            else:
                                reply = "آیدی نامعتبر."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_SIGNATURE":
                            user_states[chat_id] = None
                            config_db["channel_signature"] = text
                            save_config(config_db)
                            reply = f"امضا ثبت شد:\n{text}"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_BLACKLIST_WORD":
                            user_states[chat_id] = None
                            word = text.strip()
                            if word and word not in config_db.get("blacklist", []):
                                config_db["blacklist"].append(word)
                                save_config(config_db)
                                reply = f"کلمه {word} اضافه شد."
                            else:
                                reply = "نامعتبر یا تکراری."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_blacklist_keyboard()
                            }, timeout=3)

                        elif state == "WAITING_FOR_FORCE_POST":
                            user_states[chat_id] = None
                            sig = config_db.get("channel_signature", f"شناسه: {MY_CHANNEL}")
                            clean_text = sanitize_all_links(text)
                            post_text = f"<b>{clean_text[:40]}</b>\n\n{clean_text}\n\n#خبر_فوری\n\n{sig}"
                            success = send_telegram_post(post_text)
                            reply = "پست ارسال شد." if success else "خطا در ارسال."
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)

                        elif text.lower() in ["/start", "start", "راهنما", "پنل"]:
                            user_states[chat_id] = None
                            st_text = "فعال" if config_db.get("bot_active", True) else "غیرفعال"
                            reply = f"پنل مدیریت ربات خبری\n\nوضعیت جمع‌آوری اخبار: {st_text}\nکانال‌ها: {len(target_channels)} از ۲۰"
                            http_session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": chat_id, "text": reply, "reply_markup": get_main_panel_keyboard()
                            }, timeout=3)
        except Exception:
            time.sleep(1)

def process_single_channel(channel):
    if not config_db.get("bot_active", True) or channel not in target_channels:
        return

    url = f"https://t.me/s/{channel}"
    try:
        res = http_session.get(url, timeout=3)
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

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

news_thread = threading.Thread(target=fetch_news_loop, daemon=True)
news_thread.start()

print("ربات خبری آماده به کار است.")
fast_panel_listener()
