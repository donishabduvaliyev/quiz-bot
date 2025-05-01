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

# Load environment variables from .env file (if it exists)
load_dotenv()

# === CONFIG ===
# Get token from environment variable
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    # Fallback for local testing if .env is missing or variable not set
    TOKEN = 'YOUR_FALLBACK_TOKEN_HERE' # Replace if needed for local testing without .env
    if TOKEN == 'YOUR_FALLBACK_TOKEN_HERE':
        print("CRITICAL: Bot token not found. Set TELEGRAM_BOT_TOKEN in your .env file or environment variables.")
        exit()
    else:
        print("WARNING: Using fallback token. Set TELEGRAM_BOT_TOKEN in environment for deployment.")


# Webhook/Port setup
PORT = int(os.environ.get('PORT', 8443)) # Default port if not set by Render/environment
WEBHOOK_MODE = os.environ.get('WEBHOOK_MODE', 'False').lower() == 'true' # Set to True in Render env vars
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL") # MUST be set in Render env vars if WEBHOOK_MODE is True
WEBHOOK_PATH = "webhook" # The path Telegram will send updates to (keep consistent)

if WEBHOOK_MODE and not WEBHOOK_URL_BASE:
    print("CRITICAL: WEBHOOK_MODE is True, but WEBHOOK_URL environment variable is not set.")
    exit()

WEBHOOK_FULL_URL = f"{WEBHOOK_URL_BASE}/{WEBHOOK_PATH}" if WEBHOOK_URL_BASE else None

# Quiz file and batch size
QUIZ_FILE = 'tests.txt' # Make sure this file is in the same directory
QUESTIONS_PER_BATCH = 10

# === STATES for ConversationHandler ===
SELECTING_SUBJECT, QUIZ_IN_PROGRESS = range(2)

# === Logging Setup ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Reduce noise from underlying HTTP library
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# === Utils ===
def load_questions(file_path):
    """Loads questions from a text file into a dictionary by subject."""
    subjects = {}
    try:
        # Ensure correct encoding for potentially diverse characters
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        logger.error(f"Error: Quiz file not found at {file_path}")
        return subjects # Return empty dict if file not found

    current_subject = None
    # Split by double newline, handling potential variations in line endings
    blocks = content.replace('\r\n', '\n').strip().split('\n\n')

    for block in blocks:
        # Split each block into lines
        lines = block.strip().split('\n')

        # Check for subject line first
        if lines[0].startswith("Subject:"):
            try:
                # Split only once to handle colons in subject names if needed
                current_subject = lines[0].split(":", 1)[1].strip()
                if current_subject: # Ensure subject name is not empty
                     subjects[current_subject] = [] # Initialize subject list
                     logger.info(f"Found subject: {current_subject}")
                else:
                    logger.warning(f"Found empty subject name in block: {block}")
                    current_subject = None # Reset if subject name is empty
            except IndexError:
                 logger.warning(f"Malformed Subject line: {lines[0]}")
                 current_subject = None
            continue # Move to the next block after processing subject line

        # Process question block only if we have a valid current subject
        if current_subject is None:
             logger.warning(f"Skipping block due to missing subject context: {block}")
             continue

        # Expecting Question + 4 Options + Answer line = 6 lines minimum
        if len(lines) < 6:
            logger.warning(f"Skipping malformed block (less than 6 lines) for subject '{current_subject}': {block}")
            continue

        # Ensure the subject key exists (it should due to the check above)
        if current_subject not in subjects:
             # This case should ideally not be reached if logic is correct
             logger.error(f"Internal logic error: Subject '{current_subject}' not initialized before adding questions.")
             continue

        try:
            question_text = lines[0]
            options = lines[1:5]
            answer_line = lines[5]

            # Validate options format (e.g., "A) Text")
            if not all(len(opt) > 2 and opt[1] == ')' and opt[0].isalpha() for opt in options):
                 logger.warning(f"Malformed options format in block for subject '{current_subject}': {options}")
                 continue
            # Validate answer line format (e.g., "Answer: B")
            if not answer_line.startswith("Answer:"):
                 logger.warning(f"Malformed answer line format for subject '{current_subject}': {answer_line}")
                 continue

            # Extract correct answer letter
            correct_answer_letter = answer_line.split(":", 1)[1].strip()
            if not correct_answer_letter or len(correct_answer_letter) != 1 or not correct_answer_letter.isalpha():
                 logger.warning(f"Invalid correct answer letter '{correct_answer_letter}' for subject '{current_subject}': {answer_line}")
                 continue

            # Add the parsed question data
            subjects[current_subject].append({
                'question': question_text,
                'options': options, # Store options like ["A) Option Text", ...]
                'correct': correct_answer_letter.upper() # Store correct answer letter like "A", ensure uppercase
            })
        except IndexError:
            logger.warning(f"Skipping block due to parsing error (IndexError) for subject '{current_subject}': {block}")
        except Exception as e:
            # Catch any other unexpected errors during parsing
            logger.error(f"Unexpected error parsing block for subject '{current_subject}': {e}\nBlock: {block}", exc_info=True)

    logger.info(f"Loaded subjects: {list(subjects.keys())}")
    if not subjects:
        logger.warning("No subjects were loaded. Check tests.txt format and content.")
    return subjects

