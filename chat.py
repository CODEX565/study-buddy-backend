import logging
from flask import Blueprint, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from firebase_admin import firestore, messaging
import uuid
from datetime import datetime
import os
from werkzeug.utils import secure_filename

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Blueprint for chat routes
chat_bp = Blueprint('chat', __name__)

# Initialize SocketIO (attach to your main app later)
socketio = SocketIO(cors_allowed_origins="*")

# Firestore client
db = firestore.client()

# Upload folder for resources
UPLOAD_FOLDER = 'Uploads'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt', 'png', 'jpg', 'jpeg'}
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def send_push_notification(user_id, message, chat_id=None, group_id=None):
    try:
        user_doc = db.collection('users').document(user_id).get()
        if not user_doc.exists:
            logger.warning(f"User not found for notification: {user_id}")
            return
        fcm_token = user_doc.to_dict().get('fcm_token')
        if not fcm_token:
            logger.warning(f"No FCM token for user: {user_id}")
            return
        notification = messaging.Message(
            notification=messaging.Notification(
                title="Study Buddy",
                body=message
            ),
            data={
                'chat_id': chat_id or '',
                'group_id': group_id or ''
            },
            token=fcm_token
        )
        response = messaging.send(notification)
        logger.info(f"Sent push notification to user {user_id}: {response}")
    except Exception as e:
        logger.error(f"Failed to send push notification to {user_id}: {str(e)}")

