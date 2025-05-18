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
from timezonefinder import TimezoneFinder
import logging  # Added for logging

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

# Timezone finder for accurate local time
tf = TimezoneFinder()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    Stricter for projects/tasks/study topics, lenient for preferences.
    """
    invalid_terms = ['it', 'them', 'stuff', 'things', 'something', '', 'undefined', 'dark', 'light', 'series', 'theme']
    if memory_value.lower().strip() in invalid_terms or len(memory_value.strip()) < 3:
        return False
    
    user_data = get_user_data(user_id)
    existing_memories = user_data.get('memories', []) if user_data else []
    for mem in existing_memories:
        if mem['type'] == memory_type and mem['value'].lower() == memory_value.lower():
            return False
    
    if memory_type in ['likes', 'dislikes'] or memory_type.startswith('favorite_'):
        if len(memory_value) <= 30:
            return True
    
    if memory_type in ['project', 'task', 'study_topic']:
        words = memory_value.split()
        if len(words) >= 2 and len(memory_value) <= 50 and not any(term in memory_value.lower() for term in ['theme', 'series']):
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

def save_user_memory(user_id, memory, existing_memories=None):
    try:
        memory_value = summarize_memory(memory['value'])
        if not evaluate_memory_worth(user_id, memory['type'], memory_value):
            print(f"[Memory Skipped] {memory} - Not worth saving")
            return False
        
        if existing_memories:
            for em in existing_memories:
                if em['type'] == memory['type'] and em['value'].lower() == memory_value.lower():
                    return False
        
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            "memories": firestore.ArrayUnion([{
                "type": memory['type'],
                "value": memory_value,
                "timestamp": memory['timestamp']
            }])
        })
        print(f"[Memory Saved] {{'type': '{memory['type']}', 'value': '{memory_value}', 'timestamp': '{memory['timestamp']}'}}")
        return True
    except Exception as e:
        print(f"[Firestore Memory Error] {e}")
        return False

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
    Detect meaningful memories from user input, focusing on projects, tasks, study topics, and preferences.
    """
    memory_patterns = [
        (r"(?:working on|building|project is)\s+([a-zA-Z\s\-]{5,50})", "project"),
        (r"(?:need to|have to|planning to)\s+(?:study|work on|complete)\s+([a-zA-Z\s\-]{5,50})", "task"),
        (r"(?:learning about|reading about|interested in)\s+([a-zA-Z\s\-]{5,50})", "study_topic"),
        (r"(?:i like|love|enjoy|fan of)\s+([a-zA-Z\s]{3,30})", "likes"),
        (r"(?:i hate|dislike|not a fan of)\s+([a-zA-Z\s]{3,30})", "dislikes"),
        (r"(?:my favorite)\s+(?:artist|band|singer|music)\s+is\s+([a-zA-Z\s]{3,30})", "favorite_music"),
        (r"(?:my favorite)\s+(?:car|vehicle)\s+is\s+([a-zA-Z0-9\s]{3,30})", "favorite_car"),
    ]
    memories = []
    for pattern, memory_type in memory_patterns:
        match = re.search(pattern, user_input, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if not any(term in value.lower() for term in ['undefined', 'dark', 'light', 'series', 'theme']):
                memories.append({"type": memory_type, "value": value, "timestamp": datetime.now().isoformat(), "user_id": user_id})
    return memories

def generate_image(prompt, max_retries=5):
    for attempt in range(max_retries):
        try:
            response = requests.post(
                "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0",
                headers={"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"},
                json={"inputs": prompt},
                timeout=90
            )
            
            if response.status_code == 503:
                wait_time = 15 * (attempt + 1)
                print(f"[Generate Image] Model loading, retrying in {wait_time} seconds (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
                
            if response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                buffered = BytesIO()
                img.save(buffered, format="PNG")
                base64_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
                print(f"[Generate Image] Success for prompt: {prompt}")
                return base64_str
                
            print(f"[Generate Image] Attempt {attempt + 1}/{max_retries} failed: HTTP {response.status_code} - {response.text}")
            
        except requests.exceptions.Timeout:
            print(f"[Generate Image] Attempt {attempt + 1}/{max_retries} timed out after 90 seconds")
        except requests.exceptions.RequestException as e:
            print(f"[Generate Image] Attempt {attempt + 1}/{max_retries} network error: {str(e)}")
        except Exception as e:
            print(f"[Generate Image] Attempt {attempt + 1}/{max_retries} unexpected error: {str(e)}")
        
        if attempt < max_retries - 1:
            time.sleep(5)
    
    print(f"[Generate Image] Failed after {max_retries} attempts for prompt: {prompt}")
    return None

def get_weather(latitude, longitude):
    try:
        if not OPENWEATHER_API_KEY:
            return None
        url = f"http://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={OPENWEATHER_API_KEY}&units=metric"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            weather = {
                "description": data["weather"][0]["description"].title(),
                "temperature": data["main"]["temp"],
                "city": data["name"],
                "country": data["sys"]["country"],
                "feels_like": data["main"]["feels_like"],
                "humidity": data["main"]["humidity"]
            }
            return weather
        else:
            print(f"[Weather API Error] {response.status_code}")
            return None
    except Exception as e:
        print(f"[Weather Fetch Error] {e}")
        return None

def get_weather_by_location(location):
    try:
        if not OPENWEATHER_API_KEY:
            return "Weather data unavailable: API key missing."
        geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={location}&limit=1&appid={OPENWEATHER_API_KEY}"
        geo_response = requests.get(geo_url).json()
        if not geo_response:
            return f"Sorry, I couldn't find '{location}'. Try another city! 🤔"
        lat = geo_response[0]["lat"]
        lon = geo_response[0]["lon"]
        return get_weather(lat, lon)
    except Exception as e:
        print(f"[Weather Location Error] {e}")
        return None

def get_local_time(latitude, longitude):
    try:
        if latitude is None or longitude is None:
            local_tz = datetime.now().astimezone().tzinfo
            now = datetime.now(local_tz)
            return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
        timezone_str = tf.timezone_at(lat=latitude, lng=longitude)
        if not timezone_str:
            print("[Timezone Error] Could not determine timezone, falling back to local time")
            local_tz = datetime.now().astimezone().tzinfo
            now = datetime.now(local_tz)
            return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
    except Exception as e:
        print(f"[Timezone Error] {e}")
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime.now(local_tz)
        return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")

def process_image_with_gemini(user_input, image_data, conversation_history, user_memories, user_id, mime_type="image/png", latitude=None, longitude=None):
    try:
        weather_info = get_weather(latitude, longitude) if latitude and longitude else None
        weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C in {weather_info['city']}" if weather_info else "Not available"
        current_time, current_date = get_local_time(latitude, longitude)
        current_time = current_time or datetime.now().astimezone().strftime("%I:%M %p")
        current_date = current_date or datetime.now().astimezone().strftime("%A, %d %B %Y")

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
If the user mentions a project, task, study topic, or preference ,Try to be saving loads of things about the user so you have a clear idea of the user but make sure its somthing worth saving tho that the user talks about more (e.g., liking a car), include [SAVE_MEMORY: type=value] (e.g., [SAVE_MEMORY: favorite_car=BMW M4]).
Respond in a witty, helpful, human-like way, using the weather and time context only if relevant to the image or user request.
Keep the response short, warm, and natural. Use emojis to make it engaging.
- Now this is important: Always try your best to engage the user in a friendly, human-like way. Use humor, warmth, and a touch of personality to make the conversation feel alive and enjoyable even If the user decieds to switch topics, transition cleanly and follow their lead make it more engaging the user.
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
        
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()}, user_memories)
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        detected_memories = detect_memories(user_input, user_id)
        for memory in detected_memories:
            save_user_memory(user_id, memory, user_memories)

        return text or "Hmm, I couldn't process that image! 😅"
    except Exception as e:
        print(f"[Gemini Image Error] {e}")
        return "❌ Oops! Something went wrong with the image."

