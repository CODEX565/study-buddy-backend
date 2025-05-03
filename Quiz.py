import os
import json
import uuid
from dotenv import load_dotenv
from google import genai
from google.genai import types
from firebase_config import db  # Import Firebase from config file
from firebase_admin import firestore  # Add this import for Firestore

# Load environment variables
load_dotenv()

# Initialize Gemini API
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

# --- Core Quiz Functions ---

MAX_RETRIES = 5

def generate_quiz_question(user_id, topic=None, retry_count=0):
    quiz_history = get_quiz_history(user_id)
    study_goal = get_study_goal(user_id) if not topic else None
    effective_topic = topic or study_goal or 'programming and coding'

    history_str = ""
    for i, question in enumerate(quiz_history):
        history_str += f"Question {i+1}: {question['question']} | Answers: {', '.join(question['answers'])} | Correct Answer: {question['correct_answer']}\n"

    prompt = f"""
    Generate a brand new random quiz question about {effective_topic}.
    Only one answer should be correct. Return strictly in this JSON format:
    {{
      "question": "What is the capital of Australia?",
      "answers": ["Sydney", "Melbourne", "Canberra", "Perth"],
      "correct_answer": "Canberra"
    }}
    Do not repeat previous questions. Here is the previous quiz history:
    {history_str}
    """

    try:
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
            end_idx = text.find("```", start_idx + 7)

            if start_idx != -1 and end_idx != -1:
                json_text = text[start_idx + 7:end_idx].strip()
            else:
                json_text = text.strip()

            try:
                question_data = json.loads(json_text)

                for question in quiz_history:
                    if question['question'] == question_data['question']:
                        print(f"[Duplicate Question] {question_data['question']}")
                        if retry_count < MAX_RETRIES:
                            return generate_quiz_question(user_id, topic, retry_count + 1)
                        return {"error": "Unable to generate unique quiz question"}

                question_data['question_id'] = str(uuid.uuid4())
                save_quiz_to_history(user_id, question_data)

                return {
                    "question": question_data['question'],
                    "answers": question_data['answers'],
                    "correct_answer": question_data['correct_answer'],
                    "question_id": question_data['question_id']
                }

            except json.JSONDecodeError as e:
                print(f"[JSON Decode Error] {e}")
                return {"error": "Invalid JSON format in response"}
        else:
            print("[Quiz Generation Error] Empty or invalid response")
            return {"error": "Unable to generate quiz question"}

    except Exception as e:
        print(f"[Quiz Generation Error] {e}")
        return {"error": "Unable to generate quiz question"}

def save_quiz_response(user_id, question_id, user_answer, is_correct):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            f"quiz_responses.{question_id}": {
                "answer": user_answer,
                "is_correct": is_correct,
                "timestamp": firestore.SERVER_TIMESTAMP
            }
        })
    except Exception as e:
        print(f"[Firestore Error] {e}")

def save_quiz_to_history(user_id, question_data):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            "quiz_history": firestore.ArrayUnion([question_data])
        })
    except Exception as e:
        print(f"[Firestore Quiz History Error] {e}")

def get_quiz_history(user_id):
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        return user_doc.to_dict().get('quiz_history', []) if user_doc.exists else []
    except Exception as e:
        print(f"[Firestore Error] {e}")
        return []

def get_study_goal(user_id):
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        return user_doc.to_dict().get('study_goal', None) if user_doc.exists else None
    except Exception as e:
        print(f"[Firestore Error] {e}")
        return None

def check_answer(user_id, question_id, user_answer):
    quiz_history = get_quiz_history(user_id)

    for question in quiz_history:
        if question.get('question_id') == question_id:
            correct_answer = question.get('correct_answer')
            is_correct = user_answer == correct_answer
            save_quiz_response(user_id, question_id, user_answer, is_correct)
            return {"result": "correct" if is_correct else "incorrect"}

    return {"error": "Question not found in history"}
