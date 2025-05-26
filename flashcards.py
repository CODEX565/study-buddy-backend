import os
import uuid
import logging
from dotenv import load_dotenv
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore
import json

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('flashcards.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY not found in environment variables")
    raise ValueError("GEMINI_API_KEY is required")

# Firestore client
if not firebase_admin._apps:
    cred = credentials.Certificate('studybuddy.json')
    firebase_admin.initialize_app(cred)
db = firestore.client()

PASS_THRESHOLD = 0.8

def get_user_data(user_id):
    user_ref = db.collection('users').document(user_id)
    doc = user_ref.get()
    return doc.to_dict() if doc.exists else None

def get_failed_topics(user_id):
    user_data = get_user_data(user_id)
    if not user_data:
        return []
    failed = []
    for summary in user_data.get('quiz_summary', []):
        if summary.get('score', 1) < PASS_THRESHOLD:
            failed.append((summary['subject'], summary['topic']))
    return failed

def check_existing_flashcards(user_id, subject, topic):
    user_data = get_user_data(user_id)
    if not user_data:
        return []
    return [f for f in user_data.get('flashcards', []) if f['subject'] == subject and f['topic'] == topic]

def generate_gemini_flashcards(subject, topic, num_cards=3):
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Generate {num_cards} flashcards for {topic} in {subject}.
    Each flashcard should have a question and answer.
    Return in JSON format:
    [
      {{"q": "What is x in 2x = 6?", "a": "3"}},
      {{"q": "Simplify: 3(x + 2)", "a": "3x + 6"}}
    ]
    """
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=['TEXT'])
    )
    text = response.candidates[0].content.parts[0].text
    json_text = text.strip('```json').strip('```').strip()
    try:
        cards = json.loads(json_text)
        return cards
    except Exception as e:
        logger.error(f"Gemini flashcard generation error: {e}")
        return []

def generate_flashcards_for_failed_topics(user_id):
    failed_topics = get_failed_topics(user_id)
    all_new_flashcards = []
    for subject, topic in failed_topics:
        existing = check_existing_flashcards(user_id, subject, topic)
        if existing:
            continue
        cards = generate_gemini_flashcards(subject, topic, num_cards=3)
        if not cards:
            continue
        flashcard_obj = {
            "flashcard_id": str(uuid.uuid4()),
            "subject": subject,
            "topic": topic,
            "cards": cards,
            "source": "Gemini",
            "created_at": firestore.SERVER_TIMESTAMP
        }
        db.collection('users').document(user_id).update({
            "flashcards": firestore.ArrayUnion([flashcard_obj]),
            "flashcards_summary": firestore.ArrayUnion([{
                "subject": subject,
                "topic": topic,
                "flashcard_id": flashcard_obj["flashcard_id"],
                "timestamp": firestore.SERVER_TIMESTAMP
            }]),
            "flashcards_last_active": firestore.SERVER_TIMESTAMP
        })
        all_new_flashcards.append(flashcard_obj)
    return all_new_flashcards

def generate_flashcards_for_topic(user_id, subject, topic, num_cards=3, age=None, year_group=None):
    user_data = get_user_data(user_id)
    if not user_data:
        return []
    client = genai.Client(api_key=GEMINI_API_KEY)
    age_str = f" for a {age}-year-old" if age else ""
    year_group_str = f" for {year_group}" if year_group else ""
    prompt = f"""
    Generate {num_cards} flashcards for {topic} in {subject}{age_str}{year_group_str}.
    Each flashcard should have a question and answer.
    Return in JSON format:
    [
      {{"q": "What is x in 2x = 6?", "a": "3"}},
      {{"q": "Simplify: 3(x + 2)", "a": "3x + 6"}}
    ]
    """
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=['TEXT'])
    )
    text = response.candidates[0].content.parts[0].text
    json_text = text.strip('```json').strip('```').strip()
    try:
        cards = json.loads(json_text)
    except Exception as e:
        logger.error(f"Gemini flashcard generation error: {e}")
        return []
    flashcard_obj = {
        "flashcard_id": str(uuid.uuid4()),
        "subject": subject,
        "topic": topic,
        "cards": cards,
        "source": "Gemini",
        "created_at": firestore.SERVER_TIMESTAMP
    }
    db.collection('users').document(user_id).update({
        "flashcards": firestore.ArrayUnion([flashcard_obj]),
        "flashcards_summary": firestore.ArrayUnion([{
            "subject": subject,
            "topic": topic,
            "flashcard_id": flashcard_obj["flashcard_id"],
            "timestamp": firestore.SERVER_TIMESTAMP
        }]),
        "flashcards_last_active": firestore.SERVER_TIMESTAMP
    })
    return cards