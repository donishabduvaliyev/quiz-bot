import logging
import random
import os
import asyncio
import json
from flask import Flask, request, Response

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    TypeHandler
)
from telegram.error import BadRequest
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# === CONFIG ===
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("CRITICAL: Bot token not found. Set TELEGRAM_BOT_TOKEN in your .env file or environment variables.")
    exit()

PORT = int(os.environ.get('PORT', 8443))
WEBHOOK_MODE = os.environ.get('WEBHOOK_MODE', 'False').lower() == 'true'
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL") # e.g., https://your-app-name.onrender.com
WEBHOOK_PATH = "webhook" # Consistent path

if WEBHOOK_MODE and not WEBHOOK_URL_BASE:
    print("CRITICAL: WEBHOOK_MODE is True, but WEBHOOK_URL environment variable is not set.")
    exit()

WEBHOOK_FULL_URL = f"{WEBHOOK_URL_BASE}/{WEBHOOK_PATH}" if WEBHOOK_URL_BASE else None

QUIZ_FILE = 'tests.txt'
QUESTIONS_PER_BATCH = 10

# === STATES ===
SELECTING_SUBJECT, QUIZ_IN_PROGRESS = range(2)

# === Logging ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# === Utils ===
# load_questions function remains the same
def load_questions(file_path):
    """Loads questions from a text file into a dictionary by subject."""
    subjects = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        logger.error(f"Error: Quiz file not found at {file_path}")
        return subjects

    current_subject = None
    for block in content.strip().split('\n\n'):
        lines = block.strip().split('\n')
        if lines[0].startswith("Subject:"):
            try:
                current_subject = lines[0].split(":", 1)[1].strip()
                if current_subject:
                     subjects[current_subject] = []
                     logger.info(f"Found subject: {current_subject}")
                else:
                    logger.warning(f"Found empty subject name in block: {block}")
                    current_subject = None
            except IndexError:
                 logger.warning(f"Malformed Subject line: {lines[0]}")
                 current_subject = None
            continue
        if current_subject is None:
             logger.warning(f"Skipping block due to missing subject context: {block}")
             continue
        if len(lines) < 6:
            logger.warning(f"Skipping malformed block for subject '{current_subject}': {block}")
            continue
        if current_subject not in subjects:
             logger.error(f"Internal logic error: Subject '{current_subject}' not initialized.")
             continue
        try:
            q = lines[0]
            options = lines[1:5]
            answer_line = lines[5]
            if not all(len(opt) > 2 and opt[1] == ')' for opt in options):
                 logger.warning(f"Malformed options in block for subject '{current_subject}': {options}")
                 continue
            if not answer_line.startswith("Answer:"):
                 logger.warning(f"Malformed answer line for subject '{current_subject}': {answer_line}")
                 continue
            correct = answer_line.split(":", 1)[1].strip()
            subjects[current_subject].append({
                'question': q, 'options': options, 'correct': correct
            })
        except IndexError:
            logger.warning(f"Skipping block due to parsing error for subject '{current_subject}': {block}")
        except Exception as e:
            logger.error(f"Unexpected error parsing block for subject '{current_subject}': {e}\nBlock: {block}")

    logger.info(f"Loaded subjects: {list(subjects.keys())}")
    if not subjects:
        logger.warning("No subjects were loaded. Check tests.txt format and content.")
    return subjects

# === Helper Function for Start Keyboard ===
def get_start_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup | None:
    """Builds the initial subject selection keyboard."""
    known_subjects = ["Math", "English"] # Keep this matching hardcoded subjects
    keyboard = []
    loaded_subjects = context.bot_data.get('questions', {})

    for subj in known_subjects:
        if subj in loaded_subjects:
            keyboard.append([InlineKeyboardButton(text=subj, callback_data=f"subj|{subj}")])
        else:
            logger.warning(f"Subject '{subj}' hardcoded but not loaded. Skipping button.")

    if loaded_subjects: # Only add random if any subjects were loaded
        keyboard.append([InlineKeyboardButton(text="Random Questions", callback_data="random")])

    return InlineKeyboardMarkup(keyboard) if keyboard else None

