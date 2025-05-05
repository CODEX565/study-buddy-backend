import os
import re
import requests
import base64
from dotenv import load_dotenv
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore
from io import BytesIO
from PIL import Image
import pytz
from datetime import datetime
import time
from PyPDF2 import PdfReader
from docx import Document
import chardet
from geopy.geocoders import Nominatim

# Load environment variables
load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
HUGGINGFACE_API_KEY = os.getenv('HUGGINGFACE_API_KEY')
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')

# Firebase setup
if not firebase_admin._apps:
    cred = credentials.Certificate('studybuddy.json')
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Google Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

# Geocoder for reverse geocoding
geolocator = Nominatim(user_agent="study_buddy_app")

def get_user_data(user_id):
    try:
        ref = db.collection('users').document(user_id)
        doc = ref.get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"[Firestore Error] {e}")
        return None

def save_conversation_history(user_id, history):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({"conversation_history": history})
    except Exception as e:
        print(f"[Firestore Update Error] {e}")

def evaluate_memory_worth(user_id, memory_type, memory_value):
    """
    Evaluate if a memory is worth saving based on relevance and specificity.
    More lenient for user preferences like likes/dislikes/favorites.
    Returns True if worth saving, False otherwise.
    """
    # Skip overly generic or empty inputs
    generic_terms = ['it', 'them', 'stuff', 'things', 'something', '']
    if memory_value.lower().strip() in generic_terms:
        return False
    
    # Check for redundancy
    user_data = get_user_data(user_id)
    existing_memories = user_data.get('memories', []) if user_data else []
    for mem in existing_memories:
        if mem['type'] == memory_type and mem['value'].lower() == memory_value.lower():
            return False
    
    # Lenient rules for preferences
    if memory_type in ['likes', 'dislikes'] or memory_type.startswith('favorite_'):
        # Allow single words or short phrases, max 30 characters
        if len(memory_value) <= 30:
            return True
    
    # Stricter rules for projects/tasks/study topics
    if memory_type in ['project', 'task', 'study_topic']:
        # Require at least 2 words, max 50 characters
        if len(memory_value.split()) >= 2 and len(memory_value) <= 50:
            return True
    
    return False

def summarize_memory(memory_value):
    """
    Summarize the memory to make it concise and meaningful.
    """
    memory_value = re.sub(r'\s+', ' ', memory_value.strip())
    words = memory_value.split()
    if len(words) > 15:
        return ' '.join(words[:15]) + '...'
    return memory_value

def save_user_memory(user_id, memory):
    try:
        memory_value = summarize_memory(memory['value'])
        if not evaluate_memory_worth(user_id, memory['type'], memory_value):
            print(f"[Memory Skipped] {memory} - Not worth saving")
            return
        
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            "memories": firestore.ArrayUnion([{
                "type": memory['type'],
                "value": memory_value,
                "timestamp": memory['timestamp']
            }])
        })
        print(f"[Memory Saved] {{'type': '{memory['type']}', 'value': '{memory_value}', 'timestamp': '{memory['timestamp']}'}}")
    except Exception as e:
        print(f"[Firestore Memory Error] {e}")

def process_user_input(user_input, user_data):
    updated = user_data.copy()
    patterns = {
        'study_goal': r"(study goal)(.*)",
        'motivation_quotes': r"(motivation quotes)(.*)",
        'study_plan': r"(study plan)(.*)",
        'subscription_status': r"(subscription status)(.*)",
        'premium_expiry_date': r"(premium expiry)(.*)"
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, user_input, re.IGNORECASE)
        if match:
            value = match.group(2).strip()
            if value and value != user_data.get(key, ''):
                updated[key] = value
                print(f"✅ Updated {key.replace('_', ' ')} to '{value}'.")
    return updated

