# KeyManager 模块文档

`key_manager.py` 模块负责管理 Google Gemini API 密钥，核心目标是根据每个密钥的每分钟 Token 使用量（TPM）来动态选择可用的模型和密钥，以避免 429 速率限制错误。

## 主要功能

- **基于Token的速率限制**: 不再是计算请求次数，而是跟踪每个密钥在每个模型上过去60秒内使用的总 Token 数。
- **动态模型切换**: 定义了一个模型优先级列表 (`gemini-2.5-pro` > `gemini-2.5-flash` > ...)。当首选模型达到 TPM 限制时，系统会自动切换到下一个可用模型。
- **粘性密钥 (Sticky Key)**: 系统会优先使用当前选定的密钥，直到该密钥对于请求的模型不可用（例如，达到速率限制或遇到连续错误）。
- **智能密钥切换**: 只有在当前密钥不可用时，系统才会寻找并切换到下一个可用的密钥，而不是在多个密钥之间轮询。
- **自动恢复**: 系统会监测 `gemini-2.5-pro` 模型的使用情况。当它因为速率限制而被切换后，系统会在大约60秒后检查其是否恢复可用，并在可用时自动切回。
- **持久化存储**: 所有密钥的 Token 使用历史都保存在 `key_usage.json` 文件中，以便在服务重启后恢复状态。

##核心类：`KeyManager`

### `__init__(self, ...)`

-   **加载配置**: 从 `config.json` 读取密钥列表和模型配置（TPM限制、恢复阈值等）。
-   **加载使用数据**: 从 `key_usage.json` 加载历史 Token 使用记录。
-   **初始化状态**: 设置当前密钥索引和模型切换相关的状态变量。

### `get_model_and_key(self)`

这是模块的核心方法。

1.  **检查恢复**: 首先检查 `gemini-2.5-pro` 是否已从速率限制中恢复。
2.  **遍历模型**: 按照预设的 `model_order` 列表遍历所有模型。
3.  **检查当前密钥**: 首先检查当前激活的密钥是否对请求的模型可用。
4.  **保持或切换**: 如果当前密钥可用，则直接返回；如果不可用，则遍历其他密钥以寻找下一个可用的替代品。
5.  **返回可用组合**: 一旦找到一个可用的模型和密钥组合，立即返回它们。
5.  **返回None**: 如果所有模型和密钥都已达到速率限制，则返回 `(None, None)`。

### `record_token_usage(self, key, model, tokens)`

-   在每次成功的 API 调用后，调用此方法来记录所使用的 Token 数量。
-   记录中包含时间戳，以便计算滑动窗口内的 Token 总量。
-   在 `KeyManager` 初始化时，会全局清理一次所有过期的使用记录。此外，在每次记录新的token使用时，也会对当前key和model的记录进行清理。清理的保留时长由 `usage_record_retention_seconds` 配置项决定。

### `handle_429_error(self, key, model)`

-   当 `app.py` 中发生 429 错误时，会调用此方法。
-   它的主要作用是记录日志，并为 `gemini-2.5-pro` 模型设置一个计时器，以便稍后检查其是否恢复。实际的切换逻辑由下一次调用 `get_model_and_key()` 来处理。

## 配置文件 (`config.json`)

`config.json` 中新增了 `models` 部分和 `usage_record_retention_seconds`，用于定义模型的速率限制和记录保留策略：

```json
"models": {
  "gemini-2.5-pro": {
    "tpm_limit": 250000,
    "tpd_limit": 6000000,
    "recovery_threshold": 80000
  },
  "gemini-2.5-flash": {
    "tpm_limit": 250000,
    "tpd_limit": null,
    "recovery_threshold": 80000
  },
  // ... 其他模型
}
```

-   `tpm_limit`: 每分钟允许的 Token 总数。
-   `tpd_limit`: 每天允许的 Token 总数（当前未使用）。
-   `recovery_threshold`: 当检查模型是否恢复时，使用的较低的 Token 阈值，以提供一个缓冲。
-   `usage_record_retention_seconds` (可选): `usage_records` 中记录的保留时长（秒）。默认值为 `86400` (24小时)。

## 使用数据文件 (`key_usage.json`)

此文件现在存储一个更复杂的数据结构：

```json
{
    "usage_data": {
        "API_KEY_1": {
            "gemini-2.5-pro": [
                {"timestamp": 1678886400.0, "tokens": 1500},
                {"timestamp": 1678886405.0, "tokens": 2500}
            ],
            "gemini-2.5-flash": []
        },
        "API_KEY_2": {}
    },
    "next_reset": "..."
}
