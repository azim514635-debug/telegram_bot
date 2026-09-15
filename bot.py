import io
import re
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = "8938556104:AAG6JLCuhZOuyuGSFJ6y7nLSMGO80gDv2Sw"
bot = telebot.TeleBot(BOT_TOKEN)

user_data = {}

def get_ext_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(".py", callback_data="ext_py"),
        InlineKeyboardButton(".html", callback_data="ext_html"),
        InlineKeyboardButton(".js", callback_data="ext_js")
    )
    markup.row(
        InlineKeyboardButton(".css", callback_data="ext_css"),
        InlineKeyboardButton(".json", callback_data="ext_json"),
        InlineKeyboardButton(".txt", callback_data="ext_txt")
    )
    return markup

@bot.message_handler(commands=['start', 'cancel'])
def handle_start(message):
    if message.chat.id in user_data:
        del user_data[message.chat.id]
    bot.reply_to(message, "Send any code or text to convert it into a file.")

@bot.message_handler(func=lambda msg: msg.chat.id not in user_data)
def handle_code(message):
    code_text = message.text
    
    # Auto-detect extension from Markdown code blocks (e.g., ```python ... ```)
    match = re.match(r"^```(\w+)\n([\s\S]*)\n```$", code_text.strip())
    detected_ext = None
    
    if match:
        detected_ext = match.group(1).lower()
        code_text = match.group(2)
        if detected_ext == "python":
            detected_ext = "py"
        elif detected_ext == "javascript":
            detected_ext = "js"

    user_data[message.chat.id] = {
        "code": code_text,
        "step": "AWAITING_NAME",
        "ext": detected_ext
    }

    if detected_ext:
        bot.reply_to(
            message, 
            f"Detected <b>.{detected_ext}</b> code!\nEnter a filename (or send <b>/skip</b> to use 'file.{detected_ext}'):",
            parse_mode="HTML"
        )
    else:
        bot.reply_to(
            message, 
            "Choose an extension below or type a custom filename (e.g., <code>app.py</code>):",
            parse_mode="HTML",
            reply_markup=get_ext_keyboard()
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('ext_'))
def handle_extension_btn(call):
    chat_id = call.message.chat.id
    if chat_id not in user_data:
        bot.answer_callback_query(call.id, "Session expired. Send your code again.")
        return

    ext = call.data.split('_')[1]
    send_file(chat_id, user_data[chat_id]["code"], f"file.{ext}", call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['skip'])
def handle_skip(message):
    chat_id = message.chat.id
    if chat_id in user_data and user_data[chat_id]["ext"]:
        ext = user_data[chat_id]["ext"]
        send_file(chat_id, user_data[chat_id]["code"], f"file.{ext}", message.message_id)

@bot.message_handler(func=lambda msg: msg.chat.id in user_data)
def handle_filename(message):
    chat_id = message.chat.id
    filename = message.text.strip()
    
    if "." not in filename:
        ext = user_data[chat_id].get("ext") or "txt"
        filename = f"{filename}.{ext}"

    send_file(chat_id, user_data[chat_id]["code"], filename, message.message_id)

def send_file(chat_id, code_content, filename, reply_to_id):
    file_data = io.BytesIO(code_content.encode('utf-8'))
    file_data.name = filename

    bot.send_document(
        chat_id, 
        file_data, 
        reply_to_message_id=reply_to_id,
        caption=f"Generated file: <code>{filename}</code>",
        parse_mode="HTML"
    )
    user_data.pop(chat_id, None)

bot.infinity_polling()

