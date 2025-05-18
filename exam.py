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
        logging.FileHandler('exam.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

MAX_RETRIES = 5
PASS_THRESHOLD = 0.7  # 70% correct to pass exam

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

def generate_exam_question(db, user_id, topic=None, age=None, year_group=None, retry_count=0):
    """Generate a single exam question, assuming questions come from prior learning."""
    try:
        logger.info(f"Generating exam question for user_id: {user_id}, topic: {topic}, age: {age}, year_group: {year_group}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return {"error": "User not found"}

        memories = user_data.get('memories', [])

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

        if topic and topic != 'General':
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

        year_group_prompt = f" suitable for {effective_year_group} students" if effective_year_group and effective_year_group != 'General' else ""
        prompt = f"""
        Generate a brand new random exam question about {effective_topic}{year_group_prompt}.
        Only one answer should be correct. Provide a concise step-by-step explanation (2-3 short steps, max 50-60 words) for the correct answer.
        Return strictly in this JSON format:
        {{
          "question": "What is the capital of Australia?",
          "answers": ["Sydney", "Melbourne", "Canberra", "Perth"],
          "correct_answer": "Canberra",
          "explanation": "Canberra is the capital. Step 1: Sydney and Melbourne are major cities but not capitals. Step 2: Canberra was chosen as a neutral capital."
        }}
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
                    logger.error("Invalid exam question format")
                    return {"error": "Invalid exam question format"}
                if len(question_data['answers']) != 4 or question_data['correct_answer'] not in question_data['answers']:
                    logger.error("Invalid answers or correct_answer")
                    return {"error": "Invalid answers or correct_answer"}

                question_id = str(uuid.uuid4())
                question_data['question_id'] = question_id

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
            return {"error": "Unable to generate exam question"}

    except Exception as e:
        logger.exception(f"Exam question generation error for user_id: {user_id}: {e}")
        return {"error": "Unable to generate exam question"}

def generate_full_exam(db, user_id, topic=None, question_count=10, age=None, year_group=None):
    """Generate a full exam with multiple questions."""
    try:
        logger.info(f"Generating full exam for user_id: {user_id}, topic: {topic}, question_count: {question_count}")
        exam_id = str(uuid.uuid4())
        questions = []
        for _ in range(question_count):
            question_data = generate_exam_question(db, user_id, topic, age, year_group)
            if "error" in question_data:
                logger.error(f"Failed to generate question: {question_data['error']}")
                continue
            question_data['user_id'] = user_id
            question_data['timestamp'] = firestore.SERVER_TIMESTAMP
            questions.append(question_data)
            db.collection('exams').document(question_data['question_id']).set(question_data)

        exam_data = {
            "exam_id": exam_id,
            "user_id": user_id,
            "topic": topic or "General",
            "year_group": year_group or "General",
            "questions": questions,
            "status": "pending",
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection('exams').document(exam_id).set(exam_data)
        logger.debug(f"Generated full exam for user_id: {user_id}: {exam_data}")
        return exam_data
    except Exception as e:
        logger.exception(f"Error generating full exam for user_id: {user_id}: {e}")
        return {"error": str(e)}

def start_exam_attempt(db, user_id, exam_id):
    """Start an exam attempt and return questions without answers for an exam vibe."""
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}
        
        exam_data = exam_doc.to_dict()
        questions = exam_data.get('questions', [])
        if not questions:
            logger.error(f"No questions found in exam: {exam_id}")
            return {"error": "No questions available"}
        
        # Prepare questions without revealing correct answers
        exam_questions = [
            {
                "question_id": q['question_id'],
                "question": q['question'],
                "answers": q['answers'],
                "topic": q['topic'],
                "year_group": q['year_group']
            } for q in questions
        ]
        
        exam_data['status'] = 'in_progress'
        exam_ref.update(exam_data)
        
        return {
            "exam_id": exam_id,
            "questions": exam_questions,
            "status": "in_progress"
        }
    except Exception as e:
        logger.exception(f"Error starting exam attempt for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def submit_exam_answers(db, user_id, exam_id, answers):
    """Submit all exam answers at once and save responses without immediate feedback."""
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}
        
        exam_data = exam_doc.to_dict()
        questions = exam_data.get('questions', [])
        if len(answers) != len(questions):
            logger.error(f"Answer count mismatch for exam_id: {exam_id}, expected {len(questions)}, got {len(answers)}")
            return {"error": "Answer count mismatch"}

        for i, question in enumerate(questions):
            question_id = question['question_id']
            user_answer = answers.get(str(i))
            if user_answer:
                response_data = {
                    "question_id": question_id,
                    "question": question['question'],
                    "user_answer": user_answer,
                    "correct_answer": question['correct_answer'],
                    "is_correct": False,  # Will be updated during evaluation
                    "explanation": question.get('explanation', 'No explanation available.'),
                    "topic": question['topic'],
                    "year_group": question.get('year_group', 'General'),
                    "timestamp": firestore.SERVER_TIMESTAMP
                }
                db.collection('users').document(user_id).update({
                    f"exam_responses.{question_id}": response_data
                })
        
        exam_data['status'] = 'submitted'
        exam_ref.update(exam_data)
        logger.info(f"Exam answers submitted for user_id: {user_id}, exam_id: {exam_id}")
        return {"exam_id": exam_id, "status": "submitted"}
    except Exception as e:
        logger.exception(f"Error submitting exam answers for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def save_exam_score(db, user_id, score, total_questions, topic, year_group, results):
    """Save exam score and results to Firestore."""
    try:
        user_ref = db.collection('users').document(user_id)
        exam_score_entry = {
            'score': score,
            'total_questions': total_questions,
            'topic': topic or 'General',
            'year_group': year_group or 'General',
            'timestamp': datetime.utcnow().isoformat(),
            'mode': 'exam',
            'results': results
        }
        user_ref.update({
            'exam_scores': firestore.ArrayUnion([exam_score_entry])
        })
        logger.info(f"Saved exam score for user_id: {user_id}, score: {score}/{total_questions}, topic: {topic}, year_group: {year_group}")
    except Exception as e:
        logger.exception(f"Error saving exam score for user_id: {user_id}: {e}")

def evaluate_exam(db, user_id, exam_id):
    """Evaluate exam results after submission and determine next steps."""
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}

        exam_data = exam_doc.to_dict()
        if exam_data.get('status') != 'submitted':
            logger.warning(f"Exam not submitted yet: {exam_id}")
            return {"error": "Exam not submitted"}

        questions = exam_data.get('questions', [])
        exam_responses = db.collection('users').document(user_id).get().to_dict().get('exam_responses', {})

        correct_count = 0
        results = []
        for question in questions:
            question_id = question['question_id']
            response = exam_responses.get(question_id, {})
            user_answer = response.get('user_answer')
            correct_answer = question['correct_answer']
            is_correct = user_answer == correct_answer if user_answer else False
            if is_correct:
                correct_count += 1
            results.append({
                "question_id": question_id,
                "question": question['question'],
                "user_answer": user_answer,
                "correct_answer": correct_answer,
                "is_correct": is_correct,
                "explanation": question.get('explanation', 'No explanation available.')
            })

            # Update the response with correct evaluation
            if question_id in exam_responses:
                exam_responses[question_id]['is_correct'] = is_correct
                db.collection('users').document(user_id).update({
                    f"exam_responses.{question_id}.is_correct": is_correct
                })

        score = correct_count / len(questions) if questions else 0
        passed = score >= PASS_THRESHOLD

        exam_data['status'] = 'completed'
        exam_data['score'] = score
        exam_data['results'] = results
        exam_ref.update(exam_data)

        save_exam_score(db, user_id, correct_count, len(questions), exam_data['topic'], exam_data['year_group'], results)

        if passed:
            feedback = {
                "message": f"Congratulations! You passed the exam with a score of {score*100:.0f}%. Great job!",
                "date": datetime.utcnow().isoformat()
            }
            logger.info(f"Exam passed for user_id: {user_id}, exam_id: {exam_id}")
            return {
                "result": "pass",
                "score": score,
                "next_step": "success",
                "results": results,
                "feedback": feedback
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

def generate_summary(db, user_id, exam_id, is_exam=True):
    """Generate a summary of exam results."""
    try:
        doc_ref = db.collection('exams').document(exam_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}

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
        logger.debug(f"Generated summary for user_id: {user_id}, exam_id: {exam_id}")
        return summary
    except Exception as e:
        logger.exception(f"Error generating summary for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def generate_flashcards(db, user_id, exam_id, is_exam=True):
    """Generate flashcards based on incorrect questions."""
    try:
        doc_ref = db.collection('exams').document(exam_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}

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

        logger.debug(f"Generated {len(flashcards)} flashcards for user_id: {user_id}, exam_id: {exam_id}")
        return {
            "flashcards": flashcards,
            "next_step": "exam_again",
            "exam_id": exam_id
        }
    except Exception as e:
        logger.exception(f"Error generating flashcards for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def generate_total_summary(db, user_id):
    """Generate a summary of failed exams for review."""
    try:
        summary = {
            'failed_exams': [],
            'weak_areas': [],
            'recommendations': [],
            'total_failed_attempts': 0,
            'average_score': 0.0
        }
        
        exam_query = (db.collection('exams')
                     .where('user_id', '==', user_id)
                     .where('score', '<', PASS_THRESHOLD))
        exams = exam_query.stream()
        
        failed_exam_count = 0
        total_exam_score = 0.0
        weak_areas_set = set()
        
        for exam in exams:
            exam_data = exam.to_dict()
            exam_summary = {
                'exam_id': exam.id,
                'topic': exam_data.get('topic', 'General'),
                'correct': sum(1 for r in exam_data.get('results', []) if r.get('is_correct', False)),
                'total': len(exam_data.get('questions', [])),
                'score': exam_data.get('score', 0.0),
                'incorrect_questions': []
            }
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
        
        summary['total_failed_attempts'] = failed_exam_count
        summary['average_score'] = (total_exam_score / failed_exam_count) if failed_exam_count > 0 else 0.0
        summary['weak_areas'] = list(weak_areas_set)
        
        for area in summary['weak_areas']:
            recommendation = f"Review {area} concepts. Practice related problems to improve."
            summary['recommendations'].append(recommendation)
        
        return summary
    except Exception as e:
        return {'error': f'Failed to generate total summary: {str(e)}'}