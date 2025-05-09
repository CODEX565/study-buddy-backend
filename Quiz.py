import os
import json
import uuid
import random
from dotenv import load_dotenv
from google import genai
from google.genai import types
from firebase_admin import firestore
from datetime import datetime
from Max import save_user_memory

# Load environment variables
load_dotenv()

# Initialize Gemini API
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

# --- Core Quiz Functions ---

MAX_RETRIES = 5

def generate_quiz_question(db, user_id, topic=None, retry_count=0):
    """
    Generate a unique multiple-choice quiz question for the given user and topic.
    Prioritizes study_topic from memories, then study_goal, then defaults to 'programming and coding'.
    If topic is 'General', selects a random topic from a predefined list.
    Saves topic as study_topic memory if provided.
    """
    try:
        # Fetch user data
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            return {"error": "User not found"}

        quiz_history = get_quiz_history(db, user_id)
        memories = user_data.get('memories', [])
        
        # Define possible topics for General quizzes
        general_topics = [
            'Python',
            'JavaScript',
            'Algorithms',
            'Data Structures',
            'Databases',
            'Mathematics',
            'Physics',
            'History'
        ]

        # Determine topic
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

        # Save topic as study_topic memory if provided (but not for General to avoid overwriting)
        if topic and topic != 'General':
            save_user_memory(user_id, {
                "type": "study_topic",
                "value": topic,
                "timestamp": datetime.now().isoformat()
            })

        # Format quiz history for prompt
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

            # Extract JSON
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

                # Validate response format
                if not all(key in question_data for key in ['question', 'answers', 'correct_answer']):
                    return {"error": "Invalid quiz question format"}
                if len(question_data['answers']) != 4 or question_data['correct_answer'] not in question_data['answers']:
                    return {"error": "Invalid answers or correct_answer"}

                # Check for duplicates
                for question in quiz_history:
                    if question['question'] == question_data['question']:
                        print(f"[Duplicate Question] {question_data['question']}")
                        if retry_count < MAX_RETRIES:
                            return generate_quiz_question(db, user_id, topic, retry_count + 1)
                        return {"error": "Unable to generate unique quiz question after max retries"}

                # Generate and add question ID
                question_id = str(uuid.uuid4())
                question_data['question_id'] = question_id

                # Save to quiz_history
                history_entry = {
                    "question_id": question_id,
                    "question": question_data['question'],
                    "answers": question_data['answers'],
                    "correct_answer": question_data['correct_answer'],
                    "topic": effective_topic,
                    "timestamp": datetime.now().isoformat()
                }
                db.collection('users').document(user_id).update({
                    "quiz_history": firestore.ArrayUnion([history_entry])
                })
                print(f"[Quiz] Saved to quiz_history: {history_entry}")

                return {
                    "question_id": question_id,
                    "question": question_data['question'],
                    "answers": question_data['answers'],
                    "correct_answer": question_data['correct_answer'],
                    "topic": effective_topic
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

def save_quiz_response(db, user_id, question_id, user_answer, is_correct):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            f"quiz_responses.{question_id}": {
                "answer": user_answer,
                "is_correct": is_correct,
                "timestamp": firestore.SERVER_TIMESTAMP
            }
        })
        print(f"[Quiz] Saved to quiz_responses: {{question_id: {question_id}, answer: {user_answer}, is_correct: {is_correct}}}")
    except Exception as e:
        print(f"[Firestore Error] {e}")

def save_quiz_to_history(db, user_id, question_data):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            "quiz_history": firestore.ArrayUnion([question_data])
        })
    except Exception as e:
        print(f"[Firestore Quiz History Error] {e}")

def get_quiz_history(db, user_id):
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        return user_doc.to_dict().get('quiz_history', []) if user_doc.exists else []
    except Exception as e:
        print(f"[Firestore Error] {e}")
        return []

def get_study_goal(db, user_id):
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        return user_doc.to_dict().get('study_goal', None) if user_doc.exists else None
    except Exception as e:
        print(f"[Firestore Error] {e}")
        return None

def check_answer(db, user_id, question_id, user_answer):
    try:
        user_ref = db.collection('users').document(user_id)
        user_data = user_ref.get().to_dict()
        if not user_data:
            return {"error": "User not found"}

        quiz_history = get_quiz_history(db, user_id)
        for question in quiz_history:
            if question.get('question_id') == question_id:
                correct_answer = question.get('correct_answer')
                is_correct = user_answer == correct_answer
                save_quiz_response(db, user_id, question_id, user_answer, is_correct)
                return {
                    "result": "correct" if is_correct else "incorrect",
                    "correct_answer": correct_answer
                }

        return {"error": "Question not found"}

    except Exception as e:
        print(f"[Check Answer Error] {e}")
        return {"error": "Failed to check answer"}