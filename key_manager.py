import json
import threading
import hashlib
import os
from datetime import datetime, timedelta
import pytz
from collections import defaultdict

class KeyManager:
    def __init__(self, config_path='config.json', usage_file='key_usage.json', unavailable_file='unavailable.json'):
        self.config_path = config_path
        self.usage_file = usage_file
        self.unavailable_file = unavailable_file
        
        with open(config_path) as f:
            config = json.load(f)
        
        self.usage_limit = config.get('usage_limit_per_key', 100) # Default limit
        self.lock = threading.Lock()

        # 从文件中加载潜在不可用和已不可用的密钥
        self.potential_unavailable = self._load_potential_unavailable()
        self.unavailable_keys = self._load_unavailable_keys()

        # 从配置中加载所有密钥，并排除已永久不可用的密钥
        available_keys = [k for k in config['api_keys'] if k not in self.unavailable_keys]
        
        # 加载使用数据
        self.keys = self._load_usage_data(available_keys)

    def get_key(self):
        with self.lock:
            # 筛选出活跃且不在潜在不可用列表中的key
            active_keys = [k for k in self.keys if k['active'] and k['key'] not in self.potential_unavailable]
            if not active_keys:
                # 如果没有完全活跃的key，则尝试使用潜在不可用的key
                potential_keys = [k for k in self.keys if k['active'] and k['key'] in self.potential_unavailable]
                if not potential_keys:
                    return None
                # 返回潜在不可用key中使用次数最少的
                return min(potential_keys, key=lambda k: k['usage'])

            # 返回活跃key中使用次数最少的
            return min(active_keys, key=lambda k: k['usage'])

    def increment_usage(self, key_str):
        with self.lock:
            for key_info in self.keys:
                if key_info['key'] == key_str:
                    key_info['usage'] += 1
                    if key_info['usage'] >= self.usage_limit:
                        key_info['active'] = False
                    
                    # 如果密钥在潜在不可用列表中，成功调用后将其移除（重置错误计数）
                    if key_str in self.potential_unavailable:
                        del self.potential_unavailable[key_str]
                        self._save_unavailable_data()
                    
                    self._save_usage_data() # 保存使用情况
                    break

    def reset_all_keys(self):
        with self.lock:
            print("Resetting all API key usage counts.")
            for key_info in self.keys:
                key_info['usage'] = 0
                key_info['active'] = True
            self._save_usage_data()

    def get_key_status(self):
        with self.lock:
            # 将潜在不可用key的错误信息附加到key状态中
            keys_with_status = []
            for k in self.keys:
                key_copy = k.copy()
                if k['key'] in self.potential_unavailable:
                    key_copy['403_errors'] = self.potential_unavailable[k['key']]
                keys_with_status.append(key_copy)

            return {
                'active_keys': keys_with_status,
                'potential_unavailable': self.potential_unavailable,
                'unavailable_keys': self.unavailable_keys
            }

    def handle_403_error(self, key_str):
        with self.lock:
            # 打印屏蔽后的key
            masked_key = f"{key_str[:4]}****{key_str[-6:]}"
            print(f"403 Error reported for key: {masked_key}")

            # 获取当前错误次数，如果不存在则为0，然后加1
            current_errors = self.potential_unavailable.get(key_str, 0) + 1
            
            if current_errors >= 3:
                print(f"Key {masked_key} has reached 3 errors and will be permanently disabled.")
                # 从潜在不可用列表中移除
                if key_str in self.potential_unavailable:
                    del self.potential_unavailable[key_str]
                
                # 添加到永久不可用列表
                if key_str not in self.unavailable_keys:
                    self.unavailable_keys.append(key_str)
                
                # 从主密钥列表中移除
                self.keys = [k for k in self.keys if k['key'] != key_str]
                
                # 从配置文件中移除
                self._remove_key_from_config(key_str)
                
            else:
                # 更新潜在不可用列表中的错误次数
                print(f"Key {masked_key} now has {current_errors} error(s).")
                self.potential_unavailable[key_str] = current_errors
            
            # 立即保存状态
            self._save_unavailable_data()

    def _hash_key(self, key_str):
        return hashlib.sha256(key_str.encode()).hexdigest()

    def _load_usage_data(self, current_api_keys):
        if not os.path.exists(self.usage_file):
            print(f"'{self.usage_file}' not found. Creating a new one.")
            new_keys = [{"key": key, "usage": 0, "active": True} for key in current_api_keys]
            self._save_usage_data_internal(new_keys) # 首次创建时直接保存
            return new_keys

        with open(self.usage_file, 'r') as f:
            try:
                stored_data = json.load(f)
            except json.JSONDecodeError:
                stored_data = []

        hash_to_key = {self._hash_key(key): key for key in current_api_keys}
        synced_keys = []
        
        # 使用集合来跟踪已处理的哈希，以提高效率
        processed_hashes = set()

        ast = pytz.timezone('America/Los_Angeles') # 太平洋时间
        now_utc = datetime.now(pytz.utc)

        for item in stored_data:
            key_hash = item.get('key_hash')
            if key_hash in hash_to_key:
                key = hash_to_key[key_hash]
                next_update = datetime.fromisoformat(item['next_update']).astimezone(pytz.utc)
                
                usage = 0 if now_utc >= next_update else item['usage']
                
                synced_keys.append({
                    "key": key,
                    "usage": usage,
                    "active": usage < self.usage_limit
                })
                processed_hashes.add(key_hash)

        # 添加在配置文件中但不在使用文件中的新密钥
        for key in current_api_keys:
            if self._hash_key(key) not in processed_hashes:
                synced_keys.append({"key": key, "usage": 0, "active": True})
        
        return synced_keys

    def _load_potential_unavailable(self):
        if not os.path.exists(self.unavailable_file):
            return {}
        with open(self.unavailable_file, 'r') as f:
            try:
                data = json.load(f)
                # 将列表转换为字典以便快速查找
                return {item['key']: item['errors'] for item in data.get('potential_unavailable', [])}
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
            'potential_unavailable': [{'key': k, 'errors': v} for k, v in self.potential_unavailable.items()],
            'unavailable': self.unavailable_keys
        }
        temp_file = self.unavailable_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=4)
        os.replace(temp_file, self.unavailable_file)

    def _remove_key_from_config(self, key_str):
        with open(self.config_path, 'r+') as f:
            config = json.load(f)
            if key_str in config.get('api_keys', []):
                config['api_keys'].remove(key_str)
                f.seek(0)
                f.truncate()
                json.dump(config, f, indent=2)

    def _save_usage_data(self):
        """公共保存接口，确保 self.keys 是最新的"""
        self._save_usage_data_internal(self.keys)

    def _save_usage_data_internal(self, keys_to_save):
        """内部保存方法，用于将密钥使用数据写入文件"""
        ast = pytz.timezone('America/Los_Angeles') # 太平洋时间
        now_ast = datetime.now(ast)
        
        # 计算下次更新时间（太平洋时间明天0点）
        next_update_ast = (now_ast + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        
        data_to_save = []
        for key_info in keys_to_save:
            data_to_save.append({
                "key_hash": self._hash_key(key_info['key']),
                "usage": key_info['usage'],
                "last_saved": datetime.now(pytz.utc).isoformat(),
                "next_update": next_update_ast.isoformat()
            })

        temp_file = self.usage_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(data_to_save, f, indent=4)
        os.replace(temp_file, self.usage_file)
