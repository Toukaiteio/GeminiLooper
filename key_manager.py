import json
import threading
import os
from datetime import datetime, timedelta
import time
import pytz

class KeyManager:
    def __init__(self, config_path='config.json', usage_file='key_usage.json', unavailable_file='unavailable.json'):
        self.config_path = config_path
        self.usage_file = usage_file
        self.unavailable_file = unavailable_file
        self.lock = threading.RLock()

        # Load configuration
        with open(config_path) as f:
            self.config = json.load(f)
        
        self.models_config = self.config.get('models', {})
        self.fallback_strategy = self.config.get('fallback_strategy', {})
        self.model_order = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-flash"]
        
        self.quota_reset_datetime_str = self.config.get('quota_reset_datetime', '2025-01-01 00:00')
        self.timezone = self.config.get('timezone', 'America/Los_Angeles')
        self.usage_record_retention_seconds = self.config.get('usage_record_retention_seconds', 86400)

        # Load unavailable keys
        self.potential_unavailable = self._load_potential_unavailable()
        self.unavailable_keys = self._load_unavailable_keys()

        # Load all keys from config and filter out unavailable ones
        self.priority_keys = [k for k in self.config.get('priority_keys', []) if k not in self.unavailable_keys]
        self.secondary_keys = [k for k in self.config.get('secondary_keys', []) if k not in self.unavailable_keys]
        self.all_keys = self.priority_keys + self.secondary_keys

        # Load usage data
        self.usage_data, self.next_reset_timestamp, self.rate_limited_keys, self.model_specific_disabled = self._load_usage_data()
        self._update_next_reset_timestamp()
        
        # If usage data is empty (e.g., first run or corrupted file), initialize it.
        if not self.usage_data:
            print("INFO: Initializing new usage data structure.")
            self.usage_data = {}
            for key in self.all_keys:
                self.usage_data[key] = {}
                for model in self.model_order:
                    self.usage_data[key][model] = {
                        "usage_records": [],
                        "total_tokens": 0,
                        "daily_tokens": 0,
                        "is_temporarily_disabled": False,
                        "disabled_until": 0,
                        "last_429_error": 0,
                        "consecutive_429_count": 0
                    }
            self._save_usage_data()

        # State variables
        self.current_key_index = 0
        self.last_pro_usage_time = None

        # Perform a global cleanup of old usage records on startup.
        self._prune_all_usage_records()


    def _prune_all_usage_records(self):
        """
        Iterates through all keys and models in the usage data and prunes old
        usage records based on the configured retention period.
        """
        now = time.time()
        retention_seconds = self.usage_record_retention_seconds
        data_changed = False
        
        # Ensure usage_data is a dictionary before iterating
        if not isinstance(self.usage_data, dict):
            return

        for key, models_data in self.usage_data.items():
            # Ensure models_data is a dictionary
            if not isinstance(models_data, dict):
                continue
            for model, model_data in models_data.items():
                if isinstance(model_data, dict) and "usage_records" in model_data:
                    original_count = len(model_data["usage_records"])
                    
                    # Filter records, ensuring 'timestamp' exists and is a number
                    model_data["usage_records"] = [
                        r for r in model_data["usage_records"]
                        if isinstance(r, dict) and isinstance(r.get("timestamp"), (int, float)) and now - r["timestamp"] < retention_seconds
                    ]
                    
                    if len(model_data["usage_records"]) != original_count:
                        data_changed = True
        
        if data_changed:
            print(f"INFO: Pruned old usage records globally based on a {retention_seconds}s retention period.")
            self._save_usage_data()

    def get_model_and_key(self, requested_model=None):
        """
        Intelligent model selection engine - selects the optimal model and key combination
        based on the requested model and current state.
        """
        with self.lock:
            self._check_and_recover_models()

            if requested_model is None:
                requested_model = self.config.get('default_model', 'gemini-2.5-pro')
            
            if requested_model not in self.model_order:
                print(f"WARN: Requested model '{requested_model}' not found. Defaulting to 'gemini-2.5-pro'.")
                requested_model = 'gemini-2.5-pro'

            # --- Primary Selection Logic ---
            models_to_check = self._get_model_fallback_order(requested_model)
            
            for model in models_to_check:
                result = self._find_available_key_for_model(model)
                if result:
                    selected_model, selected_key = result
                    self._update_selection_state(selected_model, selected_key)
                    print(f"DEBUG: Selected model '{selected_model}' with key '{selected_key[:4]}****'.")
                    return selected_model, selected_key

            # --- Borrowing Logic ---
            # This block is triggered only if the primary selection logic fails for all models and keys.
            active_key = self.all_keys[self.current_key_index] if self.all_keys else None
            if active_key and self._is_last_model_cooling_down(active_key):
                print(f"INFO: Primary key '{active_key[:4]}****' is in cooldown. Attempting to borrow from other keys.")
                borrowed_model, borrowed_key = self._find_borrowed_model()
                if borrowed_model and borrowed_key:
                    # IMPORTANT: Do not update the main state (current_key_index).
                    # This is a temporary borrow.
                    print(f"DEBUG: Successfully borrowed model '{borrowed_model}' from key '{borrowed_key[:4]}****'.")
                    return borrowed_model, borrowed_key

            print("ERROR: All models and keys are currently unavailable, including borrowing options.")
            return None, None

    def _try_recover_requested_model(self, requested_model):
        """Attempts to recover the user-requested model."""
        if requested_model == "gemini-2.5-pro" and self.last_pro_usage_time:
            if time.time() - self.last_pro_usage_time > 5:
                recovery_threshold = self.models_config.get("gemini-2.5-pro", {}).get("recovery_threshold", 0)
                for key in self.all_keys:
                    if (self._is_key_model_available(key, "gemini-2.5-pro") and
                        self._get_tokens_in_last_minute(key, "gemini-2.5-pro") < recovery_threshold):
                        self.last_pro_usage_time = None  # Reset timer
                        return True
        return False

    def _get_model_fallback_order(self, requested_model):
        """Gets the fallback order for a model."""
        fallback_order = self.fallback_strategy.get(requested_model, [])
        
        if not fallback_order:
            if requested_model == "gemini-2.5-pro":
                fallback_order = [m for m in self.model_order if m != requested_model]
            else:
                non_pro_models = [m for m in self.model_order if m != "gemini-2.5-pro" and m != requested_model]
                fallback_order = non_pro_models + ["gemini-2.5-pro"]
        
        if requested_model not in fallback_order:
            fallback_order.insert(0, requested_model)
        elif fallback_order[0] != requested_model:
            fallback_order.remove(requested_model)
            fallback_order.insert(0, requested_model)
        
        return fallback_order

    def _find_available_key_for_model(self, model):
        """Finds an available key for a given model, implementing a 'sticky' strategy."""
        if not self.all_keys:
            return None

        current_key = self.all_keys[self.current_key_index]
        if self._is_key_model_available(current_key, model):
            return model, current_key

        num_keys = len(self.all_keys)
        for i in range(1, num_keys):
            next_key_index = (self.current_key_index + i) % num_keys
            next_key = self.all_keys[next_key_index]
            
            if self._is_key_model_available(next_key, model):
                return model, next_key

        return None

    def _is_last_model_cooling_down(self, key):
        """
        Checks if the key's single last available model is in a cooldown state.
        """
        # A key is in this state if it has NO available models right now,
        # but it has exactly one model that is in temporary cooldown.

        currently_available_models = [m for m in self.model_order if self._is_key_model_available(key, m)]
        if len(currently_available_models) > 0:
            return False # If any model is usable right now, we don't need to borrow.

        # Get all models for this key that are temporarily disabled.
        temporarily_disabled_models = []
        if key in self.usage_data:
            for model in self.model_order:
                if self._is_model_temporarily_disabled(key, model):
                     temporarily_disabled_models.append(model)
        
        # The condition is met if there are no available models and exactly one is cooling down.
        if len(temporarily_disabled_models) == 1:
            print(f"DEBUG: Key {key[:4]}**** is in last model cooldown for model {temporarily_disabled_models[0]}.")
            return True
            
        return False

    def _find_borrowed_model(self):
        """
        Finds an available model from a borrowable (rate-limited) key.
        """
        borrowable_keys = [k for k in self.rate_limited_keys if k in self.all_keys]
        if not borrowable_keys:
            return None, None

        # Fallback order, excluding the pro model which is assumed to be exhausted on these keys
        models_to_check = [m for m in self.model_order if m != "gemini-2.5-pro"]

        for key in borrowable_keys:
            for model in models_to_check:
                if self._is_key_model_available(key, model):
                    print(f"DEBUG: Found borrowable model '{model}' on key '{key[:4]}****'.")
                    return model, key
        
        return None, None

    def _update_selection_state(self, model, key):
        """更新选择状态"""
        # 更新当前密钥索引
        if key in self.all_keys:
            self.current_key_index = self.all_keys.index(key)
        
        # 如果选择了gemini-2.5-pro，记录使用时间
        if model == "gemini-2.5-pro":
            self.last_pro_usage_time = time.time()

    def record_token_usage(self, key, model, tokens):
        with self.lock:
            if key not in self.usage_data:
                self.usage_data[key] = {}
            if model not in self.usage_data[key]:
                self.usage_data[key][model] = {
                    "usage_records": [],
                    "total_tokens": 0,
                    "daily_tokens": 0,
                    "is_temporarily_disabled": False,
                    "disabled_until": 0,
                    "last_429_error": 0,
                    "consecutive_429_count": 0
                }
            
            # Append new usage record
            self.usage_data[key][model]["usage_records"].append({
                "timestamp": time.time(),
                "tokens": tokens
            })
            
            # Update persistent total tokens
            self.usage_data[key][model]["total_tokens"] += tokens
            self.usage_data[key][model]["daily_tokens"] = self.usage_data[key][model].get("daily_tokens", 0) + tokens
            
            # Prune old records
            self._prune_usage_records(key, model)
            self._save_usage_data()

    def record_successful_request(self, key, model):
        """Resets the consecutive error count after a successful request."""
        with self.lock:
            if key in self.usage_data and model in self.usage_data[key]:
                if self.usage_data[key][model].get("consecutive_429_count", 0) > 0:
                    print(f"INFO: Resetting consecutive 429 count for model '{model}' on key '{key[:4]}****' after successful request.")
                    self.usage_data[key][model]["consecutive_429_count"] = 0
                    self._save_usage_data()

    def handle_429_error(self, key, model):
        """
        智能处理429错误，根据当前使用量和模型类型决定处理策略
        """
        with self.lock:
            current_usage = self._get_tokens_in_last_minute(key, model)
            model_config = self.models_config.get(model, {})
            recovery_threshold = model_config.get("recovery_threshold", 0)
            max_consecutive_429 = 2  # Per user request, force switch after 2 errors.
            
            print(f"INFO: 429 Error received for key {key[:4]}**** on model {model}. Current usage: {current_usage}")
            
            # 确保模型数据存在
            if key not in self.usage_data:
                self.usage_data[key] = {}
            if model not in self.usage_data[key]:
                self.usage_data[key][model] = {
                    "usage_records": [],
                    "total_tokens": 0,
                    "daily_tokens": 0,
                    "is_temporarily_disabled": False,
                    "disabled_until": 0,
                    "last_429_error": 0,
                    "consecutive_429_count": 0
                }
            
            model_data = self.usage_data[key][model]
            model_data["last_429_error"] = time.time()
            model_data["consecutive_429_count"] = model_data.get("consecutive_429_count", 0) + 1
            
            # 判断处理策略
            if current_usage < recovery_threshold:
                # 使用量低于恢复阈值但仍然429，可能是API问题
                print(f"WARN: 429 error with low usage ({current_usage} < {recovery_threshold}). Consecutive errors: {model_data['consecutive_429_count']}")
                
                if model_data["consecutive_429_count"] >= max_consecutive_429:
                    if model == "gemini-2.5-pro":
                        # For gemini-2.5-pro, mark the whole key as rate-limited (borrowable)
                        print(f"WARN: Key {key[:4]}**** has {model_data['consecutive_429_count']} consecutive 429 errors on gemini-2.5-pro. Adding to rate-limited list.")
                        if key not in self.rate_limited_keys:
                            self.rate_limited_keys.append(key)
                    else:
                        # For other models, just disable the model on this key
                        print(f"WARN: Temporarily disabling model {model} on key {key[:4]}**** due to {model_data['consecutive_429_count']} consecutive 429 errors with low usage.")
                        self._disable_model_temporarily(key, model)
            else:
                # 正常的速率限制，临时禁用该模型
                print(f"INFO: Normal rate limiting detected. Temporarily disabling model {model} on key {key[:4]}****.")
                self._disable_model_temporarily(key, model)
            
            # 如果是gemini-2.5-pro出错，设置恢复检查计时器
            if model == "gemini-2.5-pro":
                self.last_pro_usage_time = time.time()
            
            self._save_usage_data()

    def _handle_429_with_context(self, key, model, current_usage):
        """
        根据当前使用情况和模型类型智能处理429错误
        返回处理策略：'switch_model', 'disable_key', 'disable_key_model'
        """
        model_config = self.models_config.get(model, {})
        recovery_threshold = model_config.get("recovery_threshold", 0)
        max_consecutive_429 = model_config.get("max_consecutive_429", 3)
        
        if key not in self.usage_data or model not in self.usage_data[key]:
            return 'switch_model'
        
        model_data = self.usage_data[key][model]
        consecutive_count = model_data.get("consecutive_429_count", 0)
        
        if current_usage < recovery_threshold:
            # 低使用量但仍然429错误
            if consecutive_count >= max_consecutive_429:
                if model == "gemini-2.5-pro":
                    return 'disable_key'  # 禁用整个密钥
                else:
                    return 'disable_key_model'  # 只禁用该模型
            else:
                return 'switch_model'  # 切换模型
        else:
            # 正常的速率限制
            return 'disable_key_model'  # 临时禁用该模型

    def _get_tokens_in_last_minute(self, key, model):
        # 添加防御性类型检查
        if not isinstance(key, str):
            print(f"CRITICAL ERROR: Invalid key type {type(key)} in usage check: {key}")
            return float('inf')  # 返回极大值强制切换密钥
        
        if key not in self.usage_data or model not in self.usage_data[key]:
            return 0
        
        now = time.time()
        total_tokens = 0
        # Ensure we are accessing the list of records correctly
        records = self.usage_data[key][model].get("usage_records", [])
        for record in records:
            if now - record["timestamp"] <= 60:
                total_tokens += record["tokens"]
        return total_tokens

    def _prune_usage_records(self, key, model):
        if key not in self.usage_data or model not in self.usage_data[key]:
            return

        now = time.time()
        # Keep records from the last 5 minutes to be safe
        records = self.usage_data[key][model].get("usage_records", [])
        self.usage_data[key][model]["usage_records"] = [
            r for r in records if now - r.get("timestamp", 0) < self.usage_record_retention_seconds
        ]

    def _load_usage_data(self):
        if not os.path.exists(self.usage_file):
            print(f"INFO: Usage file '{self.usage_file}' not found. Initializing with empty data.")
            return {}, None, [], {}
        
        data = {}
        try:
            with open(self.usage_file, 'r') as f:
                data = json.load(f)
            
            # Validate the structure of the loaded data
            if not isinstance(data, dict) or 'usage_data' not in data or 'next_reset' not in data:
                raise ValueError("Invalid format in usage file.")
            
            usage = data.get('usage_data', {})
            rate_limited_keys = data.get('rate_limited_keys', [])
            model_specific_disabled = data.get('model_specific_disabled', {})
            # Ensure usage is a dictionary
            if not isinstance(usage, dict):
                raise ValueError("Invalid 'usage_data' type in usage file.")

            # Data migration: Check and convert old format to new format
            for key, models_data in usage.items():
                for model, model_data in models_data.items():
                    if isinstance(model_data, list):
                        print(f"INFO: Migrating old usage data format for key '{key}' and model '{model}'.")
                        total_tokens_migrated = sum(r['tokens'] for r in model_data)
                        usage[key][model] = {
                            "usage_records": model_data,
                            "total_tokens": total_tokens_migrated,
                            "daily_tokens": 0,
                            "is_temporarily_disabled": False,
                            "disabled_until": 0,
                            "last_429_error": 0,
                            "consecutive_429_count": 0
                        }
                    elif isinstance(model_data, dict) and "is_temporarily_disabled" not in model_data:
                        # Migrate existing dict format to include new fields
                        print(f"INFO: Adding new fields to existing data for key '{key}' and model '{model}'.")
                        usage[key][model].update({
                            "daily_tokens": model_data.get("daily_tokens", 0),
                            "is_temporarily_disabled": False,
                            "disabled_until": 0,
                            "last_429_error": 0,
                            "consecutive_429_count": 0
                        })

            next_reset_str = data.get('next_reset')

            next_reset = datetime.fromisoformat(next_reset_str) if next_reset_str else None
            
            # Basic validation for next_reset
            if next_reset_str and not isinstance(next_reset, datetime):
                raise ValueError("Invalid 'next_reset' format in usage file.")

            print(f"INFO: Successfully loaded usage data from '{self.usage_file}'.")
            return usage, next_reset, rate_limited_keys, model_specific_disabled
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            timestamp = int(time.time())
            illegal_filename = f"__illegal_{timestamp}_{self.usage_file}"
            print(f"ERROR: Failed to load usage data from '{self.usage_file}' due to format mismatch or corruption: {e}")
            print(f"INFO: Renaming '{self.usage_file}' to '{illegal_filename}' and initializing with empty data.")
            # Ensure the file is closed before attempting to rename
            if os.path.exists(self.usage_file):
                try:
                    os.rename(self.usage_file, illegal_filename)
                except PermissionError:
                    print(f"WARNING: Could not rename '{self.usage_file}' to '{illegal_filename}'. It might be in use by another process. Please manually delete or rename it.")
            return {}, None, [], {}

    def _save_usage_data(self):
        full_data = {
            'usage_data': self.usage_data,
            'next_reset': self.next_reset_timestamp.isoformat() if self.next_reset_timestamp else None,
            'rate_limited_keys': self.rate_limited_keys,
            'model_specific_disabled': self.model_specific_disabled
        }
        temp_file = self.usage_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(full_data, f, indent=4)
        os.replace(temp_file, self.usage_file)

    def _update_next_reset_timestamp(self):
        """
        Calculates and sets the next quota reset timestamp based on the configured timezone.
        The reset happens at 1 AM in the specified timezone.
        """
        try:
            tz = pytz.timezone(self.timezone)
        except pytz.UnknownTimeZoneError:
            print(f"ERROR: Unknown timezone '{self.timezone}'. Defaulting to UTC.")
            tz = pytz.utc

        now = datetime.now(tz)

        # Check if the current time has passed the last known reset time.
        if self.next_reset_timestamp and now >= self.next_reset_timestamp:
            print(f"INFO: Quota reset time ({self.next_reset_timestamp.isoformat()}) has passed.")
            
            data_changed = False
            if self.rate_limited_keys:
                print(f"INFO: Clearing rate-limited keys: {self.rate_limited_keys}")
                self.rate_limited_keys.clear()
                data_changed = True

            # Reset daily token counts
            print("INFO: Resetting daily token counts for all keys.")
            if isinstance(self.usage_data, dict):
                for key, models_data in self.usage_data.items():
                    if isinstance(models_data, dict):
                        for model, model_data in models_data.items():
                            if isinstance(model_data, dict) and model_data.get("daily_tokens", 0) != 0:
                                model_data["daily_tokens"] = 0
                                data_changed = True
            
            if data_changed:
                self._save_usage_data()
        
        # Reset time is 1 AM in the specified timezone.
        # We use the quota_reset_datetime_str just to get a base date, then apply the time.
        base_datetime = datetime.fromisoformat(self.quota_reset_datetime_str.split(' ')[0])
        reset_time = tz.localize(base_datetime.replace(hour=1, minute=0, second=0, microsecond=0))

        # Find the next reset time
        next_reset = reset_time
        while next_reset <= now:
            next_reset += timedelta(days=1)

        if self.next_reset_timestamp != next_reset:
            self.next_reset_timestamp = next_reset
            print(f"INFO: Next quota reset is scheduled for: {self.next_reset_timestamp.isoformat()}")
            self._save_usage_data()

    def check_and_reset_if_missed(self):
        # This function can be used to implement daily token limits if needed in the future.
        pass

    def _get_total_tokens(self, key, model):
        if not isinstance(key, str):
            return 0
        if key not in self.usage_data or model not in self.usage_data[key]:
            return 0
        # Return the persistent total_tokens count
        return self.usage_data[key][model].get("total_tokens", 0)

    def _is_model_temporarily_disabled(self, key, model):
        """检查特定密钥的特定模型是否被临时禁用"""
        if key not in self.usage_data or model not in self.usage_data[key]:
            return False
        
        model_data = self.usage_data[key][model]
        if not model_data.get("is_temporarily_disabled", False):
            return False
        
        # 检查禁用时间是否已过期
        disabled_until = model_data.get("disabled_until", 0)
        if time.time() > disabled_until:
            # 禁用时间已过期，恢复模型
            model_data["is_temporarily_disabled"] = False
            model_data["disabled_until"] = 0
            model_data["consecutive_429_count"] = 0
            self._save_usage_data()
            print(f"INFO: Model '{model}' on key '{key[:4]}****' has been automatically re-enabled after timeout.")
            return False
        
        return True

    def _disable_model_temporarily(self, key, model, duration=None):
        """临时禁用特定密钥的特定模型"""
        if key not in self.usage_data:
            self.usage_data[key] = {}
        if model not in self.usage_data[key]:
            self.usage_data[key][model] = {
                "usage_records": [],
                "total_tokens": 0,
                "daily_tokens": 0,
                "is_temporarily_disabled": False,
                "disabled_until": 0,
                "last_429_error": 0,
                "consecutive_429_count": 0
            }
        
        model_config = self.models_config.get(model, {})
        disable_duration = duration or model_config.get("disable_duration", 300)
        
        self.usage_data[key][model]["is_temporarily_disabled"] = True
        self.usage_data[key][model]["disabled_until"] = time.time() + disable_duration
        self.usage_data[key][model]["last_429_error"] = time.time()
        
        # 更新model_specific_disabled跟踪
        if key not in self.model_specific_disabled:
            self.model_specific_disabled[key] = []
        if model not in self.model_specific_disabled[key]:
            self.model_specific_disabled[key].append(model)
        
        print(f"INFO: Temporarily disabled model '{model}' on key '{key[:4]}****' for {disable_duration} seconds.")
        self._save_usage_data()

    def _enable_model(self, key, model):
        """启用特定密钥的特定模型"""
        if key in self.usage_data and model in self.usage_data[key]:
            self.usage_data[key][model]["is_temporarily_disabled"] = False
            self.usage_data[key][model]["disabled_until"] = 0
            self.usage_data[key][model]["consecutive_429_count"] = 0
        
        # 从model_specific_disabled中移除
        if key in self.model_specific_disabled and model in self.model_specific_disabled[key]:
            self.model_specific_disabled[key].remove(model)
            if not self.model_specific_disabled[key]:  # 如果列表为空，删除该密钥
                del self.model_specific_disabled[key]
        
        print(f"INFO: Re-enabled model '{model}' on key '{key[:4]}****'.")
        self._save_usage_data()

    def _get_disabled_models_for_key(self, key):
        """获取特定密钥被禁用的模型列表"""
        disabled_models = []
        if key in self.usage_data:
            for model in self.model_order:
                if (model in self.usage_data[key] and 
                    self.usage_data[key][model].get("is_temporarily_disabled", False)):
                    disabled_models.append(model)
        return disabled_models

    def _cleanup_expired_disables(self):
        """清理已过期的禁用状态"""
        current_time = time.time()
        cleaned_keys = []
        
        for key in list(self.model_specific_disabled.keys()):
            models_to_remove = []
            for model in self.model_specific_disabled[key]:
                if (key in self.usage_data and model in self.usage_data[key]):
                    model_data = self.usage_data[key][model]
                    if (model_data.get("is_temporarily_disabled", False) and 
                        current_time > model_data.get("disabled_until", 0)):
                        models_to_remove.append(model)
            
            for model in models_to_remove:
                self._enable_model(key, model)
                cleaned_keys.append((key[:4] + "****", model))
        
        if cleaned_keys:
            for key_masked, model in cleaned_keys:
                print(f"INFO: Cleaned expired disable for model '{model}' on key '{key_masked}'.")
        
        return cleaned_keys

    def _is_key_model_available(self, key, model):
        """检查特定密钥的特定模型是否可用"""
        # A key in rate_limited_keys is only unavailable for the 'gemini-2.5-pro' model.
        # Other models on that key can still be borrowed.
        if model == "gemini-2.5-pro" and key in self.rate_limited_keys:
            return False
        
        # 检查模型是否被临时禁用
        if self._is_model_temporarily_disabled(key, model):
            return False
        
        # 检查token使用量是否超过限制
        model_config = self.models_config.get(model, {})
        tpm_limit = model_config.get("tpm_limit", float('inf'))
        current_usage = self._get_tokens_in_last_minute(key, model)
        
        return current_usage < tpm_limit

    def _get_available_keys_for_model(self, model):
        """获取特定模型的所有可用密钥"""
        available_keys = []
        for key in self.all_keys:
            if self._is_key_model_available(key, model):
                available_keys.append(key)
        return available_keys

    def _check_and_recover_models(self):
        """检查并恢复可用的模型"""
        recovered_models = []
        current_time = time.time()
        
        for key in self.all_keys:
            if key not in self.usage_data:
                continue
                
            for model in self.model_order:
                if model not in self.usage_data[key]:
                    continue
                    
                model_data = self.usage_data[key][model]
                
                # 检查临时禁用的模型是否可以恢复
                if (model_data.get("is_temporarily_disabled", False) and 
                    current_time > model_data.get("disabled_until", 0)):
                    
                    model_data["is_temporarily_disabled"] = False
                    model_data["disabled_until"] = 0
                    model_data["consecutive_429_count"] = 0
                    recovered_models.append((key[:4] + "****", model))
                
                # 检查基于使用量的恢复条件
                elif not model_data.get("is_temporarily_disabled", False):
                    recovery_threshold = self.models_config.get(model, {}).get("recovery_threshold", 0)
                    current_usage = self._get_tokens_in_last_minute(key, model)
                    
                    # 如果使用量降到恢复阈值以下，重置连续错误计数
                    if current_usage < recovery_threshold and model_data.get("consecutive_429_count", 0) > 0:
                        # We no longer reset the counter here. It's reset only on success.
                        # model_data["consecutive_429_count"] = 0
                        recovered_models.append((key[:4] + "****", model + " (usage-based recovery)"))
        
        if recovered_models:
            self._save_usage_data()
            for key_masked, model_info in recovered_models:
                print(f"INFO: Model '{model_info}' on key '{key_masked}' has been recovered.")
        
        return recovered_models

    def _can_model_recover(self, key, model):
        """检查特定模型是否可以恢复"""
        if key not in self.usage_data or model not in self.usage_data[key]:
            return True
        
        model_data = self.usage_data[key][model]
        current_time = time.time()
        
        # 检查时间基础的恢复
        if (model_data.get("is_temporarily_disabled", False) and 
            current_time > model_data.get("disabled_until", 0)):
            return True
        
        # 检查使用量基础的恢复
        recovery_threshold = self.models_config.get(model, {}).get("recovery_threshold", 0)
        current_usage = self._get_tokens_in_last_minute(key, model)
        
        return current_usage < recovery_threshold

    def _prioritize_recovery_models(self, requested_model):
        """根据请求的模型优先级排序恢复检查"""
        recovery_order = []
        
        # 首先检查请求的模型
        if requested_model:
            recovery_order.append(requested_model)
        
        # 然后按照fallback策略检查其他模型
        fallback_models = self._get_model_fallback_order(requested_model or self.config.get('default_model', 'gemini-2.5-pro'))
        for model in fallback_models:
            if model not in recovery_order:
                recovery_order.append(model)
        
        return recovery_order

    def _get_model_recovery_status(self, key, model):
        """获取模型的恢复状态信息"""
        if key not in self.usage_data or model not in self.usage_data[key]:
            return {
                'is_available': True,
                'current_usage': 0,
                'recovery_threshold': self.models_config.get(model, {}).get('recovery_threshold', 0),
                'is_temporarily_disabled': False,
                'disabled_until': 0,
                'consecutive_429_count': 0
            }
        
        model_data = self.usage_data[key][model]
        current_usage = self._get_tokens_in_last_minute(key, model)
        recovery_threshold = self.models_config.get(model, {}).get('recovery_threshold', 0)
        
        return {
            'is_available': self._is_key_model_available(key, model),
            'current_usage': current_usage,
            'recovery_threshold': recovery_threshold,
            'is_temporarily_disabled': model_data.get("is_temporarily_disabled", False),
            'disabled_until': model_data.get("disabled_until", 0),
            'consecutive_429_count': model_data.get("consecutive_429_count", 0)
        }

    def get_key_status(self):
        with self.lock:
            self._check_and_recover_models()
            
            status = {}
            grand_total_tokens = 0
            
            # Define daily quota limit from config, with a default
            daily_quota_limit = self.config.get('daily_quota_limit', 2000000)

            for key in self.all_keys:
                status[key] = {}
                key_daily_total = 0
                for model in self.model_order:
                    total_tokens = self._get_total_tokens(key, model)
                    daily_tokens = self.usage_data.get(key, {}).get(model, {}).get("daily_tokens", 0)
                    grand_total_tokens += total_tokens
                    key_daily_total += daily_tokens

                    basic_status = {
                        'tokens_last_minute': self._get_tokens_in_last_minute(key, model),
                        'total_tokens': total_tokens,
                        'daily_tokens': daily_tokens,
                    }
                    recovery_status = self._get_model_recovery_status(key, model)
                    status[key][model] = {**basic_status, **recovery_status}

                # Check daily quota for the entire key
                status[key]['daily_quota_exceeded'] = key_daily_total > daily_quota_limit if daily_quota_limit else False

            current_key = self.all_keys[self.current_key_index] if self.all_keys and self.current_key_index < len(self.all_keys) else None

            return {
                'current_key': current_key,
                'key_usage_status': status,
                'unavailable_keys': self.unavailable_keys,
                'rate_limited_keys': self.rate_limited_keys,
                'model_specific_disabled': self.model_specific_disabled,
                'model_order': self.model_order,
                'priority_keys': self.priority_keys,
                'secondary_keys': self.secondary_keys,
                'models_config': self.models_config,
                'grand_total_tokens': grand_total_tokens
            }

    # Functions for handling permanently unavailable keys (403 errors, etc.)
    # These are kept from the original file for future use if needed.
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

    def _remove_key_from_pools(self, key_str):
        self.priority_keys = [k for k in self.priority_keys if k != key_str]
        self.secondary_keys = [k for k in self.secondary_keys if k != key_str]
        self.all_keys = [k for k in self.all_keys if k != key_str]

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
