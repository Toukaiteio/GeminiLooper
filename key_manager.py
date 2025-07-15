import json
import threading
import os
from datetime import datetime, timedelta
import pytz
import hashlib

class KeyManager:
    def __init__(self, config_path='config.json', usage_file='key_usage.json', unavailable_file='unavailable.json'):
        self.config_path = config_path
        self.usage_file = usage_file
        self.unavailable_file = unavailable_file
        self.lock = threading.RLock()
        self.need_rotation = False  # Flag for deferred key rotation

        # Load configuration
        with open(config_path) as f:
            config = json.load(f)
        
        self.usage_limit = config.get('usage_limit_per_key', 100)
        self.switch_threshold = config.get('switch_threshold', 40)
        self.rotation_timeout = config.get('rotation_timeout', 30)
        self.low_quota_threshold = config.get('low_quota_threshold', 40)
        self.quota_reset_datetime_str = config.get('quota_reset_datetime', '2025-01-01 00:00') # New field
        self.timezone = config.get('timezone', 'America/Los_Angeles')
        self.default_model = config.get('default_model', 'gemini-pro') # New field for default model

        # Load unavailable keys
        self.potential_unavailable = self._load_potential_unavailable()
        self.unavailable_keys = self._load_unavailable_keys()

        # Load all keys from config and filter out unavailable ones
        priority_keys = [k for k in config.get('priority_keys', []) if k not in self.unavailable_keys]
        secondary_keys = [k for k in config.get('secondary_keys', []) if k not in self.unavailable_keys]
        
        # Load usage data and initialize key pools
        self.all_keys_usage, self.next_reset_timestamp = self._load_usage_data(priority_keys + secondary_keys)
        # 确保 next_reset_timestamp 在初始化时总是指向未来的重置点
        self._update_next_reset_timestamp()
        
        self.priority_pool = [k for k in self.all_keys_usage if k['key'] in priority_keys]
        self.secondary_pool = [k for k in self.all_keys_usage if k['key'] in secondary_keys]

        # State variables for sticky key logic
        self.current_key_info = None
        self.rotation_timer = None

    def get_key(self):
        with self.lock:
            # Check if rotation is needed
            if self.need_rotation:
                print("DEBUG: Rotation flag detected. Performing deferred rotation.")
                self._rotate_key()
                self.need_rotation = False
                if not self.current_key_info:
                    return None

            # If there's an active key, reset the rotation timer and return it
            if self.current_key_info:
                self._reset_rotation_timer()
                print(f"DEBUG: Sticking with key {self.current_key_info['key'][:4]}**** with model {self.current_key_info.get('model', self.default_model)}")
                return self.current_key_info

            # If no active key, select a new one
            new_key_info = self._select_new_key()
            if not new_key_info:
                print("ERROR: No available keys to select.")
                return None
            
            self.current_key_info = new_key_info
            print(f"DEBUG: Selected new key {self.current_key_info['key'][:4]}**** with model {self.current_key_info.get('model', self.default_model)}")
            return self.current_key_info

    def increment_usage(self, key_str):
        with self.lock:
            if self.current_key_info and self.current_key_info['key'] == key_str:
                self.current_key_info['usage'] += 1
                print(f"DEBUG: Key {key_str[:4]}**** usage is now {self.current_key_info['usage']}")
                self._save_usage_data()

                # If usage reaches the switch threshold, start the rotation timer
                if self.current_key_info['usage'] >= self.switch_threshold:
                    self._start_rotation_timer()
            
            # Handle successful usage for potentially unavailable keys
            if key_str in self.potential_unavailable:
                del self.potential_unavailable[key_str]
                self._save_unavailable_data()

    def _select_new_key(self):
        # Filter for active keys (usage < limit)
        active_priority = [k for k in self.priority_pool if k['usage'] < self.usage_limit]
        active_secondary = [k for k in self.secondary_pool if k['usage'] < self.usage_limit]

        # Check if any priority key has enough quota
        priority_with_high_quota = [k for k in active_priority if (self.usage_limit - k['usage']) > self.low_quota_threshold]

        # If there are priority keys with high quota, use the one with the least usage
        if priority_with_high_quota:
            print("DEBUG: Selecting from priority keys with high quota.")
            selected_key = min(priority_with_high_quota, key=lambda k: k['usage'])
            selected_key['model'] = self.default_model # Assign default model
            return selected_key
        
        # Otherwise, use any available key (priority or secondary), picking the one with least usage
        all_active_keys = active_priority + active_secondary
        if all_active_keys:
            print("DEBUG: No high-quota priority keys. Selecting from all available keys.")
            selected_key = min(all_active_keys, key=lambda k: k['usage'])
            selected_key['model'] = self.default_model # Assign default model
            return selected_key

        return None

    def _start_rotation_timer(self):
        self._cancel_rotation_timer() # Cancel any existing timer
        # 动态计算退避时间
        retry_count = getattr(self.current_key_info, 'retry_count', 0) + 1
        print(f"DEBUG: Switch threshold reached for key {self.current_key_info['key'][:4]}****. Starting {self.rotation_timeout}s rotation timer.")

    def _reset_rotation_timer(self):
        if self.current_key_info and self.current_key_info['usage'] >= self.switch_threshold:
            self._start_rotation_timer()

    def _cancel_rotation_timer(self):
        if self.rotation_timer:
            self.rotation_timer.cancel()
            self.rotation_timer = None

    def _rotate_key(self):
        if self.current_key_info:
            print(f"DEBUG: Rotation timer expired. Releasing key {self.current_key_info['key'][:4]}****.")
            self.current_key_info = None
            self._cancel_rotation_timer()
            # 强制立即获取新密钥
            new_key = self._select_new_key()
            if new_key:
                self.current_key_info = new_key
                print(f"DEBUG: Auto-rotated to new key {self.current_key_info['key'][:4]}****")
                self._start_rotation_timer()

    def handle_403_error(self, key_str):
        with self.lock:
            masked_key = f"{key_str[:4]}****{key_str[-6:]}"
            print(f"ERROR: 403 Error reported for key: {masked_key}")

            current_errors = self.potential_unavailable.get(key_str, 0) + 1
            
            if current_errors >= 3:
                print(f"INFO: Key {masked_key} reached 3 errors and will be permanently disabled.")
                if key_str in self.potential_unavailable:
                    del self.potential_unavailable[key_str]
                
                if key_str not in self.unavailable_keys:
                    self.unavailable_keys.append(key_str)
                
                self._remove_key_from_pools(key_str)
                self._remove_key_from_config(key_str)
            else:
                print(f"INFO: Key {masked_key} now has {current_errors} error(s).")
                self.potential_unavailable[key_str] = current_errors
            
            self._save_unavailable_data()
            
            # If the error was on the current key, force rotation
            if self.current_key_info and self.current_key_info['key'] == key_str:
                self.current_key_info = None
                self._cancel_rotation_timer()

    def handle_429_error(self, key_str):
        with self.lock:
            masked_key = f"{key_str[:4]}****{key_str[-6:]}"
            print(f"ERROR: 429 Error (Rate Limit Exceeded) reported for key: {masked_key}")

            # Mark the key's usage as exhausted
            for key_info in self.all_keys_usage:
                if key_info['key'] == key_str:
                    key_info['usage'] = self.usage_limit
                    print(f"INFO: Key {masked_key} usage marked as exhausted ({self.usage_limit}).")
                    break
            self._save_usage_data()

            # If the error is on the current key, handle model switching or rotation
            if self.current_key_info and self.current_key_info['key'] == key_str:
                # If the key is already using the flash model, it has hit another 429.
                # In this case, we set the rotation flag instead of calling _rotate_key directly
                if self.current_key_info.get('model') == 'gemini-2.5-flash':
                    print(f"INFO: Key {masked_key} received another 429 error on 'gemini-2.5-flash'. Setting rotation flag.")
                    self.need_rotation = True
                    # 确保新密钥使用默认模型
                    if self.current_key_info:
                        self.current_key_info['model'] = self.default_model
                        print(f"DEBUG: Reset model to {self.default_model} after forced rotation")
                else:
                    # First 429 error, attempt to switch to the flash model.
                    print(f"INFO: Attempting to switch model for key {masked_key} to 'gemini-2.5-flash'.")
                    self.current_key_info['model'] = 'gemini-2.5-flash'
                    self._start_rotation_timer()  # Start rotation timer for the new model
            else:
                # If the 429 error is not on the current key, just mark it as exhausted
                print(f"INFO: Key {masked_key} is not the current key. Marked as exhausted.")

    def _remove_key_from_pools(self, key_str):
        self.priority_pool = [k for k in self.priority_pool if k['key'] != key_str]
        self.secondary_pool = [k for k in self.secondary_pool if k['key'] != key_str]
        self.all_keys_usage = [k for k in self.all_keys_usage if k['key'] != key_str]

    # --- Data Loading and Saving (largely unchanged but adapted) ---

    def _load_usage_data(self, current_api_keys):
        # 初始化默认值
        stored_usage = {}
        next_reset = None
        
        if os.path.exists(self.usage_file):
            with open(self.usage_file, 'r') as f:
                try:
                    data = json.load(f)
                    # 新格式包含使用情况和下次重置时间
                    stored_usage = data.get('usage_data', {})
                    next_reset_str = data.get('next_reset')
                    next_reset = datetime.fromisoformat(next_reset_str) if next_reset_str else None
                except json.JSONDecodeError:
                    pass

        # 处理旧格式迁移 (如果需要，这里可以添加旧格式的判断和迁移逻辑)
        # 目前假设 usage_data 总是字典，如果不是，则需要更复杂的迁移逻辑
        if isinstance(stored_usage, list):
            print("DEBUG: Migrating old usage format to new structure")
            stored_usage = {item['key']: item['usage'] for item in stored_usage if 'key' in item}
            # 迁移后保存，确保下次加载是新格式
            self._save_usage_data_internal({
                'usage_data': stored_usage,
                'next_reset': next_reset.isoformat() if next_reset else None
            })

        # 创建新的使用数据，合并当前API密钥
        usage_data = []
        for key in current_api_keys:
            usage_data.append({
                "key": key,
                "usage": self._get_usage_value(stored_usage.get(key)),
                "model": self.default_model # Initialize with default model
            })
            
        return usage_data, next_reset

    def _save_usage_data(self):
        data_to_save = {item['key']: item['usage'] for item in self.all_keys_usage} # Save usage as a flat integer
        self._save_usage_data_internal(data_to_save)

    def _save_usage_data_internal(self, data_to_save):
        # 构建包含下次重置时间的完整数据结构
        full_data = {
            'usage_data': data_to_save,
            'next_reset': self.next_reset_timestamp.isoformat() if self.next_reset_timestamp else None
        }
        
        temp_file = self.usage_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(full_data, f, indent=4)
        os.replace(temp_file, self.usage_file)

    def reset_all_keys(self):
        with self.lock:
            print("Resetting all API key usage counts.")
            for key_info in self.all_keys_usage:
                key_info['usage'] = 0
                key_info['model'] = self.default_model # Reset model to default
            self._save_usage_data()
            # Also reset the current key to force re-selection
            self.current_key_info = None
            self._cancel_rotation_timer()
            # 重置后立即更新下次重置时间
            self._update_next_reset_timestamp()

    def get_key_status(self):
        with self.lock:
            return {
                'current_key': self.current_key_info,
                'priority_pool_status': self.priority_pool,
                'secondary_pool_status': self.secondary_pool,
                'potential_unavailable': self.potential_unavailable,
                'unavailable_keys': self.unavailable_keys,
                'next_reset': self.next_reset_timestamp.isoformat() if self.next_reset_timestamp else None
            }

    def _update_next_reset_timestamp(self):
        with self.lock:
            tz = pytz.timezone(self.timezone)
            now = datetime.now(tz)
            
            # 解析配置中的完整日期时间字符串
            try:
                initial_reset_dt = tz.localize(datetime.strptime(self.quota_reset_datetime_str, '%Y-%m-%d %H:%M'))
            except ValueError:
                print(f"WARNING: Invalid quota_reset_datetime format in config. Using current time for initial reset. Format should be YYYY-MM-DD HH:MM. Config value: {self.quota_reset_datetime_str}")
                initial_reset_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) # Default to start of today

            # 如果当前时间已经过了初始重置时间，则计算下一个重置点
            # 每次重置都是在 initial_reset_dt 的时间点，但日期会根据当前日期调整
            if now >= initial_reset_dt:
                # 如果当前时间已经过了配置的日期时间，则将日期推迟到明天或下一个重置周期
                # 确保重置时间点是未来的
                while initial_reset_dt <= now:
                    initial_reset_dt += timedelta(days=1)
                self.next_reset_timestamp = initial_reset_dt
            else:
                self.next_reset_timestamp = initial_reset_dt
            
            print(f"DEBUG: Updated next reset timestamp to {self.next_reset_timestamp.strftime('%Y-%m-%d %H:%M:%S%z')}")
            self._save_usage_data() # 保存更新后的下次重置时间

    def check_and_reset_if_missed(self):
        with self.lock:
            tz = pytz.timezone(self.timezone)
            now = datetime.now(tz)
            print(f"DEBUG: Current time in {self.timezone}: {now.strftime('%Y-%m-%d %H:%M:%S%z')}")
            
            # 确保 next_reset_timestamp 已经被初始化
            if not self.next_reset_timestamp:
                self._update_next_reset_timestamp()
            
            # 如果当前时间已经超过了预定的下次重置时间
            if now >= self.next_reset_timestamp:
                print(f"INFO: Performing scheduled reset at {self.next_reset_timestamp.strftime('%Y-%m-%d %H:%M:%S%z')}")
                self.reset_all_keys()
                # 重置后，立即计算并更新下一次的重置时间
                self._update_next_reset_timestamp()
            else:
                print(f"INFO: Next reset scheduled at {self.next_reset_timestamp.strftime('%Y-%m-%d %H:%M:%S%z')} (current time: {now.strftime('%Y-%m-%d %H:%M:%S%z')})")

    def _load_potential_unavailable(self):
        if not os.path.exists(self.unavailable_file):
            return {}
        with open(self.unavailable_file, 'r') as f:
            try:
                data = json.load(f)
                return data.get('potential_unavailable', {})
            except (json.JSONDecodeError, KeyError):
                return {}

    def _load_unavailable_keys(self):
        if not os.path.exists(self.unavailable_file):
            return []
        with open(self.unavailable_file, 'r') as f:
            try:
                data = json.load(f)
                return data.get('unavailable', [])
            except json.JSONDecodeError:
                return []

    def _save_unavailable_data(self):
        data = {
            'potential_unavailable': self.potential_unavailable,
            'unavailable': self.unavailable_keys
        }
        temp_file = self.unavailable_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=4)
        os.replace(temp_file, self.unavailable_file)

    def _remove_key_from_config(self, key_str):
        with open(self.config_path, 'r+') as f:
            config = json.load(f)
            config['priority_keys'] = [k for k in config.get('priority_keys', []) if k != key_str]
            config['secondary_keys'] = [k for k in config.get('secondary_keys', []) if k != key_str]
            f.seek(0)
            f.truncate()
            json.dump(config, f, indent=2)

    def _get_usage_value(self, value):
        """Helper to extract usage from old/new formats."""
        if isinstance(value, dict) and 'usage' in value:
            return value['usage']
        return value if isinstance(value, int) else 0
