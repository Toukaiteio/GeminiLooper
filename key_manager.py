import json
import threading

class KeyManager:
    def __init__(self, config_path='config.json'):
        with open(config_path) as f:
            config = json.load(f)
        
        self.keys = [{"key": key, "usage": 0, "active": True} for key in config['api_keys']]
        self.usage_limit = config['usage_limit_per_key']
        self.lock = threading.Lock()

    def get_key(self):
        with self.lock:
            active_keys = [k for k in self.keys if k['active']]
            if not active_keys:
                return None
            
            # Find the key with the minimum usage
            least_used_key = min(active_keys, key=lambda k: k['usage'])
            return least_used_key

    def increment_usage(self, key_str):
        with self.lock:
            for key_info in self.keys:
                if key_info['key'] == key_str:
                    key_info['usage'] += 1
                    if key_info['usage'] >= self.usage_limit:
                        key_info['active'] = False
                    break

    def reset_all_keys(self):
        with self.lock:
            print("Resetting all API key usage counts.")
            for key_info in self.keys:
                key_info['usage'] = 0
                key_info['active'] = True

    def get_key_status(self):
        with self.lock:
            return self.keys
