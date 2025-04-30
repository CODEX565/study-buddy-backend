# firebase_config.py
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase only once globally
if not firebase_admin._apps:
    cred = credentials.Certificate("studybuddy.json")
    firebase_admin.initialize_app(cred)

# Shared Firestore instance
db = firestore.client()
