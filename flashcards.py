import os
import logging
import json
import uuid
from datetime import datetime
from firebase_admin import firestore
import google.generativeai as genai
from google.generativeai import types
from Quiz import get_quiz_history
from Max import process_document_with_gemini

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('flashcards.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Client will use GEMINI_API_KEY from environment automatically
client = genai.GenerativeModel('gemini-1.5-flash')

def generate_flashcards_from_quiz(db, user_id, topic=None):
    try:
        logger.info(f"Generating flashcards from quiz for user_id: {user_id}, topic: {topic}")
        quiz_history = get_quiz_history(db, user_id, topic)
        if not quiz_history:
            logger.warning(f"No quiz history found for user_id: {user_id}")
            return []

        flashcards = []
        for question in quiz_history:
            if topic and question.get('topic') != topic:
                continue
            flashcard = {
                'question': question['question'],
                'answer': question['correct_answer'],
                'topic': question.get('topic', 'General'),
                'source': 'quiz',
                'status': 'review',
                'created_at': datetime.utcnow().isoformat(),
                'flashcard_id': str(question['question_id'])
            }
            flashcards.append(flashcard)
            save_flashcard(db, user_id, flashcard)
        logger.info(f"Generated {len(flashcards)} flashcards from quiz for user_id: {user_id}")
        return flashcards
    except Exception as e:
        logger.exception(f"Error generating flashcards from quiz for user_id: {user_id}: {str(e)}")
        return []

def generate_flashcards_from_document(db, user_id, document_text, user_input='', topic='General'):
    try:
        logger.info(f"Generating flashcards from document for user_id: {user_id}, topic: {topic}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return []

        conversation_history = user_data.get('conversation_history', [])
        prompt = f"""
        Summarize the following document text into 3-5 concise flashcard pairs (question and answer).
        Focus on key concepts related to {topic}. Each question should be clear and have one correct answer.
        Return in JSON format:
        [
          {{"question": "What is X?", "answer": "X is Y."}},
          ...
        ]
        Document text: {document_text}
        User input: {user_input}
        """
        response = client.generate_content(
            prompt,
            generation_config=types.GenerationConfig(
                temperature=0.7
            )
        )

        text = response.text
        start_idx = text.find("[")
        end_idx = text.rfind("]") + 1
        json_text = text[start_idx:end_idx].strip() if start_idx != -1 and end_idx != -1 else "[]"

        try:
            flashcard_data = json.loads(json_text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {str(e)}")
            return []

        flashcards = []
        for item in flashcard_data:
            flashcard = {
                'question': item['question'],
                'answer': item['answer'],
                'topic': topic,
                'source': 'document',
                'status': 'review',
                'created_at': datetime.utcnow().isoformat(),
                'flashcard_id': str(uuid.uuid4())
            }
            flashcards.append(flashcard)
            save_flashcard(db, user_id, flashcard)
        logger.info(f"Generated {len(flashcards)} flashcards from document for user_id: {user_id}")
        return flashcards
    except Exception as e:
        logger.exception(f"Error generating flashcards from document for user_id: {user_id}: {str(e)}")
        return []

def generate_flashcards_from_input(db, user_id, question, answer, topic='General'):
    try:
        logger.info(f"Generating flashcard from input for user_id: {user_id}, topic: {topic}")
        flashcard = {
            'question': question,
            'answer': answer,
            'topic': topic,
            'source': 'manual',
            'status': 'review',
            'created_at': datetime.utcnow().isoformat(),
            'flashcard_id': str(uuid.uuid4())
        }
        save_flashcard(db, user_id, flashcard)
        logger.info(f"Generated flashcard from input for user_id: {user_id}")
        return [flashcard]
    except Exception as e:
        logger.exception(f"Error generating flashcard from input for user_id: {user_id}: {str(e)}")
        return []

def save_flashcard(db, user_id, flashcard):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.collection('flashcards').document(flashcard['flashcard_id']).set(flashcard)
        logger.debug(f"Saved flashcard for user_id: {user_id}, flashcard_id: {flashcard['flashcard_id']}")
    except Exception as e:
        logger.exception(f"Error saving flashcard for user_id: {user_id}: {str(e)}")

def get_user_flashcards(db, user_id, topic=None, status=None):
    try:
        logger.info(f"Fetching flashcards for user_id: {user_id}, topic: {topic}, status: {status}")
        flashcards_ref = db.collection('users').document(user_id).collection('flashcards')
        query = flashcards_ref
        if topic:
            query = query.where('topic', '==', topic)
        if status:
            query = query.where('status', '==', status)
        docs = query.stream()
        flashcards = [doc.to_dict() for doc in docs]
        logger.debug(f"Fetched {len(flashcards)} flashcards for user_id: {user_id}")
        return flashcards
    except Exception as e:
        logger.exception(f"Error fetching flashcards for user_id: {user_id}: {str(e)}")
        return []

def update_flashcard_status(db, user_id, flashcard_id, status):
    try:
        logger.info(f"Updating flashcard status for user_id: {user_id}, flashcard_id: {flashcard_id}, status: {status}")
        flashcard_ref = db.collection('users').document(user_id).collection('flashcards').document(flashcard_id)
        flashcard_ref.update({'status': status})
        logger.debug(f"Updated flashcard status for user_id: {user_id}, flashcard_id: {flashcard_id}")
        return {'message': 'Flashcard status updated'}
    except Exception as e:
        logger.exception(f"Error updating flashcard status for user_id: {user_id}: {str(e)}")
        return {'error': str(e)}