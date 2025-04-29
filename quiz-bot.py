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
    TypeHandler # Needed for processing updates manually
)
from telegram.error import BadRequest
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# === CONFIG ===
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    # Fallback for local testing if .env is missing or variable not set
    TOKEN = 'YOUR_FALLBACK_TOKEN_HERE' # Replace if needed for local testing without .env
    if TOKEN == 'YOUR_FALLBACK_TOKEN_HERE':
        print("CRITICAL: Bot token not found. Set TELEGRAM_BOT_TOKEN in your .env file or environment variables.")
        exit()
    else:
        print("WARNING: Using fallback token. Set TELEGRAM_BOT_TOKEN in environment for deployment.")

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
# load_questions function remains the same as previous versions
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
    blocks = content.replace('\r\n', '\n').strip().split('\n\n')

    for block in blocks:
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
            logger.warning(f"Skipping malformed block (less than 6 lines) for subject '{current_subject}': {block}")
            continue
        if current_subject not in subjects:
             logger.error(f"Internal logic error: Subject '{current_subject}' not initialized before adding questions.")
             continue
        try:
            question_text = lines[0]
            options = lines[1:5]
            answer_line = lines[5]
            if not all(len(opt) > 2 and opt[1] == ')' and opt[0].isalpha() for opt in options):
                 logger.warning(f"Malformed options format in block for subject '{current_subject}': {options}")
                 continue
            if not answer_line.startswith("Answer:"):
                 logger.warning(f"Malformed answer line format for subject '{current_subject}': {answer_line}")
                 continue
            correct_answer_letter = answer_line.split(":", 1)[1].strip()
            if not correct_answer_letter or len(correct_answer_letter) != 1 or not correct_answer_letter.isalpha():
                 logger.warning(f"Invalid correct answer letter '{correct_answer_letter}' for subject '{current_subject}': {answer_line}")
                 continue
            subjects[current_subject].append({
                'question': question_text, 'options': options, 'correct': correct_answer_letter.upper()
            })
        except IndexError:
            logger.warning(f"Skipping block due to parsing error (IndexError) for subject '{current_subject}': {block}")
        except Exception as e:
            logger.error(f"Unexpected error parsing block for subject '{current_subject}': {e}\nBlock: {block}", exc_info=True)

    logger.info(f"Loaded subjects: {list(subjects.keys())}")
    if not subjects:
        logger.warning("No subjects were loaded. Check tests.txt format and content.")
    return subjects

# === Helper Function for Start Keyboard ===
# get_start_keyboard remains the same
def get_start_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup | None:
    """Builds the initial subject selection keyboard."""
    known_subjects = ["Math", "English"]
    keyboard = []
    loaded_subjects = context.bot_data.get('questions', {})
    for subj in known_subjects:
        if subj in loaded_subjects and loaded_subjects[subj]:
            keyboard.append([InlineKeyboardButton(text=subj, callback_data=f"subj|{subj}")])
        else:
            logger.warning(f"Subject '{subj}' hardcoded but not loaded or has no questions. Skipping button.")
    if any(loaded_subjects.values()):
        keyboard.append([InlineKeyboardButton(text="Random Questions", callback_data="random")])
    return InlineKeyboardMarkup(keyboard) if keyboard else None