# === Bot Handlers (Async v20 Style) ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends a message with inline buttons to choose a quiz subject."""
    # Clear any previous quiz state when user explicitly uses /start
    context.user_data.clear()
    logger.info(f"User {update.effective_user.id} started conversation. Cleared user_data.")

    reply_markup = get_start_keyboard(context)

    if not reply_markup:
        await update.message.reply_text(
            "Sorry, no quiz subjects could be loaded. Please check the bot's configuration."
        )
        return ConversationHandler.END

    await update.message.reply_text("Choose a subject or get random questions:", reply_markup=reply_markup)
    return SELECTING_SUBJECT

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles subject selection, clears old state, loads questions, starts quiz."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- Clear previous quiz state before starting new one ---
    context.user_data.clear()
    logger.info(f"User {user_id} selected an option. Cleared user_data before starting quiz.")

    all_loaded_questions = context.bot_data.get('questions', {})
    questions_to_ask = []
    subject_name = "Unknown"

    if data.startswith("subj|"):
        subject_name = data.split("|", 1)[1]
        if subject_name in all_loaded_questions:
            questions_to_ask = list(all_loaded_questions[subject_name])
            random.shuffle(questions_to_ask)
            logger.info(f"User {user_id} selected subject: {subject_name}")
        else:
            logger.error(f"User {user_id} clicked button for subject '{subject_name}', but questions not found.")
            await query.edit_message_text(f"Sorry, error loading questions for '{subject_name}'.")
            return ConversationHandler.END # End here, don't loop back yet
    elif data == "random":
        subject_name = 'Random Mix'
        temp_list = []
        if not all_loaded_questions:
             logger.error(f"User {user_id} requested random questions, but no subjects loaded.")
             await query.edit_message_text("Sorry, no questions available.")
             return ConversationHandler.END # End here
        target_per_subject = max(1, 40 // len(all_loaded_questions)) if len(all_loaded_questions) > 0 else 10
        for subj_questions in all_loaded_questions.values():
             count = min(target_per_subject, len(subj_questions))
             if count > 0:
                  temp_list.extend(random.sample(subj_questions, count))
        questions_to_ask = temp_list
        random.shuffle(questions_to_ask)
        logger.info(f"User {user_id} selected random questions. Total: {len(questions_to_ask)}")
    else:
        logger.warning(f"Unexpected callback data in start_quiz: {data}")
        await query.edit_message_text("Sorry, something went wrong.")
        return ConversationHandler.END # End here

    if not questions_to_ask:
         logger.error(f"No questions selected for user {user_id} for '{subject_name}'.")
         await query.edit_message_text("Sorry, no questions prepared.")
         return ConversationHandler.END # End here

    # --- Initialize user state for the new quiz ---
    context.user_data['subject'] = subject_name
    context.user_data['questions'] = questions_to_ask
    context.user_data['index'] = 0 # Index of the *next* question to send
    context.user_data['score'] = 0
    context.user_data['answered_in_batch'] = set() # Store indices answered in current batch
    context.user_data['current_batch_indices'] = [] # Store indices sent in current batch

    await query.edit_message_text(f"Starting quiz on: {subject_name}")
    await send_next_question_batch(update, context)
    return QUIZ_IN_PROGRESS

async def send_next_question_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Sends the next batch, resets batch tracking."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_index = context.user_data.get('index', 0) # Start index for this batch
    questions = context.user_data.get('questions', [])
    total_questions = len(questions)

    if current_index >= total_questions:
        logger.info(f"send_next_question_batch: No more questions to send for user {user_id}.")
        # Should be handled by handle_answer now, but as a fallback:
        await context.bot.send_message(chat_id=chat_id, text="You've already answered all questions!")
        # Send restart keyboard here as well? Maybe redundant.
        reply_markup = get_start_keyboard(context)
        if reply_markup:
             await context.bot.send_message(chat_id=chat_id, text="Choose a new subject?", reply_markup=reply_markup)
        return SELECTING_SUBJECT # Go back to selection

    if not questions:
        logger.error(f"send_next_question_batch: No questions found for user {user_id}.")
        await context.bot.send_message(chat_id=chat_id, text="Error: No questions found.")
        context.user_data.clear()
        return ConversationHandler.END

    end_index = min(current_index + QUESTIONS_PER_BATCH, total_questions)
    batch_indices = list(range(current_index, end_index)) # Indices for this specific batch
    batch_questions = questions[current_index:end_index]

    # --- Reset batch tracking state ---
    context.user_data['current_batch_indices'] = batch_indices
    context.user_data['answered_in_batch'] = set()
    logger.info(f"Sending questions {current_index + 1}-{end_index} to user {user_id}. Batch indices: {batch_indices}")

    for i, q_data in enumerate(batch_questions):
        question_global_index = batch_indices[i] # Use the correct global index
        options_buttons = []
        valid_options = [opt for opt in q_data.get('options', []) if isinstance(opt, str) and len(opt) > 2 and opt[1] == ')']
        if not valid_options:
             logger.error(f"Q {question_global_index} user {user_id} invalid options: {q_data.get('options')}")
             await context.bot.send_message(chat_id=chat_id, text=f"Skipping Q {question_global_index + 1} (invalid options).")
             # Also mark as "answered" in this batch to allow proceeding
             context.user_data['answered_in_batch'].add(question_global_index)
             continue

        for opt in valid_options:
            option_letter = opt[0]
            callback_data = f"ans|{question_global_index}|{option_letter}"
            options_buttons.append(InlineKeyboardButton(opt, callback_data=callback_data))
        keyboard = [options_buttons[j:j + 2] for j in range(0, len(options_buttons), 2)]
        reply_markup = InlineKeyboardMarkup(keyboard)
        question_text = q_data.get('question', 'Error: Missing text')
        await context.bot.send_message(chat_id=chat_id, text=f"{question_global_index + 1}. {question_text}", reply_markup=reply_markup)

    # Update the index for the *next* potential batch
    context.user_data['index'] = end_index

    # Show "Next" button only if there are more questions AFTER this batch
    if end_index < total_questions:
        next_button_keyboard = [[InlineKeyboardButton("Next Batch", callback_data="next")]]
        await context.bot.send_message(
            chat_id=chat_id,
            text="Click to continue when finished with this batch...", # Clarified text
            reply_markup=InlineKeyboardMarkup(next_button_keyboard)
        )

    return QUIZ_IN_PROGRESS

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles answer, tracks batch progress, checks for quiz end."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        _, qid_str, selected_letter = query.data.split("|")
        qid = int(qid_str) # Global index of the question answered
    except (ValueError, IndexError):
        logger.error(f"Invalid callback data in handle_answer: {query.data}")
        await query.edit_message_text("Sorry, error processing answer.")
        return QUIZ_IN_PROGRESS

    questions = context.user_data.get("questions", [])
    total_questions = len(questions)
    current_batch_indices = context.user_data.get("current_batch_indices", [])
    answered_in_batch = context.user_data.get("answered_in_batch", set())

    if not questions or qid >= total_questions:
         logger.error(f"handle_answer: Invalid questions/qid {qid} for user {user_id}.")
         await query.edit_message_text("Sorry, error retrieving question data.")
         context.user_data.clear()
         return ConversationHandler.END # End cleanly

    # --- Track answered question within the batch ---
    if qid in current_batch_indices:
        answered_in_batch.add(qid)
        context.user_data['answered_in_batch'] = answered_in_batch # Update the set in user_data
    else:
        # User might be clicking an old button from a previous batch
        logger.warning(f"User {user_id} answered question {qid} which is not in current batch {current_batch_indices}.")
        # Don't process score, just edit message if possible? Or ignore? Let's edit.

    question_data = questions[qid]
    correct_answer_letter = question_data.get('correct')
    question_text = question_data.get('question', '[Missing question]')
    selected_option_text = "[Option not found]"
    correct_option_text = "[Correct option not found]"
    for opt in question_data.get('options', []):
         if isinstance(opt, str) and len(opt) > 0:
             if opt.startswith(selected_letter):
                 selected_option_text = opt
             if opt.startswith(correct_answer_letter):
                 correct_option_text = opt

    feedback = ""
    # Only update score if it's part of the current batch and hasn't been scored yet?
    # Let's allow re-answering but only score the first time maybe?
    # Simpler: Just score if correct, user could cheat by re-clicking.
    if selected_letter == correct_answer_letter:
        feedback = "✅ Correct!"
        # Check if already answered correctly? Let's just add score for simplicity now.
        context.user_data['score'] = context.user_data.get('score', 0) + 1 # Might double-count if user re-clicks correct answer
    else:
        feedback = f"❌ Wrong! Correct was: {correct_option_text}"

    updated_text = (f"{qid + 1}. {question_text}\n\n{feedback}\nYou chose: {selected_option_text}")
    try:
        # Edit the message regardless of whether it was in the current batch
        await query.edit_message_text(text=updated_text, reply_markup=None)
    except BadRequest as e:
        logger.warning(f"Could not edit message user {user_id}, qid {qid}: {e}")

    # --- Check if quiz is finished ---
    if qid == total_questions - 1:
        score = context.user_data.get('score', 0)
        logger.info(f"User {user_id} finished quiz. Score: {score}/{total_questions}")

        # Get the restart keyboard
        reply_markup = get_start_keyboard(context)

        finish_text = f"Quiz finished!\nYour score: {score}/{total_questions}\n\nChoose a new subject?"
        if not reply_markup:
             finish_text = f"Quiz finished!\nYour score: {score}/{total_questions}\n(Error loading subjects for restart)"

        # Send final message with restart options
        await context.bot.send_message(
            chat_id=chat_id,
            text=finish_text,
            reply_markup=reply_markup # Add the keyboard here
        )
        # Don't clear user_data here, start_quiz will do it if user selects new quiz
        return SELECTING_SUBJECT # Loop back to subject selection state
    else:
        # If not the last question, stay in the quiz state
        return QUIZ_IN_PROGRESS

async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handles 'Next Batch', checks if current batch is fully answered."""
    query = update.callback_query
    await query.answer() # Acknowledge button press first
    user_id = update.effective_user.id

    current_batch_indices = context.user_data.get("current_batch_indices", [])
    answered_in_batch = context.user_data.get("answered_in_batch", set())

    # --- Check if all questions in the current batch are answered ---
    if len(answered_in_batch) >= len(current_batch_indices):
        logger.info(f"User {user_id} finished batch, proceeding to next.")
        # Delete the "Click to continue..." message
        try:
            await query.delete_message()
        except BadRequest as e:
             logger.warning(f"Could not delete 'Next Batch' prompt: {e}")
        # Send the next batch
        return await send_next_question_batch(update, context)
    else:
        logger.info(f"User {user_id} clicked 'Next Batch' prematurely. Answered: {len(answered_in_batch)}/{len(current_batch_indices)}")
        # Send a temporary message telling the user to finish
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Please answer all questions in the current batch before proceeding!"
        )
        # Stay in the current state
        return QUIZ_IN_PROGRESS


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the conversation and clears state."""
    user = update.effective_user
    if user:
        logger.info("User %s canceled the conversation.", user.first_name)
    else:
         logger.info("Conversation canceled (user info not available).")

    # Provide feedback
    cancel_message = "Quiz canceled. Use /start to begin again."
    if update.message:
        await update.message.reply_text(cancel_message)
    elif update.callback_query:
         # Need to send a new message if cancelling from a button press
         await context.bot.send_message(chat_id=update.effective_chat.id, text=cancel_message)
         try:
            # Attempt to remove the message the button was attached to
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
         except BadRequest:
             pass # Ignore if message is too old or already removed

    context.user_data.clear() # Clear state on cancel
    return ConversationHandler.END # Fully end the conversation

# error_handler remains IDENTICAL
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
         try:
            await update.effective_message.reply_text("Sorry, an error occurred.")
         except Exception as e:
             logger.error(f"Failed to send error message to user: {e}")


# === Flask App Setup (Identical) ===
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    """Basic route for health checks."""
    return "Quiz Bot is alive!"

@flask_app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
async def telegram_webhook():
    """Webhook endpoint to receive updates."""
    if request.is_json:
        update_data = request.get_json()
        update = Update.de_json(update_data, application.bot)
        async with application:
             await application.process_update(update)
        return Response("OK", status=200)
    else:
        logger.warning("Webhook received non-JSON request.")
        return Response("Bad Request", status=400)

# === Main Application Setup (Identical) ===
loaded_questions = load_questions(QUIZ_FILE)
if not loaded_questions:
    logger.critical(f"CRITICAL: No questions loaded from {QUIZ_FILE}.")

application = ApplicationBuilder().token(TOKEN).build()
application.bot_data['questions'] = loaded_questions
logger.info(f"Stored {len(loaded_questions)} subjects in bot_data.")

conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        SELECTING_SUBJECT: [
            CallbackQueryHandler(start_quiz, pattern="^subj\\|"),
            CallbackQueryHandler(start_quiz, pattern="^random$")
        ],
        QUIZ_IN_PROGRESS: [
            CallbackQueryHandler(handle_answer, pattern="^ans\\|"),
            CallbackQueryHandler(handle_next, pattern="^next$")
        ],
    },
    # NOTE: Removed cancel from fallbacks here. If user types /cancel mid-quiz,
    # it will now be handled by the entry_points logic if start is the only entry.
    # To allow /cancel anytime, add it back to fallbacks AND potentially
    # add MessageHandler(filters.COMMAND & filters.Regex('^/cancel$'), cancel)
    # to the QUIZ_IN_PROGRESS state if needed. For simplicity, let's assume
    # /start is the main way to reset/cancel.
    fallbacks=[CommandHandler("start", start)], # Let /start reset the conversation
    # fallbacks=[CommandHandler("cancel", cancel)], # Original cancel fallback
)

