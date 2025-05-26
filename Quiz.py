import os
import json
import uuid
import random
import logging
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore
from collections import Counter

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('quiz.log'),
        logging.StreamHandler()
    ]
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

PASS_THRESHOLD = 0.8  # 80% to pass quiz

# --- Helper Functions ---
def map_age_to_year_group(age_or_year):
    if isinstance(age_or_year, str) and age_or_year.startswith('Year '):
        return age_or_year  # Already a year group
    try:
        age = int(age_or_year)
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

def get_user_data(user_id):
    user_ref = db.collection('users').document(user_id)
    doc = user_ref.get()
    return doc.to_dict() if doc.exists else None

def determine_difficulty(proficiency):
    if proficiency < 0.4:
        return 'easy'
    elif proficiency < 0.7:
        return 'medium'
    else:
        return 'hard'

def fetch_study_material_question(subject, topic, difficulty):
    try:
        questions_ref = db.collection('study_material').document(subject).collection(topic).document('questions')
        doc = questions_ref.get()
        if doc.exists:
            questions = doc.to_dict().get('questions', [])
            filtered = [q for q in questions if q.get('difficulty') == difficulty]
            if filtered:
                return random.choice(filtered)
        return None
    except Exception as e:
        logger.warning(f"Error fetching study material: {e}")
        return None

def generate_gemini_questions(subject, topic, difficulty, age, num_questions):
    client = genai.Client(api_key=GEMINI_API_KEY)
    year_group = map_age_to_year_group(age)
    year_group_prompt = f" suitable for {year_group} students" if year_group != 'General' else ""
    prompt = f"""
    Generate {num_questions} {difficulty} level {topic} questions in {subject}{year_group_prompt}.
    Each question should have 1 correct answer and 3 incorrect answers, and a concise explanation (max 50-60 words).
    Return as a JSON array in this format:
    [
      {{
        "question": "...",
        "answers": ["...", "...", "...", "..."],
        "correct_answer": "...",
        "explanation": "..."
      }}, ...
    ]
    """
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=['TEXT'], temperature=0.9)
    )
    text = response.candidates[0].content.parts[0].text
    json_text = text.strip('```json').strip('```').strip()
    try:
        questions = json.loads(json_text)
        return questions
    except Exception as e:
        logger.error(f"Gemini question generation error: {e}")
        return []

def get_random_topics_for_year_group(year_group):
    # Define topics by year group
    topics_by_year = {
        'Year 1': {
            'Mathematics': ['Numbers', 'Basic Addition', 'Basic Subtraction', 'Shapes', 'Counting'],
            'English': ['Phonics', 'Basic Reading', 'Simple Writing', 'Vocabulary'],
            'Science': ['Plants', 'Animals', 'Weather', 'Materials']
        },
        'Year 2': {
            'Mathematics': ['Addition', 'Subtraction', 'Multiplication', 'Division', 'Fractions'],
            'English': ['Reading Comprehension', 'Writing', 'Grammar', 'Spelling'],
            'Science': ['Living Things', 'Materials', 'Space', 'Forces']
        },
        'Year 3': {
            'Mathematics': ['Fractions', 'Decimals', 'Geometry', 'Measurement'],
            'English': ['Creative Writing', 'Advanced Grammar', 'Punctuation'],
            'Science': ['Light', 'Sound', 'Magnets', 'Rocks']
        },
        'Year 4': {
            'Mathematics': ['Algebra', 'Statistics', 'Advanced Geometry', 'Problem Solving'],
            'English': ['Advanced Writing', 'Literature', 'Poetry', 'Comprehension'],
            'Science': ['Electricity', 'States of Matter', 'Food Chains', 'Human Body']
        },
        'Year 5': {
            'Mathematics': ['Advanced Algebra', 'Probability', 'Complex Geometry'],
            'English': ['Essay Writing', 'Advanced Literature', 'Text Analysis'],
            'Science': ['Forces', 'Earth and Space', 'Properties of Materials']
        },
        'Year 6': {
            'Mathematics': ['Advanced Problem Solving', 'Statistics and Data', 'Complex Operations'],
            'English': ['Advanced Essay Writing', 'Text Analysis', 'Research Skills'],
            'Science': ['Evolution', 'Living Systems', 'Light and Sound']
        },
        'Year 7': {
            'Mathematics': ['Complex Algebra', 'Calculus Basics', 'Advanced Statistics'],
            'English': ['Academic Writing', 'Critical Analysis', 'Research Methods'],
            'Science': ['Chemistry Basics', 'Physics Principles', 'Biology Systems']
        },
        'General': {
            'Mathematics': ['Basic Math', 'Problem Solving', 'Numbers', 'Geometry'],
            'English': ['Reading', 'Writing', 'Grammar', 'Vocabulary'],
            'Science': ['General Science', 'Nature', 'Technology', 'Environment']
        }
    }
    
    year_topics = topics_by_year.get(year_group, topics_by_year['General'])
    selected_subject = random.choice(list(year_topics.keys()))
    selected_topic = random.choice(year_topics[selected_subject])
    
    return selected_subject, selected_topic

