import os
import uuid
import random
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
    handlers=[logging.FileHandler('exam.log'), logging.StreamHandler()]
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

# --- Helper Functions ---
def get_user_data(user_id):
    user_ref = db.collection('users').document(user_id)
    doc = user_ref.get()
    return doc.to_dict() if doc.exists else None

def get_mastered_topics(user_id, subject):
    user_data = get_user_data(user_id)
    if not user_data:
        return []
    return [topic for topic, prof in user_data.get('subjects_mastery', {}).get(subject, {}).items() if prof > 0.6]

def fetch_study_material_question(subject, topic, difficulty=None):
    try:
        questions_ref = db.collection('study_material').document(subject).collection(topic).document('questions')
        doc = questions_ref.get()
        if doc.exists:
            questions = doc.to_dict().get('questions', [])
            if difficulty:
                filtered = [q for q in questions if q.get('difficulty') == difficulty]
                if filtered:
                    return random.choice(filtered)
            if questions:
                return random.choice(questions)
        return None
    except Exception as e:
        logger.warning(f"Error fetching study material: {e}")
        return None

def generate_gemini_exam_question(subject, topic, difficulty, age):
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Generate a {difficulty} level exam question for {topic} in {subject} for a {age}-year-old.
    Provide one correct answer and three incorrect answers. Include a concise explanation (50-60 words).
    Return in JSON format:
    {{
      "question": "...",
      "answers": ["...", "...", "...", "..."],
      "correct_answer": "...",
      "explanation": "..."
    }}
    """
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=['TEXT'], temperature=0.9)
    )
    text = response.candidates[0].content.parts[0].text
    json_text = text.strip('```json').strip('```').strip()
    try:
        question = json.loads(json_text)
        return question
    except Exception as e:
        logger.error(f"Gemini exam question generation error: {e}")
        return None

def create_exam(user_id, subject, num_questions=25, age=None):
    user_data = get_user_data(user_id)
    if not user_data:
        return {"error": "User not found"}
    age = age or user_data.get('age', 15)
    mastered_topics = get_mastered_topics(user_id, subject)
    if not mastered_topics:
        return {"error": "No mastered topics available"}
    difficulties = ['easy', 'medium', 'hard']
    questions = []
    for _ in range(num_questions):
        topic = random.choice(mastered_topics)
        difficulty = random.choice(difficulties)
        q = fetch_study_material_question(subject, topic, difficulty)
        if not q:
            q = generate_gemini_exam_question(subject, topic, difficulty, age)
        if q:
            q['question_id'] = str(uuid.uuid4())
            q['subject'] = subject
            q['topic'] = topic
            q['difficulty'] = difficulty
            q['created_at'] = firestore.SERVER_TIMESTAMP
            questions.append(q)
    exam_id = str(uuid.uuid4())
    exam_obj = {
        "exam_id": exam_id,
        "subject": subject,
        "topics": mastered_topics,
        "questions": questions,
        "status": "in_progress",
        "created_at": firestore.SERVER_TIMESTAMP,
        "num_questions": num_questions
    }
    db.collection('users').document(user_id).update({
        "exam_history": firestore.ArrayUnion([exam_obj])
    })
    return {
        "exam_id": exam_id,
        "questions": questions,
        "status": "in_progress"
    }

def submit_exam(user_id, exam_id, responses):
    user_ref = db.collection('users').document(user_id)
    user_data = user_ref.get().to_dict()
    if not user_data:
        return {"error": "User not found"}
    exam_history = user_data.get('exam_history', [])
    exam = next((e for e in exam_history if e['exam_id'] == exam_id), None)
    if not exam:
        return {"error": "Exam not found"}
    questions = exam['questions']
    subject = exam['subject']
    topics = exam['topics']
    correct_count = 0
    exam_responses = []
    for resp in responses:
        question_id = resp['question_id']
        user_answer = resp['user_answer']
        question = next((q for q in questions if q['question_id'] == question_id), None)
        if not question:
            continue
        is_correct = user_answer == question['correct_answer']
        if is_correct:
            correct_count += 1
        exam_responses.append({
            "question_id": question_id,
            "user_answer": user_answer,
            "correct_answer": question['correct_answer'],
            "is_correct": is_correct,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    score = correct_count / len(questions) if questions else 0
    passed = score >= PASS_THRESHOLD
    # Update subjects_mastery and rewards
    subjects_mastery = user_data.get('subjects_mastery', {})
    updates = {}
    if passed:
        for topic in topics:
            subject_mastery = subjects_mastery.get(subject, {})
            current_prof = subject_mastery.get(topic, 0.0)
            subject_mastery[topic] = min(1.0, current_prof + 0.2)
            subjects_mastery[subject] = subject_mastery
        updates['xp'] = user_data.get('xp', 0) + 50
        badges = user_data.get('badges', [])
        badge_name = f"{subject} Master"
        if badge_name not in badges:
            badges.append(badge_name)
        updates['badges'] = badges
    # Save exam results
    exam_summary = {
        "exam_id": exam_id,
        "score": score,
        "correct": correct_count,
        "total": len(questions),
        "timestamp": firestore.SERVER_TIMESTAMP
    }
    user_ref.update({
        "subjects_mastery": subjects_mastery,
        "exam_scores": firestore.ArrayUnion([exam_summary]),
        "exam_history": firestore.ArrayUnion([{**exam, "status": "completed", "score": score}]),
        **updates
    })
    return {
        "exam_id": exam_id,
        "score": score,
        "passed": passed,
        "next_step": "success" if passed else "flashcards"
    }