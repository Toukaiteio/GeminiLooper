# GeminiLooper

## 项目简介

GeminiLooper 是一个为 Google Gemini API 设计的智能反向代理。它的核心功能是管理多个 Gemini API 密钥，并在当前密钥达到其速率限制（TPM/TPD）时自动切换到下一个可用的密钥，从而确保服务的连续性，免除手动更换密钥的麻烦。

本项目通过“粘性密钥”机制，会尝试为特定模���复用同一个密钥，以充分利用 Gemini API 的缓存特性，进而提升响应速度并降低成本。

## 主要功能

- **自动密钥轮换**：当检测到速率限制时，在密钥池中智能切换，最大化服务可用时间。
- **优先级与备用密钥**：可配置主密钥和备用密钥，实现更精细的控制。
- **用量感知负载均衡**：监控每个密钥与模型组合的每分钟令牌数（TPM）和每日令牌数（TPD）。
- **速率限制节流**：当某个密钥的用量接近其 TPM 限制时，主动引入微小延迟，以避免触发硬性限制。
- **实时状态面板**：内置的 Web 界面（位于 `/status`）提供以下内容的实时概览：
    - 当前正在使用的 API 密钥。
    - 每个密钥和每个模型的令牌使用情况。
    - 当前被速率限制或已用尽每日配额���密钥。
    - 可视化过去一小时令牌用量的实时图表。
- **持久化用量跟踪**：将用量统计数据保存到 `key_usage.json` 文件中，确保在应用程序重启后状态得以保留。
- **自动配额重置**：根据可配置的每日计划，自动重置令牌计数器。
- **简易配置**：所有设置均通过一个简单的 `config.json` 文件进行管理，该文件在首次运行时会自动创建并填充默认值。
- **API 密钥测试器**：提供一个端点用于测试 Gemini API 密钥的有效性。

## 工作原理

GeminiLooper 在本地运行一个充当反向代理的服务器。您需要将原本直接发送到 `generativelanguage.googleapis.com` 的标准 Gemini API 请求，改为发送到这个本地服务器。

1.  **请求拦截**：代理拦截所有发往 `/v1beta/models/:model_name` 的请求。
2.  **密钥选择**：`KeyManager` 从 `config.json` 中定义的密钥池里选择一个最佳的可用 API 密钥。它会优先选择可用且未被速率限制的密钥。
3.  **请求转发**：代理将原始请求转发到 Gemini API，并附加上选定的 API 密钥。
4.  **响应处理**：
    - 如果请求成功，响应将被流式传输回客户端，并记录本次的令牌使用量。
    - 如果 API 返回 `429 Too Many Requests` 错误，`KeyManager` 会将该密钥标记为受限，并使用另一个密钥重试请求。
5.  **状态监控**：`KeyManager` 持续跟踪使用情况，并实时更新状态面板。

## 快速开始

### 环境要求

- Go (版本 1.18 或更高)

### 安装与运行

1.  **克隆仓库或下载源代码。**

2.  **配置您的 API 密钥：**
    首次运行时，程序会自动创建一个 `config.json` 文件。请打开它，并将占位符密钥替换为您的真实 Gemini API 密钥。

    ```json
    {
        "priority_keys": [
            "你的-优先-Gemini-API-密钥-1",
            "你的-优先-Gemini-API-密钥-2"
        ],
        "secondary_keys": [
            "你的-备用-Gemini-API-密钥-1"
        ],
        "models": {
            "gemini-1.5-pro-latest": {
                "tpm_limit": 250000,
                "tpd_limit": 6000000
            },
            "gemini-1.5-flash-latest": {
                "tpm_limit": 250000,
                "tpd_limit": null
            }
        },
        "reset_after": "01:00",
        "next_quota_reset_datetime": "2025-07-19 01:00",
        "timezone": "UTC",
        "default_model": "gemini-1.5-pro-latest"
    }
    ```

3.  **运行应用：**
    在项目目录中打开终端，并运行：
    ```bash
    go run .
    ```
    服务器将在 `48888` 端口上启动。

### 如何使用

修改您的应用程序，将请求发送到 GeminiLooper 代理，而不是官方的 Gemini API 端点。

-   **原始 URL**: `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent`
-   **新的 URL**: `http://localhost:48888/v1beta/models/gemini-1.5-pro-latest:generateContent`

您不再需要在请求中包含 `?key=...` 查询参数，代理会自动处理。

## API 端点

-   **代理端点**: `POST /v1beta/models/:model_name`
    -   这是代理到 Gemini API 的主要端点。`:model_name` 可以是像 `gemini-1.5-pro-latest` 这样的模型，也可以包含像 `:generateContent` 这样的操作。
-   **状态页面**: `GET /status`
    -   在浏览器中查看实时监控面板。
-   **状态数据 API**: `GET /api/status_data`
    -   获取状态页面使用的原始 JSON 数据。
-   **API 密钥测试器**: `POST /api/test_key`
    -   测试一个 Gemini API 密钥是否有效。
    -   **请求体**:
        ```json
        {
          "api_key": "你要测试的API密钥",
          "model_name": "gemini-1.5-pro-latest"
        }
        ```

## 配置详解

`config.json` 文件包含以下字段：

-   `priority_keys`: 您的主 Gemini API 密钥列表。
-   `secondary_keys`: 当主密钥不可用时使用的备用密钥列表。
-   `models`: 模型配置的映射。
    -   `tpm_limit`: 模型的每分钟令牌数限制。
    -   `tpd_limit`: 模型的每日令牌数限制。如果无每日限制，请设置为 `null`。
-   `reset_after`: 每日重置令牌计数器的时间（格式为 HH:MM）。
-   `next_quota_reset_datetime`: (内部使用) 存储下一次计划的重置时间。
-   `timezone`: `reset_after` 时间所使用的时区（例如 "UTC", "Asia/Shanghai"）。
-   `default_model`: 如果请求的模型在 `models` 映射中未找到，则使用的默认模型。