# --- Core Functions ---
def create_quiz(user_id, subject=None, topic=None, num_questions=10, age=None, year_group=None, group=None):
    user_data = get_user_data(user_id)
    if not user_data:
        logger.error(f"User not found: {user_id}")
        return {"error": "User not found"}
    
    # Handle age/year group logic
    if year_group and year_group.startswith('Year '):
        effective_year_group = year_group
    else:
        age_to_use = age or user_data.get('age', 15)
        effective_year_group = map_age_to_year_group(age_to_use)
    
    # Get user's study topics and history
    study_topics, subjects_mastery, _ = get_user_study_topics(user_id)
    
    # For general quizzes, select topics intelligently
    if subject == 'General' or (subject is None and topic is None) or topic == 'General':
        # First check if there are topics that need improvement
        topics_to_improve = get_recommended_topics(user_id)
        
        if topics_to_improve:
            # 70% chance to pick a topic that needs improvement
            if random.random() < 0.7:
                recommended = random.choice(topics_to_improve)
                subject = recommended['subject']
                topic = recommended['topic']
            else:
                # 30% chance to pick a new topic appropriate for the year group
                subject, topic = get_random_topics_for_year_group(effective_year_group)
        else:
            # If no topics need improvement, pick age-appropriate topic
            subject, topic = get_random_topics_for_year_group(effective_year_group)
            
        logger.info(f"Selected topic for general quiz: subject={subject}, topic={topic}")
    
    # Ensure subject and topic are set if they were None initially and not General
    if subject is None or topic is None:
         logger.error("Subject or topic not provided and could not be determined for a general quiz.")
         return {"error": "Subject or topic not specified and could not be determined."}

    logger.info(f"Attempting to create quiz for user {user_id}, Subject: {subject}, Topic: {topic}, Difficulty: determined based on mastery, Year Group: {effective_year_group}, Num Questions: {num_questions}")

    proficiency = user_data.get('subjects_mastery', {}).get(subject, {}).get(topic, 0.0)
    difficulty = determine_difficulty(proficiency)
    questions = [] # Initialize empty questions list

    # Try to fetch from study_material first
    study_material_questions_count = 0
    for _ in range(num_questions):
        q = fetch_study_material_question(subject, topic, difficulty)
        if q:
            q['question_id'] = str(uuid.uuid4())
            q['subject'] = subject
            q['topic'] = topic
            q['difficulty'] = difficulty
            q['created_at'] = datetime.now().isoformat()
            questions.append(q)
            study_material_questions_count += 1

    logger.info(f"Fetched {study_material_questions_count} questions from study material for topic {topic}.")

    # If not enough, generate with Gemini
    gemini_generated_count = 0
    if len(questions) < num_questions:
        needed = num_questions - len(questions)
        logger.info(f"Need {needed} more questions. Attempting to generate with Gemini.")
        gemini_questions = generate_gemini_questions(subject, topic, difficulty, effective_year_group, needed)
        if gemini_questions:
            for q in gemini_questions:
                q['question_id'] = str(uuid.uuid4())
                q['subject'] = subject
                q['topic'] = topic
                q['difficulty'] = difficulty
                q['created_at'] = datetime.now().isoformat()
                questions.append(q)
                gemini_generated_count += 1
            logger.info(f"Generated {gemini_generated_count} questions with Gemini.")
        else:
             logger.warning("Gemini did not generate any questions.")

    logger.info(f"Total questions collected for quiz: {len(questions)}")

    # Save quiz to user quiz_history
    quiz_id = str(uuid.uuid4())
    quiz_obj = {
        "quiz_id": quiz_id,
        "subject": subject,
        "topic": topic,
        "questions": questions,
        "status": "in_progress",
        "created_at": datetime.now().isoformat(),
        "num_questions": num_questions,
        "year_group": effective_year_group,
        "group": group or ""
    }
    
    db.collection('users').document(user_id).update({
        "quiz_history": firestore.ArrayUnion([quiz_obj]),
        "quiz_last_active": firestore.SERVER_TIMESTAMP  # This is fine as it's not in an array
    })
    
    return {
        "quiz_id": quiz_id,
        "questions": questions,
        "status": "in_progress",
        "year_group": effective_year_group
    }

