import os
import re
import requests
import base64
import logging
import json
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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('max.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def get_user_data(user_id):
    """Fetch user data from Firestore."""
    try:
        ref = db.collection('users').document(user_id)
        doc = ref.get()
        if not doc.exists:
            logger.warning(f"User not found: {user_id}")
            return None
        user_data = doc.to_dict()
        # Ensure ai_conversation_history exists and is a list
        if 'ai_conversation_history' not in user_data:
            user_data['ai_conversation_history'] = []
        elif not isinstance(user_data['ai_conversation_history'], list):
            user_data['ai_conversation_history'] = []
        return user_data
    except Exception as e:
        logger.error(f"Firestore Error fetching user data for user_id: {user_id}: {e}")
        return None

def save_conversation_history(user_id, history):
    """Save AI conversation history to Firestore."""
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({"conversation_history": history})
        logger.info(f"Saved conversation history for user_id: {user_id}")
    except Exception as e:
        logger.error(f"Firestore Update Error for conversation history, user_id: {user_id}: {e}")

def evaluate_memory_worth(user_id, memory_type, memory_value):
    """Evaluate if a memory is worth saving based on relevance and specificity."""
    invalid_terms = ['it', 'them', 'stuff', 'things', 'something', '', 'undefined', 'dark', 'light', 'series', 'theme']
    if memory_value.lower().strip() in invalid_terms or len(memory_value.strip()) < 3:
        return False
    
    user_data = get_user_data(user_id)
    existing_memories = user_data.get('memories', []) if user_data else []
    for mem in existing_memories:
        if mem['type'] == memory_type and mem['value'].lower() == memory_value.lower():
            return False
    
    # Study Buddy-specific memory types
    if memory_type in ['study_topic', 'study_goal', 'likes', 'dislikes'] or memory_type.startswith('favorite_'):
        if len(memory_value) <= 50:
            return True
    
    if memory_type in ['project', 'task']:
        words = memory_value.split()
        if len(words) >= 2 and len(memory_value) <= 50 and not any(term in memory_value.lower() for term in ['theme', 'series']):
            return True
    
    return False

def summarize_memory(memory_value):
    """Summarize memory to make it concise."""
    memory_value = re.sub(r'\s+', ' ', memory_value.strip())
    words = memory_value.split()
    if len(words) > 15:
        return ' '.join(words[:15]) + '...'
    return memory_value

def save_user_memory(user_id, memory, existing_memories=None):
    """Save a user memory to Firestore."""
    try:
        memory_value = summarize_memory(memory['value'])
        if not evaluate_memory_worth(user_id, memory['type'], memory_value):
            logger.info(f"Memory skipped for user_id: {user_id}: {memory} - Not worth saving")
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
        logger.info(f"Memory saved for user_id: {user_id}: {{'type': '{memory['type']}', 'value': '{memory_value}', 'timestamp': '{memory['timestamp']}'}}")
        return True
    except Exception as e:
        logger.error(f"Firestore Memory Error for user_id: {user_id}: {e}")
        return False

def process_user_input(user_id, user_input, user_data):
    """Update user data based on input patterns."""
    updated = user_data.copy() if user_data else {}
    patterns = {
        'study_goal': r"(study goal|learning goal|what i want to learn)(.*)",
        'study_plan': r"(study plan|learning plan)(.*)",
        'subscription_status': r"(subscription status|membership)(.*)",
        'premium_expiry_date': r"(premium expiry|subscription end)(.*)"
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, user_input, re.IGNORECASE)
        if match:
            value = match.group(2).strip()
            if value and value != user_data.get(key, ''):
                updated[key] = value
                logger.info(f"Updated {key.replace('_', ' ')} to '{value}' for user_id: {user_id}")
    return updated

def detect_memories(user_id, user_input):
    """Detect study-related memories from user input."""
    memory_patterns = [
        (r"(?:studying|learning about|reading about|interested in)\s+([a-zA-Z\s\-]{5,50})", "study_topic"),
        (r"(?:my study goal is|want to learn|learning goal is)\s+([a-zA-Z\s\-]{5,50})", "study_goal"),
        (r"(?:working on|building|project is)\s+([a-zA-Z\s\-]{5,50})", "project"),
        (r"(?:need to|have to|planning to)\s+(?:study|work on|complete)\s+([a-zA-Z\s\-]{5,50})", "task"),
        (r"(?:i like|love|enjoy|fan of)\s+([a-zA-Z\s]{3,30})", "likes"),
        (r"(?:i hate|dislike|not a fan of)\s+([a-zA-Z\s]{3,30})", "dislikes"),
        (r"(?:my favorite)\s+(?:subject|topic)\s+is\s+([a-zA-Z\s]{3,30})", "favorite_subject"),
    ]
    memories = []
    for pattern, memory_type in memory_patterns:
        match = re.search(pattern, user_input, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if not any(term in value.lower() for term in ['undefined', 'dark', 'light', 'series', 'theme']):
                memories.append({
                    "type": memory_type,
                    "value": value,
                    "timestamp": datetime.now().isoformat(),
                    "user_id": user_id
                })
    return memories

def generate_image(prompt, max_retries=5):
    """Generate an image using HuggingFace API."""
    # First check if HuggingFace API key is available
    if not HUGGINGFACE_API_KEY:
        logger.error("Image generation not available: HuggingFace API key not configured")
        return "SERVICE_UNAVAILABLE"

    for attempt in range(max_retries):
        try:
            response = requests.post(
                "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0",
                headers={"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"},
                json={"inputs": prompt},
                timeout=90
            )
            
            # Check for quota exceeded
            if response.status_code == 402:
                logger.error(f"Image generation quota exceeded: {response.text}")
                return "QUOTA_EXCEEDED"
            
            # Check for service unavailable
            if response.status_code == 503:
                logger.error("Image generation service temporarily unavailable")
                return "SERVICE_UNAVAILABLE"
                
            # Model still loading
            if response.status_code == 503:
                wait_time = 15 * (attempt + 1)
                logger.info(f"Generate Image: Model loading, retrying in {wait_time} seconds (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
                
            # Success case
            if response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                buffered = BytesIO()
                img.save(buffered, format="PNG")
                base64_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
                logger.info(f"Generated image for prompt: {prompt}")
                return base64_str
                
            logger.error(f"Generate Image: Attempt {attempt + 1}/{max_retries} failed: HTTP {response.status_code} - {response.text}")
            
        except requests.exceptions.Timeout:
            logger.error(f"Generate Image: Attempt {attempt + 1}/{max_retries} timed out after 90 seconds")
        except requests.exceptions.RequestException as e:
            logger.error(f"Generate Image: Attempt {attempt + 1}/{max_retries} network error: {str(e)}")
        except Exception as e:
            logger.error(f"Generate Image: Attempt {attempt + 1}/{max_retries} unexpected error: {str(e)}")
        
        if attempt < max_retries - 1:
            time.sleep(5)
    
    logger.error(f"Generate Image: Failed after {max_retries} attempts for prompt: {prompt}")
    return None

def get_weather(latitude, longitude):
    """Fetch weather data for coordinates."""
    try:
        if not OPENWEATHER_API_KEY:
            logger.warning("Weather API key missing")
            return None
        url = f"http://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={OPENWEATHER_API_KEY}&units=metric"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            return {
                "description": data["weather"][0]["description"].title(),
                "temperature": data["main"]["temp"],
                "city": data["name"],
                "country": data["sys"]["country"],
                "feels_like": data["main"]["feels_like"],
                "humidity": data["main"]["humidity"]
            }
        logger.error(f"Weather API Error: {response.status_code}")
        return None
    except Exception as e:
        logger.error(f"Weather Fetch Error: {e}")
        return None

def get_weather_by_location(location):
    """Fetch weather data for a location name."""
    try:
        if not OPENWEATHER_API_KEY:
            logger.warning("Weather API key missing")
            return "Weather data unavailable: API key missing."
        geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={location}&limit=1&appid={OPENWEATHER_API_KEY}"
        geo_response = requests.get(geo_url).json()
        if not geo_response:
            return f"Sorry, I couldn't find '{location}'. Try another city! 🤔"
        lat = geo_response[0]["lat"]
        lon = geo_response[0]["lon"]
        return get_weather(lat, lon)
    except Exception as e:
        logger.error(f"Weather Location Error: {e}")
        return None

def get_local_time(latitude, longitude):
    """Get local time based on coordinates."""
    try:
        if latitude is None or longitude is None:
            local_tz = datetime.now().astimezone().tzinfo
            now = datetime.now(local_tz)
            return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
        timezone_str = tf.timezone_at(lat=latitude, lng=longitude)
        if not timezone_str:
            logger.warning("Could not determine timezone, falling back to local time")
            local_tz = datetime.now().astimezone().tzinfo
            now = datetime.now(local_tz)
            return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")
    except Exception as e:
        logger.error(f"Timezone Error: {e}")
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime.now(local_tz)
        return now.strftime("%I:%M %p"), now.strftime("%A, %d %B %Y")

def process_image_with_gemini(user_id, user_input, image_data, conversation_history, user_memories, mime_type="image/png", latitude=None, longitude=None):
    """Process an image with Gemini, integrating Study Buddy context."""
    try:
        weather_info = get_weather(latitude, longitude) if latitude and longitude else None
        weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C in {weather_info['city']}" if weather_info else "Not available"
        current_time, current_date = get_local_time(latitude, longitude)
        current_time = current_time or datetime.now().astimezone().strftime("%I:%M %p")
        current_date = current_date or datetime.now().astimezone().strftime("%A, %d %B %Y")

        user_data = get_user_data(user_id)
        subjects_mastery = user_data.get('subjects_mastery', {}) if user_data else {}
        study_topics = user_data.get('study_topics', []) if user_data else []
        learning_history = user_data.get('learning_history', []) if user_data else []

        history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])
        memories_text = "\n".join([f"{m['type']}: {m['value']}" for m in user_memories[-3:]]) if user_memories else "No memories available"
        mastery_text = "\n".join([f"{subject}: {topics}" for subject, topics in subjects_mastery.items()]) if subjects_mastery else "No mastery data"

        prompt = f"""
You are Max, the friendly AI inside the Study Buddy app, helping with studying and more.
User Data:
- Study Topics: {', '.join(study_topics) or 'None'}
- Subjects Mastery: {mastery_text}
- Learning History: {', '.join([h.get('topic', '') for h in learning_history[-3:]]) or 'None'}
- Recent Memories:
{memories_text}

Recent conversation:
{history_text}

User says: "{user_input}"
Current weather: {weather_text}
Current time: {current_time}
Current date: {current_date}

Analyze the provided image and respond based on the user's text.
- Personalize responses using study topics, mastery, and learning history (e.g., suggest quizzes for low-proficiency topics).
- If the image relates to a subject (e.g., math equations), suggest a quiz or flashcards [ACTION: start_quiz=subject/topic] or [ACTION: generate_flashcards=subject/topic].
- Transition cleanly if the user switches topics.
- Include [SAVE_MEMORY: type=value] for study topics, goals, or preferences (e.g., [SAVE_MEMORY: study_topic=Algebra]).
- Respond in a witty, helpful, human-like way, using emojis for engagement.
- Suggest study actions (e.g., quiz, flashcards, exam) based on context.
- Only mention weather/time if relevant to the image or request.

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
        
        text = response.candidates[0].content.parts[0].text
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()}, user_memories)
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        action_match = re.search(r"\[ACTION:\s*(\w+)=(.+?)\]", text)
        action = None
        if action_match:
            action_type, action_value = action_match.groups()
            action = f"[ACTION: {action_type}={action_value}]"
            text = re.sub(r"\[ACTION:[^\]]+\]", "", text).strip()

        detected_memories = detect_memories(user_id, user_input)
        for memory in detected_memories:
            save_user_memory(user_id, memory, user_memories)

        return text or "Hmm, I couldn't process that image! 😅", action
    except Exception as e:
        logger.error(f"Gemini Image Error for user_id: {user_id}: {e}")
        return "❌ Oops! Something went wrong with the image.", None

def process_document_with_gemini(user_id, document_text, user_input, conversation_history, user_memories, latitude=None, longitude=None):
    """Process a document with Gemini, integrating Study Buddy context."""
    try:
        user_data = get_user_data(user_id)
        if not user_data:
            logger.warning(f"User not found: {user_id}")
            return None, None

        weather_info = get_weather(latitude, longitude) if latitude and longitude else None
        weather_text = f"{weather_info['description']}, {weather_info['temperature']}°C in {weather_info['city']}" if weather_info else "Not available"
        current_time, current_date = get_local_time(latitude, longitude)
        current_time = current_time or datetime.now().astimezone().strftime("%I:%M %p")
        current_date = current_date or datetime.now().astimezone().strftime("%A, %d %B %Y")

        subjects_mastery = user_data.get('subjects_mastery', {})
        study_topics = user_data.get('study_topics', [])
        learning_history = user_data.get('learning_history', [])

        history_text = "\n".join([f"User: {msg['user']}\nMax: {msg['max']}" for msg in conversation_history[-5:]])
        memories_text = "\n".join([f"{m['type']}: {m['value']}" for m in user_memories[-3:]]) if user_memories else "No memories available"
        mastery_text = "\n".join([f"{subject}: {topics}" for subject, topics in subjects_mastery.items()]) if subjects_mastery else "No mastery data"

        prompt = f"""
You are Max, the friendly AI inside the Study Buddy app.
User Data:
- Study Topics: {', '.join(study_topics) or 'None'}
- Subjects Mastery: {mastery_text}
- Learning History: {', '.join([h.get('topic', '') for h in learning_history[-3:]]) or 'None'}
- Recent Memories:
{memories_text}

Recent conversation:
{history_text}

User says: "{user_input}"
Current weather: {weather_text}
Current time: {current_time}
Current date: {current_date}

The user uploaded a document:
```
{document_text[:2000]}
```

Summarize the document and respond based on the user's text.
- Personalize using study topics, mastery, and learning history.
- If the document relates to a subject, suggest a quiz or flashcards [ACTION: start_quiz=subject/topic] or [ACTION: generate_flashcards=subject/topic].
- Include [SAVE_MEMORY: type=value] for study topics or goals.
- Suggest study actions (e.g., quiz, flashcards, exam).
- Respond in a witty, helpful, human-like way with emojis.
- Only mention weather/time if relevant.
"""
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=['TEXT'])
        )

        text = response.candidates[0].content.parts[0].text
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            memory_type, memory_value = memory_match.groups()
            save_user_memory(user_id, {"type": memory_type, "value": memory_value, "timestamp": datetime.now().isoformat()}, user_memories)
            text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()

        action_match = re.search(r"\[ACTION:\s*(\w+)=(.+?)\]", text)
        action = None
        if action_match:
            action_type, action_value = action_match.groups()
            action = f"[ACTION: {action_type}={action_value}]"
            text = re.sub(r"\[ACTION:[^\]]+\]", "", text).strip()

        detected_memories = detect_memories(user_id, user_input)
        for memory in detected_memories:
            save_user_memory(user_id, memory, user_memories)

        return text or "Hmm, I couldn't process that document! 😅", action
    except Exception as e:
        logger.error(f"Gemini Document Error for user_id: {user_id}: {e}")
        return "❌ Oops! Something went wrong with the document.", None

def generate_gemini_response(user_data, user_input, conversation_history, user_id, image_data=None, mime_type=None, latitude=None, longitude=None):
    """Generate a Gemini response, integrating Study Buddy context."""
    try:
        if not user_input or not user_id:
            logger.error("Missing required parameters: user_input or user_id")
            return "I need more information to help you. Could you try again? 😊", None
            
        # Check if we're discussing a previous image
        last_image_entry = None
        if not image_data:  # Only look for previous image if no new image is provided
            image_related_keywords = ['image', 'picture', 'photo', 'it', 'that', 'this']
            if any(keyword in user_input.lower() for keyword in image_related_keywords):
                # Look for the most recent image in conversation history
                for entry in reversed(conversation_history):
                    if entry.get('type') == 'image' and entry.get('image_base64'):
                        last_image_entry = entry
                        image_data = entry.get('image_base64')
                        mime_type = 'image/png'  # Default mime type for stored images
                        break

        if not user_data:
            logger.warning(f"No user data found for user_id: {user_id}")
            user_data = {}

        # Handle weather queries
        weather_query = bool(re.search(r"\b(weather|forecast|temperature)\b", user_input.lower()))
        location_match = re.search(r"\b(?:in|for|at)\s+([a-zA-Z\s]+)", user_input, re.IGNORECASE) if weather_query else None
        location = location_match.group(1).strip() if location_match else None
        
        # Get weather information
        weather_info = None
        if weather_query:
            if location:
                weather_info = get_weather_by_location(location)
            elif latitude and longitude:
                weather_info = get_weather(latitude, longitude)
        
        weather_text = (f"{weather_info['description']}, {weather_info['temperature']}°C in {weather_info['city']}"
                       if weather_info and isinstance(weather_info, dict) else "Not available")

        # Get time information
        current_time, current_date = get_local_time(latitude, longitude)
        
        # Get user context with safe defaults
        subjects_mastery = user_data.get('subjects_mastery', {})
        study_topics = user_data.get('study_topics', [])
        learning_history = user_data.get('learning_history', [])
        
        # Format context safely
        try:
            history_text = "\n".join([f"User: {msg.get('user', '')}\nMax: {msg.get('max', '')}" 
                                    for msg in (conversation_history or [])[-5:]])
        except Exception as e:
            logger.error(f"Error formatting conversation history: {e}")
            history_text = ""
            
        try:
            memories_text = "\n".join([f"{m.get('type', 'memory')}: {m.get('value', '')}" 
                                     for m in user_data.get('memories', [])[-3:]])
        except Exception as e:
            logger.error(f"Error formatting memories: {e}")
            memories_text = "No memories available"
            
        try:
            mastery_text = "\n".join([f"{subject}: {topics}" 
                                    for subject, topics in subjects_mastery.items()])
        except Exception as e:
            logger.error(f"Error formatting mastery text: {e}")
            mastery_text = "No mastery data"

        # Build prompt
        # Prepare image context
        image_context = ""
        if last_image_entry:
            prev_user_input = last_image_entry.get('user', '')
            image_context = f"\nWe previously discussed an image where you asked: '{prev_user_input}'"

        prompt = f"""
You are Max, the friendly AI inside the Study Buddy app, helping with studying and more.
User Data:
- Name: {user_data.get('name', 'Unknown')}
- Age: {user_data.get('age', 'not specified')}
- Study Goal: {user_data.get('study_goal', 'not specified')}
- Study Topics: {', '.join(study_topics) or 'None'}
- Subjects Mastery: {mastery_text}
- Learning History: {', '.join([h.get('topic', '') for h in learning_history[-3:]]) or 'None'}
- Subscription Status: {user_data.get('subscription_status', 'not specified')}
- XP: {user_data.get('xp', 0)}
- Badges: {', '.join(user_data.get('badges', [])) or 'None'}
- Recent Memories:
{memories_text}{image_context}

Recent conversation:
{history_text}

User says: "{user_input}"
Current time: {current_time}
Current date: {current_date}
Current weather: {weather_text}

Instructions:
- Personalize responses using study topics, mastery, and learning history
- If the user mentions a subject/topic, suggest a quiz or flashcards [ACTION: start_quiz=subject/topic] or [ACTION: generate_flashcards=subject/topic]
- For low proficiency topics (<0.6), suggest studying or flashcards
- Include [SAVE_MEMORY: type=value] for study topics, goals, or preferences
- If the user asks to generate an image, include [GENERATE_IMAGE: description]
- Respond in a witty, helpful, human-like way with emojis
- Only mention weather/time if relevant to the context
- Dont sound like a robot, be friendly and engaging and not saying Hey then the user name  all the time keep the whole chat clean and always understand it so you know what to say ot just greating the user with the same thign all the time.
"""
        # Generate response with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                contents = [{"parts": [{"text": prompt}]}]
                
                # If we have an image to analyze (new or previous), include it in the request
                if image_data:
                    contents = [{
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"data": image_data, "mime_type": mime_type or "image/png"}}
                        ]
                    }]
                
                response = client.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_modalities=['TEXT'],
                        temperature=0.7,
                        candidate_count=1,
                        max_output_tokens=1000
                    )
                )
                
                text = response.candidates[0].content.parts[0].text
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to generate response after {max_retries} attempts: {e}")
                    return "I'm having trouble thinking right now. Could you try again in a moment? 😅", None
                time.sleep(2 ** attempt)  # Exponential backoff

        # Handle memory saving
        memory_match = re.search(r"\[SAVE_MEMORY:\s*(\w+)=(.+?)\]", text)
        if memory_match:
            try:
                memory_type, memory_value = memory_match.groups()
                memory = {
                    "type": memory_type,
                    "value": memory_value,
                    "timestamp": datetime.now().isoformat()
                }
                save_user_memory(user_id, memory)
                text = re.sub(r"\[SAVE_MEMORY:[^\]]+\]", "", text).strip()
            except Exception as e:
                logger.error(f"Error saving memory: {e}")

        # Handle actions
        action_match = re.search(r"\[ACTION:\s*(\w+)=(.+?)\]", text)
        action = None
        if action_match:
            try:
                action_type, action_value = action_match.groups()
                action = {"type": action_type, "value": action_value}
                text = re.sub(r"\[ACTION:[^\]]+\]", "", text).strip()
            except Exception as e:
                logger.error(f"Error processing action: {e}")

        # Handle image generation
        image_match = re.search(r"\[GENERATE_IMAGE:\s*(.+?)\]", text)
        if image_match:
            try:
                image_prompt = image_match.group(1)
                generated_image = generate_image(image_prompt)
                if generated_image:
                    action = {"type": "show_image", "value": generated_image}
                text = re.sub(r"\[GENERATE_IMAGE:[^\]]+\]", "", text).strip()
            except Exception as e:
                logger.error(f"Error generating image: {e}")

        # Save detected memories
        try:
            detected_memories = detect_memories(user_id, user_input)
            for memory in detected_memories:
                save_user_memory(user_id, memory)
        except Exception as e:
            logger.error(f"Error processing memories: {e}")

        return text.strip() or "I'm not sure how to respond to that. Could you try rephrasing? 😊", action

    except Exception as e:
        logger.error(f"Gemini Response Error for user_id: {user_id}: {e}")
        return "I encountered an error. Could you please try again? 😅", None

def process_pdf(file_path):
    """Process a PDF file."""
    try:
        with open(file_path, 'rb') as file:
            reader = PdfReader(file)
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
        return text.strip()
    except Exception as e:
        logger.error(f"PDF Processing Error: {e}")
        return None

def process_docx(file_path):
    """Process a DOCX file."""
    try:
        doc = Document(file_path)
        return "\n".join([paragraph.text for paragraph in doc.paragraphs])
    except Exception as e:
        logger.error(f"DOCX Processing Error: {e}")
        return None

def process_text_file(file_path):
    """Process a text file."""
    try:
        # First try UTF-8
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                return file.read()
        except UnicodeDecodeError:
            # If UTF-8 fails, detect encoding
            with open(file_path, 'rb') as file:
                raw_data = file.read()
            detected = chardet.detect(raw_data)
            encoding = detected['encoding']
            
            # Try reading with detected encoding
            with open(file_path, 'r', encoding=encoding) as file:
                return file.read()
    except Exception as e:
        logger.error(f"Text File Processing Error: {e}")
        return None

def process_document(file_path):
    """Process different types of document files."""
    try:
        file_extension = os.path.splitext(file_path)[1].lower()
        
        if file_extension == '.pdf':
            return process_pdf(file_path)
        elif file_extension == '.docx':
            return process_docx(file_path)
        elif file_extension in ['.txt', '.md', '.json', '.py', '.js']:
            return process_text_file(file_path)
        else:
            logger.error(f"Unsupported file type: {file_extension}")
            return None
    except Exception as e:
        logger.error(f"Document Processing Error: {e}")
        return None