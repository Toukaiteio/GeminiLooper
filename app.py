import requests
import json
from flask import Flask, request, jsonify, Response, render_template
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
        hour=0,
        minute=0,
        timezone='America/Los_Angeles'
    )
    scheduler.start()
    print(f"Scheduler started. Keys will be reset daily at {hour:02d}:{minute:02d}.")

# --- ROUTES ---
@app.route('/status', methods=['GET'])
def status():
    return jsonify(key_manager.get_key_status())

@app.route('/status_test', methods=['GET'])
def status_test():
    from_param = request.args.get('from', type=str)
    to_param = request.args.get('to', type=str)
    
    try:
        # 尝试解析为数字索引
        from_idx = int(from_param)
        to_idx = int(to_param)
        result = key_manager.get_key_status_range_by_index(from_idx, to_idx)
    except ValueError:
        # 作为字符串处理
        result = key_manager.get_key_status_range_by_key(from_param, to_param)
    
    return jsonify(result)

@app.route('/prompt_test', methods=['GET'])
def prompt_test():
    keys = key_manager.get_key_status()
    return render_template('prompt_test.html', keys=keys['active_keys'])

@app.route('/api/test_prompt', methods=['POST'])
def api_test_prompt():
    data = request.get_json()
    api_key = data.get('api_key')
    prompt = data.get('prompt')
    model = data.get('model')

    if not all([api_key, prompt, model]):
        return jsonify({"error": "Missing required parameters"}), 400

    def generate():
        try:
            url = f"{GOOGLE_API_BASE_URL}/v1beta/models/{model}:streamGenerateContent?key={api_key}"
            payload = {
                "contents": [{
                    "parts": [{
                        "text": prompt
                    }]
                }]
            }
            headers = {'Content-Type': 'application/json'}
            
            resp = requests.post(url, json=payload, headers=headers, stream=True)
            
            if resp.status_code != 200:
                yield f"Error: {resp.status_code} {resp.text}"
                return

            for chunk in resp.iter_content(chunk_size=None):
                yield chunk

        except Exception as e:
            yield f"Error: {str(e)}"

    return Response(generate(), mimetype='text/event-stream')

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
        elif resp.status_code == 403:
            key_manager.handle_403_error(api_key)

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