def submit_quiz(user_id, quiz_id, responses):
    user_ref = db.collection('users').document(user_id)
    user_data = user_ref.get().to_dict()
    if not user_data:
        return {"error": "User not found"}
        
    quiz_history = user_data.get('quiz_history', [])
    quiz = next((q for q in quiz_history if q['quiz_id'] == quiz_id), None)
    if not quiz:
        return {"error": "Quiz not found"}
        
    questions = quiz['questions']
    subject = quiz['subject']
    topic = quiz['topic']
    
    # Validate responses
    if len(responses) != len(questions):
        return {"error": f"Expected {len(questions)} answers, got {len(responses)}"}
        
    correct_count = 0
    quiz_responses = []
    results = []
    
    for resp in responses:
        question_id = resp['question_id']
        user_answer = resp['user_answer']
        timestamp = resp.get('timestamp', datetime.now().isoformat())
        
        question = next((q for q in questions if q['question_id'] == question_id), None)
        if not question:
            continue
            
        is_correct = user_answer == question['correct_answer']
        if is_correct:
            correct_count += 1
            
        response_data = {
            "question_id": question_id,
            "question": question['question'],
            "user_answer": user_answer,
            "correct_answer": question['correct_answer'],
            "is_correct": is_correct,
            "explanation": question.get('explanation', ''),
            "topic": topic,
            "subject": subject,
            "timestamp": timestamp,
            "difficulty": question.get('difficulty', 'medium')
        }
        quiz_responses.append(response_data)
        results.append({
            "question_id": question_id,
            "is_correct": is_correct,
            "user_answer": user_answer,
            "correct_answer": question['correct_answer'],
            "explanation": question.get('explanation', ''),
            "question": question['question'],
            "topic_mastery": subject_mastery.get(topic, 0.0)
        })

    score = correct_count / len(questions) if questions else 0
    passed = score >= PASS_THRESHOLD
    
    # Update subjects_mastery with weighted difficulty adjustment
    subjects_mastery = user_data.get('subjects_mastery', {})
    subject_mastery = subjects_mastery.get(subject, {})
    current_proficiency = subject_mastery.get(topic, 0.0)
    
    # Adjust mastery based on difficulty and score
    difficulty_weights = {'easy': 0.5, 'medium': 1.0, 'hard': 1.5}
    avg_difficulty = sum(difficulty_weights.get(q.get('difficulty', 'medium'), 1.0) for q in questions) / len(questions)
    
    delta = (0.1 * avg_difficulty) if score >= PASS_THRESHOLD else (-0.05 * avg_difficulty) if score < 0.5 else 0.0
    new_proficiency = max(0.0, min(1.0, current_proficiency + delta))
    
    subject_mastery[topic] = new_proficiency
    subjects_mastery[subject] = subject_mastery
    
    # Save quiz results with timestamps
    quiz_summary = {
        "subject": subject,
        "topic": topic,
        "correct": correct_count,
        "wrong": len(questions) - correct_count,
        "score": score,
        "timestamp": firestore.SERVER_TIMESTAMP,
        "difficulty": dict(Counter(q.get('difficulty', 'medium') for q in questions))
    }
    
    quiz_score = {
        "quiz_id": quiz_id,
        "score": score,
        "correct": correct_count,
        "total": len(questions),
        "timestamp": firestore.SERVER_TIMESTAMP,
        "avg_difficulty": avg_difficulty
    }
    
    # Update user data and learning history
    performance_data = {
        "quiz_id": quiz_id,
        "score": score,
        "mastery_level": new_proficiency,
        "questions_total": len(questions),
        "questions_correct": correct_count,
        "difficulty_distribution": dict(Counter(q.get('difficulty', 'medium') for q in questions))
    }
    
    # Update learning history
    update_learning_history(user_id, subject, topic, performance_data)
    
    # Update user data
    user_ref.update({
        "subjects_mastery": subjects_mastery,
        "quiz_summary": firestore.ArrayUnion([quiz_summary]),
        "quiz_scores": firestore.ArrayUnion([quiz_score]),
        "quiz_responses": firestore.ArrayUnion(quiz_responses),
        "quiz_last_active": firestore.SERVER_TIMESTAMP,
        f"quiz_history.{quiz_id}.status": "completed",
        f"quiz_history.{quiz_id}.completed_at": firestore.SERVER_TIMESTAMP
    })

    # Prepare next steps suggestions
    next_steps = {
        "flashcards": score < PASS_THRESHOLD,
        "exam_ready": score >= PASS_THRESHOLD and new_proficiency >= 0.8,
        "practice_needed": score < 0.7,
        "suggested_topics": []
    }
    
    if not passed:
        wrong_topics = [r["topic"] for r in results if not r["is_correct"]]
        next_steps["suggested_topics"] = list(set(wrong_topics))

    return {
        "quiz_id": quiz_id,
        "score": score,
        "passed": passed,
        "results": results,
        "mastery_level": new_proficiency,
        "next_steps": next_steps,
        "timestamp": datetime.now().isoformat()
    }

