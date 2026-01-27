from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import requests
import json
import os
import random
import uuid
import string
from datetime import datetime, date, timedelta
from pymongo import MongoClient
import like_pb2
import like_count_pb2
import uid_generator_pb2
from google.protobuf.message import DecodeError

app = Flask(__name__)

MONGODB_URI = os.environ.get('MONGODB_URI')
mongo_client = None
db = None

def get_mongo_db():
    global mongo_client, db
    if mongo_client is None:
        mongo_client = MongoClient(MONGODB_URI)
        db = mongo_client.get_database('test')
    return db

def generate_api_key():
    """Generate a unique API key"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

def create_api_key(quota=100):
    """Create a new API key with quota"""
    try:
        database = get_mongo_db()
        collection = database['api_keys']
        
        api_key = generate_api_key()
        key_data = {
            'api_key': api_key,
            'quota': quota,
            'used': 0,
            'created_at': datetime.now(),
            'last_reset_date': date.today().isoformat(),
            'active': True
        }
        
        collection.insert_one(key_data)
        app.logger.info(f"Created API key with quota {quota}")
        return api_key
    except Exception as e:
        app.logger.error(f"Error creating API key: {e}")
        return None

def validate_api_key(api_key):
    """Validate API key and return key data"""
    try:
        database = get_mongo_db()
        collection = database['api_keys']
        
        key_data = collection.find_one({'api_key': api_key, 'active': True})
        if not key_data:
            return None
        
        # Check if it's a new day and reset quota if needed
        last_reset = key_data.get('last_reset_date')
        today = date.today().isoformat()
        
        if last_reset != today:
            # Reset quota for new day
            collection.update_one(
                {'api_key': api_key},
                {
                    '$set': {
                        'used': 0,
                        'last_reset_date': today
                    }
                }
            )
            app.logger.info(f"Daily quota reset for API key {api_key}")
            key_data['used'] = 0
            key_data['last_reset_date'] = today
        
        return key_data
    except Exception as e:
        app.logger.error(f"Error validating API key: {e}")
        return None

def check_and_increment_quota(api_key):
    """Check if quota is available and increment used count"""
    try:
        database = get_mongo_db()
        collection = database['api_keys']
        
        key_data = collection.find_one({'api_key': api_key, 'active': True})
        if not key_data:
            return False, "Invalid API key"
        
        # Check if it's a new day and reset quota if needed
        last_reset = key_data.get('last_reset_date')
        today = date.today().isoformat()
        
        if last_reset != today:
            # Reset quota for new day
            collection.update_one(
                {'api_key': api_key},
                {
                    '$set': {
                        'used': 0,
                        'last_reset_date': today
                    }
                }
            )
            app.logger.info(f"Daily quota reset for API key {api_key}")
            key_data['used'] = 0
        
        if key_data['used'] >= key_data['quota']:
            return False, f"Quota exceeded. Used: {key_data['used']}/{key_data['quota']}"
        
        collection.update_one(
            {'api_key': api_key},
            {'$inc': {'used': 1}}
        )
        return True, f"Requests remaining: {key_data['quota'] - key_data['used'] - 1}/{key_data['quota']}"
    except Exception as e:
        app.logger.error(f"Error checking quota: {e}")
        return False, str(e)

def load_tokens(server_name):
    try:
        database = get_mongo_db()
        if server_name == "IND":
            collection = database['region_IND']
        elif server_name in {"BR", "US", "SAC", "NA"}:
            collection = database['region_BR']
        elif server_name == "ME":
            collection = database['region_ME']
        else:
            collection = database['region_ME']
        
        docs = list(collection.find({}, {'_id': 0, 'jwt_token': 1}))
        if not docs:
            app.logger.error(f"No tokens found for server {server_name}")
            return None
        tokens = [{'token': doc['jwt_token']} for doc in docs if doc.get('jwt_token')]
        if not tokens:
            app.logger.error(f"No valid jwt_tokens found for server {server_name}")
            return None
        return tokens
    except Exception as e:
        app.logger.error(f"Error loading tokens from MongoDB for server {server_name}: {e}")
        return None

def encrypt_message(plaintext):
    try:
        key = b'Yg&tc%DEuh6%Zc^8'
        iv = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded_message = pad(plaintext, AES.block_size)
        encrypted_message = cipher.encrypt(padded_message)
        return binascii.hexlify(encrypted_message).decode('utf-8')
    except Exception as e:
        app.logger.error(f"Error encrypting message: {e}")
        return None

def create_protobuf_message(user_id, region):
    try:
        message = like_pb2.like()
        message.uid = int(user_id)
        message.region = region
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating protobuf message: {e}")
        return None

async def send_request(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 9 Pro Build/AD1A.240505.001)",
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/octet-stream",
            'Expect': "100-continue",
            'X-Unity-Version': "2022.3.10f1",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB52"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers) as response:
                if response.status != 200:
                    app.logger.error(f"Request failed with status code: {response.status}")
                    return response.status
                return await response.text()
    except Exception as e:
        app.logger.error(f"Exception in send_request: {e}")
        return None

async def send_multiple_requests(uid, server_name, url):
    try:
        region = server_name
        protobuf_message = create_protobuf_message(uid, region)
        if protobuf_message is None:
            app.logger.error("Failed to create protobuf message.")
            return None, 0
        encrypted_uid = encrypt_message(protobuf_message)
        if encrypted_uid is None:
            app.logger.error("Encryption failed.")
            return None, 0
        tasks = []
        tokens = load_tokens(server_name)
        if tokens is None:
            app.logger.error("Failed to load tokens.")
            return None, 0
        tokens_used = len(tokens)
        for token_data in tokens:
            token = token_data["token"]
            tasks.append(send_request(encrypted_uid, token, url))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results, tokens_used
    except Exception as e:
        app.logger.error(f"Exception in send_multiple_requests: {e}")
        return None, 0

def create_protobuf(uid):
    try:
        message = uid_generator_pb2.uid_generator()
        message.saturn_ = int(uid)
        message.garena = 1
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating uid protobuf: {e}")
        return None

def enc(uid):
    protobuf_data = create_protobuf(uid)
    if protobuf_data is None:
        return None
    encrypted_uid = encrypt_message(protobuf_data)
    return encrypted_uid

def make_request(encrypt, server_name, token):
    try:
        if server_name == "IND":
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
        else:
            url = "https://clientbp.ggblueshark.com/GetPlayerPersonalShow"
        edata = bytes.fromhex(encrypt)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 9 Pro Build/AD1A.240505.001)",
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/octet-stream",
            'Expect': "100-continue",
            'X-Unity-Version': "2022.3.10f1",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB52"
        }
        response = requests.post(url, data=edata, headers=headers, verify=False)
        hex_data = response.content.hex()
        binary = bytes.fromhex(hex_data)
        decode = decode_protobuf(binary)
        if decode is None:
            app.logger.error("Protobuf decoding returned None.")
        return decode
    except Exception as e:
        app.logger.error(f"Error in make_request: {e}")
        return None

def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except DecodeError as e:
        app.logger.error(f"Error decoding Protobuf data: {e}")
        return None
    except Exception as e:
        app.logger.error(f"Unexpected error during protobuf decoding: {e}")
        return None

async def make_request_async(encrypt, server_name, token):
    try:
        if server_name == "IND":
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
        else:
            url = "https://clientbp.ggblueshark.com/GetPlayerPersonalShow"
        edata = bytes.fromhex(encrypt)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 9 Pro Build/AD1A.240505.001)",
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/octet-stream",
            'Expect': "100-continue",
            'X-Unity-Version': "2022.3.10f1",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB52"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers) as response:
                content = await response.read()
                hex_data = content.hex()
                binary = bytes.fromhex(hex_data)
                decode = decode_protobuf(binary)
                if decode is None:
                    app.logger.error("Protobuf decoding returned None.")
                return decode
    except Exception as e:
        app.logger.error(f"Error in make_request_async: {e}")
        return None

@app.route('/create-api-key', methods=['POST', 'GET'])
def create_key():
    """Create a new API key with specified quota"""
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
            quota = data.get('quota', 100)
        else:  # GET method
            quota = request.args.get('quota', 100)
        
        try:
            quota = int(quota)
        except (ValueError, TypeError):
            return jsonify({"error": "Quota must be a valid integer"}), 400
        
        if quota < 1:
            return jsonify({"error": "Quota must be a positive integer"}), 400
        
        api_key = create_api_key(quota)
        if not api_key:
            return jsonify({"error": "Failed to create API key"}), 500
        
        return jsonify({
            "api_key": api_key,
            "quota": quota,
            "message": "API key created successfully"
        }), 201
    except Exception as e:
        app.logger.error(f"Error in create_key: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api-key-status', methods=['GET'])
def api_key_status():
    """Check API key status and remaining quota"""
    try:
        api_key = request.args.get('api_key')
        if not api_key:
            return jsonify({"error": "api_key parameter required"}), 400
        
        key_data = validate_api_key(api_key)
        if not key_data:
            return jsonify({"error": "Invalid API key"}), 401
        
        return jsonify({
            "quota": key_data['quota'],
            "used": key_data['used'],
            "remaining": key_data['quota'] - key_data['used'],
            "active": key_data['active']
        }), 200
    except Exception as e:
        app.logger.error(f"Error in api_key_status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/like', methods=['GET'])
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()
    api_key = request.args.get("api_key")
    
    if not uid or not server_name or not api_key:
        return jsonify({"error": "uid, server_name, and api_key are required"}), 400

    # Validate API key
    key_data = validate_api_key(api_key)
    if not key_data:
        return jsonify({"error": "Invalid or inactive API key"}), 401
    
    # Check if quota is available
    quota_available = key_data['used'] < key_data['quota']
    
    if quota_available:
        # Quota available - process real request with all tokens
        quota_ok, quota_msg = check_and_increment_quota(api_key)
        if not quota_ok:
            return jsonify({"error": quota_msg}), 429

        try:
            async def process_real_request_async():
                tokens = load_tokens(server_name)
                if tokens is None:
                    raise Exception("Failed to load tokens.")
                
                # Randomly select one token for initial check
                token = random.choice(tokens)['token']
                
                encrypted_uid = enc(uid)
                if encrypted_uid is None:
                    raise Exception("Encryption of UID failed.")

                # Initial and like requests can be somewhat parallelized but initial check is usually needed for 'before' count
                before = await make_request_async(encrypted_uid, server_name, token)
                if before is None:
                    raise Exception("Failed to retrieve initial player info.")
                
                try:
                    jsone = MessageToJson(before)
                except Exception as e:
                    raise Exception(f"Error converting 'before' protobuf to JSON: {e}")
                data_before = json.loads(jsone)
                before_like = int(data_before.get('AccountInfo', {}).get('Likes', 0))
                app.logger.info(f"Likes before command: {before_like}")

                if server_name == "IND":
                    url = "https://client.ind.freefiremobile.com/LikeProfile"
                elif server_name in {"BR", "US", "SAC", "NA"}:
                    url = "https://client.us.freefiremobile.com/LikeProfile"
                else:
                    url = "https://clientbp.ggblueshark.com/LikeProfile"

                # Send multiple requests in parallel
                results_task = send_multiple_requests(uid, server_name, url)
                
                # Wait for likes to be sent
                _, tokens_used = await results_task

                # Get final count
                after = await make_request_async(encrypted_uid, server_name, token)
                if after is None:
                    raise Exception("Failed to retrieve player info after like requests.")
                
                try:
                    jsone_after = MessageToJson(after)
                except Exception as e:
                    raise Exception(f"Error converting 'after' protobuf to JSON: {e}")
                data_after = json.loads(jsone_after)
                
                after_like = int(data_after.get('AccountInfo', {}).get('Likes', 0))
                player_uid = int(data_after.get('AccountInfo', {}).get('UID', 0))
                player_name = str(data_after.get('AccountInfo', {}).get('PlayerNickname', ''))
                
                like_given = after_like - before_like
                status = 1 if like_given != 0 else 2
                
                return {
                    "API": "Mohit Like API",
                    "LikesGivenByAPI": like_given,
                    "LikesafterCommand": after_like,
                    "LikesbeforeCommand": before_like,
                    "PlayerNickname": player_name,
                    "UID": player_uid,
                    "TokensUsed": tokens_used,
                    "status": status
                }

            # Use asyncio to run the async process
            result = asyncio.run(process_real_request_async())
            response = app.response_class(
                response=json.dumps(result, ensure_ascii=False),
                status=200,
                mimetype='application/json'
            )
            return response
        except Exception as e:
            app.logger.error(f"Error processing request: {e}")
            return jsonify({"error": str(e)}), 500
    else:
        # Quota exceeded - return informative message with 2 likes using limited tokens
        try:
            async def process_limited_request_async():
                tokens = load_tokens(server_name)
                if tokens is None or len(tokens) == 0:
                    raise Exception("Failed to load tokens.")
                
                # Use only 2 tokens
                limited_tokens = tokens[:2]
                
                # Randomly select one token for initial check
                token = random.choice(limited_tokens)['token']
                
                encrypted_uid = enc(uid)
                if encrypted_uid is None:
                    raise Exception("Encryption of UID failed.")

                before = await make_request_async(encrypted_uid, server_name, token)
                if before is None:
                    raise Exception("Failed to retrieve initial player info.")
                
                try:
                    jsone = MessageToJson(before)
                except Exception as e:
                    raise Exception(f"Error converting 'before' protobuf to JSON: {e}")
                data_before = json.loads(jsone)
                before_like = int(data_before.get('AccountInfo', {}).get('Likes', 0))

                if server_name == "IND":
                    url = "https://client.ind.freefiremobile.com/LikeProfile"
                elif server_name in {"BR", "US", "SAC", "NA"}:
                    url = "https://client.us.freefiremobile.com/LikeProfile"
                else:
                    url = "https://clientbp.ggblueshark.com/LikeProfile"

                # Send requests using limited tokens
                tasks = []
                for token_data in limited_tokens:
                    tasks.append(send_request(encrypted_uid, token_data["token"], url))
                
                await asyncio.gather(*tasks, return_exceptions=True)
                tokens_used = len(limited_tokens)

                # Get final count
                after = await make_request_async(encrypted_uid, server_name, token)
                if after is None:
                    raise Exception("Failed to retrieve player info after limited requests.")
                
                try:
                    jsone_after = MessageToJson(after)
                except Exception as e:
                    raise Exception(f"Error converting 'after' protobuf to JSON: {e}")
                data_after = json.loads(jsone_after)
                
                after_like = int(data_after.get('AccountInfo', {}).get('Likes', 0))
                player_uid = int(data_after.get('AccountInfo', {}).get('UID', 0))
                player_name = str(data_after.get('AccountInfo', {}).get('PlayerNickname', ''))
                
                like_given = after_like - before_like
                status = 1 if like_given != 0 else 2
                
                return {
                    "API": "Mohit Like API",
                    "LikesGivenByAPI": like_given,
                    "LikesafterCommand": after_like,
                    "LikesbeforeCommand": before_like,
                    "PlayerNickname": player_name,
                    "UID": player_uid,
                    "TokensUsed": tokens_used,
                    "status": status,
                    "message": "Daily quota reached. Sent limited likes (2 tokens)."
                }

            result = asyncio.run(process_limited_request_async())
            response = app.response_class(
                response=json.dumps(result, ensure_ascii=False),
                status=200,
                mimetype='application/json'
            )
            return response
        except Exception as e:
            app.logger.error(f"Error processing limited request: {e}")
            return jsonify({
                "status": 2,
                "error": "Your today like send limit reached come tomorrow",
                "message": f"Daily quota exceeded. Error processing limited likes: {str(e)}"
            }), 429

@app.route('/token-count', methods=['GET'])
def get_token_count():
    """Get count of JWT tokens from all region collections"""
    try:
        database = get_mongo_db()
        regions = ['region_IND', 'region_BR', 'region_ME']
        summary = {}
        total = 0
        
        for region in regions:
            count = database[region].count_documents({})
            summary[region] = count
            total += count
            
        return jsonify({
            "counts": summary,
            "total_tokens": total
        }), 200
    except Exception as e:
        app.logger.error(f"Error counting tokens: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok"}), 200

@app.route('/cleanup-tokens', methods=['POST', 'GET'])
def cleanup_tokens():
    """Remove JWT tokens older than 6 hours from all region collections"""
    try:
        database = get_mongo_db()
        six_hours_ago = datetime.now() - timedelta(hours=6)
        
        regions = ['region_IND', 'region_BR', 'region_ME']
        summary = {}
        
        for region in regions:
            collection = database[region]
            # Assuming tokens have a 'created_at' or similar timestamp field.
            # If not, we might need to rely on MongoDB's _id if it's an ObjectId.
            # Given the previous code, it's safer to check for common timestamp fields
            # or use the _id timestamp if they are standard ObjectIds.
            
            result = collection.delete_many({
                'created_at': {'$lt': six_hours_ago}
            })
            summary[region] = result.deleted_count
            
        app.logger.info(f"Cleaned up old tokens: {summary}")
        return jsonify({
            "message": "Cleanup completed",
            "deleted_counts": summary,
            "threshold": six_hours_ago.isoformat()
        }), 200
    except Exception as e:
        app.logger.error(f"Error cleaning up tokens: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
