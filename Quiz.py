import os
import json
import uuid
import random
import logging
from dotenv import load_dotenv
from google import genai
from google.genai import types
from firebase_admin import firestore
from datetime import datetime
from Max import save_user_memory

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('quiz.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

MAX_RETRIES = 5

def map_age_to_year_group(age):
    """Map age to an approximate year group."""
    try:
        age = int(age)
        if 5 <= age <= 6:
            return 'Year 1'
        elif 7 <= age <= 8:
            return 'Year 2'
        elif 9 <= age <= 10:
            return 'Year 3'
        elif 11 <= age <= 12:
            return 'Year 4'
        elif 13 <= age <= 14:
            return 'Year 5'
        elif 15 <= age <= 16:
            return 'Year 6'
        elif 17 <= age <= 18:
            return 'Year 7'
        else:
            return 'General'
    except (ValueError, TypeError):
        return 'General'

def get_user_data(db, user_id):
    """Fetch user data including age, year_group, and study_topic."""
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            logger.warning(f"User not found: {user_id}")
            return {"error": "User not found"}
        
        user_data = user_doc.to_dict()
        return {
            "age": user_data.get('age'),
            "year_group": user_data.get('year_group', 'General'),
            "study_topic": user_data.get('study_topic'),
        }
    except Exception as e:
        logger.exception(f"Error fetching user data for user_id: {user_id}: {e}")
        return {"error": "Failed to fetch user data"}

def get_incorrect_questions(db, user_id, topic=None):
    try:
        user_ref = db.collection('users').document(user_id)
        user_data = user_ref.get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return []

        quiz_history = user_data.get('quiz_history', [])
        quiz_responses = user_data.get('quiz_responses', {})
        incorrect_questions = []

        for question in quiz_history:
            if topic and question.get('topic') != topic:
                continue
            question_id = question.get('question_id')
            if question_id in quiz_responses:
                response = quiz_responses[question_id]
                latest_attempt = response.get('attempts', [])[-1] if response.get('attempts') else response
                if not latest_attempt.get('is_correct'):
                    incorrect_questions.append(question)

        logger.debug(f"Found {len(incorrect_questions)} incorrect questions for user_id: {user_id}, topic: {topic}")
        return incorrect_questions
    except Exception as e:
        logger.exception(f"Error fetching incorrect questions for user_id: {user_id}: {e}")
        return []

def generate_quiz_question(db, user_id, topic=None, age=None, year_group=None, multiplayer=False, retry_count=0):
    try:
        logger.info(f"Generating quiz question for user_id: {user_id}, topic: {topic}, age: {age}, year_group: {year_group}, multiplayer: {multiplayer}")
        user_data = db.collection('users').document(user_id).get().to_dict() if db else {}
        if not user_data and not multiplayer:
            logger.warning(f"User not found: {user_id}")
            return {"error": "User not found"}

        quiz_history = get_quiz_history(db, user_id) if db else []
        memories = user_data.get('memories', []) if user_data else []

        if not multiplayer and db:
            incorrect_questions = get_incorrect_questions(db, user_id, topic)
            if incorrect_questions:
                question = random.choice(incorrect_questions)
                logger.info(f"Repeating incorrect question for user_id: {user_id}, question_id: {question['question_id']}")
                return {
                    "question_id": question['question_id'],
                    "question": question['question'],
                    "answers": question['answers'],
                    "correct_answer": question['correct_answer'],
                    "explanation": question.get('explanation', 'No explanation available.'),
                    "topic": question['topic'],
                    "year_group": question.get('year_group', 'General')
                }

        logger.info(f"Generating new question for user_id: {user_id}")

        general_topics = [
            'Python', 'JavaScript', 'Algorithms', 'Data Structures', 'Databases',
            'Mathematics', 'Physics', 'Chemistry', 'Biology', 'History',
            'Literature', 'English Grammar', 'Geography', 'Economics', 'Computer Science'
        ]

        effective_topic = topic
        if effective_topic == 'General':
            effective_topic = random.choice(general_topics)
        elif not effective_topic:
            for memory in memories:
                if memory['type'] == 'study_topic':
                    effective_topic = memory['value']
                    break
        if not effective_topic:
            effective_topic = get_study_goal(db, user_id) or 'programming and coding'

        if topic and topic != 'General' and not multiplayer and db:
            save_user_memory(user_id, {
                "type": "study_topic",
                "value": topic,
                "timestamp": datetime.now().isoformat()
            })

        # Determine effective year group
        effective_year_group = year_group or user_data.get('year_group', None)
        if not effective_year_group and age:
            effective_year_group = map_age_to_year_group(age)
        if not effective_year_group:
            effective_year_group = 'General'

        history_str = ""
        for i, question in enumerate(quiz_history):
            history_str += f"Question {i+1}: {question['question']} | Answers: {', '.join(question['answers'])} | Correct Answer: {question['correct_answer']}\n"

        year_group_prompt = f" suitable for {effective_year_group} students" if effective_year_group and effective_year_group != 'General' else ""
        prompt = f"""
        Generate a brand new random quiz question about {effective_topic}{year_group_prompt}.
        Only one answer should be correct. Provide a concise step-by-step explanation (2-3 short steps, max 50-60 words) for the correct answer.
        Return strictly in this JSON format:
        {{
          "question": "What is the capital of Australia?",
          "answers": ["Sydney", "Melbourne", "Canberra", "Perth"],
          "correct_answer": "Canberra",
          "explanation": "Canberra is the capital. Step 1: Sydney and Melbourne are major cities but not capitals. Step 2: Canberra was chosen as a neutral capital."
        }}
        Do not repeat previous questions. Here is the previous quiz history:
        {history_str}
        """

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=['TEXT'],
                temperature=0.9
            )
        )

        if response and hasattr(response, 'candidates') and len(response.candidates) > 0:
            text = ""
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    text += part.text

            start_idx = text.find("```json")
            end_idx = text.find("```", start_idx + 7) if start_idx != -1 else -1
            if start_idx != -1 and end_idx != -1:
                json_text = text[start_idx + 7:end_idx].strip()
            else:
                start_idx = text.find("{")
                end_idx = text.rfind("}") + 1
                json_text = text[start_idx:end_idx].strip() if start_idx != -1 and end_idx != -1 else text.strip()

            try:
                question_data = json.loads(json_text)

                if not all(key in question_data for key in ['question', 'answers', 'correct_answer', 'explanation']):
                    logger.error("Invalid quiz question format")
                    return {"error": "Invalid quiz question format"}
                if len(question_data['answers']) != 4 or question_data['correct_answer'] not in question_data['answers']:
                    logger.error("Invalid answers or correct_answer")
                    return {"error": "Invalid answers or correct_answer"}

                for question in quiz_history:
                    if question['question'] == question_data['question']:
                        logger.warning(f"Duplicate question detected: {question_data['question']}")
                        if retry_count < MAX_RETRIES:
                            return generate_quiz_question(db, user_id, topic, age, effective_year_group, multiplayer, retry_count + 1)
                        logger.error("Unable to generate unique quiz question after max retries")
                        return {"error": "Unable to generate unique quiz question after max retries"}

                question_id = str(uuid.uuid4())
                question_data['question_id'] = question_id

                if not multiplayer and db:
                    history_entry = {
                        "question_id": question_id,
                        "question": question_data['question'],
                        "answers": question_data['answers'],
                        "correct_answer": question_data['correct_answer'],
                        "explanation": question_data['explanation'],
                        "topic": effective_topic,
                        "year_group": effective_year_group,
                        "timestamp": datetime.now().isoformat()
                    }
                    db.collection('users').document(user_id).update({
                        "quiz_history": firestore.ArrayUnion([history_entry])
                    })
                    logger.info(f"Saved to quiz_history for user_id: {user_id}: {history_entry}")

                return {
                    "question_id": question_id,
                    "question": question_data['question'],
                    "answers": question_data['answers'],
                    "correct_answer": question_data['correct_answer'],
                    "explanation": question_data['explanation'],
                    "topic": effective_topic,
                    "year_group": effective_year_group
                }

            except json.JSONDecodeError as e:
                logger.exception(f"JSON decode error: {e}")
                return {"error": "Invalid JSON format in response"}
        else:
            logger.error("Empty or invalid response from Gemini API")
            return {"error": "Unable to generate quiz question"}

    except Exception as e:
        logger.exception(f"Quiz generation error for user_id: {user_id}: {e}")
        return {"error": "Unable to generate quiz question"}

