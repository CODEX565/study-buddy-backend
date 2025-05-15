from flask import Blueprint, request, jsonify
import os
import logging
import base64
from Max import process_image_with_gemini

image_answer_bp = Blueprint('image_answer', __name__)

# Configure logging
logging.basicConfig(filename='studybuddy.log', level=logging.INFO)

@image_answer_bp.route('/generate_image_answer', methods=['POST'])
def generate_image_answer():
    try:
        user_id = request.form.get('user_id')
        subject = request.form.get('subject')
        image = request.files.get('image')

        if not user_id or not subject or not image:
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
        from firebase_admin import firestore
        db = firestore.client()
        user_data = db.collection('users').document(user_id).get().to_dict()
        if not user_data:
            logging.warning(f"User not found: {user_id}")
            return jsonify({'error': 'User not found'}), 404

        # Process image with Gemini
        user_input = f"Provide a concise answer or solution for this {subject} content."
        conversation_history = user_data.get('conversation_history', [])
        response = process_image_with_gemini(
            user_input=user_input,
            image_base64=image_base64,
            conversation_history=conversation_history,
            memories=user_data.get('memories', []),
            user_id=user_id,
            mime_type="image/jpeg"  # Adjust based on image type (jpeg, png, etc.)
        )

        logging.info(f"Generated answer for user {user_id}, subject {subject}")
        return jsonify({'answer': response}), 200
    except Exception as e:
        logging.error(f"Error in generate_image_answer: {str(e)}")
        return jsonify({'error': str(e)}), 500