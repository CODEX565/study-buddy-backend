from flask import Flask, request, jsonify, session
import Max  # Your Max.py logic
import Quiz  # Your Quiz.py logic
import re
import os
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import tempfile

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Secret key for session security (pulled from .env)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")

# Allowed file extensions for document processing
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --------- EXISTING ROUTES --------- #

@app.route("/get_user_data/<user_id>", methods=["GET"])
def get_user_info(user_id):
    user_data = Max.get_user_data(user_id)
    if user_data:
        return jsonify(user_data)
    return jsonify({"error": "User not found"}), 404

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_input = data.get("user_input")
    user_id = data.get("user_id")
    image_base64 = data.get("image_base64")  # Optional image data

    if not user_input or not user_id:
        return jsonify({"error": "Missing user_input or user_id"}), 400

    user_data = Max.get_user_data(user_id)
    if not user_data:
        return jsonify({"error": "User not found"}), 404

    conversation_history = user_data.get("conversation_history", [])
    if not isinstance(conversation_history, list):
        conversation_history = []

    user_data = Max.process_user_input(user_input, user_data)
    response_text = Max.generate_gemini_response(
        user_data, 
        user_input, 
        conversation_history, 
        image_data=image_base64, 
        mime_type="image/png" if image_base64 else None
    )

    image_response_base64 = None
    if "[GENERATE_IMAGE:" in response_text:
        match = re.search(r"\[GENERATE_IMAGE:(.*?)\]", response_text)
        if match:
            image_prompt = match.group(1).strip()
            image_response_base64 = Max.generate_image(image_prompt)
            if image_response_base64:
                response_text = "Here's the image I created for you! 🎨"
            else:
                response_text = "❌ Failed to generate the image."

    conversation_history.append({"user": user_input, "max": response_text})
    Max.save_conversation_history(user_id, conversation_history)

    return jsonify({
        "response": response_text,
        "image_base64": image_response_base64
    })

@app.route("/weather", methods=["GET"])
def weather():
    city = request.args.get("city")
    if not city:
        return jsonify({"error": "City parameter is required"}), 400

    weather_data = Max.get_weather(city)
    if weather_data:
        return jsonify(weather_data)
    return jsonify({"error": "Unable to fetch weather"}), 500

# --------- DOCUMENT PROCESSING ROUTE --------- #

@app.route("/process_document", methods=["POST"])
def process_document():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Use PDF, DOCX, or TXT."}), 400

    # Get user_id from form data
    user_id = request.form.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    # Save file temporarily
    try:
        filename = secure_filename(file.filename)
        temp_path = os.path.join(tempfile.gettempdir(), filename)
        file.save(temp_path)

        # Process based on file extension
        extension = filename.rsplit('.', 1)[1].lower()
        if extension == 'pdf':
            text = Max.process_pdf(temp_path)
        elif extension == 'docx':
            text = Max.process_docx(temp_path)
        elif extension == 'txt':
            text = Max.process_text_file(temp_path)
        else:
            text = None

        # Clean up temporary file
        os.remove(temp_path)

        if text:
            # Get user data and conversation history
            user_data = Max.get_user_data(user_id)
            if not user_data:
                return jsonify({"error": "User not found"}), 404

            conversation_history = user_data.get("conversation_history", [])
            if not isinstance(conversation_history, list):
                conversation_history = []

            # Generate conversational response using Gemini
            response_text = Max.process_document_with_gemini(user_id, text, conversation_history)
            if response_text:
                # Update conversation history
                conversation_history.append({"user": f"Uploaded file: {filename}", "max": response_text})
                Max.save_conversation_history(user_id, conversation_history)
                return jsonify({"response": response_text})
            return jsonify({"error": "Failed to process document content"}), 500
        return jsonify({"error": "Failed to extract text from document"}), 500

    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"[Document Processing Error] {e}")
        return jsonify({"error": "Error processing document"}), 500

# --------- IMAGE PROCESSING ROUTE --------- #

@app.route("/process_image", methods=["POST"])
def process_image():
    data = request.get_json()
    user_input = data.get("user_input")
    user_id = data.get("user_id")
    image_base64 = data.get("image_base64")

    if not user_input or not user_id or not image_base64:
        return jsonify({"error": "Missing user_input, user_id, or image_base64"}), 400

    user_data = Max.get_user_data(user_id)
    if not user_data:
        return jsonify({"error": "User not found"}), 404

    conversation_history = user_data.get("conversation_history", [])
    if not isinstance(conversation_history, list):
        conversation_history = []

    # Process image with Gemini
    response_text = Max.generate_gemini_response(
        user_data,
        user_input,
        conversation_history,
        image_data=image_base64,
        mime_type="image/png"
    )

    conversation_history.append({"user": user_input, "max": response_text})
    Max.save_conversation_history(user_id, conversation_history)

    return jsonify({"response": response_text})

# --------- QUIZ ROUTES --------- #

@app.route("/generate_quiz", methods=["GET"])
def generate_quiz():
    user_id = request.args.get("user_id")
    topic = request.args.get("topic")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    quiz = Quiz.generate_quiz_question(user_id, topic)
    if 'error' in quiz:
        return jsonify(quiz), 500
    return jsonify(quiz)

@app.route("/submit_quiz_answer", methods=["POST"])
def submit_quiz_answer():
    data = request.get_json()
    user_id = data.get("user_id")
    question_id = data.get("question_id")
    user_answer = data.get("user_answer")
    if not all([user_id, question_id, user_answer]):
        return jsonify({"error": "Missing required fields"}), 400
    result = Quiz.check_answer(user_id, question_id, user_answer)
    if 'error' in result:
        return jsonify(result), 404
    return jsonify({"is_correct": result.get("result") == "correct"})

# --------- MAIN --------- #

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)