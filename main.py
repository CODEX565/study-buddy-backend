import logging
from flask import Flask, request, jsonify
from flask_socketio import SocketIO
import firebase_admin
from firebase_admin import credentials, firestore
from Max import generate_gemini_response, process_image_with_gemini, process_document_with_gemini, process_pdf, process_docx, process_text_file, generate_image
from Quiz import generate_quiz_question, check_answer, get_user_data, save_quiz_score
from flashcards import generate_flashcards_from_quiz, generate_flashcards_from_document, generate_flashcards_from_input, get_user_flashcards, update_flashcard_status
import os
from werkzeug.utils import secure_filename
from datetime import datetime
from gemini_image_processor import image_processor_bp

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

if not firebase_admin._apps:
    cred = credentials.Certificate('studybuddy.json')
    firebase_admin.initialize_app(cred)

db = firestore.client()

UPLOAD_FOLDER = 'Uploads'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET'])
def index():
    logger.info(f"GET / request: headers={request.headers}, args={request.args}, remote_addr={request.remote_addr}")
    return jsonify({
        'status': 'Study Buddy backend running',
        'message': 'Use POST to /chat, /generate_quiz, /process_document, etc.'
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
        logger.debug(f"Chat history cleared for user_id: {user_id}")
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
        quiz_id = data.get('quiz_id', str(uuid.uuid4()))
        if not user_id or score is None or total_questions is None:
            logger.error(f"Missing required fields: user_id={user_id}, score={score}, total_questions={total_questions}")
            return jsonify({'error': 'user_id, score, and total_questions are required'}), 400
        logger.info(f"Saving quiz score for user_id: {user_id}, score: {score}/{total_questions}, topic: {topic}, year_group: {year_group}")
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404
        save_quiz_score(db, user_id, score, total_questions, topic, year_group, results, quiz_id)
        logger.debug(f"Saved quiz score for user_id: {user_id}: {score}/{total_questions}")
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
            return jsonify({'error': 'File type not allowed. Use pdf, docx, or txt'}), 400
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