def detect_memories(user_input, user_id):
    """
    Detect meaningful memories from user input, focusing on projects, tasks, study topics, and user preferences.
    """
    memory_patterns = [
        (r"(?:working on|building|studying|project is|task is)\s+([a-zA-Z\s\-]+)", "project"),
        (r"(?:need to|have to|planning to)\s+(?:study|work on|complete)\s+([a-zA-Z\s\-]+)", "task"),
        (r"(?:learning about|reading about|interested in)\s+([a-zA-Z\s\-]+)", "study_topic"),
        (r"(?:i like|love|enjoy|fan of)\s+([a-zA-Z\s]+)", "likes"),
        (r"(?:i hate|dislike|not a fan of)\s+([a-zA-Z\s]+)", "dislikes"),
        (r"(?:my favorite)\s+(?:artist|band|singer|music)\s+is\s+([a-zA-Z\s]+)", "favorite_music"),
        (r"(?:my favorite)\s+(?:car|vehicle)\s+is\s+([a-zA-Z0-9\s]+)", "favorite_car"),
    ]
    memories = []
    for pattern, memory_type in memory_patterns:
        match = re.search(pattern, user_input, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            memories.append({"type": memory_type, "value": value, "timestamp": datetime.now().isoformat(), "user_id": user_id})
    return memories

def generate_image(prompt, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.post(
                "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0",
                headers={"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"},
                json={"inputs": prompt},
                timeout=60
            )
            
            if response.status_code == 503:
                wait_time = 10 * (attempt + 1)
                print(f"Model loading, retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
                
            if response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                buffered = BytesIO()
                img.save(buffered, format="PNG")
                return base64.b64encode(buffered.getvalue()).decode('utf-8')
                
            print(f"Attempt {attempt + 1} failed: ${response.status_code} - ${response.text}")
            
        except Exception as e:
            print(f"Attempt {attempt + 1} error: ${str(e)}")
            if attempt == max_retries - 1:
                return None
            time.sleep(5)
    
    return None

def get_weather(latitude, longitude):
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={OPENWEATHER_API_KEY}&units=metric"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            weather = {
                "description": data["weather"][0]["description"].title(),
                "temperature": data["main"]["temp"],
                "city": data["name"],
                "country": data["sys"]["country"]
            }
            return weather
        else:
            print("[Weather API Error]", response.text)
    except Exception as e:
        print("[Weather Fetch Error]", e)
    return None

def get_local_time(latitude, longitude):
    try:
        location = geolocator.reverse((latitude, longitude), language='en')
        if location:
            timezone_str = location.raw.get('timezone', 'UTC')
            tz = pytz.timezone(timezone_str)
            now = datetime.now(tz)
            return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
        return None, None
    except Exception as e:
        print("[Timezone Error]", e)
        return None, None

def process_image_with_gemini(user_input, image_data, conversation_history, user_memories, user_id, mime_type="image/png", latitude=None, longitude=None):
    try:
        weather_info = get_weather(latitude, longitude) if latitude and longitude else None
        weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C" if weather_info else "Not available"
        current_time, current_date = get_local_time(latitude, longitude) if latitude and longitude else (None, None)
        current_time = current_time or "Not available"
        current_date = current_date or "Not available"

        if not isinstance(conversation_history, list):
            conversation_history = []
        history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])

        memories_text = "\n".join([f"{m['type']}: {m['value']}" for m in user_memories[-3:]]) if user_memories else "No memories available"

        prompt = f"""
You are Max, the friendly AI inside the Study Buddy app.
Recent conversation:
{history_text}

User memories:
{memories_text}

User says: "{user_input}"
Current weather: {weather_text}
Current time: {current_time}
Current date: {current_date}

Analyze the provided image and incorporate the user's text in your response.
Use the conversation history and user memories to maintain context and personalize responses.
Avoid using greetings like "Hey [Name]" unless it's the first message in a new conversation.
If the user switches topics, transition cleanly and keep the response realistic and human-like.
If the user mentions a project, task, study topic, or preference (e.g., liking a car), include [SAVE_MEMORY: type=value] (e.g., [SAVE_MEMORY: favorite_car=BMW M4]).
Respond in a witty, helpful, human-like way, using the weather and time context only if relevant to the image or user request.
Keep the response short, warm, and natural. Use emojis to make it engaging.
"""
        contents = [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"data": image_data, "mime_type": mime_type}}
                ]
            }
        ]
        
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(response_modalities=['TEXT'])
        )
        
        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text
        
        # Check for memory-saving instructions
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()})
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        # Detect additional memories from user input
        detected_memories = detect_memories(user_input, user_id)
        for memory in detected_memories:
            save_user_memory(user_id, memory)

        return text or "Hmm, I couldn't process that image!"
    except Exception as e:
        print(f"[Gemini Image Error] {e}")
        return "❌ Oops! Something went wrong with the image."

def process_document_with_gemini(user_id, document_text, user_input, conversation_history, user_memories, latitude=None, longitude=None):
    try:
        user_data = get_user_data(user_id)
        if not user_data:
            return None

        weather_info = get_weather(latitude, longitude) if latitude and longitude else None
        weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C" if weather_info else "Not available"
        current_time, current_date = get_local_time(latitude, longitude) if latitude and longitude else (None, None)
        current_time = current_time or "Not available"
        current_date = current_date or "Not available"

        history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])
        memories_text = "\n".join([f"{m['type']}: {m['value']}" for m in user_memories[-3:]]) if user_memories else "No memories available"

        prompt = f"""
You are Max, the friendly AI inside the Study Buddy app.
Recent conversation:
{history_text}

User memories:
{memories_text}

User says: "{user_input}"
Current weather: {weather_text}
Current time: {current_time}
Current date: {current_date}

The user has uploaded a document with the following content:
```
{document_text[:2000]}
```

Summarize what the document is about and incorporate the user's text in your response.
Use the conversation history and user memories to maintain context and personalize responses.
Avoid using greetings like "Hey [Name]" unless it's the first message in a new conversation.
If the user switches topics, transition cleanly and keep the response realistic and human-like.
If the document or user input mentions a project, task, study topic, or preference, include [SAVE_MEMORY: type=value] (e.g., [SAVE_MEMORY: favorite_car=BMW M4]).
Use the weather and time context only if relevant to the document or user request.
Suggest what the user might want to do with it (e.g., study, quiz, summarize).
Keep the response short, warm, and natural. Use emojis to make it engaging.
"""
        full_prompt = f"""
Recent conversation:
{history_text}

{prompt}
"""

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=full_prompt,
            config=types.GenerateContentConfig(response_modalities=['TEXT'])
        )

        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text
        
        # Check for memory-saving instructions
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()})
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        # Detect additional memories from user input
        detected_memories = detect_memories(user_input, user_id)
        for memory in detected_memories:
            save_user_memory(user_id, memory)

        return text or "Hmm, I couldn't process that document!"
    except Exception as e:
        print(f"[Gemini Document Error] {e}")
        return "❌ Oops! Something went wrong with the document."

