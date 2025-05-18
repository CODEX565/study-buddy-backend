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
PASS_THRESHOLD = 0.7  # 70% correct to pass quiz or exam

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
    """Fetch incorrect questions for a user, optionally filtered by topic."""
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
    """Generate a single quiz question, reusing incorrect questions if available."""
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
    """Save user's quiz response to Firestore."""
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
    """Save quiz score and results to Firestore."""
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
    """Save quiz question to user's history."""
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            "quiz_history": firestore.ArrayUnion([question_data])
        })
        logger.debug(f"Saved quiz to history for user_id: {user_id}: {question_data}")
    except Exception as e:
        logger.exception(f"Firestore quiz history error for user_id: {user_id}: {e}")

def get_quiz_history(db, user_id, topic=None, year_group=None):
    """Fetch quiz history, optionally filtered by topic or year group."""
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
    """Fetch user's study goal."""
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
    """Check if the user's answer is correct and save the response."""
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

def generate_full_quiz(db, user_id, topic=None, question_count=5, age=None, year_group=None, group=None):
    """Generate a full quiz with multiple questions."""
    try:
        logger.info(f"Generating full quiz for user_id: {user_id}, topic: {topic}, question_count: {question_count}")
        quiz_id = str(uuid.uuid4())
        questions = []
        for _ in range(question_count):
            question_data = generate_quiz_question(db, user_id, topic, age, year_group)
            if "error" in question_data:
                logger.error(f"Failed to generate question: {question_data['error']}")
                continue
            question_data['user_id'] = user_id
            question_data['timestamp'] = firestore.SERVER_TIMESTAMP
            questions.append(question_data)
            db.collection('quizzes').document(question_data['question_id']).set(question_data)

        quiz_data = {
            "quiz_id": quiz_id,
            "user_id": user_id,
            "topic": topic or "General",
            "year_group": year_group or "General",
            "group": group,
            "questions": questions,
            "status": "pending",
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection('quizzes').document(quiz_id).set(quiz_data)
        logger.debug(f"Generated full quiz for user_id: {user_id}: {quiz_data}")
        return quiz_data
    except Exception as e:
        logger.exception(f"Error generating full quiz for user_id: {user_id}: {e}")
        return {"error": str(e)}

def evaluate_quiz(db, user_id, quiz_id):
    """Evaluate quiz results and determine next steps."""
    try:
        quiz_ref = db.collection('quizzes').document(quiz_id)
        quiz_doc = quiz_ref.get()
        if not quiz_doc.exists:
            logger.warning(f"Quiz not found: {quiz_id}")
            return {"error": "Quiz not found"}

        quiz_data = quiz_doc.to_dict()
        questions = quiz_data.get('questions', [])
        quiz_responses = db.collection('users').document(user_id).get().to_dict().get('quiz_responses', {})

        correct_count = 0
        results = []
        for question in questions:
            question_id = question['question_id']
            response = quiz_responses.get(question_id, {})
            is_correct = response.get('is_correct', False)
            if is_correct:
                correct_count += 1
            results.append({
                "question_id": question_id,
                "question": question['question'],
                "user_answer": response.get('user_answer'),
                "correct_answer": question['correct_answer'],
                "is_correct": is_correct,
                "explanation": question.get('explanation', 'No explanation available.')
            })

        score = correct_count / len(questions) if questions else 0
        passed = score >= PASS_THRESHOLD

        quiz_data['status'] = 'completed'
        quiz_data['score'] = score
        quiz_data['results'] = results
        quiz_ref.update(quiz_data)

        save_quiz_score(db, user_id, correct_count, len(questions), quiz_data['topic'], quiz_data['year_group'], results)

        if passed:
            exam_id = save_to_exam_mode(db, user_id, quiz_id, quiz_data)
            logger.info(f"Quiz passed for user_id: {user_id}, quiz_id: {quiz_id}, saved to exam mode: {exam_id}")
            return {
                "result": "pass",
                "score": score,
                "next_step": "exam_mode",
                "exam_id": exam_id,
                "results": results
            }
        else:
            summary = generate_summary(db, user_id, quiz_id)
            logger.info(f"Quiz failed for user_id: {user_id}, quiz_id: {quiz_id}, proceeding to summary")
            return {
                "result": "fail",
                "score": score,
                "next_step": "summary",
                "summary": summary,
                "results": results
            }

    except Exception as e:
        logger.exception(f"Error evaluating quiz for user_id: {user_id}, quiz_id: {quiz_id}: {e}")
        return {"error": str(e)}

def save_to_exam_mode(db, user_id, quiz_id, quiz_data):
    """Save quiz to exam mode for a full exam-like experience."""
    try:
        exam_id = str(uuid.uuid4())
        exam_data = {
            "exam_id": exam_id,
            "user_id": user_id,
            "quiz_id": quiz_id,
            "topic": quiz_data['topic'],
            "year_group": quiz_data['year_group'],
            "group": quiz_data.get('group'),
            "questions": quiz_data['questions'],
            "status": "pending",
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection('exams').document(exam_id).set(exam_data)
        logger.debug(f"Saved exam for user_id: {user_id}, exam_id: {exam_id}")
        return exam_id
    except Exception as e:
        logger.exception(f"Error saving to exam mode for user_id: {user_id}: {e}")
        return None

def generate_exam(db, user_id, exam_id):
    """Generate exam data for the user."""
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}

        exam_data = exam_doc.to_dict()
        return exam_data
    except Exception as e:
        logger.exception(f"Error generating exam for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def evaluate_exam(db, user_id, exam_id):
    """Evaluate exam results and determine next steps."""
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}

        exam_data = exam_doc.to_dict()
        questions = exam_data.get('questions', [])
        quiz_responses = db.collection('users').document(user_id).get().to_dict().get('quiz_responses', {})

        correct_count = 0
        results = []
        for question in questions:
            question_id = question['question_id']
            response = quiz_responses.get(question_id, {})
            is_correct = response.get('is_correct', False)
            if is_correct:
                correct_count += 1
            results.append({
                "question_id": question_id,
                "question": question['question'],
                "user_answer": response.get('user_answer'),
                "correct_answer": question['correct_answer'],
                "is_correct": is_correct,
                "explanation": question.get('explanation', 'No explanation available.')
            })

        score = correct_count / len(questions) if questions else 0
        passed = score >= PASS_THRESHOLD

        exam_data['status'] = 'completed'
        exam_data['score'] = score
        exam_data['results'] = results
        exam_ref.update(exam_data)

        if passed:
            logger.info(f"Exam passed for user_id: {user_id}, exam_id: {exam_id}")
            return {
                "result": "pass",
                "score": score,
                "next_step": "success",
                "results": results
            }
        else:
            summary = generate_summary(db, user_id, exam_id, is_exam=True)
            logger.info(f"Exam failed for user_id: {user_id}, exam_id: {exam_id}, proceeding to summary")
            return {
                "result": "fail",
                "score": score,
                "next_step": "summary",
                "summary": summary,
                "results": results
            }

    except Exception as e:
        logger.exception(f"Error evaluating exam for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def generate_summary(db, user_id, quiz_or_exam_id, is_exam=False):
    """Generate a summary of quiz or exam results."""
    try:
        collection = 'exams' if is_exam else 'quizzes'
        doc_ref = db.collection(collection).document(quiz_or_exam_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"{'Exam' if is_exam else 'Quiz'} not found: {quiz_or_exam_id}")
            return {"error": f"{'Exam' if is_exam else 'Quiz'} not found"}

        doc_data = doc.to_dict()
        results = doc_data.get('results', [])
        summary = {
            "score": doc_data.get('score', 0),
            "total_questions": len(doc_data.get('questions', [])),
            "correct_count": sum(1 for r in results if r['is_correct']),
            "incorrect_questions": [
                {
                    "question_id": r['question_id'],
                    "question": r['question'],
                    "user_answer": r['user_answer'],
                    "correct_answer": r['correct_answer'],
                    "explanation": r['explanation']
                } for r in results if not r['is_correct']
            ],
            "next_step": "flashcards"
        }
        logger.debug(f"Generated summary for user_id: {user_id}, {'exam' if is_exam else 'quiz'}_id: {quiz_or_exam_id}")
        return summary
    except Exception as e:
        logger.exception(f"Error generating summary for user_id: {user_id}, {'exam' if is_exam else 'quiz'}_id: {quiz_or_exam_id}: {e}")
        return {"error": str(e)}

def generate_flashcards(db, user_id, quiz_or_exam_id, is_exam=False):
    """Generate flashcards based on incorrect questions."""
    try:
        collection = 'exams' if is_exam else 'quizzes'
        doc_ref = db.collection(collection).document(quiz_or_exam_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"{'Exam' if is_exam else 'Quiz'} not found: {quiz_or_exam_id}")
            return {"error": f"{'Exam' if is_exam else 'Quiz'} not found"}

        doc_data = doc.to_dict()
        results = doc_data.get('results', [])
        flashcards = [
            {
                "flashcard_id": str(uuid.uuid4()),
                "question_id": r['question_id'],
                "front": r['question'],
                "back": f"Correct Answer: {r['correct_answer']}\nExplanation: {r['explanation']}",
                "topic": doc_data['topic'],
                "year_group": doc_data['year_group'],
                "timestamp": firestore.SERVER_TIMESTAMP
            } for r in results if not r['is_correct']
        ]

        for flashcard in flashcards:
            db.collection('flashcards').document(flashcard['flashcard_id']).set(flashcard)

        logger.debug(f"Generated {len(flashcards)} flashcards for user_id: {user_id}, {'exam' if is_exam else 'quiz'}_id: {quiz_or_exam_id}")
        return {
            "flashcards": flashcards,
            "next_step": "quiz_again",
            "quiz_id": quiz_or_exam_id if not is_exam else None
        }
    except Exception as e:
        logger.exception(f"Error generating flashcards for user_id: {user_id}, {'exam' if is_exam else 'quiz'}_id: {quiz_or_exam_id}: {e}")
        return {"error": str(e)}

def generate_total_summary(db, user_id):
    try:
        # Initialize summary structure
        summary = {
            'failed_quizzes': [],
            'failed_exams': [],
            'weak_areas': [],
            'recommendations': [],
            'total_failed_attempts': 0,
            'average_score': 0.0
        }
        
        # Query failed quizzes (< 80% score)
        quiz_query = (db.collection('quizzes')
                     .where('user_id', '==', user_id)
                     .where('score', '<', 0.8))
        quizzes = quiz_query.stream()
        
        failed_quiz_count = 0
        total_quiz_score = 0.0
        weak_areas_set = set()
        
        for quiz in quizzes:
            quiz_data = quiz.to_dict()
            quiz_summary = {
                'quiz_id': quiz.id,
                'topic': quiz_data.get('topic', 'General'),
                'correct': quiz_data.get('correct', 0),
                'total': quiz_data.get('total_questions', 1),
                'score': quiz_data.get('score', 0.0),
                'incorrect_questions': []
            }
            # Collect incorrect questions
            for result in quiz_data.get('results', []):
                if not result.get('is_correct', False):
                    question_topic = result.get('topic', quiz_data.get('topic', 'General'))
                    weak_areas_set.add(question_topic)
                    quiz_summary['incorrect_questions'].append({
                        'question': result.get('question'),
                        'user_answer': result.get('user_answer'),
                        'correct_answer': result.get('correct_answer'),
                        'topic': question_topic
                    })
            summary['failed_quizzes'].append(quiz_summary)
            failed_quiz_count += 1
            total_quiz_score += quiz_summary['score']
        
        # Query failed exams (< 80% score)
        exam_query = (db.collection('exams')
                     .where('user_id', '==', user_id)
                     .where('score', '<', 0.8))
        exams = exam_query.stream()
        
        failed_exam_count = 0
        total_exam_score = 0.0
        
        for exam in exams:
            exam_data = exam.to_dict()
            exam_summary = {
                'exam_id': exam.id,
                'topic': exam_data.get('topic', 'General'),
                'correct': exam_data.get('correct', 0),
                'total': exam_data.get('total_questions', 1),
                'score': exam_data.get('score', 0.0),
                'incorrect_questions': []
            }
            # Collect incorrect questions
            for result in exam_data.get('results', []):
                if not result.get('is_correct', False):
                    question_topic = result.get('topic', exam_data.get('topic', 'General'))
                    weak_areas_set.add(question_topic)
                    exam_summary['incorrect_questions'].append({
                        'question': result.get('question'),
                        'user_answer': result.get('user_answer'),
                        'correct_answer': result.get('correct_answer'),
                        'topic': question_topic
                    })
            summary['failed_exams'].append(exam_summary)
            failed_exam_count += 1
            total_exam_score += exam_summary['score']
        
        # Calculate totals and averages
        summary['total_failed_attempts'] = failed_quiz_count + failed_exam_count
        total_attempts = failed_quiz_count + failed_exam_count
        total_score = total_quiz_score + total_exam_score
        summary['average_score'] = (total_score / total_attempts) if total_attempts > 0 else 0.0
        summary['weak_areas'] = list(weak_areas_set)
        
        # Generate recommendations based on weak areas
        for area in summary['weak_areas']:
            recommendation = f"Review {area} concepts. Practice related problems to improve."
            summary['recommendations'].append(recommendation)
        
        return summary
    except Exception as e:
        return {'error': f'Failed to generate total summary: {str(e)}'}