import os
import logging
import traceback
import html
import json
import tempfile
import pydub
from pathlib import Path
from datetime import datetime

import telegram
from telegram import Update, User, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils


# setup
db = database.Database()
logger = logging.getLogger(__name__)

HELP_MESSAGE = """Commands:
⚪ /retry – Regenerate last bot answer
⚪ /new – Start new dialog
⚪ /mode – Select chat mode
⚪ /history – Show dialog History
⚪ /balance – Show balance
⚪ /help – Show help
"""


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)
    
    reply_text = "Hi! I'm <b>ChatGPT</b> bot implemented with GPT-3.5 OpenAI API 🤖\n\n"
    reply_text += HELP_MESSAGE

    reply_text += "\nAnd now... ask me anything!"
    
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def retry_handle(update: Update, context: CallbackContext, user_id=None):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = user_id or update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No message to retry 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False, user_id=user_id)


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True, user_id=None):
    # check if message is edited
    if getattr(update, "edited_message", None) is not None:
        await edited_message_handle(update, context)
        return
        
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = user_id or update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    message = message or update.message.text

    # new dialog timeout
    if use_new_dialog_timeout:
        if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
            if config.ask_on_timeout:
                await ask_timeout_handle(update, context, message)
                return
            else:
                db.start_new_dialog(user_id)
                await update.message.reply_text(f"Starting new dialog due to timeout (<b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # send typing action
    await update.message.chat.send_action(action="typing")

    try:
        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
        chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

        chatgpt_instance = openai_utils.ChatGPT(use_chatgpt_api=config.use_chatgpt_api)
        answer, n_used_tokens, n_first_dialog_messages_removed = await chatgpt_instance.send_message(
            message,
            dialog_messages=dialog_messages,
            chat_mode=chat_mode
        )

        # update user data
        new_dialog_message = {"user": message, "bot": answer, "date": datetime.now()}
        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))

    except Exception as e:
        error_text = f"Something went wrong during completion. Reason: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return

    # send message if some messages were removed from the context
    if n_first_dialog_messages_removed > 0:
        if n_first_dialog_messages_removed == 1:
            text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
        else:
            text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # split answer into multiple messages due to 4096 character limit
    for answer_chunk in split_text_into_chunks(answer, 4000):
        try:
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[openai_utils.CHAT_MODES[chat_mode]["parse_mode"]]
            
            await update.message.reply_text(answer_chunk, parse_mode=parse_mode)
        except telegram.error.BadRequest:
            # answer has invalid characters, so we send it without parse_mode
            await update.message.reply_text(answer_chunk)


async def voice_message_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    voice = update.message.voice
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        voice_ogg_path = tmp_dir / "voice.ogg"

        # download
        voice_file = await context.bot.get_file(voice.file_id)
        await voice_file.download_to_drive(voice_ogg_path)

        # convert to mp3
        voice_mp3_path = tmp_dir / "voice.mp3"
        pydub.AudioSegment.from_file(voice_ogg_path).export(voice_mp3_path, format="mp3")

        # transcribe
        with open(voice_mp3_path, "rb") as f:
            transcribed_text = await openai_utils.transcribe_audio(f)

    text = f"🎤: <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    await message_handle(update, context, message=transcribed_text)

    # calculate spent dollars
    n_spent_dollars = voice.duration * (config.whisper_price_per_1_min / 60)

    # normalize dollars to tokens (it's very convenient to measure everything in a single unit)
    price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
    n_used_tokens = int(n_spent_dollars / (price_per_1000_tokens / 1000))
    db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))


async def ask_timeout_handle(update: Update, context: CallbackContext, message):
    user_id = update.message.from_user.id

    keyboard = [[
        InlineKeyboardButton("✅ Yes, a new one!", callback_data=f"new_dialog|true"),
        InlineKeyboardButton("❌ No, continue.", callback_data=f"new_dialog|false"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    new_dialog_message = {"user": message, "date": datetime.now()}
    db.set_dialog_messages(
        user_id,
        db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
        dialog_id=None
    )

    await update.message.reply_text(f"Long time no see. Start new dialog?", reply_markup=reply_markup)


async def answer_timeout_handle(update: Update, context: CallbackContext):
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    new_dialog = query.data.split("|")[1]
    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await query.edit_message_text("I don't see any dialog. You are using a new one. 🙅‍♂️")
        return
    elif 'bot' in dialog_messages[-1]:  # already answered, do nothing
        await query.edit_message_text("I must have served you. 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    if new_dialog == "true":
        await query.edit_message_text("Sure, a new dialog. 🫡")
        await new_dialog_handle(update.callback_query, context, user_id)
        await message_handle(update.callback_query, context, message=last_dialog_message["user"], use_new_dialog_timeout=False, user_id=user_id)
    else:  # false
        await query.edit_message_text("Sure, let's continue. 🫡")
        await retry_handle(update.callback_query, context, user_id)


async def new_dialog_handle(update: Update, context: CallbackContext, user_id=None):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = user_id or update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    db.start_new_dialog(user_id)
    await update.message.reply_text("Starting new dialog ✅")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def history_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("Sorry, you don't ask me anything. 🤗")
    else:
        await update.message.reply_text("Sure, our dialog is as follows ⬇️")
        for msg in dialog_messages:
            await update.message.reply_text(
                f"🖐️ You: {msg['user']}", parse_mode=ParseMode.HTML)
            await update.message.reply_text(
                f"🤖 Me: {msg['bot']}", parse_mode=ParseMode.HTML)


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    cur_mode = db.get_user_attribute(user_id, "current_chat_mode")

    keyboard = []
    for chat_mode, chat_mode_dict in openai_utils.CHAT_MODES.items():
        keyboard.append([InlineKeyboardButton(chat_mode_dict["name"], callback_data=f"set_chat_mode|{chat_mode}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Your current chat mode is {openai_utils.CHAT_MODES[cur_mode]['name']}. "
        + "Select chat mode if you want to change:", reply_markup=reply_markup)


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await query.edit_message_text(
        f"<b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> chat mode is set",
        parse_mode=ParseMode.HTML
    )

    await query.edit_message_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    n_used_tokens = db.get_user_attribute(user_id, "n_used_tokens")

    price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
    n_spent_dollars = n_used_tokens * (price_per_1000_tokens / 1000)

    text = f"You spent <b>{n_spent_dollars:.03f}$</b>\n"
    text += f"You used <b>{n_used_tokens}</b> tokens\n\n"

    text += "🏷️ Prices\n"
    text += f"<i>- ChatGPT: {price_per_1000_tokens}$ per 1000 tokens\n"
    text += f"- Whisper (voice recognition): {config.whisper_price_per_1_min}$ per 1 minute</i>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    text = "🥲 Unfortunately, message <b>editing</b> is not supported"
    await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4000):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")

def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .build()
    )

    # add handlers
    if len(config.allowed_telegram_usernames) == 0:
        user_filter = filters.ALL
    else:
        user_filter = filters.User(username=config.allowed_telegram_usernames)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("history", history_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(answer_timeout_handle, pattern="^new_dialog"))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))
    
    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))
    
    application.add_error_handler(error_handle)
    
    # start the bot
    application.run_polling()


if __name__ == "__main__":
    run_bot()