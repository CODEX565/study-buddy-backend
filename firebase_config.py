import firebase_admin
from firebase_admin import credentials, firestore
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FirestoreClient:
    def __init__(self):
        try:
            # Use environment variable for credential path, fallback to default
            cred_path = os.getenv("FIREBASE_CREDENTIALS", "studybuddy.json")
            if not os.path.exists(cred_path):
                raise FileNotFoundError(f"Firebase credential file not found at {cred_path}")

            # Initialize Firebase app if not already initialized
            if not firebase_admin._apps:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                logger.info("Firebase app initialized successfully")
            self.db = firestore.client()
            logger.info("Firestore client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Firestore client: {str(e)}")
            raise

# Shared Firestore instance (optional, for modules not using FirestoreClient)
try:
    db = FirestoreClient().db
except Exception as e:
    logger.error(f"Failed to create shared Firestore instance: {str(e)}")
    db = None