# === Helper Function for Start Keyboard ===
def get_start_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup | None:
    """Builds the initial subject selection keyboard."""
    # These names must match the keys generated by load_questions from tests.txt
    known_subjects = ["osimlik-moyi" ,"O'simlik-moyi-texnologiyasi"]
    keyboard = []
    # Access loaded questions stored in bot_data
    loaded_subjects = context.bot_data.get('questions', {})

    # Create a button for each known subject IF questions were loaded for it
    for subj in known_subjects:
        if subj in loaded_subjects and loaded_subjects[subj]: # Also check if list is not empty
            keyboard.append([InlineKeyboardButton(text=subj, callback_data=f"subj|{subj}")])
        else:
            logger.warning(f"Subject '{subj}' hardcoded but not loaded or has no questions. Skipping button.")

    # Add the random button only if there are *any* loaded questions
    if any(loaded_subjects.values()): # Check if at least one subject has questions
        keyboard.append([InlineKeyboardButton(text="Random savollar", callback_data="random")])

    return InlineKeyboardMarkup(keyboard) if keyboard else None

# === Bot Handlers (Async v20 Style) ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends the initial message and subject selection keyboard."""
    user = update.effective_user
    # Clear any previous quiz state when user explicitly uses /start
    context.user_data.clear()
    logger.info(f"User {user.id} ({user.first_name}) started conversation. Cleared user_data.")

    reply_markup = get_start_keyboard(context)

    if not reply_markup:
        await update.message.reply_text(
            "Uzr hozirda hech qanday fanga doir savollar topilmadi , Doniyor bilan bog'laning."
        )
        return ConversationHandler.END # End if no subjects loaded

    await update.message.reply_text("Choose a subject or get random questions:", reply_markup=reply_markup)
    # Transition to the state where the bot waits for the user's button choice
    return SELECTING_SUBJECT

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles subject selection button press, clears old state, loads questions, starts quiz."""
    query = update.callback_query
    await query.answer() # Acknowledge button press
    data = query.data
    user_id = update.effective_user.id

    # --- Clear previous quiz state before starting new one ---
    context.user_data.clear()
    logger.info(f"User {user_id} selected an option '{data}'. Cleared user_data before starting quiz.")

    all_loaded_questions = context.bot_data.get('questions', {})
    questions_to_ask = []
    subject_name = "Unknown"

    # --- Determine questions based on button pressed ---
    if data.startswith("subj|"):
        subject_name = data.split("|", 1)[1]
        if subject_name in all_loaded_questions and all_loaded_questions[subject_name]:
            # Make a copy and shuffle for this user's session
            questions_to_ask = list(all_loaded_questions[subject_name])
            random.shuffle(questions_to_ask)
            logger.info(f"User {user_id} selected subject: {subject_name}, {len(questions_to_ask)} questions.")
        else:
            logger.error(f"User {user_id} clicked button for subject '{subject_name}', but questions not loaded/empty.")
            await query.edit_message_text(f"Sorry, an error occurred loading questions for '{subject_name}'.")
            return ConversationHandler.END # End cleanly
    elif data == "random":
        subject_name = 'Random Mix'
        temp_list = []
        if not any(all_loaded_questions.values()): # Check if any questions exist at all
             logger.error(f"User {user_id} requested random questions, but no subjects/questions loaded.")
             await query.edit_message_text("Sorry, no questions are available to randomize.")
             return ConversationHandler.END # End cleanly

        # Aim for roughly 40 questions, sampling proportionally from available subjects
        total_available = sum(len(qs) for qs in all_loaded_questions.values())
        target_total = min(50, total_available) # Don't try to get more than available

        for subj, subj_questions in all_loaded_questions.items():
            if not subj_questions: continue # Skip empty subjects
            # Calculate proportional count, ensuring at least 1 if possible
            proportion = len(subj_questions) / total_available if total_available > 0 else 0
            count = max(1, round(target_total * proportion)) if total_available > 0 else min(10, len(subj_questions)) # Fallback count
            # Take min(calculated_count, available_in_subject)
            actual_count = min(count, len(subj_questions))
            if actual_count > 0:
                temp_list.extend(random.sample(subj_questions, actual_count))

        questions_to_ask = temp_list
        random.shuffle(questions_to_ask) # Shuffle the combined list
        logger.info(f"User {user_id} selected random questions. Prepared {len(questions_to_ask)} questions.")
    else:
        # Should not happen if buttons are generated correctly
        logger.warning(f"Received unexpected callback data in start_quiz state: {data}")
        await query.edit_message_text("Uzr , kutilmagan xato , botni qayta ishga tushuring /start")
        return ConversationHandler.END # End cleanly

    # --- Final check if questions were prepared ---
    if not questions_to_ask:
         logger.error(f"Failed to prepare any questions for user {user_id} for selection '{data}'.")
         await query.edit_message_text("Uzr, savollar topilmadi.")
         return ConversationHandler.END # End cleanly

    # --- Initialize user state for the new quiz ---
    context.user_data['subject'] = subject_name
    context.user_data['questions'] = questions_to_ask
    context.user_data['index'] = 0 # Index of the *next* question to send
    context.user_data['score'] = 0
    context.user_data['answered_in_batch'] = set() # Store indices answered in current batch
    context.user_data['current_batch_indices'] = [] # Store indices sent in current batch

    await query.edit_message_text(f"Starting quiz on: {subject_name}")
    await send_next_question_batch(update, context) # Send first batch
    return QUIZ_IN_PROGRESS

async def send_next_question_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Sends the next batch, resets batch tracking, includes options in text."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_index = context.user_data.get('index', 0) # Start index for this batch
    questions = context.user_data.get('questions', [])
    total_questions = len(questions)

    # Check if quiz is already finished
    if current_index >= total_questions:
        logger.info(f"send_next_question_batch: No more questions to send for user {user_id}.")
        # This case should ideally be reached only if user clicks 'Next' after finishing
        await context.bot.send_message(chat_id=chat_id, text="Barcha savollarga javob berdingiz!")
        reply_markup = get_start_keyboard(context)
        if reply_markup:
             await context.bot.send_message(chat_id=chat_id, text="Yangi fanni tanlash?", reply_markup=reply_markup)
        return SELECTING_SUBJECT # Go back to selection state

    if not questions:
        logger.error(f"send_next_question_batch: No questions found for user {user_id}.")
        await context.bot.send_message(chat_id=chat_id, text="Error: savollar topilmadi.")
        context.user_data.clear()
        return ConversationHandler.END

    # Determine questions and indices for this batch
    end_index = min(current_index + QUESTIONS_PER_BATCH, total_questions)
    batch_indices = list(range(current_index, end_index)) # Global indices for this specific batch
    batch_questions = questions[current_index:end_index]

    # --- Reset batch tracking state for the new batch ---
    context.user_data['current_batch_indices'] = batch_indices
    context.user_data['answered_in_batch'] = set()
    logger.info(f"Sending questions {current_index + 1}-{end_index} to user {user_id}. Batch indices: {batch_indices}")

    for i, q_data in enumerate(batch_questions):
        question_global_index = batch_indices[i] # Use the correct global index
        options_buttons = []
        options_text_parts = [] # To build the options text for the message

        # Validate options from the loaded data
        valid_options = [opt for opt in q_data.get('options', []) if isinstance(opt, str) and len(opt) > 2 and opt[1] == ')']
        if not valid_options:
             logger.error(f"Q {question_global_index} user {user_id} invalid options: {q_data.get('options')}")
             await context.bot.send_message(chat_id=chat_id, text=f"Skipping Q {question_global_index + 1} (invalid options).")
             # Mark as "answered" in this batch to allow proceeding if user clicks next
             context.user_data['answered_in_batch'].add(question_global_index)
             continue # Skip this question

        # --- Prepare options text and buttons ---
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
        keyboard = [options_buttons] # Put all letter buttons in one row
        reply_markup = InlineKeyboardMarkup(keyboard)

        # --- Send message ---
        await context.bot.send_message(
            chat_id=chat_id,
            text=full_message_text, # Send question + options in text
            reply_markup=reply_markup # Send buttons with just letters
        )

    # Update the index for the *next* potential batch
    context.user_data['index'] = end_index

    # Show "Next" button only if there are more questions AFTER this batch
    if end_index < total_questions:
        next_button_keyboard = [[InlineKeyboardButton("keyingi test", callback_data="next")]]
        await context.bot.send_message(
            chat_id=chat_id,
            text="Barchasini yechib bolib keyingisiga o'ting...", # Clarified text
            reply_markup=InlineKeyboardMarkup(next_button_keyboard)
        )

    # Stay in the quiz state after sending questions
    return QUIZ_IN_PROGRESS

# --- MODIFIED FUNCTION ---
async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles answer, tracks batch progress, checks for quiz end correctly."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        _, qid_str, selected_letter = query.data.split("|")
        qid = int(qid_str) # Global index of the question answered
    except (ValueError, IndexError):
        logger.error(f"Invalid callback data in handle_answer: {query.data}")
        try: await query.edit_message_text("Uzr, noto'g'ri tugma va javob format.")
        except BadRequest: pass
        return QUIZ_IN_PROGRESS # Stay in state

    questions = context.user_data.get("questions", [])
    total_questions = len(questions)
    current_batch_indices = context.user_data.get("current_batch_indices", [])
    answered_in_batch = context.user_data.get("answered_in_batch", set())

    if not questions or qid >= total_questions:
         logger.error(f"handle_answer: Invalid questions/qid {qid} for user {user_id}.")
         try: await query.edit_message_text("Uzr, savollarni yuklashda xatolik.")
         except BadRequest: pass
         context.user_data.clear()
         return ConversationHandler.END # End cleanly

    # --- Track answered question within the batch ---
    is_new_answer_in_batch = False
    if qid in current_batch_indices:
        if qid not in answered_in_batch:
             answered_in_batch.add(qid)
             context.user_data['answered_in_batch'] = answered_in_batch # Update the set in user_data
             is_new_answer_in_batch = True
             logger.info(f"User {user_id} answered question {qid} in current batch. Batch answered: {len(answered_in_batch)}/{len(current_batch_indices)}")
        else:
             logger.info(f"User {user_id} re-answered question {qid} in current batch.")
    else:
        logger.warning(f"User {user_id} answered question {qid} which is not in current batch {current_batch_indices}.")

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
             options_text_parts.append(opt) # Add to list for display
             if opt.startswith(selected_letter):
                 selected_option_text = opt # Get full text of selected option
             if opt.startswith(correct_answer_letter):
                 correct_option_text = opt # Get full text of correct option
    options_text_formatted = "\n".join(options_text_parts)


    feedback = ""
    # --- Score Update (prevent double scoring) ---
    correctly_answered_key = f"correct_{qid}"
    is_correct = (selected_letter == correct_answer_letter)

    if is_correct:
        feedback = "✅ To'gri!"
        if not context.user_data.get(correctly_answered_key, False):
             context.user_data['score'] = context.user_data.get('score', 0) + 1
             context.user_data[correctly_answered_key] = True # Mark as correctly answered
             logger.info(f"User {user_id} answered Q {qid} correctly. Score: {context.user_data['score']}")
        else:
             logger.info(f"User {user_id} re-answered Q {qid} correctly. Score not changed.")
    else:
        feedback = f"❌ Xato! To'g'ri javob: {correct_option_text}"
        # If user answered correctly before and now changes to wrong, remove the point?
        # Let's keep it simple: score only increases, never decreases.
        # if context.user_data.get(correctly_answered_key, False):
        #     context.user_data['score'] = context.user_data.get('score', 1) - 1 # Deduct if changing from correct to wrong
        #     context.user_data[correctly_answered_key] = False # Mark as no longer correct

    # --- Edit Message ---
    updated_text = (
        f"{qid + 1}. {question_text}\n\n"
        f"{options_text_formatted}\n\n" # Show the options again
        f"--------------------\n"
        f"{feedback}\n"
        f"Siz tanladingiz: {selected_option_text}"
    )
    try:
        await query.edit_message_text(text=updated_text, reply_markup=None)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.warning(f"Could not edit message user {user_id}, qid {qid}: {e}")

    # --- Check if quiz is finished (CORRECTED LOGIC) ---
    # The quiz ends if:
    # 1. The current batch includes the last question (index total_questions - 1)
    # 2. All questions in the current batch have now been answered
    is_last_batch = (total_questions - 1) in current_batch_indices
    all_in_batch_answered = len(answered_in_batch) >= len(current_batch_indices)

    if is_last_batch and all_in_batch_answered:
        score = context.user_data.get('score', 0)
        logger.info(f"User {user_id} finished the final batch. Quiz finished. Score: {score}/{total_questions}")

        reply_markup = get_start_keyboard(context)
        finish_text = f"Test tugadi!\nSizning natijangiz: {score}/{total_questions}\n\nYangi fan tanlash?"
        if not reply_markup:
             finish_text = f"Test tugadi!\nSizning natijangiz: {score}/{total_questions}\n(Fan tanlashda xatolik , botni qayta ishga tushuring)"

        await context.bot.send_message(
            chat_id=chat_id,
            text=finish_text,
            reply_markup=reply_markup
        )
        # Don't clear user_data, start_quiz/start will handle it
        return SELECTING_SUBJECT # Loop back to subject selection state
    else:
        # If not the end of the quiz, stay in the quiz state
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
        logger.info(f"User {user_id} finished batch {current_batch_indices}, proceeding to next.")
        # Delete the "Click to continue..." message
        try:
            await query.delete_message()
        except BadRequest as e:
             logger.warning(f"Could not delete 'Next Batch' prompt: {e}")
        # Send the next batch
        return await send_next_question_batch(update, context)
    else:
        # Calculate remaining count for a clearer message
        remaining_count = len(current_batch_indices) - len(answered_in_batch)
        plural = "s" if remaining_count > 1 else ""
        logger.info(f"User {user_id} clicked 'Next Batch' prematurely. Answered: {len(answered_in_batch)}/{len(current_batch_indices)}")
        # Send a temporary message telling the user to finish
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Iltimos qolgan {remaining_count} savollarga{plural} javob  bering!"
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

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
         try:
            # Avoid sending error message if the error was about editing an old message
            if not isinstance(context.error, BadRequest) or "Message is not modified" not in str(context.error):
                 await update.effective_message.reply_text("Uzr, kutilmagan xato , /start ni bosib qayta uruning")
         except Exception as e:
             logger.error(f"Failed to send error message to user: {e}")


# === Flask App Setup ===
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    """Basic route for health checks."""
    return "Quiz Bot is alive!"

@flask_app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
async def telegram_webhook():
    """Webhook endpoint to receive updates."""
    if request.is_json:
        update = Update.de_json(request.get_json(), application.bot)
        async with application:
             await application.process_update(update)
        return Response("OK", status=200)
    else:
        logger.warning("Webhook received non-JSON request.")
        return Response("Bad Request", status=400)

# === Main Application Setup ===
# Load questions once at startup
loaded_questions = load_questions(QUIZ_FILE)
if not loaded_questions:
    logger.critical(f"CRITICAL: No questions loaded from {QUIZ_FILE}. Bot may not function.")
    # Consider exiting if no questions loaded: exit()

# Build the PTB Application
application = ApplicationBuilder().token(TOKEN).build()

# Store loaded questions in bot_data
application.bot_data['questions'] = loaded_questions
logger.info(f"Stored {len(loaded_questions)} subjects in bot_data.")

# Setup ConversationHandler
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
    fallbacks=[CommandHandler("start", start)], # Let /start reset the conversation anytime
    # Optional: Add CommandHandler("cancel", cancel) to fallbacks if you want /cancel command
)

# Add handlers to the application
application.add_handler(conv_handler)
application.add_error_handler(error_handler)

# === Running the Application ===
async def setup_webhook():
    """Sets the webhook URL with Telegram."""
    logger.info(f"Attempting to set webhook to: {WEBHOOK_FULL_URL}")
    if not WEBHOOK_FULL_URL:
         logger.error("WEBHOOK_FULL_URL is not defined. Cannot set webhook.")
         return False
    try:
        # Initialize application before accessing bot attribute
        await application.initialize()
        webhook_info = await application.bot.get_webhook_info()
        # Set webhook only if it's not already set to the correct URL
        if webhook_info.url != WEBHOOK_FULL_URL:
            logger.info(f"Webhook currently set to '{webhook_info.url}'. Setting to '{WEBHOOK_FULL_URL}'...")
            await application.bot.set_webhook(url=WEBHOOK_FULL_URL, allowed_updates=Update.ALL_TYPES)
            # Verify webhook setting
            new_webhook_info = await application.bot.get_webhook_info()
            if new_webhook_info.url == WEBHOOK_FULL_URL:
                 logger.info("Webhook set successfully.")
                 return True
            else:
                 logger.error(f"Failed to set webhook. Current URL: {new_webhook_info.url}")
                 return False
        else:
            logger.info("Webhook is already set correctly.")
            return True
    except Exception as e:
        logger.error(f"Exception during webhook setup: {e}", exc_info=True)
        return False
    finally:
         # Ensure application is shutdown if setup fails or finishes
         # This might prevent Flask from starting if run within asyncio.run()
         # Consider removing shutdown() here if Flask needs to start afterwards
         # await application.shutdown()
         pass


async def main_async_setup():
    """Only performs async setup steps like setting the webhook."""
    if WEBHOOK_MODE:
        logger.info("Running webhook setup...")
        await setup_webhook()
    else:
        logger.info("Polling mode enabled. Skipping webhook setup.")


if __name__ == "__main__":
    # Run async setup once before starting the main loop/server
    try:
        # We run the setup, but don't want it to shut down the application object
        # if setup_webhook includes shutdown() in finally block.
        asyncio.run(main_async_setup())
    except Exception as e:
        logger.error(f"Error during initial async setup: {e}", exc_info=True)
        # Decide if fatal or not.
        # exit() # Uncomment to make setup failure fatal

    # Start either Flask (for webhook) or Polling
    if WEBHOOK_MODE:
        logger.info(f"Starting Flask server on host 0.0.0.0 port {PORT} for webhook...")
        # Use waitress or gunicorn in production instead of flask_app.run
        # Example using waitress (install with pip install waitress):
        # from waitress import serve
        # serve(flask_app, host='0.0.0.0', port=PORT)
        flask_app.run(host="0.0.0.0", port=PORT) # Use Flask's dev server for simplicity here
    else:
        # If not in webhook mode, just start polling directly.
        # run_polling handles the asyncio loop internally.
        logger.info("Starting bot polling...")
        try:
            # Initialize application before polling
            # No need to call initialize() explicitly, run_polling does it.
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        except KeyboardInterrupt:
            logger.info("Polling stopped manually.")
        except Exception as e:
            logger.error(f"Error during polling: {e}", exc_info=True)

  