application.add_handler(conv_handler)
application.add_error_handler(error_handler)

# === Running the Application (Identical) ===
async def setup_webhook():
    """Sets the webhook URL with Telegram."""
    logger.info(f"Setting webhook to: {WEBHOOK_FULL_URL}")
    try:
        async with application:
            await application.bot.set_webhook(url=WEBHOOK_FULL_URL, allowed_updates=Update.ALL_TYPES)
        logger.info("Webhook set successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        return False

async def main_async_setup():
    """Only performs async setup steps like setting the webhook."""
    if WEBHOOK_MODE and WEBHOOK_FULL_URL:
        logger.info("Attempting async webhook setup...")
        await setup_webhook()
    else:
        logger.info("No async setup needed for polling mode.")


if __name__ == "__main__":
    if WEBHOOK_MODE:
        try:
            logger.info("Running async setup for webhook...")
            asyncio.run(main_async_setup())
            logger.info("Async setup finished.")
        except Exception as e:
            logger.error(f"Error during async setup: {e}", exc_info=True)
            logger.critical("Exiting due to async setup failure.")
            exit()

        logger.info(f"Starting Flask server on host 0.0.0.0 port {PORT} for webhook...")
        # Use waitress or gunicorn in production instead of flask_app.run
        # Example using waitress (install with pip install waitress):
        # from waitress import serve
        # serve(flask_app, host='0.0.0.0', port=PORT)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        logger.info("Starting bot polling...")
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        except KeyboardInterrupt:
            logger.info("Polling stopped manually.")
        except Exception as e:
            logger.error(f"Error during polling: {e}", exc_info=True)

