import logging
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
import firebase_admin
from firebase_admin import credentials, firestore
from Max import generate_gemini_response, process_image_with_gemini, process_document_with_gemini, process_pdf, process_docx, process_text_file, generate_image
from Quiz import generate_quiz_question, check_answer, get_user_data, save_quiz_score, generate_full_quiz, evaluate_quiz, save_to_exam_mode
from exam import generate_full_exam, start_exam_attempt, submit_exam_answers, evaluate_exam, generate_summary, generate_flashcards, generate_total_summary
from flashcards import generate_flashcards_from_quiz, generate_flashcards_from_document, generate_flashcards_from_input, get_user_flashcards, update_flashcard_status
from gemini_image_processor import image_processor_bp
import os
import uuid
import json
import random
from werkzeug.utils import secure_filename
from datetime import datetime
import google.generativeai as genai
from google.generativeai import types
from chat import chat_bp, socketio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('studybuddy.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.register_blueprint(image_processor_bp)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'your-secret-key')
socketio = SocketIO(app, cors_allowed_origins="*")

app.register_blueprint(chat_bp)
socketio.init_app(app, cors_allowed_origins="*")

if not firebase_admin._apps:
    cred = credentials.Certificate('studybuddy.json')
    firebase_admin.initialize_app(cred)

db = firestore.client()

UPLOAD_FOLDER = 'Uploads'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt', 'png', 'jpg', 'jpeg'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
client = genai.GenerativeModel('gemini-1.5-flash')

# SocketIO event handlers for chat functionality
@socketio.on('connect')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to Study Buddy server'})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on('join_chat')
def handle_join_chat(data):
    chat_id = data.get('chat_id')
    user_id = data.get('user_id')
    if not chat_id or not user_id:
        logger.error(f"Missing chat_id or user_id in join_chat: {data}")
        emit('error', {'error': 'chat_id and user_id are required'})
        return
    logger.info(f"User {user_id} joining chat {chat_id}")
    chat_doc = db.collection('chats').document(chat_id).get()
    if not chat_doc.exists:
        logger.warning(f"Chat not found: {chat_id}")
        emit('error', {'error': 'Chat not found'})
        return
    from flask_socketio import join_room
    join_room(chat_id)
    emit('joined_chat', {'message': f'Joined chat {chat_id}'}, room=chat_id)

@socketio.on('new_message')
def handle_new_message(data):
    chat_id = data.get('chat_id')
    user_id = data.get('user_id')
    text = data.get('text')
    resource_url = data.get('resource_url')
    resource_name = data.get('resource_name')
    if not chat_id or not user_id or (not text and not resource_url):
        logger.error(f"Invalid new_message data: {data}")
        emit('error', {'error': 'chat_id, user_id, and either text or resource_url are required'})
        return
    logger.info(f"New message in chat {chat_id} from user {user_id}")
    message_data = {
        'sender_id': user_id,
        'text': text or '',
        'resource_url': resource_url or None,
        'resource_name': resource_name or None,
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }
    db.collection('chats').document(chat_id).collection('messages').add(message_data)
    emit('new_message', message_data, room=chat_id)

# New endpoint to fetch chats for a user
@app.route('/get_chats', methods=['POST'])
def get_chats():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in get_chats request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in get_chats request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Fetching chats for user_id: {user_id}")
        chats_ref = db.collection('chats').where('participants', 'array_contains', user_id)
        chats = chats_ref.stream()
        chat_list = []
        for chat in chats:
            chat_data = chat.to_dict()
            chat_list.append({
                'chat_id': chat.id,
                'title': chat_data.get('title', 'Untitled Chat'),
                'isGroup': chat_data.get('isGroup', False),
                'participants': chat_data.get('participants', []),
                'createdAt': chat_data.get('createdAt')
            })
        logger.debug(f"Fetched {len(chat_list)} chats for user_id: {user_id}")
        return jsonify({'chats': chat_list}), 200
    except Exception as e:
        logger.exception(f"Get chats error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

# New endpoint for sending messages
@app.route('/send_message', methods=['POST'])
def send_message():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in send_message request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        chat_id = data.get('chat_id')
        user_id = data.get('user_id')
        text = data.get('text')
        if not chat_id or not user_id or not text:
            logger.error(f"Missing required fields: chat_id={chat_id}, user_id={user_id}, text={text}")
            return jsonify({'error': 'chat_id, user_id, and text are required'}), 400
        logger.info(f"Sending message in chat {chat_id} by user {user_id}")
        chat_doc = db.collection('chats').document(chat_id).get()
        if not chat_doc.exists:
            logger.warning(f"Chat not found: {chat_id}")
            return jsonify({'error': 'Chat not found'}), 404
        message_data = {
            'sender_id': user_id,
            'text': text,
            'resource_url': None,
            'resource_name': None,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }
        db.collection('chats').document(chat_id).collection('messages').add(message_data)
        socketio.emit('new_message', message_data, room=chat_id)
        logger.debug(f"Message sent successfully in chat {chat_id}")
        return jsonify({'message': 'Message sent successfully'}), 200
    except Exception as e:
        logger.exception(f"Send message error: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

# New endpoint for sharing resources
@app.route('/share_resource', methods=['POST'])
def share_resource():
    try:
        if 'file' not in request.files:
            logger.error("No file provided in share_resource request")
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        chat_id = request.form.get('chat_id')
        user_id = request.form.get('user_id')
        if not file or not chat_id or not user_id or file.filename == '':
            logger.error(f"Missing required fields: chat_id={chat_id}, user_id={user_id}, file={'provided' if file else 'missing'}")
            return jsonify({'error': 'File, chat_id, and user_id are required'}), 400
        if not allowed_file(file.filename):
            logger.error(f"Invalid file type: {file.filename}")
            return jsonify({'error': 'File type not allowed. Use pdf, docx, txt, png, jpg, or jpeg'}), 400
        logger.info(f"Sharing resource in chat {chat_id} by user {user_id}, file: {file.filename}")
        chat_doc = db.collection('chats').document(chat_id).get()
        if not chat_doc.exists:
            logger.warning(f"Chat not found: {chat_id}")
            return jsonify({'error': 'Chat not found'}), 404
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(file_path)
        # Store file in Firestore Storage (simplified; replace with actual Firebase Storage upload)
        resource_url = f"uploads/{unique_filename}"  # Placeholder; update with actual storage URL
        message_data = {
            'sender_id': user_id,
            'text': '',
            'resource_url': resource_url,
            'resource_name': filename,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }
        db.collection('chats').document(chat_id).collection('messages').add(message_data)
        socketio.emit('new_message', message_data, room=chat_id)
        logger.debug(f"Resource shared successfully in chat {chat_id}")
        return jsonify({'message': 'Resource shared successfully', 'resource_url': resource_url}), 200
    except Exception as e:
        logger.exception(f"Share resource error: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/', methods=['GET'])
def index():
    logger.info(f"GET / request: headers={request.headers}, args={request.args}, remote_addr={request.remote_addr}")
    return jsonify({
        'status': 'Study Buddy backend running',
        'message': 'Use POST to /chat, /generate_quiz, /generate_full_exam, /start_exam, /submit_exam, /evaluate_exam, /get_summary, /generate_flashcards, /get_total_summary, /process_document, etc.'
    }), 200

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in chat request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_input = data.get('user_input')
        user_id = data.get('user_id')
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if not user_input or not user_id:
            logger.error(f"Missing required fields: user_input={user_input}, user_id={user_id}")
            return jsonify({'error': 'user_input and user_id are required'}), 400
        logger.info(f"Processing chat for user_id: {user_id}, input: {user_input}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        conversation_history = user_data.get('conversation_history', [])
        response = generate_gemini_response(user_data, user_input, conversation_history, user_id, latitude=latitude, longitude=longitude)
        image_tag = None
        if isinstance(response, tuple):
            response, image_tag = response
        elif response.startswith('[GENERATE_IMAGE:'):
            image_tag = response
            response = "Here’s the image you requested!"
        if image_tag and image_tag.startswith('[GENERATE_IMAGE:'):
            image_prompt = image_tag[len('[GENERATE_IMAGE:'):-1].strip()
            logger.info(f"Generating image for prompt: {image_prompt}")
            image_base64 = generate_image(image_prompt)
            if image_base64:
                response_data = {'response': response, 'image_base64': image_base64}
            else:
                logger.error(f"Failed to generate image for prompt: {image_prompt}")
                response_data = {'response': 'Sorry, I couldn’t generate the image. Try again? 😅'}
        else:
            response_data = {'response': response}
        conversation_history.append({'user': user_input, 'max': response_data['response']})
        db.collection('users').document(user_id).update({'conversation_history': conversation_history})
        logger.debug(f"Updated conversation history for user_id: {user_id}")
        return jsonify(response_data), 200
    except Exception as e:
        logger.exception(f"Chat error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/process_image', methods=['POST'])
def process_image():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in process_image request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_input = data.get('user_input')
        user_id = data.get('user_id')
        image_base64 = data.get('image_base64')
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if not user_input or not user_id or not image_base64:
            logger.error(f"Missing required fields: user_input={user_input}, user_id={user_id}, image_base64={'provided' if image_base64 else 'missing'}")
            return jsonify({'error': 'user_input, user_id, and image_base64 are required'}), 400
        logger.info(f"Processing image for user_id: {user_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        conversation_history = user_data.get('conversation_history', [])
        response = process_image_with_gemini(user_input, image_base64, conversation_history, user_data.get('memories', []), user_id, mime_type="image/png", latitude=latitude, longitude=longitude)
        conversation_history.append({'user': user_input, 'max': response})
        db.collection('users').document(user_id).update({'conversation_history': conversation_history})
        logger.debug(f"Updated conversation history for user_id: {user_id}")
        return jsonify({'response': response}), 200
    except Exception as e:
        logger.exception(f"Process image error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/process_document', methods=['POST'])
def process_document():
    try:
        if 'file' not in request.files:
            logger.error("No file provided in process_document request")
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        user_id = request.form.get('user_id')
        user_input = request.form.get('user_input', '')
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        if not file or not user_id or file.filename == '':
            logger.error(f"Missing required fields: user_id={user_id}, file={'provided' if file else 'missing'}")
            return jsonify({'error': 'File and user_id are required'}), 400
        if not allowed_file(file.filename):
            logger.error(f"Invalid file type: {file.filename}")
            return jsonify({'error': 'File type not allowed. Use pdf, docx, or txt'}), 400
        logger.info(f"Processing document for user_id: {user_id}, file: {file.filename}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        if filename.endswith('.pdf'):
            document_text = process_pdf(file_path)
        elif filename.endswith('.docx'):
            document_text = process_docx(file_path)
        elif filename.endswith('.txt'):
            document_text = process_text_file(file_path)
        else:
            os.remove(file_path)
            logger.error(f"Unsupported file type: {filename}")
            return jsonify({'error': 'Unsupported file type'}), 400
        os.remove(file_path)
        if not document_text:
            logger.error(f"Failed to process document: {filename}")
            return jsonify({'error': 'Failed to process document'}), 500
        conversation_history = user_data.get('conversation_history', [])
        response = process_document_with_gemini(user_id, document_text, user_input, conversation_history, user_data.get('memories', []), latitude=latitude, longitude=longitude)
        conversation_history.append({'user': user_input, 'max': response})
        db.collection('users').document(user_id).update({'conversation_history': conversation_history})
        logger.debug(f"Updated conversation history for user_id: {user_id}")
        return jsonify({'response': response}), 200
    except Exception as e:
        logger.exception(f"Process document error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in clear_chat request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in clear_chat request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Clearing chat history for user_id: {user_id}")
        user_ref = db.collection('users').document(user_id)
        user_ref.update({'conversation_history': []})
        logger.debug(f" Streaks cleared for user_id: {user_id}")
        return jsonify({'message': 'Chat history cleared successfully'}), 200
    except Exception as e:
        logger.exception(f"Clear chat error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Failed to clear chat history: {str(e)}'}), 500

@app.route('/get_user_data', methods=['POST'])
def get_user_data_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in get_user_data request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in get_user_data request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Fetching user data for user_id: {user_id}")
        user_doc = db.collection('users').document(user_id).get()
        if not user_doc.exists:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        user_data = user_doc.to_dict()
        response_data = {
            'age': user_data.get('age'),
            'year_group': user_data.get('year_group'),
            'study_topic': user_data.get('study_topic'),
            'display_name': user_data.get('display_name')
        }
        logger.debug(f"Fetched user data for user_id: {user_id}: {response_data}")
        return jsonify(response_data), 200
    except Exception as e:
        logger.exception(f"Get user data error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Failed to fetch user data: {str(e)}'}), 500

@app.route('/set_display_name', methods=['POST'])
def set_display_name():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in set_display_name request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        display_name = data.get('display_name')
        if not user_id or not display_name:
            logger.error(f"Missing required fields: user_id={user_id}, display_name={display_name}")
            return jsonify({'error': 'user_id and display_name are required'}), 400
        if len(display_name.strip()) < 2:
            logger.error(f"Invalid display_name: {display_name}")
            return jsonify({'error': 'Display name must be at least 2 characters'}), 400
        logger.info(f"Setting display name for user_id: {user_id}, display_name: {display_name}")
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        user_ref.update({'display_name': display_name.strip()})
        logger.debug(f"Display name set for user_id: {user_id}")
        return jsonify({'message': 'Display name set successfully'}), 200
    except Exception as e:
        logger.exception(f"Set display name error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Failed to set display name: {str(e)}'}), 500

@app.route('/clear_study_topic', methods=['POST'])
def clear_study_topic():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in clear_study_topic request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in clear_study_topic request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Clearing study topic for user_id: {user_id}")
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        user_ref.update({'study_topic': firestore.DELETE_FIELD})
        logger.debug(f"Study topic cleared for user_id: {user_id}")
        return jsonify({'message': 'Study topic cleared successfully'}), 200
    except Exception as e:
        logger.exception(f"Clear study topic error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Failed to clear study topic: {str(e)}'}), 500

@app.route('/generate_quiz', methods=['POST'])
def generate_quiz():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in generate_quiz request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        topic = data.get('topic')
        age = data.get('age')
        year_group = data.get('year_group')
        if not user_id:
            logger.error("Missing user_id in generate_quiz request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Generating quiz for user_id: {user_id}, topic: {topic}, age: {age}, year_group: {year_group}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        quiz_data = generate_quiz_question(db, user_id, topic, age, year_group)
        logger.debug(f"Quiz response for user_id: {user_id}: {quiz_data}")
        if 'error' in quiz_data:
            logger.error(f"Quiz generation error: {quiz_data['error']}")
            return jsonify({'error': quiz_data['error']}), 500
        return jsonify(quiz_data), 200
    except Exception as e:
        logger.exception(f"Generate quiz error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/generate_full_quiz', methods=['POST'])
def generate_full_quiz_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in generate_full_quiz request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        topic = data.get('topic')
        question_count = data.get('question_count')
        age = data.get('age')
        year_group = data.get('year_group')
        group = data.get('group')
        if not user_id or not question_count:
            logger.error(f"Missing required fields: user_id={user_id}, question_count={question_count}")
            return jsonify({'error': 'user_id and question_count are required'}), 400
        if not isinstance(question_count, int) or question_count <= 0:
            logger.error(f"Invalid question_count: {question_count}")
            return jsonify({'error': 'question_count must be a positive integer'}), 400
        logger.info(f"Generating full quiz for user_id: {user_id}, topic: {topic}, question_count: {question_count}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        quiz_data = generate_full_quiz(db, user_id, topic, question_count, age, year_group, group)
        logger.debug(f"Full quiz response for user_id: {user_id}: {quiz_data}")
        if 'error' in quiz_data:
            logger.error(f"Full quiz generation error: {quiz_data['error']}")
            return jsonify({'error': quiz_data['error']}), 500
        return jsonify(quiz_data), 200
    except Exception as e:
        logger.exception(f"Generate full quiz error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/check_answer', methods=['POST'])
def check_answer_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in check_answer request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        question_id = data.get('question_id')
        user_answer = data.get('user_answer')
        if not user_id or not question_id or not user_answer:
            logger.error(f"Missing required fields: user_id={user_id}, question_id={question_id}, user_answer={user_answer}")
            return jsonify({'error': 'user_id, question_id, and user_answer are required'}), 400
        logger.info(f"Checking answer for user_id: {user_id}, question_id: {question_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        result = check_answer(db, user_id, question_id, user_answer)
        logger.debug(f"Check answer response for user_id: {user_id}: {result}")
        if 'error' in result:
            logger.error(f"Check answer error: {result['error']}")
            return jsonify({'error': result['error']}), 404
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Check answer error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/evaluate_quiz', methods=['POST'])
def evaluate_quiz_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in evaluate_quiz request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        quiz_id = data.get('quiz_id')
        if not user_id or not quiz_id:
            logger.error(f"Missing required fields: user_id={user_id}, quiz_id={quiz_id}")
            return jsonify({'error': 'user_id and quiz_id are required'}), 400
        logger.info(f"Evaluating quiz for user_id: {user_id}, quiz_id: {quiz_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        result = evaluate_quiz(db, user_id, quiz_id)
        logger.debug(f"Evaluate quiz response for user_id: {user_id}: {result}")
        if 'error' in result:
            logger.error(f"Evaluate quiz error: {result['error']}")
            return jsonify({'error': result['error']}), 500
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Evaluate quiz error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/generate_full_exam', methods=['POST'])
def generate_full_exam_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in generate_full_exam request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        topic = data.get('topic')
        question_count = data.get('question_count', 10)
        age = data.get('age')
        year_group = data.get('year_group')
        if not user_id:
            logger.error("Missing user_id in generate_full_exam request")
            return jsonify({'error': 'user_id is required'}), 400
        if not isinstance(question_count, int) or question_count <= 0:
            logger.error(f"Invalid question_count: {question_count}")
            return jsonify({'error': 'question_count must be a positive integer'}), 400
        logger.info(f"Generating full exam for user_id: {user_id}, topic: {topic}, question_count: {question_count}, age: {age}, year_group: {year_group}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        exam_data = generate_full_exam(db, user_id, topic, question_count, age, year_group)
        logger.debug(f"Full exam response for user_id: {user_id}: {exam_data}")
        if 'error' in exam_data:
            logger.error(f"Full exam generation error: {exam_data['error']}")
            return jsonify({'error': exam_data['error']}), 500
        return jsonify(exam_data), 200
    except Exception as e:
        logger.exception(f"Generate full exam error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/start_exam', methods=['POST'])
def start_exam_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in start_exam request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        exam_id = data.get('exam_id')
        if not user_id or not exam_id:
            logger.error(f"Missing required fields: user_id={user_id}, exam_id={exam_id}")
            return jsonify({'error': 'user_id and exam_id are required'}), 400
        logger.info(f"Starting exam for user_id: {user_id}, exam_id: {exam_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        result = start_exam_attempt(db, user_id, exam_id)
        logger.debug(f"Start exam response for user_id: {user_id}: {result}")
        if 'error' in result:
            logger.error(f"Start exam error: {result['error']}")
            return jsonify({'error': result['error']}), 500
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Start exam error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/submit_exam', methods=['POST'])
def submit_exam_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in submit_exam request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        exam_id = data.get('exam_id')
        answers = data.get('answers')
        if not user_id or not exam_id or not answers:
            logger.error(f"Missing required fields: user_id={user_id}, exam_id={exam_id}, answers={'provided' if answers else 'missing'}")
            return jsonify({'error': 'user_id, exam_id, and answers are required'}), 400
        if not isinstance(answers, dict):
            logger.error(f"Invalid answers format: {answers}")
            return jsonify({'error': 'answers must be a dictionary'}), 400
        logger.info(f"Submitting exam for user_id: {user_id}, exam_id: {exam_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        result = submit_exam_answers(db, user_id, exam_id, answers)
        logger.debug(f"Submit exam response for user_id: {user_id}: {result}")
        if 'error' in result:
            logger.error(f"Submit exam error: {result['error']}")
            return jsonify({'error': result['error']}), 500
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Submit exam error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/evaluate_exam', methods=['POST'])
def evaluate_exam_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in evaluate_exam request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        exam_id = data.get('exam_id')
        if not user_id or not exam_id:
            logger.error(f"Missing required fields: user_id={user_id}, exam_id={exam_id}")
            return jsonify({'error': 'user_id and exam_id are required'}), 400
        logger.info(f"Evaluating exam for user_id: {user_id}, exam_id: {exam_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        result = evaluate_exam(db, user_id, exam_id)
        logger.debug(f"Evaluate exam response for user_id: {user_id}: {result}")
        if 'error' in result:
            logger.error(f"Evaluate exam error: {result['error']}")
            return jsonify({'error': result['error']}), 500
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Evaluate exam error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/get_summary', methods=['POST'])
def get_summary_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in get_summary request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        quiz_or_exam_id = data.get('quiz_or_exam_id')
        is_exam = data.get('is_exam', False)
        if not user_id or not quiz_or_exam_id:
            logger.error(f"Missing required fields: user_id={user_id}, quiz_or_exam_id={quiz_or_exam_id}")
            return jsonify({'error': 'user_id and quiz_or_exam_id are required'}), 400
        logger.info(f"Generating summary for user_id: {user_id}, {'exam' if is_exam else 'quiz'}_id: {quiz_or_exam_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        summary = generate_summary(db, user_id, quiz_or_exam_id, is_exam)
        logger.debug(f"Summary response for user_id: {user_id}: {summary}")
        if 'error' in summary:
            logger.error(f"Generate summary error: {summary['error']}")
            return jsonify({'error': summary['error']}), 500
        return jsonify(summary), 200
    except Exception as e:
        logger.exception(f"Generate summary error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/get_total_summary', methods=['POST'])
def get_total_summary_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in get_total_summary request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        if not user_id:
            logger.error("Missing user_id in get_total_summary request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Generating total summary for user_id: {user_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        summary = generate_total_summary(db, user_id)
        logger.debug(f"Total summary response for user_id: {user_id}: {summary}")
        if 'error' in summary:
            logger.error(f"Generate total summary error: {summary['error']}")
            return jsonify({'error': summary['error']}), 500
        return jsonify(summary), 200
    except Exception as e:
        logger.exception(f"Generate total summary error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/generate_flashcards', methods=['POST'])
def generate_flashcards_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in generate_flashcards request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        quiz_or_exam_id = data.get('quiz_or_exam_id')
        is_exam = data.get('is_exam', False)
        if not user_id or not quiz_or_exam_id:
            logger.error(f"Missing required fields: user_id={user_id}, quiz_or_exam_id={quiz_or_exam_id}")
            return jsonify({'error': 'user_id and quiz_or_exam_id are required'}), 400
        logger.info(f"Generating flashcards for user_id: {user_id}, {'exam' if is_exam else 'quiz'}_id: {quiz_or_exam_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        flashcards = generate_flashcards(db, user_id, quiz_or_exam_id, is_exam)
        logger.debug(f"Flashcards response for user_id: {user_id}: {flashcards}")
        if 'error' in flashcards:
            logger.error(f"Generate flashcards error: {flashcards['error']}")
            return jsonify({'error': flashcards['error']}), 500
        return jsonify(flashcards), 200
    except Exception as e:
        logger.exception(f"Generate flashcards error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/save_quiz_score', methods=['POST'])
def save_quiz_score_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in save_quiz_score request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        score = data.get('score')
        total_questions = data.get('total_questions')
        topic = data.get('topic', 'General')
        year_group = data.get('year_group', 'General')
        results = data.get('results', [])
        quiz_id = data.get('quiz_id')
        if not user_id or score is None or total_questions is None:
            logger.error(f"Missing required fields: user_id={user_id}, score={score}, total_questions={total_questions}")
            return jsonify({'error': 'user_id, score, and total_questions are required'}), 400
        logger.info(f"Saving quiz score for user_id: {user_id}, score: {score}/{total_questions}, topic: {topic}, year_group: {year_group}, quiz_id: {quiz_id}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        save_quiz_score(db, user_id, score, total_questions, topic, year_group, results, quiz_id=quiz_id)
        logger.debug(f"Saved quiz score for user_id: {user_id}")
        return jsonify({'message': 'Quiz score saved successfully'}), 200
    except Exception as e:
        logger.exception(f"Save quiz score error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Failed to save quiz score: {str(e)}'}), 500

@app.route('/generate_flashcards_from_quiz', methods=['POST'])
def generate_flashcards_from_quiz_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in generate_flashcards_from_quiz request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        topic = data.get('topic')
        if not user_id:
            logger.error("Missing user_id in generate_flashcards_from_quiz request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Generating flashcards from quiz for user_id: {user_id}, topic: {topic}")
        user_ref = db.collection('users').document(user_id)
        if not user_ref.get().exists:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        flashcards = generate_flashcards_from_quiz(db, user_id, topic)
        if not flashcards:
            logger.warning(f"No flashcards generated for user_id: {user_id}")
            return jsonify({'message': 'No flashcards generated. Try a different topic or take more quizzes.'}), 200
        return jsonify({'flashcards': flashcards}), 200
    except Exception as e:
        logger.exception(f"Generate flashcards from quiz error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/generate_flashcards_from_document', methods=['POST'])
def generate_flashcards_from_document_endpoint():
    try:
        if 'file' not in request.files:
            logger.error("No file provided in generate_flashcards_from_document request")
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        user_id = request.form.get('user_id')
        topic = request.form.get('topic', 'General')
        user_input = request.form.get('user_input', '')
        if not file or not user_id or file.filename == '':
            logger.error(f"Missing required fields: user_id={user_id}, file={'provided' if file else 'missing'}")
            return jsonify({'error': 'File and user_id are required'}), 400
        if not allowed_file(file.filename):
            logger.error(f"Invalid file type: {file.filename}")
            return jsonify({'error': 'File type not allowed. Use pdf, docx, txt'}), 400
        logger.info(f"Generating flashcards from document for user_id: {user_id}, file: {file.filename}")
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        if filename.endswith('.pdf'):
            document_text = process_pdf(file_path)
        elif filename.endswith('.docx'):
            document_text = process_docx(file_path)
        elif filename.endswith('.txt'):
            document_text = process_text_file(file_path)
        else:
            os.remove(file_path)
            logger.error(f"Unsupported file type: {filename}")
            return jsonify({'error': 'Unsupported file type'}), 400
        os.remove(file_path)
        if not document_text:
            logger.error(f"Failed to process document: {filename}")
            return jsonify({'error': 'Failed to process document'}), 500
        flashcards = generate_flashcards_from_document(db, user_id, document_text, user_input, topic)
        if not flashcards:
            logger.warning(f"No flashcards generated from document for user_id: {user_id}")
            return jsonify({'message': 'No flashcards generated from document.'}), 200
        return jsonify({'flashcards': flashcards}), 200
    except Exception as e:
        logger.exception(f"Generate flashcards from document error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/generate_flashcards_from_input', methods=['POST'])
def generate_flashcards_from_input_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in generate_flashcards_from_input request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        question = data.get('question')
        answer = data.get('answer')
        topic = data.get('topic', 'General')
        if not user_id or not question or not answer:
            logger.error(f"Missing required fields: user_id={user_id}, question={question}, answer={answer}")
            return jsonify({'error': 'user_id, question, and answer are required'}), 400
        logger.info(f"Generating flashcard from input for user_id: {user_id}, topic: {topic}")
        flashcards = generate_flashcards_from_input(db, user_id, question, answer, topic)
        return jsonify({'flashcards': flashcards}), 200
    except Exception as e:
        logger.exception(f"Generate flashcard from input error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/get_flashcards', methods=['POST'])
def get_flashcards_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in get_flashcards request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        topic = data.get('topic')
        status = data.get('status')
        if not user_id:
            logger.error("Missing user_id in get_flashcards request")
            return jsonify({'error': 'user_id is required'}), 400
        logger.info(f"Fetching flashcards for user_id: {user_id}, topic: {topic}, status: {status}")
        flashcards = get_user_flashcards(db, user_id, topic, status)
        return jsonify({'flashcards': flashcards}), 200
    except Exception as e:
        logger.exception(f"Get flashcards error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/update_flashcard_status', methods=['POST'])
def update_flashcard_status_endpoint():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data provided in update_flashcard_status request")
            return jsonify({'error': 'Invalid JSON payload'}), 400
        user_id = data.get('user_id')
        flashcard_id = data.get('flashcard_id')
        status = data.get('status')
        if not user_id or not flashcard_id or not status:
            logger.error(f"Missing required fields: user_id={user_id}, flashcard_id={flashcard_id}, status={status}")
            return jsonify({'error': 'user_id, flashcard_id, and status are required'}), 400
        if status not in ['review', 'mastered']:
            logger.error(f"Invalid status: {status}")
            return jsonify({'error': 'Status must be "review" or "mastered"'}), 400
        logger.info(f"Updating flashcard status for user_id: {user_id}, flashcard_id: {flashcard_id}")
        result = update_flashcard_status(db, user_id, flashcard_id, status)
        if 'error' in result:
            logger.error(f"Update flashcard status error: {result['error']}")
            return jsonify({'error': result['error']}), 500
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Update flashcard status error for user_id: {user_id}: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

if __name__ == '__main__':
    logger.info("Starting Flask application")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)