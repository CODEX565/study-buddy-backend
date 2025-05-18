import os
import json
import uuid
import logging
from datetime import datetime
from firebase_admin import firestore
import google.generativeai as genai

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('flashcards.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
client = genai.GenerativeModel('gemini-1.5-flash')

def generate_flashcards_from_quiz(db, user_id, topic):
    """Generate flashcards from a quiz or exam topic."""
    try:
        logger.info(f"Generating flashcards for user_id: {user_id}, topic: {topic}")
        # Fetch recent quiz or exam for the topic
        exams = db.collection('exams').where('user_id', '==', user_id).where('topic', '==', topic).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).get()
        questions = []
        source = 'exam'
        
        if exams:
            exam_data = exams[0].to_dict()
            questions = exam_data.get('questions', [])
        else:
            quizzes = db.collection('quizzes').where('user_id', '==', user_id).where('topic', '==', topic).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).get()
            if quizzes:
                quiz_data = quizzes[0].to_dict()
                questions = quiz_data.get('questions', [])
                source = 'quiz'

        if not questions:
            logger.warning(f"No questions found for topic: {topic}")
            return []

        flashcards = []
        for question in questions:
            prompt = f"""
            Create a flashcard based on this question about {topic}:
            Question: {question['question']}
            Correct Answer: {question['correct_answer']}
            Explanation: {question.get('explanation', 'No explanation available.')}
            Return in JSON format:
            {{
              "question": "Simplified question",
              "answer": "Concise answer",
              "topic": "{topic}",
              "source": "{source}"
            }}
            """
            try:
                response = client.generate_content(prompt).text
                start_idx = response.find("{")
                end_idx = response.rfind("}") + 1
                if start_idx == -1 or end_idx == 0:
                    logger.error("Invalid JSON response from Gemini API")
                    continue
                json_text = response[start_idx:end_idx].strip()
                flashcard_data = json.loads(json_text)

                if not all(key in flashcard_data for key in ['question', 'answer', 'topic', 'source']):
                    logger.error("Incomplete flashcard data")
                    continue

                flashcard_data['flashcard_id'] = str(uuid.uuid4())
                flashcard_data['user_id'] = user_id
                flashcard_data['status'] = 'review'
                flashcard_data['timestamp'] = datetime.now().isoformat()
                flashcards.append(flashcard_data)
                db.collection('flashcards').document(flashcard_data['flashcard_id']).set(flashcard_data)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                continue
            except Exception as e:
                logger.error(f"Error generating flashcard: {e}")
                continue

        logger.info(f"Generated {len(flashcards)} flashcards for user_id: {user_id}, topic: {topic}")
        return flashcards
    except Exception as e:
        logger.exception(f"Error generating flashcards for user_id: {user_id}: {e}")
        return []

def generate_flashcards_from_document(db, user_id, document, topic='General'):
    """Generate flashcards from a document."""
    try:
        logger.info(f"Generating flashcards from document for user_id: {user_id}, topic: {topic}")
        prompt = f"""
        Generate 3 flashcards from the following document content about {topic}:
        {document}
        Each flashcard should have a question and answer.
        Return in JSON format:
        [
          {{
            "question": "Question text",
            "answer": "Answer text",
            "topic": "{topic}",
            "source": "document"
          }}
        ]
        """
        response = client.generate_content(prompt).text
        start_idx = response.find("[")
        end_idx = response.rfind("]") + 1
        if start_idx == -1 or end_idx == 0:
            logger.error("Invalid JSON response from Gemini API")
            return []

        json_text = response[start_idx:end_idx].strip()
        flashcard_list = json.loads(json_text)

        flashcards = []
        for flashcard_data in flashcard_list:
            if not all(key in flashcard_data for key in ['question', 'answer', 'topic', 'source']):
                logger.error("Incomplete flashcard data")
                continue
            flashcard_data['flashcard_id'] = str(uuid.uuid4())
            flashcard_data['user_id'] = user_id
            flashcard_data['status'] = 'review'
            flashcard_data['timestamp'] = datetime.now().isoformat()
            flashcards.append(flashcard_data)
            db.collection('flashcards').document(flashcard_data['flashcard_id']).set(flashcard_data)

        logger.info(f"Generated {len(flashcards)} flashcards from document for user_id: {user_id}")
        return flashcards
    except Exception as e:
        logger.exception(f"Error generating flashcards from document: {e}")
        return []

def generate_flashcards_from_input(db, user_id, question, answer, topic='General', status='review'):
    """Generate a flashcard from user input."""
    try:
        logger.info(f"Generating flashcard from input for user_id: {user_id}, topic: {topic}")
        flashcard_data = {
            'flashcard_id': str(uuid.uuid4()),
            'user_id': user_id,
            'question': question,
            'answer': answer,
            'topic': topic,
            'status': status,
            'source': 'manual',
            'timestamp': datetime.now().isoformat()
        }
        db.collection('flashcards').document(flashcard_data['flashcard_id']).set(flashcard_data)
        logger.info(f"Generated flashcard for user_id: {user_id}")
        return [flashcard_data]
    except Exception as e:
        logger.exception(f"Error generating flashcard from input: {e}")
        return []

def get_user_flashcards(db, user_id):
    """Retrieve all flashcards for a user."""
    try:
        logger.info(f"Fetching flashcards for user_id: {user_id}")
        flashcards = db.collection('flashcards').where('user_id', '==', user_id).get()
        flashcard_list = [{'flashcard_id': f.id, **f.to_dict()} for f in flashcards]
        logger.info(f"Fetched {len(flashcard_list)} flashcards for user_id: {user_id}")
        return flashcard_list
    except Exception as e:
        logger.exception(f"Error fetching flashcards: {e}")
        return []

def update_flashcard_status(db, user_id, flashcard_id, status):
    """Update the status of a flashcard."""
    try:
        logger.info(f"Updating flashcard status for user_id: {user_id}, flashcard_id: {flashcard_id}, status: {status}")
        flashcard_ref = db.collection('flashcards').document(flashcard_id)
        flashcard_doc = flashcard_ref.get()
        if not flashcard_doc.exists or flashcard_doc.to_dict()['user_id'] != user_id:
            logger.warning(f"Unauthorized or invalid flashcard_id: {flashcard_id}")
            return {"error": "Unauthorized or flashcard not found"}
        flashcard_ref.update({'status': status})
        logger.info(f"Updated flashcard status to {status}")
        return {"success": True}
    except Exception as e:
        logger.exception(f"Error updating flashcard status: {e}")
        return {"error": str(e)}