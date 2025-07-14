import requests
import json
from flask import Flask, request, jsonify, Response, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from key_manager import KeyManager
import hashlib
import threading

# --- CONFIGURATION ---
GOOGLE_API_BASE_URL = "https://generativelanguage.googleapis.com" # Example, please change to your target API
CONFIG_FILE = 'config.json'

# --- INITIALIZATION ---
app = Flask(__name__)
key_manager = KeyManager(config_path=CONFIG_FILE)
RESPONSE_CACHE = {}
CACHE_LOCK = threading.Lock()

# --- SCHEDULER ---
def setup_scheduler():
    # 使用 key_manager 中计算好的 next_reset_timestamp
    next_reset_dt = key_manager.next_reset_timestamp
    timezone_str = key_manager.timezone

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=lambda: key_manager.reset_all_keys(),
        trigger='date', # 使用 'date' 触发器来指定具体的日期时间
        run_date=next_reset_dt,
        timezone=timezone_str
    )
    scheduler.start()
    print(f"Scheduler started. Keys will be reset at {next_reset_dt.strftime('%Y-%m-%d %H:%M:%S%z')} ({timezone_str} Time).")

# --- ROUTES ---
@app.route('/status', methods=['GET'])
def status():
    status_data = key_manager.get_key_status()
    usage_limit = key_manager.usage_limit

    def prepare_keys_for_template(keys):
        prepared = []
        for k in keys:
            key_copy = k.copy()
            # Mask key for security
            original_key = key_copy['key']
            key_copy['masked_key'] = f"{original_key[:4]}...{original_key[-4:]}"
            prepared.append(key_copy)
        return prepared

    priority_keys = prepare_keys_for_template(status_data.get('priority_pool_status', []))
    secondary_keys = prepare_keys_for_template(status_data.get('secondary_pool_status', []))
    
    current_key_info = status_data.get('current_key')
    current_masked_key = None
    if current_key_info:
        original_key = current_key_info['key']
        current_masked_key = f"{original_key[:4]}...{original_key[-4:]}"

    return render_template(
        'status.html',
        priority_keys=priority_keys,
        secondary_keys=secondary_keys,
        usage_limit=usage_limit,
        current_masked_key=current_masked_key
    )

@app.route('/prompt_test', methods=['GET'])
def prompt_test():
    status_data = key_manager.get_key_status()

    def prepare_keys_for_template(keys):
        prepared = []
        for k in keys:
            key_copy = k.copy()
            original_key = key_copy['key']
            key_copy['masked_key'] = f"{original_key[:4]}...{original_key[-4:]}"
            prepared.append(key_copy)
        return prepared

    priority_keys = prepare_keys_for_template(status_data.get('priority_pool_status', []))
    secondary_keys = prepare_keys_for_template(status_data.get('secondary_pool_status', []))

    return render_template(
        'prompt_test.html',
        priority_keys=priority_keys,
        secondary_keys=secondary_keys
    )

@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy(path):
    request_data = request.get_data()
    cache_key = hashlib.sha256(f"{path}::{request_data}".encode()).hexdigest()

    with CACHE_LOCK:
        if cache_key in RESPONSE_CACHE:
            cached_response = RESPONSE_CACHE[cache_key]
            print(f"CACHE HIT: Returning cached response for key: {cache_key[:10]}...")
            def generate_from_cache():
                for chunk in cached_response['chunks']:
                    yield chunk
            return Response(generate_from_cache(), cached_response['status'], cached_response['headers'])

    # If not in cache, get a key using the new sticky logic
    key_info = key_manager.get_key()
    if not key_info:
        return jsonify({"error": "All API keys are currently unavailable or have reached their usage limit."}), 503

    api_key = key_info['key']
    target_url = f"{GOOGLE_API_BASE_URL}/{path}"
    params = request.args.to_dict()
    params['key'] = api_key

    try:
        print(f"MISS: Requesting from API for key: {cache_key[:10]}...")
        resp = requests.request(
            method=request.method,
            url=target_url,
            params=params,
            headers={key: value for (key, value) in request.headers if key != 'Host'},
            data=request_data,
            allow_redirects=False,
            stream=True
        )

        # Process usage counting and errors
        # Only increment usage if the model is NOT a flash model
        model_name_in_path = path.lower()
        is_flash_model = 'flash' in model_name_in_path

        if 200 <= resp.status_code < 300:
            if not is_flash_model:
                key_manager.increment_usage(api_key)
        elif resp.status_code == 429:
            # Treat 429 as a usage increment to potentially rotate the key, regardless of model type
            key_manager.increment_usage(api_key)
        elif resp.status_code == 403:
            key_manager.handle_403_error(api_key)

        # Prepare headers for the client response
        response_headers = [(key, value) for (key, value) in resp.raw.headers.items()
                            if key.lower() not in ['content-encoding', 'transfer-encoding']]

        # If the response is successful, cache it
        if resp.status_code == 200:
            chunks = list(resp.iter_content(chunk_size=8192))
            with CACHE_LOCK:
                RESPONSE_CACHE[cache_key] = {
                    'chunks': chunks,
                    'status': resp.status_code,
                    'headers': response_headers
                }
                print(f"CACHED: Stored response for key: {cache_key[:10]}...")
            
            def generate_and_stream():
                for chunk in chunks:
                    yield chunk
            
            return Response(generate_and_stream(), resp.status_code, response_headers)
        else:
            # For non-200 responses, just stream them without caching
            def generate_error():
                for chunk in resp.iter_content(chunk_size=8192):
                    yield chunk
            return Response(generate_error(), resp.status_code, response_headers)

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    key_manager.check_and_reset_if_missed() # Check and reset keys if a reset was missed due to server downtime
    setup_scheduler()
    app.run(host='0.0.0.0', port=48888)
