import requests
import json
from flask import Flask, request, jsonify, Response, render_template
from key_manager import KeyManager
import hashlib
import threading
import time

# --- CONFIGURATION ---
GOOGLE_API_BASE_URL = "https://generativelanguage.googleapis.com"
CONFIG_FILE = 'config.json'
MAX_RETRIES = 5 # Max retries for a single request

# --- INITIALIZATION ---
app = Flask(__name__)
key_manager = KeyManager(config_path=CONFIG_FILE)
RESPONSE_CACHE = {}
CACHE_LOCK = threading.Lock()

# --- ROUTES ---
@app.route('/status', methods=['GET'])
def status():
    status_data = key_manager.get_key_status()
    current_key = status_data.get('current_key')
    masked_current_key = f"{current_key[:4]}...{current_key[-4:]}" if current_key else "None"

    # Sort keys: active keys first, then by whether the quota is exceeded
    def sort_key_function(key):
        key_status = status_data.get('key_usage_status', {}).get(key, {})
        is_exceeded = key_status.get('daily_quota_exceeded', False)
        return (is_exceeded, key)

    # Update status_data with sorted keys before passing to template
    status_data['priority_keys'] = sorted(status_data.get('priority_keys', []), key=sort_key_function)
    status_data['secondary_keys'] = sorted(status_data.get('secondary_keys', []), key=sort_key_function)

    return render_template('status.html',
                           current_masked_key=masked_current_key,
                           **status_data)

@app.route('/prompt_test', methods=['GET'])
def prompt_test():
    # This page is now more for initiating prompts. Status page shows key details.
    return render_template('prompt_test.html')

@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy(path):
    request_data_bytes = request.get_data()
    cache_key = hashlib.sha256(f"{path}::{request_data_bytes}".encode()).hexdigest()

    with CACHE_LOCK:
        if cache_key in RESPONSE_CACHE:
            cached_response = RESPONSE_CACHE[cache_key]
            print(f"CACHE HIT: Returning cached response for key: {cache_key[:10]}...")
            def generate_from_cache():
                for chunk in cached_response['chunks']:
                    yield chunk
            return Response(generate_from_cache(), cached_response['status'], cached_response['headers'])

    retries = 0
    while retries < MAX_RETRIES:
        model, api_key = key_manager.get_model_and_key()

        if not api_key or not model:
            return jsonify({"error": "All API keys and models are currently rate-limited or unavailable."}), 503

        # Modify the path to include the selected model if it's a generateContent request
        if "generateContent" in path:
            # Path is expected to be like v1beta/models/gemini-pro:generateContent
            parts = path.split('/')
            parts[-2] = f"models/{model}" # Replace model part
            target_path = "/".join(parts)
        else:
            target_path = path

        target_url = f"{GOOGLE_API_BASE_URL}/{target_path}"
        params = request.args.to_dict()
        params['key'] = api_key

        try:
            print(f"Attempt {retries + 1}/{MAX_RETRIES}: Requesting from API with model '{model}' and key '{api_key[:4]}****'...")
            resp = requests.request(
                method=request.method,
                url=target_url,
                params=params,
                headers={key: value for (key, value) in request.headers if key != 'Host'},
                data=request_data_bytes,
                allow_redirects=False,
                stream=True,
                timeout=120 # Extend timeout to 120 seconds
            )

            if resp.status_code == 429:
                print(f"WARN: Received 429 for model '{model}' with key '{api_key[:4]}****'. Retrying...")
                key_manager.handle_429_error(api_key, model)
                retries += 1
                time.sleep(1) # Wait a moment before retrying
                continue # Go to the next iteration of the while loop

            # For other errors, handle them as before
            if resp.status_code == 403:
                key_manager.handle_403_error(api_key)
                return jsonify({"error": "Forbidden - API key may be invalid or disabled."}), 403

            # If successful, process and return the response
            if 200 <= resp.status_code < 300:
                # We need to consume the stream to get the full response and find usage_metadata
                response_content_list = list(resp.iter_content(chunk_size=8192))
                full_response_str = b"".join(response_content_list).decode('utf-8')

                total_tokens = 0
                try:
                    # More robustly parse token usage from the potentially chunked/streamed response
                    for line in full_response_str.split('\n'):
                        line = line.strip()
                        if line.startswith('data:'):
                            line = line[5:].strip() # Remove 'data:' prefix
                        
                        if line.startswith('{'):
                            try:
                                data_chunk = json.loads(line)
                                # Check for usageMetadata which contains the final token count
                                if 'usageMetadata' in data_chunk:
                                    total_tokens = data_chunk['usageMetadata'].get('totalTokenCount', 0)
                                    break # Found it, no need to look further
                                # Fallback for non-streamed or different format
                                elif 'candidates' in data_chunk and data_chunk['candidates']:
                                    if 'tokenCount' in data_chunk['candidates'][0]:
                                        total_tokens = data_chunk['candidates'][0].get('tokenCount', 0)
                                        # Don't break here, as usageMetadata might still appear
                            except json.JSONDecodeError:
                                continue # Ignore lines that are not valid JSON

                    if total_tokens > 0:
                        key_manager.record_token_usage(api_key, model, total_tokens)
                        # Reset the consecutive error counter for this key-model pair on success
                        key_manager.record_successful_request(api_key, model)
                        print(f"SUCCESS: Recorded {total_tokens} tokens for model '{model}' with key '{api_key[:4]}****'.")
                    else:
                        print(f"INFO: No token usage metadata found in the response for model '{model}'.")

                except Exception as e:
                    print(f"WARN: An unexpected error occurred while parsing token usage: {e}")


                response_headers = [(key, value) for (key, value) in resp.raw.headers.items()
                                    if key.lower() not in ['content-encoding', 'transfer-encoding']]

                # Cache the successful response
                with CACHE_LOCK:
                    RESPONSE_CACHE[cache_key] = {
                        'chunks': response_content_list,
                        'status': resp.status_code,
                        'headers': response_headers
                    }
                    print(f"CACHED: Stored response for key: {cache_key[:10]}...")

                def generate_and_stream():
                    for chunk in response_content_list:
                        yield chunk
                
                return Response(generate_and_stream(), resp.status_code, response_headers)
            
            # For other non-200, non-429 responses, stream them without caching or retrying
            else:
                def generate_error():
                    for chunk in resp.iter_content(chunk_size=8192):
                        yield chunk
                return Response(generate_error(), resp.status_code, resp.headers)


        except requests.exceptions.RequestException as e:
            print(f"ERROR: Request failed: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "Request failed after multiple retries due to rate limiting."}), 503


if __name__ == '__main__':
    # The scheduler for daily resets is no longer needed here as we focus on TPM.
    # key_manager.check_and_reset_if_missed() can be used if daily limits are re-introduced.
    app.run(host='0.0.0.0', port=48888)