# === Bot Handlers (Async v20 Style) ===
# start, start_quiz remain IDENTICAL to the previous version
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends the initial message and subject selection keyboard."""
    user = update.effective_user
    context.user_data.clear() # Clear previous state
    logger.info(f"User {user.id} ({user.first_name}) started conversation. Cleared user_data.")
    reply_markup = get_start_keyboard(context)
    if not reply_markup:
        await update.message.reply_text("Sorry, no quiz subjects could be loaded.")
        return ConversationHandler.END
    await update.message.reply_text("Choose a subject or get random questions:", reply_markup=reply_markup)
    return SELECTING_SUBJECT

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles subject selection, clears old state, loads questions, starts quiz."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    context.user_data.clear() # Clear previous state
    logger.info(f"User {user_id} selected an option '{data}'. Cleared user_data before starting quiz.")
    all_loaded_questions = context.bot_data.get('questions', {})
    questions_to_ask = []
    subject_name = "Unknown"

    if data.startswith("subj|"):
        subject_name = data.split("|", 1)[1]
        if subject_name in all_loaded_questions and all_loaded_questions[subject_name]:
            questions_to_ask = list(all_loaded_questions[subject_name])
            random.shuffle(questions_to_ask)
            logger.info(f"User {user_id} selected subject: {subject_name}, {len(questions_to_ask)} questions.")
        else:
            logger.error(f"User {user_id} clicked button for subject '{subject_name}', but questions not loaded/empty.")
            await query.edit_message_text(f"Sorry, error loading questions for '{subject_name}'.")
            return ConversationHandler.END
    elif data == "random":
        subject_name = 'Random Mix'
        temp_list = []
        if not any(all_loaded_questions.values()):
             logger.error(f"User {user_id} requested random questions, but no subjects/questions loaded.")
             await query.edit_message_text("Sorry, no questions available to randomize.")
             return ConversationHandler.END
        total_available = sum(len(qs) for qs in all_loaded_questions.values())
        target_total = min(40, total_available)
        for subj, subj_questions in all_loaded_questions.items():
            if not subj_questions: continue
            proportion = len(subj_questions) / total_available if total_available > 0 else 0
            count = max(1, round(target_total * proportion)) if total_available > 0 else min(10, len(subj_questions)) # Fallback count
            actual_count = min(count, len(subj_questions))
            if actual_count > 0:
                temp_list.extend(random.sample(subj_questions, actual_count))
        questions_to_ask = temp_list
        random.shuffle(questions_to_ask)
        logger.info(f"User {user_id} selected random questions. Prepared {len(questions_to_ask)} questions.")
    else:
        logger.warning(f"Received unexpected callback data in start_quiz state: {data}")
        await query.edit_message_text("Sorry, an unexpected error occurred.")
        return ConversationHandler.END

    if not questions_to_ask:
         logger.error(f"Failed to prepare any questions for user {user_id} for selection '{data}'.")
         await query.edit_message_text("Sorry, no questions could be prepared.")
         return ConversationHandler.END

    # Initialize user state (score, index, etc.)
    context.user_data['subject'] = subject_name
    context.user_data['questions'] = questions_to_ask
    context.user_data['index'] = 0
    context.user_data['score'] = 0
    # Note: Batch tracking ('answered_in_batch', 'current_batch_indices') was added in the *next* revision

    await query.edit_message_text(f"Starting quiz on: {subject_name}")
    await send_next_question_batch(update, context) # Send first batch
    return QUIZ_IN_PROGRESS

# --- THIS FUNCTION CONTAINS THE FIX FOR LONG OPTIONS ---
async def send_next_question_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Sends the next batch, including options in text, using letters on buttons."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_index = context.user_data.get('index', 0) # Start index for this batch
    questions = context.user_data.get('questions', [])
    total_questions = len(questions)

    # Check if quiz is already finished (should be handled by handle_answer ideally)
    if current_index >= total_questions:
        logger.info(f"send_next_question_batch: No more questions to send for user {user_id}.")
        # This case should ideally not be reached if handle_answer works correctly
        await context.bot.send_message(chat_id=chat_id, text="You have already finished the quiz!")
        reply_markup = get_start_keyboard(context)
        if reply_markup:
             await context.bot.send_message(chat_id=chat_id, text="Choose a new subject?", reply_markup=reply_markup)
        return SELECTING_SUBJECT # Go back to selection state

    if not questions:
        logger.error(f"send_next_question_batch: No questions found for user {user_id}.")
        await context.bot.send_message(chat_id=chat_id, text="Error: No questions found.")
        context.user_data.clear()
        return ConversationHandler.END

    # Determine questions for this batch
    end_index = min(current_index + QUESTIONS_PER_BATCH, total_questions)
    batch_questions = questions[current_index:end_index]
    logger.info(f"Sending questions {current_index + 1}-{end_index} to user {user_id}.")

    for i, q_data in enumerate(batch_questions):
        question_global_index = current_index + i # Calculate global index
        options_buttons = []
        options_text_parts = [] # To build the options text for the message

        # Validate options from the loaded data
        valid_options = [opt for opt in q_data.get('options', []) if isinstance(opt, str) and len(opt) > 2 and opt[1] == ')']
        if not valid_options:
             logger.error(f"Q {question_global_index} user {user_id} invalid options: {q_data.get('options')}")
             await context.bot.send_message(chat_id=chat_id, text=f"Skipping Q {question_global_index + 1} (invalid options).")
             continue # Skip this question

        # --- Prepare options text and buttons (CORE CHANGE) ---
        for opt in valid_options:
            option_letter = opt[0].upper() # A, B, C, D (ensure uppercase)
            # Add full option text (e.g., "A) Some long text") to the message body parts
            options_text_parts.append(f"{opt}")
            # Create button with just the letter as its text
            callback_data = f"ans|{question_global_index}|{option_letter}"
            options_buttons.append(InlineKeyboardButton(text=option_letter, callback_data=callback_data))

        # --- Construct message text ---
        question_text_body = q_data.get('question', 'Error: Missing question text')
        options_text_formatted = "\n".join(options_text_parts) # Join options with newlines
        full_message_text = f"{question_global_index + 1}. {question_text_body}\n\n{options_text_formatted}"

        # --- Create keyboard with letter buttons ---
        # Arrange buttons (e.g., 4 in one row)
        keyboard = [options_buttons] # Put all letter buttons in one row
        reply_markup = InlineKeyboardMarkup(keyboard)

        # --- Send message with full options in text, letters on buttons ---
        await context.bot.send_message(
            chat_id=chat_id,
            text=full_message_text,
            reply_markup=reply_markup
        )

    # Update the index for the *next* potential batch
    context.user_data['index'] = end_index

    # Show "Next" button only if there are more questions AFTER this batch
    if end_index < total_questions:
        next_button_keyboard = [[InlineKeyboardButton("Next Batch", callback_data="next")]]
        await context.bot.send_message(
            chat_id=chat_id,
            text="Click to continue...", # Original text
            reply_markup=InlineKeyboardMarkup(next_button_keyboard)
        )

    # Stay in the quiz state after sending questions
    return QUIZ_IN_PROGRESS

# --- THIS FUNCTION WAS MODIFIED TO HANDLE THE LAST QUESTION AND SEND RESTART KEYBOARD ---
async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles answer, checks for quiz end, sends restart keyboard."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        _, qid_str, selected_letter = query.data.split("|")
        qid = int(qid_str)
    except (ValueError, IndexError):
        logger.error(f"Invalid callback data in handle_answer: {query.data}")
        try: await query.edit_message_text("Sorry, error processing answer button.")
        except BadRequest: pass
        return QUIZ_IN_PROGRESS # Stay in state

    questions = context.user_data.get("questions", [])
    total_questions = len(questions)
    # Note: Batch tracking was added in the *next* revision

    if not questions or qid >= total_questions:
         logger.error(f"handle_answer: Invalid questions/qid {qid} for user {user_id}.")
         try: await query.edit_message_text("Sorry, error retrieving question data.")
         except BadRequest: pass
         context.user_data.clear()
         return ConversationHandler.END

    question_data = questions[qid]
    correct_answer_letter = question_data.get('correct')
    question_text = question_data.get('question', '[Missing question]')

    # Reconstruct options text for the feedback message
    options_text_parts = []
    selected_option_text = f"({selected_letter})" # Fallback
    correct_option_text = f"({correct_answer_letter})" # Fallback
    valid_options = question_data.get('options', [])
    for opt in valid_options:
         if isinstance(opt, str) and len(opt) > 0:
             options_text_parts.append(opt)
             if opt.startswith(selected_letter):
                 selected_option_text = opt # Get full text of selected option
             if opt.startswith(correct_answer_letter):
                 correct_option_text = opt # Get full text of correct option
    options_text_formatted = "\n".join(options_text_parts)

    feedback = ""
    if selected_letter == correct_answer_letter:
        feedback = "✅ Correct!"
        context.user_data['score'] = context.user_data.get('score', 0) + 1
    else:
        feedback = f"❌ Wrong! Correct answer: {correct_option_text}"

    # Edit Message to show feedback, removing letter buttons
    updated_text = (
        f"{qid + 1}. {question_text}\n\n"
        f"{options_text_formatted}\n\n" # Show options again for context
        f"--------------------\n"
        f"{feedback}\n"
        f"You chose: {selected_option_text}"
    )
    try:
        await query.edit_message_text(text=updated_text, reply_markup=None)
    except BadRequest as e:
        logger.warning(f"Could not edit message user {user_id}, qid {qid}: {e}")

    # --- Check if quiz is finished ---
    if qid == total_questions - 1: # Check if this was the last question
        score = context.user_data.get('score', 0)
        logger.info(f"User {user_id} finished quiz. Score: {score}/{total_questions}")

        reply_markup = get_start_keyboard(context) # Get the initial keyboard
        finish_text = f"Quiz finished!\nYour score: {score}/{total_questions}\n\nChoose a new subject?"
        if not reply_markup:
             finish_text = f"Quiz finished!\nYour score: {score}/{total_questions}\n(Error loading subjects for restart)"

        # Send final message WITH the restart keyboard
        await context.bot.send_message(
            chat_id=chat_id,
            text=finish_text,
            reply_markup=reply_markup
        )
        # Don't clear user_data, start_quiz will do it
        return SELECTING_SUBJECT # Go back to the start state
    else:
        # If not the last question, stay in the quiz state
        return QUIZ_IN_PROGRESS

# handle_next, cancel, error_handler remain the same as previous version
async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handles the 'Next Batch' button."""
    # NOTE: This version does NOT check if all questions in the batch were answered
    query = update.callback_query
    await query.answer()
    try:
        await query.delete_message() # Delete the "Click to continue..." message
    except BadRequest as e:
         logger.warning(f"Could not delete 'Next Batch' prompt: {e}")
    # Call send_next_question_batch which will send the next set
    return await send_next_question_batch(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the conversation and clears state."""
    user = update.effective_user
    if user: logger.info("User %s canceled the conversation.", user.first_name)
    else: logger.info("Conversation canceled (user info not available).")
    cancel_message = "Quiz canceled. Use /start to begin again."
    if update.message: await update.message.reply_text(cancel_message)
    elif update.callback_query:
         await context.bot.send_message(chat_id=update.effective_chat.id, text=cancel_message)
         try: await update.callback_query.edit_message_reply_markup(reply_markup=None)
         except BadRequest: pass
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
         try: await update.effective_message.reply_text("Sorry, an error occurred.")
         except Exception as e: logger.error(f"Failed to send error message to user: {e}")


# === Flask App Setup (Identical) ===
flask_app = Flask(__name__)
@flask_app.route("/")
def index(): return "Quiz Bot is alive!"
@flask_app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
async def telegram_webhook():
    if request.is_json:
        update = Update.de_json(request.get_json(), application.bot)
        async with application: await application.process_update(update)
        return Response("OK", status=200)
    return Response("Bad Request", status=400)

# === Main Application Setup (Identical) ===
loaded_questions = load_questions(QUIZ_FILE)
if not loaded_questions: logger.critical(f"CRITICAL: No questions loaded from {QUIZ_FILE}.")
application = ApplicationBuilder().token(TOKEN).build()
application.bot_data['questions'] = loaded_questions
logger.info(f"Stored {len(loaded_questions)} subjects in bot_data.")
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        SELECTING_SUBJECT: [ CallbackQueryHandler(start_quiz, pattern="^subj\\|"), CallbackQueryHandler(start_quiz, pattern="^random$") ],
        QUIZ_IN_PROGRESS: [ CallbackQueryHandler(handle_answer, pattern="^ans\\|"), CallbackQueryHandler(handle_next, pattern="^next$") ],
    },
    fallbacks=[CommandHandler("start", start)], # Reset with /start
)
application.add_handler(conv_handler)
application.add_error_handler(error_handler)

# === Running the Application (Identical) ===
async def setup_webhook():
    logger.info(f"Setting webhook to: {WEBHOOK_FULL_URL}")
    try:
        async with application: await application.bot.set_webhook(url=WEBHOOK_FULL_URL, allowed_updates=Update.ALL_TYPES)
        logger.info("Webhook set successfully.")
        return True
    except Exception as e: logger.error(f"Failed to set webhook: {e}"); return False
async def main_async_setup():
    if WEBHOOK_MODE and WEBHOOK_FULL_URL: logger.info("Attempting async webhook setup..."); await setup_webhook()
    else: logger.info("No async setup needed for polling mode.")

if __name__ == "__main__":
    if WEBHOOK_MODE:
        try: logger.info("Running async setup for webhook..."); asyncio.run(main_async_setup()); logger.info("Async setup finished.")
        except Exception as e: logger.error(f"Error during async setup: {e}", exc_info=True); logger.critical("Exiting due to async setup failure."); exit()
        logger.info(f"Starting Flask server on host 0.0.0.0 port {PORT} for webhook...")
        # from waitress import serve # Recommended for production
        # serve(flask_app, host='0.0.0.0', port=PORT) # Recommended for production
        flask_app.run(host="0.0.0.0", port=PORT) # Use Flask's dev server for simplicity
    else:
        logger.info("Starting bot polling...")
        try: application.run_polling(allowed_updates=Update.ALL_TYPES)
        except KeyboardInterrupt: logger.info("Polling stopped manually.")
        except Exception as e: logger.error(f"Error during polling: {e}", exc_info=True)