@chat_bp.route('/add_friend', methods=['POST'])
def add_friend():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        friend_id = data.get('friend_id')
        if not user_id or not friend_id:
            return jsonify({'error': 'user_id and friend_id required'}), 400
        user_ref = db.collection('users').document(user_id)
        friend_ref = db.collection('users').document(friend_id)
        if not user_ref.get().exists or not friend_ref.get().exists:
            return jsonify({'error': 'User or friend not found'}), 404
        user_ref.update({'friends': firestore.ArrayUnion([friend_id])})
        friend_ref.update({'friends': firestore.ArrayUnion([user_id])})
        notification_id = str(uuid.uuid4())
        db.collection('notifications').document(notification_id).set({
            'user_id': friend_id,
            'type': 'friend_request',
            'message': f"{user_ref.get().to_dict().get('display_name')} added you as a friend!",
            'timestamp': datetime.utcnow(),
            'read': False
        })
        send_push_notification(
            friend_id,
            f"{user_ref.get().to_dict().get('display_name')} added you as a friend!"
        )
        logger.info(f"Friend added: {user_id} -> {friend_id}")
        return jsonify({'message': 'Friend added successfully'}), 200
    except Exception as e:
        logger.error(f"Add friend error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chat_bp.route('/create_group', methods=['POST'])
def create_group():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        group_name = data.get('group_name')
        member_ids = data.get('member_ids', [])
        if not user_id or not group_name:
            return jsonify({'error': 'user_id and group_name required'}), 400
        user_ref = db.collection('users').document(user_id)
        if not user_ref.get().exists:
            return jsonify({'error': 'User not found'}), 404
        group_id = str(uuid.uuid4())
        db.collection('groups').document(group_id).set({
            'name': group_name,
            'creator_id': user_id,
            'members': [user_id] + member_ids,
            'created_at': datetime.utcnow()
        })
        user_ref.update({'groups': firestore.ArrayUnion([group_id])})
        for member_id in member_ids:
            member_ref = db.collection('users').document(member_id)
            if member_ref.get().exists:
                member_ref.update({'groups': firestore.ArrayUnion([group_id])})
                notification_id = str(uuid.uuid4())
                db.collection('notifications').document(notification_id).set({
                    'user_id': member_id,
                    'type': 'group_join',
                    'message': f"You were added to group {group_name}!",
                    'group_id': group_id,
                    'timestamp': datetime.utcnow(),
                    'read': False
                })
                send_push_notification(member_id, f"You were added to group {group_name}!", group_id=group_id)
        logger.info(f"Group created: {group_id} by {user_id}")
        return jsonify({'group_id': group_id}), 200
    except Exception as e:
        logger.error(f"Create group error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chat_bp.route('/join_group', methods=['POST'])
def join_group():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        group_id = data.get('group_id')
        if not user_id or not group_id:
            return jsonify({'error': 'user_id and group_id required'}), 400
        user_ref = db.collection('users').document(user_id)
        group_ref = db.collection('groups').document(group_id)
        if not user_ref.get().exists or not group_ref.get().exists:
            return jsonify({'error': 'User or group not found'}), 404
        group_data = group_ref.get().to_dict()
        group_ref.update({'members': firestore.ArrayUnion([user_id])})
        user_ref.update({'groups': firestore.ArrayUnion([group_id])})
        display_name = user_ref.get().to_dict().get('display_name')
        for member_id in group_data['members']:
            if member_id != user_id:
                db.collection('notifications').document(str(uuid.uuid4())).set({
                    'user_id': member_id,
                    'type': 'group_join',
                    'message': f"{display_name} joined {group_data['name']}!",
                    'group_id': group_id,
                    'timestamp': datetime.utcnow(),
                    'read': False
                })
                send_push_notification(
                    member_id,
                    f"{display_name} joined {group_data['name']}!",
                    group_id=group_id
                )
        logger.info(f"User {user_id} joined group {group_id}")
        return jsonify({'message': 'Joined group successfully'}), 200
    except Exception as e:
        logger.error(f"Join group error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chat_bp.route('/send_message', methods=['POST'])
def send_message():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        chat_id = data.get('chat_id')
        text = data.get('text')
        if not user_id or not chat_id or not text:
            return jsonify({'error': 'user_id, chat_id, and text required'}), 400
        user_ref = db.collection('users').document(user_id)
        chat_ref = db.collection('chats').document(chat_id)
        if not user_ref.get().exists or not chat_ref.get().exists:
            return jsonify({'error': 'User or chat not found'}), 404
        message_id = str(uuid.uuid4())
        message_data = {
            'sender_id': user_id,
            'text': text,
            'timestamp': datetime.utcnow()
        }
        chat_ref.collection('messages').document(message_id).set(message_data)
        chat_ref.update({
            'last_message': text,
            'last_message_time': datetime.utcnow(),
            'last_message_sender': user_id
        })
        chat_data = chat_ref.get().to_dict()
        participants = chat_data.get('participants', [])
        for participant_id in participants:
            if participant_id != user_id:
                db.collection('notifications').document(str(uuid.uuid4())).set({
                    'user_id': participant_id,
                    'type': 'message',
                    'message': f"New message from {user_ref.get().to_dict().get('display_name')}: {text[:50]}...",
                    'chat_id': chat_id,
                    'timestamp': datetime.utcnow(),
                    'read': False
                })
                send_push_notification(
                    participant_id,
                    f"New message from {user_ref.get().to_dict().get('display_name')}: {text[:50]}...",
                    chat_id=chat_id
                )
        socketio.emit('new_message', {
            'chat_id': chat_id,
            'message': {**message_data, 'message_id': message_id}
        }, room=chat_id)
        logger.info(f"Message sent to chat {chat_id} by {user_id}")
        return jsonify({'message_id': message_id}), 200
    except Exception as e:
        logger.error(f"Send message error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chat_bp.route('/share_resource', methods=['POST'])
def share_resource():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        user_id = request.form.get('user_id')
        chat_id = request.form.get('chat_id')
        if not file or not user_id or not chat_id:
            return jsonify({'error': 'file, user_id, and chat_id required'}), 400
        if not allowed_file(file.filename):
            return jsonify({'error': 'File type not allowed'}), 400
        filename = secure_filename(file.filename)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(file_path)

        message_id = str(uuid.uuid4())
        message_data = {
            'sender_id': user_id,
            'file_url': file_path,
            'timestamp': datetime.utcnow(),
            'type': 'file'
        }

        chat_ref = db.collection('chats').document(chat_id)
        chat_ref.collection('messages').document(message_id).set(message_data)
        chat_ref.update({
            'last_message': f"📎 File shared",
            'last_message_time': datetime.utcnow(),
            'last_message_sender': user_id
        })

        socketio.emit('new_message', {
            'chat_id': chat_id,
            'message': {**message_data, 'message_id': message_id}
        }, room=chat_id)

        logger.info(f"File shared in chat {chat_id} by {user_id}")
        return jsonify({'message_id': message_id, 'file_url': file_path}), 200
    except Exception as e:
        logger.error(f"Share resource error: {str(e)}")
        return jsonify({'error': str(e)}), 500
