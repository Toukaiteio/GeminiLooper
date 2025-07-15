# 修改报告：实现基于Token的速率限制和动态模型切换

## 1. 概述

本次任务的目标是将原有的基于请求次数的 API 密钥管理系统，重构为基于每分钟 Token 使用量（TPM）的动态管理系统。这使得系统能够更精确地遵守 Google Gemini API 的速率限制，并通过在不同模型和密钥之间自动切换，最大限度地提高服务的可用性。

## 2. 主要修改内容

### 2.1 `config.json`

-   **添加了 `models` 配置项**:
    -   为 `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash`, `gemini-2.0-flash-lite`, 和 `gemini-1.5-flash` 等模型定义了详细的速率限制。
    -   每个模型包含 `tpm_limit` (每分钟Token限制) 和 `recovery_threshold` (用于判断模型是否恢复的较低阈值)。

### 2.2 `key_manager.py` (核心重构)

-   **数据结构变更**:
    -   `key_usage.json` 的数据结构从记录简单的使用次数，变更为记录每个密钥在每个模型上带时间戳的 Token 使用历史，例如：`{key: {model: [{'timestamp': ts, 'tokens': num}]}}`。
-   **核心逻辑重写**:
    -   **`get_model_and_key()`**: 替换了旧的 `get_key()`。此函数现在是系统的决策核心，它会根据预设的模型顺序和每个密钥的实时 TPM 使用情况，智能地选择下一个可用的模型和密钥组合。
    -   **`record_token_usage()`**: 新增函数，用于在每次成功请求后，记录所消耗的 Token 数量和时间戳。
    -   **滑动窗口计算**: 内部实现了 `_get_tokens_in_last_minute()`，用于实时计算任何密钥在任何模型上过去60秒的 Token 总量。
    -   **自动恢复机制**: 特别针对 `gemini-2.5-pro` 模型，实现了一个机制，在因速率限制被切换后，能在大约60秒后自动检查其是否恢复，并在可用时切回。

### 2.3 `app.py` (集成新逻辑)

-   **重写代理逻辑**:
    -   `/` 代理路由现在包含一个重试循环 (`while retries < MAX_RETRIES`)。
    -   在循环的每次迭代中，都会调用 `key_manager.get_model_and_key()` 来获取最新的可用资源。
    -   当收到 `429` 错误时，不会立即失败，而是触发 `key_manager.handle_429_error()` 并进入下一次循环，从而实现无缝的模型/密钥切换。
-   **Token 解析**:
    -   在收到成功的 `200` 响应后，应用会解析响应体，从 `usageMetadata` 或 `candidates` 中提取 `totalTokenCount`。
    -   然后调用 `key_manager.record_token_usage()` 来更新使用记录。
-   **状态页更新**: `/status` 路由现在会显示每个密钥在所有受管模型上的实时分钟级 Token 使用情况。

### 2.4 `templates/`

-   **`prompt_test.html`**:
    -   更新了模型下拉列表，以包含所有新支持的模型。
    -   简化了页面功能，使其专注于发送 prompt，因为复杂的密钥测试和状态显示功能已移至 `/status` 页面和后端逻辑中。
-   **`status.html`**: (隐式修改，通过 `app.py` 传递了新的数据结构)
    -   现在能够展示更详细的、基于 Token 的使用状态。

## 3. 文档更新

-   **`doc/zh/key_manager.md`**: 完全重写，以反映新的基于 Token 的速率限制、动态模型切换和自动恢复机制。
-   **`doc/zh/app.md`**: 新建文档，详细描述了 Flask 应用如何与新的 `KeyManager` 集成，特别是其自动重试和故障转移的代理逻辑。

## 4. 总结

通过以上修改，系统现在能够更加智能和高效地利用可用的 API 资源。它不再因为单个模型的速率限制而中断服务，而是通过灵活的降级和恢复策略，确保了服务的最大连续性和鲁棒性。