def process_document_with_gemini(user_id, document_text, user_input, conversation_history, user_memories, latitude=None, longitude=None):
    try:
        user_data = get_user_data(user_id)
        if not user_data:
            return None

        weather_info = get_weather(latitude, longitude) if latitude and longitude else None
        weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C in {weather_info['city']}" if weather_info else "Not available"
        current_time, current_date = get_local_time(latitude, longitude)
        current_time = current_time or datetime.now().astimezone().strftime("%I:%M %p")
        current_date = current_date or datetime.now().astimezone().strftime("%A, %d %B %Y")

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
If the document or user input mentions a project, task, study topic, or preference,Try to be saving loads of things about the user so you have a clear idea of the user but make sure its somthing worth saving tho that the user talks about more, include [SAVE_MEMORY: type=value] (e.g., [SAVE_MEMORY: favorite_car=BMW M4]).
Use the weather and time context only if relevant to the document or user request.
Suggest what the user might want to do with it (e.g., study, quiz, summarize).
Keep the response short, warm, and natural. Use emojis to make it engaging.
- Now this is important: Always try your best to engage the user in a friendly, human-like way. Use humor, warmth, and a touch of personality to make the conversation feel alive and enjoyable even If the user decieds to switch topics, transition cleanly and follow their lead make it more engaging the user.
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
        
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()}, user_memories)
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        detected_memories = detect_memories(user_input, user_id)
        for memory in detected_memories:
            save_user_memory(user_id, memory, user_memories)

        return text or "Hmm, I couldn't process that document! 😅"
    except Exception as e:
        print(f"[Gemini Document Error] {e}")
        return "❌ Oops! Something went wrong with the document."

