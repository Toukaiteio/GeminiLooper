import requests
import json
from flask import Flask, request, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler
from key_manager import KeyManager

# --- CONFIGURATION ---
GOOGLE_API_BASE_URL = "https://generativelanguage.googleapis.com" # Example, please change to your target API
CONFIG_FILE = 'config.json'

# --- INITIALIZATION ---
app = Flask(__name__)
key_manager = KeyManager(config_path=CONFIG_FILE)

# --- SCHEDULER ---
def setup_scheduler():
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    
    reset_time = config.get('quota_reset_time', '00:05').split(':')
    hour = int(reset_time[0])
    minute = int(reset_time[1])

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        key_manager.reset_all_keys, 
        'cron', 
        hour=hour, 
        minute=minute,
        timezone='Asia/Shanghai' # Or your local timezone
    )
    scheduler.start()
    print(f"Scheduler started. Keys will be reset daily at {hour:02d}:{minute:02d}.")

# --- ROUTES ---
@app.route('/status', methods=['GET'])
def status():
    return jsonify(key_manager.get_key_status())

@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy(path):
    key_info = key_manager.get_key()

    if not key_info:
        return jsonify({"error": "All API keys have reached their usage limit."}), 503

    api_key = key_info['key']
    target_url = f"{GOOGLE_API_BASE_URL}/{path}"

    # Add the API key to the request params
    params = request.args.to_dict()
    params['key'] = api_key

    try:
        # Enable streaming to handle all response types properly
        resp = requests.request(
            method=request.method,
            url=target_url,
            params=params,
            headers={key: value for (key, value) in request.headers if key != 'Host'},
            data=request.get_data(),
            allow_redirects=False,
            stream=True 
        )

        # Process usage counting before streaming the body
        if 200 <= resp.status_code < 300:
            model_name_in_path = path.lower()
            should_increment = False
            if 'pro' in model_name_in_path:
                should_increment = True
            if 'flash' in model_name_in_path:
                should_increment = False
            
            if should_increment:
                key_manager.increment_usage(api_key)
        
        elif resp.status_code == 429:
            key_manager.increment_usage(api_key)

        # Stream the response back to the client
        def generate():
            for chunk in resp.iter_content(chunk_size=8192):
                yield chunk

        # Get headers, excluding any that would interfere with streaming
        headers = [(key, value) for (key, value) in resp.raw.headers.items()
                   if key.lower() not in ['content-encoding', 'transfer-encoding']]

        return Response(generate(), resp.status_code, headers)

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    setup_scheduler()
    app.run(host='0.0.0.0', port=48888)