def save_quiz_response(db, user_id, question_id, user_answer, is_correct, question_data, multiplayer=False, game_code=None, score=0, response_time=None):
    try:
        user_ref = db.collection('users').document(user_id)
        response_data = {
            "question_id": question_id,
            "question": question_data['question'],
            "user_answer": user_answer,
            "correct_answer": question_data['correct_answer'],
            "is_correct": is_correct,
            "explanation": question_data.get('explanation', 'No explanation available.'),
            "topic": question_data['topic'],
            "year_group": question_data.get('year_group', 'General'),
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        if multiplayer and game_code:
            response_data.update({
                "score": score,
                "response_time": response_time,
                "game_code": game_code
            })
            user_ref.update({
                f"multiplayer_responses.{game_code}.{question_id}": response_data
            })
            logger.info(f"Saved multiplayer response for user_id: {user_id}, game_code: {game_code}, question_id: {question_id}, score: {score}")
        else:
            user_ref.update({
                f"quiz_responses.{question_id}": response_data
            })
            logger.info(f"Saved single-player response for user_id: {user_id}, question_id: {question_id}, answer: {user_answer}, is_correct: {is_correct}")
    except Exception as e:
        logger.exception(f"Firestore error saving quiz response for user_id: {user_id}, question_id: {question_id}: {e}")

def save_quiz_score(db, user_id, score, total_questions, topic, year_group, results, multiplayer=False, game_code=None):
    try:
        user_ref = db.collection('users').document(user_id)
        quiz_score_entry = {
            'score': score,
            'total_questions': total_questions,
            'topic': topic or 'General',
            'year_group': year_group or 'General',
            'timestamp': datetime.utcnow().isoformat(),
            'mode': 'multiplayer' if multiplayer else 'singleplayer',
            'results': results
        }
        if multiplayer and game_code:
            quiz_score_entry['game_code'] = game_code
        user_ref.update({
            'quiz_scores': firestore.ArrayUnion([quiz_score_entry])
        })
        logger.info(f"Saved quiz score for user_id: {user_id}, score: {score}/{total_questions}, topic: {topic}, year_group: {year_group}")
    except Exception as e:
        logger.exception(f"Error saving quiz score for user_id: {user_id}: {e}")

def save_quiz_to_history(db, user_id, question_data):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            "quiz_history": firestore.ArrayUnion([question_data])
        })
        logger.debug(f"Saved quiz to history for user_id: {user_id}: {question_data}")
    except Exception as e:
        logger.exception(f"Firestore quiz history error for user_id: {user_id}: {e}")