# --- Study Topics and Learning Management ---
def get_user_study_topics(user_id):
    """Fetch user's study topics and learning history."""
    user_data = get_user_data(user_id)
    if not user_data:
        return [], {}
        
    study_topics = user_data.get('study_topics', [])
    subjects_mastery = user_data.get('subjects_mastery', {})
    learning_history = user_data.get('learning_history', [])
    
    return study_topics, subjects_mastery, learning_history

def update_learning_history(user_id, subject, topic, performance_data):
    """Update user's learning history with new study activity."""
    user_ref = db.collection('users').document(user_id)
    
    new_entry = {
        "subject": subject,
        "topic": topic,
        "activity_type": "quiz",
        "timestamp": datetime.now().isoformat(),
        "performance": performance_data
    }
    
    user_ref.update({
        "learning_history": firestore.ArrayUnion([new_entry])
    })

def get_recommended_topics(user_id):
    """Get recommended topics based on user's study history and mastery levels."""
    study_topics, subjects_mastery, learning_history = get_user_study_topics(user_id)
    
    # Find topics that need improvement (mastery < 0.7)
    topics_to_improve = []
    for subject, topics in subjects_mastery.items():
        for topic, mastery in topics.items():
            if mastery < 0.7:  # Below 70% mastery
                topics_to_improve.append({
                    "subject": subject,
                    "topic": topic,
                    "mastery": mastery
                })
    
    # Sort by mastery level (focus on lowest mastery first)
    topics_to_improve.sort(key=lambda x: x['mastery'])
    
    return topics_to_improve

def get_topics_for_year_group(year_group):
    """Get age-appropriate topics for a year group."""
    # Define topics by year group
    topics_by_year = {
        'Year 1': {
            'Mathematics': ['Numbers', 'Basic Addition', 'Basic Subtraction', 'Shapes', 'Counting'],
            'English': ['Phonics', 'Basic Reading', 'Simple Writing', 'Vocabulary'],
            'Science': ['Plants', 'Animals', 'Weather', 'Materials']
        },
        'Year 2': {
            'Mathematics': ['Addition', 'Subtraction', 'Multiplication', 'Division', 'Fractions'],
            'English': ['Reading Comprehension', 'Writing', 'Grammar', 'Spelling'],
            'Science': ['Living Things', 'Materials', 'Space', 'Forces']
        },
        'Year 3': {
            'Mathematics': ['Fractions', 'Decimals', 'Geometry', 'Measurement'],
            'English': ['Creative Writing', 'Advanced Grammar', 'Punctuation'],
            'Science': ['Light', 'Sound', 'Magnets', 'Rocks']
        },
        'Year 4': {
            'Mathematics': ['Algebra', 'Statistics', 'Advanced Geometry', 'Problem Solving'],
            'English': ['Advanced Writing', 'Literature', 'Poetry', 'Comprehension'],
            'Science': ['Electricity', 'States of Matter', 'Food Chains', 'Human Body']
        },
        'Year 5': {
            'Mathematics': ['Advanced Algebra', 'Probability', 'Complex Geometry'],
            'English': ['Essay Writing', 'Advanced Literature', 'Text Analysis'],
            'Science': ['Forces', 'Earth and Space', 'Properties of Materials']
        },
        'Year 6': {
            'Mathematics': ['Advanced Problem Solving', 'Statistics and Data', 'Complex Operations'],
            'English': ['Advanced Essay Writing', 'Text Analysis', 'Research Skills'],
            'Science': ['Evolution', 'Living Systems', 'Light and Sound']
        },
        'Year 7': {
            'Mathematics': ['Complex Algebra', 'Calculus Basics', 'Advanced Statistics'],
            'English': ['Academic Writing', 'Critical Analysis', 'Research Methods'],
            'Science': ['Chemistry Basics', 'Physics Principles', 'Biology Systems']
        },
        'General': {
            'Mathematics': ['Basic Math', 'Problem Solving', 'Numbers', 'Geometry'],
            'English': ['Reading', 'Writing', 'Grammar', 'Vocabulary'],
            'Science': ['General Science', 'Nature', 'Technology', 'Environment']
        }
    }
    
    year_topics = topics_by_year.get(year_group, topics_by_year['General'])
    
    # Add learning history to recommended topics
    recommended = []
    for subject, topics in year_topics.items():
        for topic in topics:
            recommended.append({
                "subject": subject,
                "topic": topic,
                "type": "year_appropriate"
            })
    
    return recommended
