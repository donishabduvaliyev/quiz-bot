import logging
import random
from telegram.error import BadRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Updater, CommandHandler, CallbackQueryHandler,
                          CallbackContext, ConversationHandler)

# === CONFIG ===
TOKEN = '7321605986:AAEpGoxjZzzUBqh5aNUSw8FPHJOPoMHfZj8'
QUIZ_FILE = './tests.txt' 
QUESTIONS_PER_BATCH = 10

# === STATES ===
SELECTING_SUBJECT, QUIZ_IN_PROGRESS = range(2)

# === Globals ===
user_data_store = {}

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Utils ===
def load_questions(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    subjects = {}
    current_subject = None
    for block in content.strip().split('\n\n'):
        lines = block.strip().split('\n')
        if len(lines) < 6:
            continue
        if lines[0].startswith("Subject:"):
            current_subject = lines[0].split(":")[1].strip()
            continue
        if current_subject not in subjects:
            subjects[current_subject] = []

        q = lines[0]
        options = lines[1:5]
        answer_line = lines[5]
        correct = answer_line.split(":")[1].strip()

        subjects[current_subject].append({
            'question': q,
            'options': options,
            'correct': correct
        })

    return subjects

# === Bot Handlers ===
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext
# Assuming SELECTING_SUBJECT is defined elsewhere

# === Your other code (imports, constants, load_questions, etc.) ===

def start(update: Update, context: CallbackContext):
    # --- OLD CODE ---
    # subjects = list(context.bot_data['questions'].keys())
    # print(f"DEBUG: Subjects loaded: {subjects}") # Keep this if debugging the dynamic version
    # keyboard = [[InlineKeyboardButton(subj, callback_data=f"subj|{subj}")]
    #             for subj in subjects]
    # keyboard.append([InlineKeyboardButton("Random 40 Questions", callback_data="random")])
    # reply_markup = InlineKeyboardMarkup(keyboard)
    # --- END OLD CODE ---

    # +++ NEW HARDCODED CODE +++
    # Define your subjects manually here
    known_subjects = ["Math", "English"] # Add other subjects from your file exactly as named

    keyboard = []
    for subj in known_subjects:
        # Create a button for each known subject
        keyboard.append([InlineKeyboardButton(text=subj, callback_data=f"subj|{subj}")])

    # Add the random button separately
    keyboard.append([InlineKeyboardButton(text="Random 40 Questions", callback_data="random")])

    # Create the Reply Markup
    reply_markup = InlineKeyboardMarkup(keyboard)
    # +++ END NEW HARDCODED CODE +++


    # Send the message (this line remains the same)
    # If the error happened here, it was due to the content of reply_markup
    try:
        update.message.reply_text("Choose a subject:", reply_markup=reply_markup)
        return SELECTING_SUBJECT
    except Exception as e:
        print(f"ERROR sending start message: {e}") # Add error logging here
        # Handle the error appropriately, maybe send a text message without keyboard
        update.message.reply_text("Sorry, there was an error setting up the subjects.")
        # Decide what state to return or if to end conversation
        return ConversationHandler.END # Or appropriate fallback state


# === Rest of your bot code ===

def start_quiz(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data

    if data.startswith("subj|"):
        subject = data.split("|")[1]
        questions = context.bot_data['questions'][subject]
    elif data == "random":
        questions = []
        for subj_questions in context.bot_data['questions'].values():
            questions.extend(random.sample(subj_questions, min(10, len(subj_questions))))
        random.shuffle(questions)
        subject = 'Random'

    user_id = update.effective_user.id
    user_data_store[user_id] = {
        'subject': subject,
        'questions': questions,
        'index': 0
    }
    send_next_question(update, context)
    return QUIZ_IN_PROGRESS

def send_next_question(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    data = user_data_store.get(user_id)
    if not data:
        return

    index = data['index']
    questions = data['questions']
    

    if index >= len(questions):
        context.user_data.clear()
        context.bot.send_message(chat_id=user_id, text="All questions finished!")
        return

    batch = questions[index:index+QUESTIONS_PER_BATCH]
    for i, q in enumerate(batch):
        options = [InlineKeyboardButton(opt, callback_data=f"ans|{index+i}|{opt[0]}") for opt in q['options']]
        reply_markup = InlineKeyboardMarkup([options])
        context.bot.send_message(chat_id=user_id, text=q['question'], reply_markup=reply_markup)

    if index + QUESTIONS_PER_BATCH < len(questions):
        button = [[InlineKeyboardButton("Next 10", callback_data="next")]]
        context.bot.send_message(chat_id=user_id, text="Click to continue:", reply_markup=InlineKeyboardMarkup(button))

    data['index'] += QUESTIONS_PER_BATCH
    
    # In send_next_question


def handle_answer(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    _, qid, selected = query.data.split("|")
    qid = int(qid)
    user_id = query.from_user.id

    session = user_data_store.get(user_id, {})
    question = session.get("questions", [])[qid]
    correct_answer = question['correct']

    if selected == correct_answer:
        feedback = "✅ Correct!"
    else:
        feedback = "❌ Wrong!"

    updated_text = (
        f"{feedback}\n"
        f"Question: {question['question']}\n"
        f"You chose: {selected}\n"
        f"Correct answer: {correct_answer}"
    )

    try:
        context.bot.edit_message_text(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            text=updated_text
        )
    except:
        pass

def handle_next(update: Update, context: CallbackContext):
    update.callback_query.answer()
    send_next_question(update, context)

def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    subjects = load_questions(QUIZ_FILE)
    # updater.bot_data['questions'] = subjects
    dp.bot_data['questions'] = subjects


    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_SUBJECT: [CallbackQueryHandler(start_quiz)],
            QUIZ_IN_PROGRESS: [CallbackQueryHandler(handle_answer, pattern="^ans\\|"),
                               CallbackQueryHandler(handle_next, pattern="^next")]
        },
        fallbacks=[]
    )

    dp.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