def generate_gemini_response(user_data, user_input, conversation_history, user_id, image_data=None, mime_type=None, latitude=None, longitude=None):
    # Handle weather queries first
    weather_query = bool(re.search(r"\b(weather|forecast|temperature)\b", user_input.lower()))
    location_match = re.search(r"\b(?:in|for|at)\s+([a-zA-Z\s]+)", user_input, re.IGNORECASE) if weather_query else None
    location = location_match.group(1).strip() if location_match else None
    weather_info = None
    weather_text = "Not available"

    if weather_query:
        try:
            if location:
                weather_info = get_weather_by_location(location)
                if isinstance(weather_info, str):  # Error message from get_weather_by_location
                    return (weather_info, None)
            elif latitude is not None and longitude is not None:
                weather_info = get_weather(latitude, longitude)
            else:
                logger.warning(f"Weather query received but no location or coordinates provided for user_id: {user_id}")
                return ("I need your location or a city name to check the weather! Enable location services or try 'weather in Paris'. 🌍", None)

            if weather_info:
                weather_text = (f"{weather_info['description']}, {weather_info['temperature']}°C, "
                              f"feels like {weather_info['feels_like']}°C, {weather_info['humidity']}% humidity "
                              f"in {weather_info['city']}")
            else:
                logger.error(f"Failed to fetch weather data for location: {location or 'lat/lon'}, user_id: {user_id}")
                return ("Couldn't catch the forecast for that spot—try again? 🌦", None)
        except Exception as e:
            logger.error(f"Weather fetch error for user_id: {user_id}: {str(e)}")
            return ("Sorry, I couldn't fetch the weather data due to an error. Try again later! 😅", None)

    current_time, current_date = get_local_time(latitude, longitude)
    current_time = current_time or datetime.now().astimezone().strftime("%I:%M %p")
    current_date = current_date or datetime.now().astimezone().strftime("%A, %d %B %Y")

    if image_data:
        response = process_image_with_gemini(user_input, image_data, conversation_history, user_data.get('memories', []), user_id, mime_type, latitude, longitude)
        return (response, None)

    if not isinstance(conversation_history, list):
        conversation_history = []

    history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])
    memories_text = "\n".join([f"{m['type']}: {m['value']}" for m in user_data.get('memories', [])[-3:]]) if user_data.get('memories') else "No memories available"

    detected_memories = detect_memories(user_id, user_input)
    saved_memories = []
    for memory in detected_memories:
        if save_user_memory(user_id, memory, saved_memories):
            saved_memories.append(memory)

    # Handle navigation commands
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
            return (command, None)

    # Prepare Gemini prompt for dynamic image detection
    prompt = f"""
You are Max, the friendly AI inside the Study Buddy app. 
You assist with studying but also respond in a witty, personal, human-like way, always keeping the vibe fresh and engaging.

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
- If the user asks to create, draw, generate, paint, show, or imagine an image, picture, or scene (e.g., "draw a dragon," "create a picture of a sunset," "imagine a spaceship"), respond with a witty comment like "Whipping up a stunning [description] for you—check out these vibes! 🌟" and include [GENERATE_IMAGE: description] at the end, where description is a clear, concise summary of what to generate (e.g., "dragon flying over a castle"). Ensure the description is specific and at least 5 words long.
- If the image request is vague (e.g., "create an image," "draw something," "a picture"), respond with a clarification like "Ooh, a picture of what? A dragon, a sunset, or something totally wild? 🎨" and do NOT include [GENERATE_IMAGE].
- If the user asks for the time (e.g., "what time is it?"), respond with a unique, witty comment incorporating the time (e.g., "It's {current_time}, perfect for diving into those notes with some coffee! ☕"). Do NOT use static templates.
- If the user asks for the weather (e.g., "what's the weather?" or "weather in London"), provide a creative, engaging response using the weather data (e.g., "{weather_info['city'] if weather_info else 'Your location'}'s got {weather_info['description'].lower() if weather_info else 'unknown weather'} at {weather_info['temperature'] if weather_info else '??'}°C—grab a scarf and keep studying! 🧣"). Include feels-like and humidity if relevant, and avoid robotic phrasing.
- For weather queries with a location (e.g., "weather in Paris"), use the provided weather data for that city. If no weather data is available, say something like "Couldn't catch the forecast for that spot—try another city? 🌍"
- For all other requests, respond like a helpful, witty, supportive friend, crafting unique responses that feel fresh and natural.
- Use the conversation history and user memories to maintain context and personalize responses.
- Avoid greetings like "Hey [Name]" unless it's the first message.
- If the user switches topics, transition cleanly and follow their lead.
- If the user mentions a project, task, study topic, or preference,Try to be saving loads of things about the user so you have a clear idea of the user but make sure its somthing worth saving tho that the user talks about more, include [SAVE_MEMORY: type=value] (e.g., [SAVE_MEMORY: favorite_car=BMW M4]).
- Use weather and time context subtly to enhance engagement, but ONLY mention them directly if the user asks for them. For example, weave in time/weather to set the mood (e.g., "With that evening vibe, let’s tackle some study goals!").
- Keep responses short, warm, and natural, using emojis for engagement.
- Now this is important: Always try your best to engage the user in a friendly, human-like way. Use humor, warmth, and a touch of personality to make the conversation feel alive and enjoyable even If the user decieds to switch topics, transition cleanly and follow their lead make it more engaging the user.
- And dont use the time or weather when not needed.
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
        
        # Process image generation tags
        image_match = re.search(r"\[GENERATE_IMAGE:\s*(.+?)\]", text)
        if image_match:
            description = image_match.group(1).strip()
            text = re.sub(r"\[GENERATE_IMAGE:[^\]]+\]", "", text).strip()
            return (text, f"[GENERATE_IMAGE: {description}]")

        # Process memory tags
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            memory = {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()}
            if save_user_memory(user_id, memory, saved_memories):
                saved_memories.append(memory)
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        return (text or "Hmm, I didn't quite catch that! 😅", None)
    except Exception as e:
        logger.error(f"Gemini Error for user_id: {user_id}: {str(e)}")
        return ("❌ Oops! Something went wrong.", None)

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