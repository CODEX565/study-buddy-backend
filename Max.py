# Max.py
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

# --------- AI FUNCTIONS --------- #

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

def generate_image(prompt, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.post(
                "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-2-1",
                headers={"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"},
                json={"inputs": prompt},
                timeout=60
            )
            
            if response.status_code == 503:  # Model loading
                wait_time = 10 * (attempt + 1)
                print(f"Model loading, retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
                
            if response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                buffered = BytesIO()
                img.save(buffered, format="PNG")
                return base64.b64encode(buffered.getvalue()).decode('utf-8')
                
            print(f"Attempt {attempt + 1} failed: {response.status_code} - {response.text}")
            
        except Exception as e:
            print(f"Attempt {attempt + 1} error: {str(e)}")
            if attempt == max_retries - 1:
                return None
            time.sleep(5)
    
    return None

def get_weather(city_name):
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather?q={city_name}&appid={OPENWEATHER_API_KEY}&units=metric"
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

def generate_gemini_response(user_data, user_input, conversation_history):
    uk_tz = pytz.timezone('Europe/London')
    current_time = datetime.now(uk_tz).strftime("%I:%M %p")

    weather_info = get_weather("London")
    weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C" if weather_info else "Not available"

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

    if not isinstance(conversation_history, list):
        conversation_history = []

    history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])

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
- Current Time: {current_time}
- Current Weather: {weather_text}

Recent conversation:
{history_text}

User now says: "{user_input}"

Instructions:
- If the user asks for an image to be created (example: "draw", "create a picture", "make an image"),
  reply with [GENERATE_IMAGE: description of the image].
- Otherwise, respond like a helpful, witty, supportive friend.
- Keep responses short, warm, and natural.
- Only mention the time or weather if the user asks.
- If navigation is requested, reply with the command (e.g., "go_to_quiz_screen").
- Always sound natural and human-like.
"""

    try:
        response = client.models.generate_content(
            model="gemma-3-27b-it",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=['TEXT'])
        )
        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text
        return text or "Hmm, I didn't quite catch that!"
    except Exception as e:
        print(f"[Gemini Error] {e}")
        return "❌ Oops! Something went wrong."

