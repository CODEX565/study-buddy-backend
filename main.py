from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from Max import generate_gemini_response, process_image_with_gemini, process_document_with_gemini, process_pdf, process_docx, process_text_file
from Quiz import generate_quiz_question, check_answer
import os
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)

# Firebase setup
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

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        user_input = data.get('user_input')
        user_id = data.get('user_id')
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        if not user_input or not user_id:
            return jsonify({'error': 'user_input and user_id are required'}), 400

        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            return jsonify({'error': 'User not found'}), 404

        conversation_history = user_data.get('conversation_history', [])
        response = generate_gemini_response(user_data, user_input, conversation_history, user_id, latitude=latitude, longitude=longitude)

        if response.startswith('[GENERATE_IMAGE:'):
            image_prompt = response[len('[GENERATE_IMAGE:'):-1].strip()
            from Max import generate_image
            image_base64 = generate_image(image_prompt)
            if image_base64:
                response_data = {'response': 'Here’s the image you requested!', 'image_base64': image_base64}
            else:
                response_data = {'response': 'Sorry, I couldn’t generate the image. Try again? 😅'}
        else:
            response_data = {'response': response}

        conversation_history.append({'user': user_input, 'max': response_data['response']})
        db.collection('users').document(user_id).update({'conversation_history': conversation_history})

        return jsonify(response_data), 200
    except Exception as e:
        print(f"[Chat Error] {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/process_image', methods=['POST'])
def process_image():
    try:
        data = request.get_json()
        user_input = data.get('user_input')
        user_id = data.get('user_id')
        image_base64 = data.get('image_base64')
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        if not user_input or not user_id or not image_base64:
            return jsonify({'error': 'user_input, user_id, and image_base64 are required'}), 400

        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            return jsonify({'error': 'User not found'}), 404

        conversation_history = user_data.get('conversation_history', [])
        response = process_image_with_gemini(user_input, image_base64, conversation_history, user_data.get('memories', []), user_id, mime_type="image/png", latitude=latitude, longitude=longitude)

        conversation_history.append({'user': user_input, 'max': response})
        db.collection('users').document(user_id).update({'conversation_history': conversation_history})

        return jsonify({'response': response}), 200
    except Exception as e:
        print(f"[Process Image Error] {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/process_document', methods=['POST'])
def process_document():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']
        user_id = request.form.get('user_id')
        user_input = request.form.get('user_input', '')
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')

        if not file or not user_id or file.filename == '':
            return jsonify({'error': 'File and user_id are required'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': 'File type not allowed. Use pdf, docx, or txt'}), 400

        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
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
            return jsonify({'error': 'Unsupported file type'}), 400

        os.remove(file_path)

        if not document_text:
            return jsonify({'error': 'Failed to process document'}), 500

        conversation_history = user_data.get('conversation_history', [])
        response = process_document_with_gemini(user_id, document_text, user_input, conversation_history, user_data.get('memories', []), latitude=latitude, longitude=longitude)

        conversation_history.append({'user': user_input, 'max': response})
        db.collection('users').document(user_id).update({'conversation_history': conversation_history})

        return jsonify({'response': response}), 200
    except Exception as e:
        print(f"[Process Document Error] {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400

        user_ref = db.collection('users').document(user_id)
        user_ref.update({'conversation_history': []})
        return jsonify({'message': 'Chat history cleared successfully'}), 200
    except Exception as e:
        print(f"[Clear Chat Error] {e}")
        return jsonify({'error': 'Failed to clear chat history'}), 500

@app.route('/get_user_data', methods=['POST'])
def get_user_data():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400

        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404

        user_data = user_doc.to_dict()
        memories = user_data.get('memories', [])
        study_topic = None
        for memory in memories:
            if memory.get('type') == 'study_topic':
                study_topic = memory.get('value')
                break

        response_data = {
            'age': user_data.get('age', None),
            'study_topic': study_topic
        }
        print(f"[Get User Data] Fetched for user {user_id}: {response_data}")
        return jsonify(response_data), 200
    except Exception as e:
        print(f"[Get User Data Error] {e}")
        return jsonify({'error': 'Failed to fetch user data'}), 500

@app.route('/generate_quiz', methods=['POST'])
def generate_quiz():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        topic = data.get('topic')

        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400

        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            return jsonify({'error': 'User not found'}), 404

        quiz_data = generate_quiz_question(db, user_id, topic)
        print(f"[Generate Quiz] Response: {quiz_data}")
        if 'error' in quiz_data:
            return jsonify({'error': quiz_data['error']}), 500

        return jsonify(quiz_data), 200
    except Exception as e:
        print(f"[Generate Quiz Error] {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/check_answer', methods=['POST'])
def check_answer_endpoint():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        question_id = data.get('question_id')
        user_answer = data.get('user_answer')

        if not user_id or not question_id or not user_answer:
            return jsonify({'error': 'user_id, question_id, and user_answer are required'}), 400

        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            return jsonify({'error': 'User not found'}), 404

        result = check_answer(db, user_id, question_id, user_answer)
        print(f"[Check Answer] Response: {result}")
        if 'error' in result:
            return jsonify({'error': result['error']}), 404

        return jsonify(result), 200
    except Exception as e:
        print(f"[Check Answer Error] {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/save_quiz_score', methods=['POST'])
def save_quiz_score():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        score = data.get('score')
        total_questions = data.get('total_questions')
        topic = data.get('topic', 'General')

        if not user_id or score is None or total_questions is None:
            return jsonify({'error': 'user_id, score, and total_questions are required'}), 400

        user_ref = db.collection('users').document(user_id)
        user_data = user_ref.get()
        if not user_data.exists:
            return jsonify({'error': 'User not found'}), 404

        quiz_score_entry = {
            'score': score,
            'total_questions': total_questions,
            'topic': topic,
            'timestamp': datetime.utcnow().isoformat()
        }
        user_ref.update({
            'quiz_scores': firestore.ArrayUnion([quiz_score_entry])
        })
        print(f"[Save Quiz Score] Saved for user {user_id}: {quiz_score_entry}")

        return jsonify({'message': 'Quiz score saved successfully'}), 200
    except Exception as e:
        print(f"[Save Quiz Score Error] {e}")
        return jsonify({'error': 'Failed to save quiz score'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)