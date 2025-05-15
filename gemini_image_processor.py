from flask import Blueprint, request, jsonify
import os
import logging
import base64
from dotenv import load_dotenv
import google.generativeai as genai
from firebase_admin import firestore

image_processor_bp = Blueprint('image_processor', __name__)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(filename='studybuddy.log', level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Gemini client
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY not found in environment variables")
    raise ValueError("GEMINI_API_KEY is required")
genai.configure(api_key=GEMINI_API_KEY)

@image_processor_bp.route('/process_image_gemini', methods=['POST'])
def process_image_gemini():
    try:
        user_id = request.form.get('user_id')
        image = request.files.get('image')

        if not user_id or not image:
            logger.error(f"Missing required fields: user_id={user_id}, image={'provided' if image else 'missing'}")
            return jsonify({'error': 'Missing required fields'}), 400

        # Save image temporarily
        image_path = f'temp_{user_id}_{image.filename}'
        image.save(image_path)

        # Convert image to Base64
        with open(image_path, 'rb') as image_file:
            image_base64 = base64.b64encode(image_file.read()).decode('utf-8')

        # Clean up
        os.remove(image_path)

        # Get user data from Firestore
        db = firestore.client()
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404

        # Prepare Gemini 1.5 Flash request
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = "Analyze this image and provide a concise answer or solution based on its content."
        image_content = {
            "mime_type": "image/jpeg",  # Adjust based on image type
            "data": image_base64
        }

        # Send request to Gemini
        response = model.generate_content([prompt, image_content])
        answer = response.text.strip() if response.text else "No relevant information found in the image."

        logger.info(f"Generated answer for user {user_id}: {answer}")
        return jsonify({'answer': answer}), 200
    except Exception as e:
        logger.error(f"Error in process_image_gemini: {str(e)}")
        return jsonify({'error': str(e)}), 500