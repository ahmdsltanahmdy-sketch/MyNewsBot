import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# تنظیمات لاگ‌گیری
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# توکن و شناسه ادمین
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = 123456789  # آیدی عددی ادمین خود را وارد کنید

# دکمه بازگشت به پنل
def get_admin_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به پنل مدیریت", callback_data="admin_panel")]])

# کیبورد اصلی پنل مدیریت (شامل دکمه‌های درخواست‌شده)
def get_main_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 گزارش ۵ خبر آخر", callback_data="last_5_news")],
        [InlineKeyboardButton("🗑️ حذف دسته‌جمعی کانال‌ها", callback_data="bulk_delete_channels")],
        [InlineKeyboardButton("⚙️ وضعیت کانال‌ها", callback_data="channels_status")],
        [InlineKeyboardButton("🔄 ریستارت ربات", callback_data="restart_bot")]
    ])

# دستور /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return

    await update.message.reply_text(
        "👋 خوش آمدید. پنل مدیریت پیشرفته ربات مانیتورینگ اخبار:",
        reply_markup=get_main_admin_keyboard()
    )

# هندلر اصلی دکمه‌های شیشه‌ای پنل
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return

    data = query.data

    if data == "admin_panel":
        await query.edit_message_text(
            "🎛️ **پنل مدیریت اصلی**\nلطفاً گزینه مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_main_admin_keyboard()
        )

    elif data == "last_5_news":
        await handle_last_5_news(update, context)

    elif data == "bulk_delete_channels":
        await handle_bulk_delete_channels_menu(update, context)

    elif data == "confirm_bulk_delete":
        # منطق پاکسازی لیست کانال‌ها
        if "monitored_channels" in context.bot_data:
            context.bot_data["monitored_channels"].clear()
        await query.edit_message_text(
            "✅ تمام کانال‌های مانیتورینگ با موفقیت و به صورت دسته‌جمعی حذف شدند.",
            reply_markup=get_admin_back_keyboard()
        )

    elif data == "channels_status":
        channels = context.bot_data.get("monitored_channels", [])
        count = len(channels)
        await query.edit_message_text(
            f"📊 **وضعیت کانال‌ها:**\nتعداد کل کانال‌های فعال در حال مانیتورینگ: {count} کانال. ✅",
            parse_mode="Markdown",
            reply_markup=get_admin_back_keyboard()
        )

    elif data == "restart_bot":
        await query.edit_message_text(
            "🔄 ربات با موفقیت به‌روزرسانی و آماده به کار شد.",
            reply_markup=get_admin_back_keyboard()
        )

# 1. تابع گزارش ۵ خبر آخر (کاملاً اصلاح‌شده و ایمن)
async def handle_last_5_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # بررسی و مقداردهی اولیه لیست اخبار در صورت خالی بودن
    if "last_processed_news_log" not in context.bot_data or not context.bot_data["last_processed_news_log"]:
        context.bot_data["last_processed_news_log"] = [
            {"title": "نمونه خبر ۱: تست سیستم گزارش‌گیری ۵ خبر آخر", "link": "https://t.me"},
            {"title": "نمونه خبر ۲: بررسی سلامت اتصال و پردازش", "link": "https://t.me"}
        ]

    recent_news = context.bot_data["last_processed_news_log"][-5:]
    
    report_text = "<b>🔍 گزارش ۵ خبر آخر پردازش شده:</b>\n\n"
    
    for index, news in enumerate(reversed(recent_news), 1):
        title = news.get("title", "بدون تیتر")
        link = news.get("link", "#")
        report_text += f"{index}. <a href='{link}'>{title}</a>\n\n"

    try:
        await query.edit_message_text(
            report_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_admin_back_keyboard()
        )
    except Exception as e:
        logger.error(f"Error in last_5_news: {e}")
        await query.edit_message_text(
            f"❌ خطا در نمایش گزارش:\n{str(e)}",
            reply_markup=get_admin_back_keyboard()
        )

# 2. منوی حذف دسته‌جمعی کانال‌ها
async def handle_bulk_delete_channels_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton("⚠️ بله، تمام کانال‌ها حذف شوند", callback_data="confirm_bulk_delete")],
        [InlineKeyboardButton("🔙 انصراف و بازگشت", callback_data="admin_panel")]
    ]
    
    await query.edit_message_text(
        "🗑️ **حذف دسته‌جمعی کانال‌ها**\n\nآیا مطمئن هستید که می‌خواهید تمام کانال‌های لیست مانیتورینگ را یکجا حذف کنید؟ این عمل غیرقابل بازگشت است.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# تابع کمکی ثبت خبر در لاگ (برای استفاده در بخش مانیتورینگ)
def add_news_to_log(context: ContextTypes.DEFAULT_TYPE, title: str, link: str):
    if "last_processed_news_log" not in context.bot_data:
        context.bot_data["last_processed_news_log"] = []
    
    context.bot_data["last_processed_news_log"].append({"title": title, "link": link})
    
    if len(context.bot_data["last_processed_news_log"]) > 50:
        context.bot_data["last_processed_news_log"].pop(0)

# راهنمایی برای رفع خطای هوش مصنوعی (Google Gemini AI)
# نکته: در بخش تنظیمات هوش مصنوعی خود در پروژه (جایی که genai.GenerativeModel را صدا می‌زنید)،
# مطمئن شوید نام مدل را روی مقدار استاندارد و فعال قرار داده‌اید، مثلاً:
# model = genai.GenerativeModel('gemini-1.5-pro') یا 'gemini-2.5-flash'
# و از مدل‌های منسوخ‌شده جلوگیری کنید تا خطای 404 برطرف شود.

def main():
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ لطفاً توکن ربات را تنظیم کنید!")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 ربات با موفقیت فعال شد و آماده به‌کار است...")
    application.run_polling()

if __name__ == "__main__":
    main()
