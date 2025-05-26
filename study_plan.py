import os
import logging
import uuid
from datetime import datetime, timedelta
import calendar
from dotenv import load_dotenv
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore
import json

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('study_plan.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY not found in environment variables")
    raise ValueError("GEMINI_API_KEY is required")

# Firestore client
if not firebase_admin._apps:
    cred = credentials.Certificate('studybuddy.json')
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Helper Functions ---
def get_user_data(user_id):
    user_ref = db.collection('users').document(user_id)
    doc = user_ref.get()
    return doc.to_dict() if doc.exists else None

def save_study_plan(user_id, plan):
    db.collection('users').document(user_id).update({"study_plan": plan})

def get_initial_proficiency(user_data, subject, topic):
    return user_data.get('subjects_mastery', {}).get(subject, {}).get(topic, 0.0)

def generate_calendar_schedule(start_date, end_date, days_per_week, topics, daily_duration_minutes):
    """Generate a calendar-based schedule"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "Invalid date format. Use YYYY-MM-DD"}
    
    if start > end:
        return {"error": "Start date must be before end date"}

    # Convert days_per_week to actual days (e.g., [0,2,4] for Mon,Wed,Fri)
    weekdays = list(range(7))[:days_per_week]  # 0=Monday, 6=Sunday
    
    calendar_data = {}
    current_date = start
    topic_index = 0
    
    while current_date <= end:
        # Only schedule for selected weekdays
        if current_date.weekday() in weekdays:
            date_str = current_date.strftime("%Y-%m-%d")
            
            # Calculate study sessions for the day
            num_sessions = max(1, daily_duration_minutes // 30)  # 30-min sessions
            daily_schedule = []
            
            for _ in range(num_sessions):
                topic = topics[topic_index % len(topics)]
                session = {
                    "topic": topic,
                    "duration": 30,  # minutes
                    "status": "pending",
                    "completed": False,
                    "session_id": str(uuid.uuid4()),
                    "time_slots": [
                        {
                            "start": "09:00",  # Default time slots
                            "end": "09:30"
                        }
                    ]
                }
                daily_schedule.append(session)
                topic_index += 1
            
            calendar_data[date_str] = {
                "date": date_str,
                "weekday": calendar.day_name[current_date.weekday()],
                "sessions": daily_schedule,
                "total_duration": daily_duration_minutes,
                "completed_duration": 0,
                "status": "pending",
                "notes": ""
            }
        
        current_date += timedelta(days=1)
    
    return calendar_data

# --- Core Functions ---
def initialize_study_plan(user_id, goal, start_date, end_date, days_per_week, daily_duration_minutes):
    user_data = get_user_data(user_id)
    if not user_data:
        return {"error": "User not found"}
        
    age = user_data.get('age', 15)
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Get AI-suggested topics
    prompt = f"""
    The user is {age} years old and their study goal is: '{goal}'.
    Suggest a structured study plan with subjects and topics. Return as JSON:
    {{
      "subjects": [
        {{
          "name": "Math",
          "topics": [
            {{
              "name": "Algebra",
              "subtopics": ["Linear Equations", "Quadratic Equations"],
              "estimated_hours": 4,
              "difficulty": "medium"
            }}
          ]
        }}
      ]
    }}
    """
    
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=['TEXT'])
    )
    
    try:
        plan_suggestions = json.loads(response.candidates[0].content.parts[0].text.strip('```json').strip('```').strip())
    except Exception as e:
        logger.error(f"Gemini study plan suggestion error: {e}")
        return {"error": "Failed to generate study plan suggestions"}

    # Flatten topics for scheduling
    topics_list = []
    topics_details = {}
    
    for subject in plan_suggestions.get('subjects', []):
        subject_name = subject['name']
        for topic in subject.get('topics', []):
            topic_name = topic['name']
            full_topic = f"{subject_name}: {topic_name}"
            topics_list.append(full_topic)
            
            topics_details[full_topic] = {
                "subject": subject_name,
                "name": topic_name,
                "subtopics": topic.get('subtopics', []),
                "estimated_hours": topic.get('estimated_hours', 2),
                "difficulty": topic.get('difficulty', 'medium'),
                "status": "pending",
                "proficiency": get_initial_proficiency(user_data, subject_name, topic_name),
                "progress": 0,
                "last_studied": None,
                "next_review": start_date,
                "resources": [],
                "notes": []
            }

    # Generate calendar schedule
    calendar_data = generate_calendar_schedule(
        start_date, 
        end_date, 
        days_per_week, 
        topics_list, 
        daily_duration_minutes
    )
    
    if "error" in calendar_data:
        return calendar_data

    study_plan = {
        "plan_id": str(uuid.uuid4()),
        "user_id": user_id,
        "goal": goal,
        "start_date": start_date,
        "end_date": end_date,
        "days_per_week": days_per_week,
        "daily_duration_minutes": daily_duration_minutes,
        "topics": topics_details,
        "calendar": calendar_data,
        "progress": {
            "completed_sessions": 0,
            "total_sessions": sum(len(day['sessions']) for day in calendar_data.values()),
            "overall_progress": 0,
            "streak": 0,
            "last_studied": None
        },
        "settings": {
            "reminder_enabled": True,
            "reminder_times": ["09:00", "14:00", "18:00"],
            "notification_preferences": {
                "daily_reminder": True,
                "progress_updates": True,
                "achievement_alerts": True
            }
        },
        "created_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat()
    }
    
    save_study_plan(user_id, study_plan)
    return study_plan

def log_daily_study(user_id, date, completed_sessions, time_spent, notes=None):
    """Log daily study progress and update calendar"""
    user_ref = db.collection('users').document(user_id)
    user_data = get_user_data(user_id)
    if not user_data:
        return {"error": "User not found"}
        
    study_plan = user_data.get('study_plan', {})
    calendar_data = study_plan.get('calendar', {})
    
    if date not in calendar_data:
        return {"error": "Date not found in study plan"}
        
    # Update session completion status
    day_data = calendar_data[date]
    completed_count = 0
    
    for session in day_data['sessions']:
        session_id = session['session_id']
        if session_id in completed_sessions:
            session['completed'] = True
            session['status'] = 'completed'
            session['actual_duration'] = completed_sessions[session_id].get('duration', session['duration'])
            session['completion_time'] = datetime.now().isoformat()
            completed_count += 1
            
            # Update topic progress
            topic = session['topic']
            if topic in study_plan['topics']:
                topic_data = study_plan['topics'][topic]
                # Update proficiency and progress
                topic_data['last_studied'] = date
                topic_data['progress'] = min(100, topic_data['progress'] + 10)
                
                # Schedule next review based on spaced repetition
                days_until_review = 1
                if topic_data['proficiency'] > 0.8:
                    days_until_review = 7
                elif topic_data['proficiency'] > 0.6:
                    days_until_review = 3
                
                next_review = (datetime.strptime(date, "%Y-%m-%d") + 
                             timedelta(days=days_until_review)).strftime("%Y-%m-%d")
                topic_data['next_review'] = next_review
    
    # Update daily summary
    day_data['completed_duration'] = time_spent
    day_data['status'] = 'completed' if completed_count == len(day_data['sessions']) else 'partial'
    day_data['notes'] = notes or ""
    day_data['last_updated'] = datetime.now().isoformat()
    
    # Update overall progress
    total_sessions = study_plan['progress']['total_sessions']
    completed_sessions_total = sum(
        len([s for s in d['sessions'] if s.get('completed', False)])
        for d in calendar_data.values()
    )
    
    # Calculate streak
    current_streak = study_plan['progress']['streak']
    yesterday = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if yesterday in calendar_data and calendar_data[yesterday]['status'] == 'completed':
        current_streak += 1
    else:
        current_streak = 1 if day_data['status'] == 'completed' else 0
    
    study_plan['progress'].update({
        'completed_sessions': completed_sessions_total,
        'overall_progress': (completed_sessions_total / total_sessions) * 100 if total_sessions > 0 else 0,
        'streak': current_streak,
        'last_studied': date
    })
    
    # Save updates
    user_ref.update({
        'study_plan': study_plan,
        'learning_history': firestore.ArrayUnion([{
            "date": date,
            "completed_sessions": completed_sessions,
            "time_spent": time_spent,
            "notes": notes or "",
            "timestamp": firestore.SERVER_TIMESTAMP
        }])
    })
    
    return {
        "date": date,
        "sessions_completed": completed_count,
        "total_sessions": len(day_data['sessions']),
        "time_spent": time_spent,
        "status": day_data['status'],
        "streak": current_streak,
        "overall_progress": study_plan['progress']['overall_progress']
    }