# KeyManager 模块文档

## 概述

`KeyManager` 模块负责管理 API 密钥的生命周期，包括密钥的选择、使用计数、轮换以及处理错误。本次更新主要通过将 `threading.Lock` 替换为 `threading.RLock` 来修复所有潜在的嵌套锁问题，从而增强了模块的线程安全性和稳定性。

## 主要修改

### 1. 嵌套锁问题修复

- **问题描述**: 在之前的代码中，多个方法（如 `reset_all_keys` 和 `_update_next_reset_timestamp`）在已经持有锁的情况下再次尝试获取同一个锁，由于使用的是非重入锁（`threading.Lock`），这会导致线程死锁。
- **解决方案**:
  - 将模块中的锁从 `self.lock = threading.Lock()` 更改为 `self.lock = threading.RLock()`。
  - `threading.RLock` 是一个可重入锁，它允许同一个线程多次获取同一个锁而不会导致死锁，从而从根本上解决了所有嵌套锁问题。

### 2. 锁使用注意事项

- **可重入锁**: 现在使用的是可重入锁，它简化了在复杂调用链中对共享资源的安全访问。
- **线程安全**: 所有对共享数据（如密钥池、使用情况等）的访问仍然必须在 `with self.lock:` 的保护下进行，以确保线程安全。

### 3. 影响

- **线程安全**: 彻底消除了所有已知的嵌套锁和死锁风险，显著提高了模块在多线程环境下的稳定性和可靠性。
- **代码简化**: 无需再使用复杂的状态标志（如 `need_rotation`）来避免锁嵌套，代码逻辑更直接、更易于理解和维护。

## 配置示例

```json
{
    "priority_keys": ["key1", "key2"],
    "secondary_keys": ["key3", "key4"],
    "usage_limit_per_key": 100,
    "switch_threshold": 40,
    "rotation_timeout": 30,
    "low_quota_threshold": 40,
    "quota_reset_datetime": "2025-01-01 00:00",
    "timezone": "America/Los_Angeles",
    "default_model": "gemini-pro"
}
