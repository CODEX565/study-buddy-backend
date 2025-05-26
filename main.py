import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import logging
from datetime import datetime
import base64
import re
from io import BytesIO
from PIL import Image
from werkzeug.utils import secure_filename
from quiz import create_quiz, submit_quiz, get_user_data
from flashcards import generate_flashcards_for_failed_topics, generate_flashcards_for_topic
from exam import create_exam, submit_exam
from max import (
    generate_gemini_response, get_user_data, process_image_with_gemini, 
    process_document_with_gemini, process_user_input, generate_image,
    process_pdf, process_docx
)
from study_plan import initialize_study_plan, log_daily_study

# File upload configuration
UPLOAD_FOLDER = 'Uploads'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def save_conversation_history(user_id, conversation_history):
    """Save the conversation history for a user in Firestore."""
    db.collection('users').document(user_id).update({
        "ai_conversation_history": conversation_history
    })

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Load environment variables
load_dotenv()

# Initialize Firestore
if not firebase_admin._apps:
    cred = credentials.Certificate('studybuddy.json')
    firebase_admin.initialize_app(cred)

db = firestore.client()

@app.route('/get_user_data', methods=['POST'])
def get_user_data_endpoint():
    """Fetch user data for quiz setup."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400
        user_data = get_user_data(user_id)
        if not user_data:
            logger.warning(f"User data fetch failed for user_id: {user_id}")
            return jsonify({"error": "User not found"}), 404
        logger.info(f"Fetched user data for user_id: {user_id}")
        return jsonify(user_data), 200
    except Exception as e:
        logger.exception(f"Error in get_user_data: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/start_quiz', methods=['POST'])
def start_quiz_endpoint():
    """Start a new quiz session."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        subject = data.get('subject')
        topic = data.get('topic')
        num_questions = data.get('num_questions', 10)
        age = data.get('age')
        year_group = data.get('year_group')
        group = data.get('group')

        if not user_id or not subject or not topic:
            logger.error("Missing required fields: user_id, subject, or topic")
            return jsonify({"error": "Missing required fields"}), 400

        quiz_data = create_quiz(user_id, subject, topic, num_questions, age, year_group, group)
        if "error" in quiz_data:
            logger.error(f"Quiz start failed: {quiz_data['error']}")
            return jsonify(quiz_data), 400

        logger.info(f"Started quiz for user_id: {user_id}, quiz_id: {quiz_data['quiz_id']}")
        return jsonify(quiz_data), 200
    except Exception as e:
        logger.exception(f"Error in start_quiz: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/submit_quiz', methods=['POST'])
def submit_quiz_endpoint():
    """Submit quiz answers and get results."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        quiz_id = data.get('quiz_id')
        responses = data.get('responses')

        if not user_id or not quiz_id or not responses:
            logger.error("Missing required fields: user_id, quiz_id, or responses")
            return jsonify({"error": "Missing required fields"}), 400

        result = submit_quiz(user_id, quiz_id, responses)
        if "error" in result:
            logger.error(f"Quiz submission failed: {result['error']}")
            return jsonify(result), 400

        logger.info(f"Quiz submitted for user_id: {user_id}, quiz_id: {quiz_id}, score: {result['score']}")
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Error in submit_quiz: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/generate_flashcards', methods=['POST'])
def generate_flashcards_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400
        flashcards = generate_flashcards_for_failed_topics(user_id)
        logger.info(f"Generated flashcards for user_id: {user_id}, count: {len(flashcards)}")
        return jsonify({"flashcards": flashcards}), 200
    except Exception as e:
        logger.exception(f"Error in generate_flashcards: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/start_exam', methods=['POST'])
def start_exam_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        subject = data.get('subject')
        num_questions = data.get('num_questions', 25)
        age = data.get('age')
        if not user_id or not subject:
            logger.error("Missing required fields: user_id or subject")
            return jsonify({"error": "Missing required fields"}), 400
        exam_data = create_exam(user_id, subject, num_questions, age)
        if "error" in exam_data:
            logger.error(f"Exam start failed: {exam_data['error']}")
            return jsonify(exam_data), 400
        logger.info(f"Started exam for user_id: {user_id}, exam_id: {exam_data['exam_id']}")
        return jsonify(exam_data), 200
    except Exception as e:
        logger.exception(f"Error in start_exam: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/submit_exam', methods=['POST'])
def submit_exam_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        exam_id = data.get('exam_id')
        responses = data.get('responses')
        if not user_id or not exam_id or not responses:
            logger.error("Missing required fields: user_id, exam_id, or responses")
            return jsonify({"error": "Missing required fields"}), 400
        result = submit_exam(user_id, exam_id, responses)
        if "error" in result:
            logger.error(f"Exam submission failed: {result['error']}")
            return jsonify(result), 400
        logger.info(f"Exam submitted for user_id: {user_id}, exam_id: {exam_id}, score: {result['score']}")
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Error in submit_exam: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/clear_study_topic', methods=['POST'])
def clear_study_topic_endpoint():
    """Clear user's study topic."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400

        user_ref = db.collection('users').document(user_id)
        user_ref.update({"study_goal": ""})
        logger.info(f"Cleared study topic for user_id: {user_id}")
        return jsonify({"message": "Study topic cleared"}), 200
    except Exception as e:
        logger.exception(f"Error in clear_study_topic: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        user_input = data.get('user_input')
        conversation_history = data.get('conversation_history', [])
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        
        if not user_id or not user_input:
            logger.error("Missing required fields: user_id or user_input")
            return jsonify({"error": "Missing user_id or user_input"}), 400
        
        user_data = get_user_data(user_id)
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({"error": "User not found"}), 404
            
        # Use conversation history from user_data if none provided
        if not conversation_history and 'ai_conversation_history' in user_data:
            conversation_history = user_data.get('ai_conversation_history', [])
            
        # Update user data based on input
        user_data = process_user_input(user_id, user_input, user_data)

        # Generate MAX's response
        response, action = generate_gemini_response(
            user_data, user_input, conversation_history, user_id, 
            latitude=latitude, longitude=longitude
        )
        
        # Check for image generation tag
        image_data = None
        if "[GENERATE_IMAGE:" in response:
            match = re.search(r"\[GENERATE_IMAGE:\s*(.*?)\]", response)
            if match:
                image_prompt = match.group(1).strip()
                try:
                    generated_image = generate_image(image_prompt)
                    if generated_image == "QUOTA_EXCEEDED":
                        response = "I apologize, but I've hit my image generation limit for now. Please try again later! 🎨"
                    elif generated_image == "SERVICE_UNAVAILABLE":
                        response = "I'm sorry, but I can't generate images right now. The image generation service is temporarily unavailable. Please try again later or let me assist you with something else! 🎨"
                    elif generated_image:
                        image_data = generated_image
                        response = "Here's your image! 🖼️"
                    else:
                        response = "I tried to generate the image but wasn't able to. The image service might be having issues. Let me know if you'd like to try something else! 🎨"
                except Exception as e:
                    logger.error(f"Error generating image: {e}")
                    response = "I tried to generate an image but something went wrong. The image generation service might be having issues right now. 😕"
        
        # Save conversation history
        try:
            history_entry = {
                "user": user_input,
                "max": response,
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "type": "chat"
            }
            new_history = conversation_history + [history_entry]
            save_conversation_history(user_id, new_history)
        except Exception as e:
            logger.error(f"Error saving conversation history: {e}")

        return jsonify({
            "response": response,
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "image_base64": image_data
        }), 200

    except Exception as e:
        logger.exception(f"Error in max_chat: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/process_image', methods=['POST'])
def process_image():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        user_input = data.get('user_input', '')  # Make user_input optional with default empty string
        # Check both image_base64 and image_data for compatibility
        image_data = data.get('image_base64') or data.get('image_data')
        conversation_history = data.get('conversation_history', [])
        mime_type = data.get('mime_type', 'image/png')
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        
        if not user_id or not image_data:
            missing = []
            if not user_id: missing.append('user_id')
            if not image_data: missing.append('image_base64')
            logger.error(f"Missing required fields for image processing: {', '.join(missing)}")
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        user_data = get_user_data(user_id)
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({"error": "User not found"}), 404

        user_memories = user_data.get('memories', [])
        
        # Try to decode base64 image
        try:
            decoded_image = base64.b64decode(image_data)
        except Exception as e:
            logger.error(f"Error decoding base64 image data: {e}")
            return jsonify({"error": "Invalid base64 image data format"}), 400

        # Verify it's a valid image
        try:
            Image.open(BytesIO(decoded_image))
        except Exception as e:
            logger.error(f"Invalid image format: {e}")
            return jsonify({"error": "Invalid image format. Please provide a valid image."}), 400

        # Update user data based on input
        user_data = process_user_input(user_id, user_input, user_data)
        
        # Process image with Gemini
        response, action = process_image_with_gemini(
            user_id, user_input, decoded_image, conversation_history, 
            user_memories, mime_type, latitude, longitude
        )
        try:
            history_entry = {
                "user": user_input,
                "max": response,
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "type": "image",
                "image_base64": image_data
            }
            new_history = conversation_history + [history_entry]
            save_conversation_history(user_id, new_history)
        except Exception as e:
            logger.error(f"Error saving conversation history: {e}")

        return jsonify({
            "response": response,
            "action": action,
            "timestamp": datetime.now().isoformat()
        }, 200)

    except Exception as e:
        logger.exception(f"Error in max_image: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/process_document', methods=['POST'])
def process_document():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']
        user_id = request.form.get('user_id')
        user_input = request.form.get('user_input', '')
        conversation_history = request.form.get('conversation_history', '[]')
        if isinstance(conversation_history, str):
            import json
            conversation_history = json.loads(conversation_history)
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        
        if not file or not user_id or file.filename == '':
            logger.error("Missing required fields: file and user_id")
            return jsonify({"error": "File and user_id are required"}), 400

        if not allowed_file(file.filename):
            logger.error("File type not allowed")
            return jsonify({"error": "File type not allowed. Use pdf, docx, or txt"}), 400

        user_data = get_user_data(user_id)
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({"error": "User not found"}), 404

        user_memories = user_data.get('memories', [])
        
        # Update user data based on input
        user_data = process_user_input(user_id, user_input, user_data)
        
        # Save and process the file
        filename = secure_filename(file.filename)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(file_path)

        try:
            if filename.endswith('.pdf'):
                processed_text = process_pdf(file_path)
            elif filename.endswith('.docx'):
                processed_text = process_docx(file_path)
            elif filename.endswith('.txt'):
                with open(file_path, 'r', encoding='utf-8') as f:
                    processed_text = f.read()
            else:
                os.remove(file_path)
                return jsonify({"error": "Unsupported file type"}), 400

            os.remove(file_path)  # Clean up after processing
        except Exception as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            logger.error(f"Error processing document: {e}")
            return jsonify({"error": "Failed to process document"}), 500
        
        response, action = process_document_with_gemini(
            user_id, processed_text, user_input, conversation_history, 
            user_memories, latitude, longitude
        )

        # Save conversation history
        try:
            history_entry = {
                "user": user_input,
                "max": response,
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "type": "document",
                "document_summary": processed_text[:200] + "..." if len(processed_text) > 200 else processed_text
            }
            new_history = conversation_history + [history_entry]
            save_conversation_history(user_id, new_history)
        except Exception as e:
            logger.error(f"Error saving conversation history: {e}")

        return jsonify({
            "response": response,
            "action": action,
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        logger.exception(f"Error in max_document: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/study_plan/init', methods=['POST'])
def study_plan_init_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        goal = data.get('goal')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        days_per_week = data.get('days_per_week')
        daily_duration_minutes = data.get('daily_duration_minutes')
        if not all([user_id, goal, start_date, end_date, days_per_week, daily_duration_minutes]):
            return jsonify({"error": "Missing required fields"}), 400
        plan = initialize_study_plan(user_id, goal, start_date, end_date, days_per_week, daily_duration_minutes)
        return jsonify(plan), 200
    except Exception as e:
        logger.exception(f"Error in study_plan_init: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/study_plan/log_daily', methods=['POST'])
def study_plan_log_daily_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        date = data.get('date')
        completed_topics = data.get('completed_topics', [])
        time_spent = data.get('time_spent')
        notes = data.get('notes')
        if not all([user_id, date, time_spent]):
            return jsonify({"error": "Missing required fields"}), 400
        log_entry = log_daily_study(user_id, date, completed_topics, time_spent, notes)
        return jsonify(log_entry), 200
    except Exception as e:
        logger.exception(f"Error in study_plan_log_daily: {e}")
        return jsonify({"error": "Server error"}), 500

# To be implemented when AI feedback loop is ready
# @app.route('/study_plan/feedback', methods=['POST'])
# def study_plan_feedback_endpoint():
#     pass

# To be implemented when goal completion checking is ready
# @app.route('/study_plan/check_goal', methods=['POST'])
# def study_plan_check_goal_endpoint():
#     pass

@app.route('/generate_flashcards/topic', methods=['POST'])
def generate_flashcards_for_topic_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        subject = data.get('subject')
        topic = data.get('topic')
        num_cards = int(data.get('num_cards', 3))
        age = data.get('age')
        year_group = data.get('year_group')
        if not user_id or not subject or not topic:
            return jsonify({"error": "Missing required fields"}), 400
        cards = generate_flashcards_for_topic(user_id, subject, topic, num_cards, age, year_group)
        return jsonify({"flashcards": cards}), 200
    except Exception as e:
        logger.exception(f"Error in generate_flashcards_for_topic: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400

        # Clear conversation history in Firestore
        try:
            db.collection('users').document(user_id).update({
                "ai_conversation_history": []
            })
            logger.info(f"Cleared chat history for user_id: {user_id}")
            return jsonify({
                "message": "Chat history cleared successfully",
                "timestamp": datetime.now().isoformat()
            }), 200
        except Exception as e:
            logger.error(f"Error clearing chat history: {e}")
            return jsonify({"error": "Failed to clear chat history"}), 500

    except Exception as e:
        logger.exception(f"Error in clear_chat: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/update_user_profile', methods=['POST'])
def update_user_profile():
    """Update user's basic profile information."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        updates = data.get('updates', {})
        
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400
            
        # Validate the updates
        allowed_fields = {'name', 'age', 'year_group', 'study_goal'}
        invalid_fields = set(updates.keys()) - allowed_fields
        if invalid_fields:
            return jsonify({"error": f"Invalid fields: {invalid_fields}"}), 400
            
        # Update the user document
        user_ref = db.collection('users').document(user_id)
        user_ref.update(updates)
        
        logger.info(f"Updated profile for user_id: {user_id}, fields: {list(updates.keys())}")
        return jsonify({"message": "Profile updated successfully"}), 200
        
    except Exception as e:
        logger.exception(f"Error in update_user_profile: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/update_study_topics', methods=['POST'])
def update_study_topics():
    """Update user's study topics."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        action = data.get('action')  # 'add' or 'remove'
        topics = data.get('topics', [])  # list of {subject: string, topic: string}
        
        if not user_id or not action or not topics:
            logger.error("Missing required fields: user_id, action, or topics")
            return jsonify({"error": "Missing required fields"}), 400
            
        if action not in ['add', 'remove']:
            return jsonify({"error": "Invalid action. Use 'add' or 'remove'"}), 400
            
        user_ref = db.collection('users').document(user_id)
        user_data = user_ref.get().to_dict()
        
        if not user_data:
            return jsonify({"error": "User not found"}), 404
            
        current_topics = user_data.get('study_topics', [])
        
        if action == 'add':
            # Add new topics, avoiding duplicates
            for topic in topics:
                if topic not in current_topics:
                    user_ref.update({
                        'study_topics': firestore.ArrayUnion([topic])
                    })
        else:  # action == 'remove'
            # Remove topics
            for topic in topics:
                if topic in current_topics:
                    user_ref.update({
                        'study_topics': firestore.ArrayRemove([topic])
                    })
                    
        logger.info(f"{action.capitalize()}ed study topics for user_id: {user_id}, topics: {topics}")
        return jsonify({"message": f"Study topics {action}ed successfully"}), 200
        
    except Exception as e:
        logger.exception(f"Error in update_study_topics: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/get_study_progress', methods=['POST'])
def get_study_progress():
    """Get user's study progress across all topics."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400
            
        user_data = get_user_data(user_id)
        if not user_data:
            return jsonify({"error": "User not found"}), 404
            
        # Get relevant data
        study_topics = user_data.get('study_topics', [])
        subjects_mastery = user_data.get('subjects_mastery', {})
        learning_history = user_data.get('learning_history', [])
        
        # Calculate progress for each topic
        topics_progress = []
        for topic_info in study_topics:
            subject = topic_info.get('subject')
            topic = topic_info.get('topic')
            
            # Get mastery level
            mastery = subjects_mastery.get(subject, {}).get(topic, 0.0)
            
            # Get recent activities
            recent_activities = [
                activity for activity in learning_history
                if activity.get('subject') == subject and activity.get('topic') == topic
            ][-5:]  # Last 5 activities
            
            topics_progress.append({
                'subject': subject,
                'topic': topic,
                'mastery_level': mastery,
                'recent_activities': recent_activities,
                'needs_improvement': mastery < 0.7
            })
            
        logger.info(f"Fetched study progress for user_id: {user_id}")
        return jsonify({
            'study_topics': study_topics,
            'topics_progress': topics_progress
        }), 200
        
    except Exception as e:
        logger.exception(f"Error in get_study_progress: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/get_user_overview', methods=['POST'])
def get_user_overview():
    """Get comprehensive user overview including basic info and learning status."""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        
        if not user_id:
            logger.error("Missing user_id in request")
            return jsonify({"error": "Missing user_id"}), 400
            
        user_data = get_user_data(user_id)
        if not user_data:
            return jsonify({"error": "User not found"}), 404
            
        # Get basic user info
        basic_info = {
            "name": user_data.get('name', ''),
            "age": user_data.get('age', 0),
            "year_group": user_data.get('year_group') or map_age_to_year_group(user_data.get('age', 15)),
            "created_at": user_data.get('created_at'),
            "last_active": user_data.get('last_active'),
            "subscription_status": user_data.get('subscription_status', 'free'),
            "subscription_tier": user_data.get('subscription_tier', 'free')
        }
        
        # Get study progress
        study_topics = user_data.get('study_topics', [])
        subjects_mastery = user_data.get('subjects_mastery', {})
        
        # Calculate overall progress
        total_mastery = 0
        topics_count = 0
        subjects_progress = {}
        
        for subject, topics in subjects_mastery.items():
            subject_total = 0
            subject_count = 0
            for topic, mastery in topics.items():
                subject_total += mastery
                subject_count += 1
                total_mastery += mastery
                topics_count += 1
            
            if subject_count > 0:
                subjects_progress[subject] = {
                    "average_mastery": subject_total / subject_count,
                    "topics_count": subject_count
                }
        
        overall_progress = {
            "average_mastery": total_mastery / topics_count if topics_count > 0 else 0,
            "total_topics": topics_count,
            "subjects": subjects_progress
        }
        
        # Get recent activity
        recent_quizzes = []
        quiz_history = user_data.get('quiz_history', [])
        for quiz in sorted(quiz_history, key=lambda x: x.get('created_at', ''), reverse=True)[:5]:
            recent_quizzes.append({
                "quiz_id": quiz.get('quiz_id'),
                "subject": quiz.get('subject'),
                "topic": quiz.get('topic'),
                "score": quiz.get('score', 0),
                "status": quiz.get('status'),
                "created_at": quiz.get('created_at')
            })
        
        # Get achievements and stats
        achievements = {
            "total_quizzes": len(quiz_history),
            "challenges_completed": user_data.get('challenges_completed', 0),
            "xp": user_data.get('xp', 0),
            "badges": user_data.get('badges', []),
            "leaderboard_position": user_data.get('leaderboard_position', 0),
            "leaderboard_rank": user_data.get('leaderboard_rank', '')
        }
        
        # Get study preferences
        preferences = {
            "study_planner_enabled": user_data.get('study_planner_enabled', True),
            "exam_mode_enabled": user_data.get('exam_mode_enabled', True),
            "smart_flashcards_enabled": user_data.get('smart_flashcards_enabled', True),
            "reminders_enabled": user_data.get('reminders_enabled', True),
            "weekly_challenges_enabled": user_data.get('weekly_challenges_enabled', True)
        }
        
        # Get recommended next steps
        study_topics, _, learning_history = get_user_study_topics(user_id)
        topics_to_improve = get_recommended_topics(user_id)
        
        next_steps = {
            "topics_to_review": [
                {"subject": t['subject'], "topic": t['topic'], "mastery": t['mastery']}
                for t in topics_to_improve[:3]  # Top 3 topics that need improvement
            ],
            "suggested_new_topics": get_topics_for_year_group(basic_info['year_group'])[:3]  # 3 new topics to try
        }
        
        return jsonify({
            "basic_info": basic_info,
            "study_progress": overall_progress,
            "recent_activity": {
                "quizzes": recent_quizzes,
                "learning_history": learning_history[-5:] if learning_history else []  # Last 5 learning activities
            },
            "achievements": achievements,
            "preferences": preferences,
            "next_steps": next_steps,
            "timestamp": datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        logger.exception(f"Error in get_user_overview: {e}")
        return jsonify({"error": "Server error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
