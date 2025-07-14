# KeyManager 模块文档

## 概述

`KeyManager` 模块负责管理 API 密钥的生命周期，包括密钥的选择、使用计数、轮换以及处理错误。本次更新主要修改了密钥使用配额的重置逻辑，从保存上次重置时间改为保存下次重置时间，并确保重置时间为每天的美国太平洋时间（UTC-7）的0点。

## 主要修改

### 1. 重置时间逻辑变更

- **旧逻辑**: `KeyManager` 内部使用 `last_reset_timestamp` 记录上次密钥使用配额重置的时间。
- **新逻辑**: `KeyManager` 内部现在使用 `next_reset_timestamp` 记录下一次密钥使用配额重置的时间。这使得重置逻辑更加清晰和可预测。

### 2. 重置时间点

- 密钥使用配额的重置时间现在通过 `config.json` 中的 `quota_reset_datetime` 字段指定，该字段应包含完整的日期和时间信息（例如 `YYYY-MM-DD HH:MM`）。
- `config.json` 中的 `timezone` 配置项应设置为正确的时区（例如 `America/Los_Angeles`）以确保时区正确。
- 系统将根据 `quota_reset_datetime` 和 `timezone` 计算下一次重置的具体时间。

### 3. 相关方法更新

以下方法已根据新的重置逻辑进行了修改：

- `__init__(self, ...)`: 初始化时加载 `next_reset_timestamp`，并确保在实例创建时调用 `_update_next_reset_timestamp()` 来设置正确的下次重置时间。
- `_load_usage_data(self, current_api_keys)`: 从 `key_usage.json` 文件中加载 `next_reset` 时间戳。
- `_save_usage_data_internal(self, data_to_save)`: 将 `next_reset` 时间戳保存到 `key_usage.json` 文件中。
- `reset_all_keys(self)`: 在重置所有密钥使用计数后，立即调用 `_update_next_reset_timestamp` 方法来计算并设置下一次重置的时间。
- `get_key_status(self)`: 返回的密钥状态信息中包含了 `next_reset` 时间戳。
- `_update_next_reset_timestamp(self)`: **新增方法**。此方法负责根据当前时间、配置的时区和重置时间点，计算出下一次重置的具体时间，并更新 `self.next_reset_timestamp`。
- `check_and_reset_if_missed(self)`: 根据 `self.next_reset_timestamp` 判断是否需要执行重置操作。如果当前时间已超过 `next_reset_timestamp`，则执行重置并更新 `next_reset_timestamp`。

## 配置示例

为了使新的重置逻辑生效，请确保 `config.json` 文件包含以下配置：

```json
{
    "timezone": "America/Los_Angeles",
    "quota_reset_datetime": "2025-01-01 00:00"
}
```

## 影响

- **更精确的重置**: 通过保存下次重置时间，系统可以更精确地在预定时间点执行重置，避免了因系统时间漂移或服务重启导致的重置不准确问题。
- **时区统一**: 强制使用美国太平洋时间（UTC-7）的0点作为重置点，确保了所有密钥在同一逻辑时间点进行配额重置。
- **可观测性增强**: `get_key_status` 方法现在提供了 `next_reset` 时间戳，方便监控下一次重置的具体时间。
