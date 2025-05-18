import logging
import uuid
from datetime import datetime
from firebase_admin import firestore

# Configure logging to match main.py and Quiz.py
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('exam.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Pass threshold for exams (consistent with Quiz.py)
PASS_THRESHOLD = 0.7  # 70% correct to pass exam

def save_to_exam_mode(db, user_id, quiz_id, quiz_data):
    """
    Save a passed quiz to exam mode for a full exam-like experience.
    Args:
        db: Firestore client instance
        user_id: ID of the user
        quiz_id: ID of the quiz to save
        quiz_data: Dictionary containing quiz details
    Returns:
        exam_id: ID of the created exam, or None if failed
    """
    try:
        exam_id = str(uuid.uuid4())
        exam_data = {
            "exam_id": exam_id,
            "user_id": user_id,
            "quiz_id": quiz_id,
            "topic": quiz_data['topic'],
            "year_group": quiz_data['year_group'],
            "group": quiz_data.get('group'),
            "questions": quiz_data['questions'],
            "status": "pending",
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection('exams').document(exam_id).set(exam_data)
        logger.debug(f"Saved exam for user_id: {user_id}, exam_id: {exam_id}")
        return exam_id
    except Exception as e:
        logger.exception(f"Error saving to exam mode for user_id: {user_id}: {e}")
        return None

def generate_exam(db, user_id, exam_id):
    """
    Retrieve exam data for the user to take the exam.
    Args:
        db: Firestore client instance
        user_id: ID of the user
        exam_id: ID of the exam to retrieve
    Returns:
        Dictionary with exam data, or error message
    """
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}
        exam_data = exam_doc.to_dict()
        logger.debug(f"Generated exam for user_id: {user_id}, exam_id: {exam_id}")
        return exam_data
    except Exception as e:
        logger.exception(f"Error generating exam for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}

def evaluate_exam(db, user_id, exam_id):
    """
    Evaluate exam results and determine next steps based on pass/fail.
    Args:
        db: Firestore client instance
        user_id: ID of the user
        exam_id: ID of the exam to evaluate
    Returns:
        Dictionary with result (pass/fail), score, next_step, and results
    """
    try:
        exam_ref = db.collection('exams').document(exam_id)
        exam_doc = exam_ref.get()
        if not exam_doc.exists:
            logger.warning(f"Exam not found: {exam_id}")
            return {"error": "Exam not found"}

        exam_data = exam_doc.to_dict()
        questions = exam_data.get('questions', [])
        quiz_responses = db.collection('users').document(user_id).get().to_dict().get('quiz_responses', {})

        correct_count = 0
        results = []
        for question in questions:
            question_id = question['question_id']
            response = quiz_responses.get(question_id, {})
            is_correct = response.get('is_correct', False)
            if is_correct:
                correct_count += 1
            results.append({
                "question_id": question_id,
                "question": question['question'],
                "user_answer": response.get('user_answer'),
                "correct_answer": question['correct_answer'],
                "is_correct": is_correct,
                "explanation": question.get('explanation', 'No explanation available.')
            })

        score = correct_count / len(questions) if questions else 0
        passed = score >= PASS_THRESHOLD

        exam_data['status'] = 'completed'
        exam_data['score'] = score
        exam_data['results'] = results
        exam_ref.update(exam_data)

        if passed:
            logger.info(f"Exam passed for user_id: {user_id}, exam_id: {exam_id}")
            return {
                "result": "pass",
                "score": score,
                "next_step": "success",
                "results": results
            }
        else:
            from Quiz import generate_summary  # Import here to avoid circular dependency
            summary = generate_summary(db, user_id, exam_id, is_exam=True)
            logger.info(f"Exam failed for user_id: {user_id}, exam_id: {exam_id}, proceeding to summary")
            return {
                "result": "fail",
                "score": score,
                "next_step": "summary",
                "summary": summary,
                "results": results
            }

    except Exception as e:
        logger.exception(f"Error evaluating exam for user_id: {user_id}, exam_id: {exam_id}: {e}")
        return {"error": str(e)}