def get_quiz_history(db, user_id, topic=None, year_group=None):
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        quiz_history = user_doc.to_dict().get('quiz_history', []) if user_doc.exists else []
        if topic:
            quiz_history = [q for q in quiz_history if q.get('topic') == topic]
        if year_group:
            quiz_history = [q for q in quiz_history if q.get('year_group') == year_group]
        logger.debug(f"Fetched quiz history for user_id: {user_id}, topic: {topic}, year_group: {year_group}, count: {len(quiz_history)}")
        return quiz_history
    except Exception as e:
        logger.exception(f"Firestore error fetching quiz history for user_id: {user_id}: {e}")
        return []

def get_study_goal(db, user_id):
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        study_goal = user_doc.to_dict().get('study_goal', None) if user_doc.exists else None
        logger.debug(f"Fetched study goal for user_id: {user_id}: {study_goal}")
        return study_goal
    except Exception as e:
        logger.exception(f"Firestore error fetching study goal for user_id: {user_id}: {e}")
        return None

def check_answer(db, user_id, question_id, user_answer, multiplayer=False, game_code=None, response_time=None):
    try:
        user_ref = db.collection('users').document(user_id)
        user_data = user_ref.get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return {"error": "User not found"}

        quiz_history = get_quiz_history(db, user_id)
        for question in quiz_history:
            if question.get('question_id') == question_id:
                correct_answer = question.get('correct_answer')
                is_correct = user_answer == correct_answer
                score = 0
                if multiplayer and is_correct:
                    max_time = 15
                    score = 100
                    if response_time and response_time < max_time:
                        score += int((1 - response_time / max_time) * 50)
                save_quiz_response(db, user_id, question_id, user_answer, is_correct, question, multiplayer, game_code, score, response_time)
                logger.info(f"Checked answer for user_id: {user_id}, question_id: {question_id}, is_correct: {is_correct}, score: {score if multiplayer else 0}")
                return {
                    "result": "correct" if is_correct else "incorrect",
                    "correct_answer": correct_answer,
                    "score": score if multiplayer else 0
                }

        logger.error(f"Question not found for user_id: {user_id}, question_id: {question_id}")
        return {"error": "Question not found"}

    except Exception as e:
        logger.exception(f"Check answer error for user_id: {user_id}, question_id: {question_id}: {e}")
        return {"error": "Failed to check answer"}