def generate_gemini_response(user_data, user_input, conversation_history, user_id, image_data=None, mime_type=None, latitude=None, longitude=None):
    weather_info = get_weather(latitude, longitude) if latitude and longitude else None
    weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C" if weather_info else "Not available"
    current_time, current_date = get_local_time(latitude, longitude) if latitude and longitude else (None, None)
    current_time = current_time or "Not available"
    current_date = current_date or "Not available"

    navigation_keywords = {
        "home screen": "go_to_home_screen",
        "quiz screen": "go_to_quiz_screen",
        "alarm screen": "go_to_alarm_screen",
        "notes screen": "go_to_notes_screen",
        "flashcards screen": "go_to_flashcards_screen",
        "settings screen": "go_to_settings_screen",
        "profile screen": "go_to_profile_screen",
        "ai chat screen": "go_to_ai_chat_screen",
        "reminder screen": "go_to_reminder_screen",
        "summary screen": "go_to_summary_screen"
    }
    for keyword, command in navigation_keywords.items():
        if re.search(rf"(go|take|navigate|open|show).*{keyword}", user_input, re.IGNORECASE):
            return command

    if image_data:
        return process_image_with_gemini(user_input, image_data, conversation_history, user_data.get('memories', []), user_id, mime_type, latitude, longitude)

    if not isinstance(conversation_history, list):
        conversation_history = []

    history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])
    memories_text = "\n".join([f"{m['type']}: {m['value']}" for m in user_data.get('memories', [])[-3:]]) if user_data.get('memories') else "No memories available"

    # Detect memories from user input
    detected_memories = detect_memories(user_input, user_id)
    for memory in detected_memories:
        save_user_memory(user_id, memory)

    prompt = f"""
You are Max, the friendly AI inside the Study Buddy app. 
You assist with studying but you also respond in a witty, personal, human-like way.

Here’s what you know about the user:
- Name: {user_data.get('name', 'Unknown')}
- Age: {user_data.get('age', 'not specified')}
- Study Goal: {user_data.get('study_goal', 'not specified')}
- Subscription Status: {user_data.get('subscription_status', 'not specified')}
- Premium Expiry Date: {user_data.get('premium_expiry_date', 'not specified')}
- Study Plan: {user_data.get('study_plan', 'not specified')}
- Weekly Challenges Enabled: {user_data.get('weekly_challenges_enabled', False)}
- Focus Music Enabled: {user_data.get('focus_music_enabled', False)}
- Current Weather: {weather_text}
- Current Time: {current_time}
- Current Date: {current_date}
- User Memories:
{memories_text}

Recent conversation:
{history_text}

User now says: "{user_input}"

Instructions:
- If the user asks for an image to be created (example: "draw", "create a picture", "make an image"),
  reply with [GENERATE_IMAGE: description of the image].
- Otherwise, respond like a helpful, witty, supportive friend.
- Use the conversation history and user memories to maintain context and personalize responses.
- Avoid using greetings like "Hey [Name]" unless it's the first message in a new conversation.
- If the user switches topics, transition cleanly and keep the response realistic and human-like.
- If the user mentions a project, task, study topic, or preference (e.g., liking a car), include [SAVE_MEMORY: type=value] (e.g., [SAVE_MEMORY: favorite_car=BMW M4]).
- Use the weather and time context only if relevant to the user’s request or to enhance the response.
- Keep responses short, warm, and natural.
- If navigation is requested, reply with the command (e.g., "go_to_quiz_screen").
- Always sound natural and human-like, using emojis to make the conversation engaging.
"""
    try:
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=['TEXT'])
        )
        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text
        
        # Check for memory-saving instructions
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()})
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        return text or "Hmm, I didn't quite catch that!"
    except Exception as e:
        print(f"[Gemini Error] {e}")
        return "❌ Oops! Something went wrong."

# --------- DOCUMENT PROCESSING FUNCTIONS --------- #

def process_pdf(file_path):
    try:
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        print(f"[PDF Processing Error] {e}")
        return None

def process_docx(file_path):
    try:
        doc = Document(file_path)
        text = ""
        for para in doc.paragraphs:
            text += para.text + "\n"
        return text
    except Exception as e:
        print(f"[DOCX Processing Error] {e}")
        return None

def process_text_file(file_path):
    try:
        with open(file_path, 'rb') as file:
            raw_data = file.read()
            result = chardet.detect(raw_data)
            encoding = result['encoding']
            text = raw_data.decode(encoding)
            return text
    except Exception as e:
        print(f"[Text File Processing Error] {e}")
        return None