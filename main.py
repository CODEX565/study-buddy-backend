from flask import Flask, request, jsonify, session
import Max  # Your Max.py logic
import Quiz  # Your Quiz.py logic
import re
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Secret key for session security (pulled from .env)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")

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

    if not user_input or not user_id:
        return jsonify({"error": "Missing user_input or user_id"}), 400

    user_data = Max.get_user_data(user_id)
    if not user_data:
        return jsonify({"error": "User not found"}), 404

    conversation_history = user_data.get("conversation_history", [])
    if not isinstance(conversation_history, list):
        conversation_history = []

    user_data = Max.process_user_input(user_input, user_data)
    response_text = Max.generate_gemini_response(user_data, user_input, conversation_history)

    image_base64 = None
    if "[GENERATE_IMAGE:" in response_text:
        match = re.search(r"\[GENERATE_IMAGE:(.*?)\]", response_text)
        if match:
            image_prompt = match.group(1).strip()
            image_base64 = Max.generate_image(image_prompt)
            if image_base64:
                response_text = "Here's the image I created for you! 🎨"
            else:
                response_text = "❌ Failed to generate the image."

    conversation_history.append({"user": user_input, "max": response_text})
    Max.save_conversation_history(user_id, conversation_history)

    return jsonify({
        "response": response_text,
        "image_base64": image_base64
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
    app.run(host="0.0.0.0", port=5000)
