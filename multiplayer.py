from flask import Blueprint, request, jsonify
from flask_socketio import emit, join_room, leave_room
import uuid
import logging
from threading import Lock
from datetime import datetime
from Quiz import generate_quiz_question

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('multiplayer.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

games = {}
game_lock = Lock()

def multiplayer_bp(socketio):
    bp = Blueprint('multiplayer', __name__, url_prefix='/api')

    def create_game(user_id, topic, year_group, max_questions):
        game_code = str(uuid.uuid4())[:6].upper()
        with game_lock:
            games[game_code] = {
                'host': user_id,
                'topic': topic,
                'year_group': year_group,
                'max_questions': max_questions,
                'players': {user_id: {'username': f'Player_{user_id[:4]}', 'score': 0}},
                'state': 'lobby',
                'current_question': None,
                'question_id': None,
                'correct_answer': None,
                'answers': [],
                'countdown': 0,
                'question_number': 0,
                'answer_counts': {},
                'all_answered': False
            }
        logger.info(f"Game created: {game_code}, Topic: {topic}, Year Group: {year_group}, Max Questions: {max_questions}, Host: {user_id}")
        return game_code

    def join_game(user_id, game_code, username):
        with game_lock:
            if game_code not in games:
                raise Exception("Game not found")
            if user_id in games[game_code]['players']:
                raise Exception("User already in game")
            games[game_code]['players'][user_id] = {'username': username, 'score': 0}
            logger.info(f"User {user_id} ({username}) joined game: {game_code}")
        return games[game_code]

    def start_game(user_id, game_code):
        with game_lock:
            if game_code not in games:
                raise Exception("Game not found")
            if games[game_code]['host'] != user_id:
                raise Exception("Only the host can start the game")
            if games[game_code]['state'] != 'lobby':
                raise Exception("Game already started")
            games[game_code]['state'] = 'starting'
            games[game_code]['countdown'] = 5
            logger.info(f"Game {game_code} starting with countdown: {games[game_code]['countdown']}")
        return games[game_code]['countdown']

    def generate_new_question(game_code):
        with game_lock:
            if game_code not in games:
                raise Exception("Game not found")
            game = games[game_code]
            if game['question_number'] >= game['max_questions']:
                game['state'] = 'ended'
                logger.info(f"Game {game_code} ended after {game['max_questions']} questions")
                return None
            question_data = generate_quiz_question(
                db=None,  # Handled in Quiz.py
                user_id=game['host'],
                topic=game['topic'],
                year_group=game['year_group'],
                multiplayer=True
            )
            game['current_question'] = question_data['question']
            game['question_id'] = question_data['question_id']
            game['correct_answer'] = question_data['correct_answer']
            game['answers'] = question_data['answers']
            game['question_number'] += 1
            game['state'] = 'playing'
            game['answer_counts'] = {}
            game['all_answered'] = False
            logger.info(f"New question generated for game {game_code}: ID={game['question_id']}")
            return {
                'question': game['current_question'],
                'answers': game['answers'],
                'question_id': game['question_id'],
                'correct_answer': game['correct_answer'],
                'question_number': game['question_number']
            }

    @bp.route('/create_game', methods=['POST'])
    def api_create_game():
        try:
            data = request.get_json()
            user_id = data.get('user_id')
            topic = data.get('topic', 'General')
            year_group = data.get('year_group', 'General')
            max_questions = data.get('max_questions', 10)
            if not user_id:
                return jsonify({'error': 'User ID is required'}), 400
            if max_questions < 1 or max_questions > 20:
                return jsonify({'error': 'Max questions must be between 1 and 20'}), 400
            game_code = create_game(user_id, topic, year_group, max_questions)
            return jsonify({
                'game_code': game_code,
                'players': games[game_code]['players'],
                'topic': topic,
                'year_group': year_group,
                'max_questions': max_questions
            })
        except Exception as e:
            logger.error(f"Error creating game: {str(e)}")
            return jsonify({'error': str(e)}), 400

    @bp.route('/join_game', methods=['POST'])
    def api_join_game():
        try:
            data = request.get_json()
            user_id = data.get('user_id')
            game_code = data.get('game_code')
            username = data.get('username', f'Player_{user_id[:4]}')
            if not user_id or not game_code:
                return jsonify({'error': 'User ID and game code are required'}), 400
            game = join_game(user_id, game_code, username)
            socketio.emit('player_joined', {
                'user_id': user_id,
                'username': username,
                'players': game['players']
            }, room=game_code, namespace='/multiplayer')
            return jsonify({
                'game_code': game_code,
                'players': game['players'],
                'topic': game['topic'],
                'year_group': game['year_group'],
                'max_questions': game['max_questions']
            })
        except Exception as e:
            logger.error(f"Error joining game: {str(e)}")
            return jsonify({'error': str(e)}), 400

    @bp.route('/start_game', methods=['POST'])
    def api_start_game():
        try:
            data = request.get_json()
            user_id = data.get('user_id')
            game_code = data.get('game_code')
            if not user_id or not game_code:
                return jsonify({'error': 'User ID and game code are required'}), 400
            countdown = start_game(user_id, game_code)
            socketio.emit('game_starting', {'countdown': countdown}, room=game_code, namespace='/multiplayer')
            return jsonify({'countdown': countdown})
        except Exception as e:
            logger.error(f"Error starting game: {str(e)}")
            return jsonify({'error': str(e)}), 400

    @bp.route('/submit_answer', methods=['POST'])
    def api_submit_answer():
        try:
            data = request.get_json()
            user_id = data.get('user_id')
            game_code = data.get('game_code')
            question_id = data.get('question_id')
            answer = data.get('answer')
            response_time = data.get('response_time')
            if not all([user_id, game_code, question_id, answer, response_time]):
                return jsonify({'error': 'Missing required fields'}), 400
            with game_lock:
                if game_code not in games:
                    return jsonify({'error': 'Game not found'}), 400
                game = games[game_code]
                if game['question_id'] != question_id:
                    logger.error(f"Question not found: Submitted ID={question_id}, Current ID={game['question_id']}")
                    return jsonify({'error': 'Question not found'}), 400
                is_correct = answer == game['correct_answer']
                max_time = 15
                score = 100 if is_correct else 0
                if is_correct and response_time < max_time:
                    score += int((1 - response_time / max_time) * 50)
                game['players'][user_id]['score'] += score
                game['answer_counts'][user_id] = {
                    'answer': answer,
                    'is_correct': is_correct,
                    'score': score,
                    'response_time': response_time
                }
                all_answered = len(game['answer_counts']) == len(game['players'])
                game['all_answered'] = all_answered
                logger.info(f"Answer submitted for game {game_code}: User={user_id}, Correct={is_correct}, Score={score}, AllAnswered={all_answered}")
                socketio.emit('answer_submitted', {
                    'user_id': user_id,
                    'username': game['players'][user_id]['username'],
                    'is_correct': is_correct,
                    'score': game['players'][user_id]['score'],
                    'all_answered': all_answered
                }, room=game_code, namespace='/multiplayer')
                if all_answered:
                    new_question = generate_new_question(game_code)
                    if new_question:
                        socketio.emit('new_question', new_question, room=game_code, namespace='/multiplayer')
                    else:
                        leaderboard = [
                            {'username': player['username'], 'score': player['score']}
                            for player in game['players'].values()
                        ]
                        leaderboard.sort(key=lambda x: x['score'], reverse=True)
                        socketio.emit('game_ended', {'leaderboard': leaderboard}, room=game_code, namespace='/multiplayer')
                return jsonify({
                    'is_correct': is_correct,
                    'score': game['players'][user_id]['score']
                })
        except Exception as e:
            logger.error(f"Error submitting answer: {str(e)}")
            return jsonify({'error': str(e)}), 400

    @bp.route('/game_state', methods=['GET'])
    def api_game_state():
        try:
            game_code = request.args.get('game_code')
            user_id = request.args.get('user_id')
            if not game_code or not user_id:
                return jsonify({'error': 'Game code and user ID are required'}), 400
            with game_lock:
                if game_code not in games:
                    return jsonify({'error': 'Game not found'}), 400
                game = games[game_code]
                if game['state'] == 'ended':
                    leaderboard = [
                        {'username': player['username'], 'score': player['score']}
                        for player in game['players'].values()
                    ]
                    leaderboard.sort(key=lambda x: x['score'], reverse=True)
                    return jsonify({
                        'event': 'game_ended',
                        'leaderboard': leaderboard
                    })
                if game['state'] == 'playing':
                    return jsonify({
                        'event': 'new_question',
                        'question': game['current_question'],
                        'answers': game['answers'],
                        'question_id': game['question_id'],
                        'correct_answer': game['correct_answer'],
                        'question_number': game['question_number']
                    })
                return jsonify({
                    'event': 'game_state',
                    'state': game['state'],
                    'players': game['players'],
                    'countdown': game['countdown'],
                    'topic': game['topic'],
                    'year_group': game['year_group'],
                    'max_questions': game['max_questions']
                })
        except Exception as e:
            logger.error(f"Error fetching game state: {str(e)}")
            return jsonify({'error': str(e)}), 400

    @bp.route('/leave_game', methods=['POST'])
    def api_leave_game():
        try:
            data = request.get_json()
            user_id = data.get('user_id')
            game_code = data.get('game_code')
            if not user_id or not game_code:
                return jsonify({'error': 'User ID and game code are required'}), 400
            with game_lock:
                if game_code not in games:
                    return jsonify({'error': 'Game not found'}), 400
                game = games[game_code]
                if user_id in game['players']:
                    del game['players'][user_id]
                    logger.info(f"User {user_id} left game: {game_code}")
                    socketio.emit('player_left', {
                        'user_id': user_id,
                        'players': game['players']
                    }, room=game_code, namespace='/multiplayer')
                    if not game['players']:
                        del games[game_code]
                        logger.info(f"Game {game_code} deleted as no players remain")
                    elif game['host'] == user_id:
                        new_host = next(iter(game['players']), None)
                        if new_host:
                            game['host'] = new_host
                            socketio.emit('host_changed', {
                                'new_host': new_host,
                                'username': game['players'][new_host]['username']
                            }, room=game_code, namespace='/multiplayer')
            return jsonify({'message': 'Left game successfully'})
        except Exception as e:
            logger.error(f"Error leaving game: {str(e)}")
            return jsonify({'error': str(e)}), 400

    return bp