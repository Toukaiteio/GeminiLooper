# GeminiLooper

## Overview

GeminiLooper is an intelligent reverse proxy for the Google Gemini API. It's designed to manage multiple Gemini API keys, automatically switching to a new key when the current one hits its rate limit (TPM/TPD). This ensures service continuity without manual intervention.

The project features a "sticky key" mechanism, which attempts to reuse the same key for a specific model to leverage the Gemini API's caching, thereby improving response speed and reducing costs.

## Features

- **Automatic API Key Rotation**: Intelligently switches between a pool of API keys when rate limits are detected, maximizing uptime.
- **Priority and Secondary Keys**: Configure primary and fallback keys for granular control.
- **Usage-Aware Load Balancing**: Monitors Tokens Per Minute (TPM) and Tokens Per Day (TPD) for each key and model combination.
- **Rate Limit Throttling**: Proactively introduces small delays when a key is approaching its TPM limit to avoid hitting the hard limit.
- **Live Status Dashboard**: A built-in web interface at `/status` provides a real-time overview of:
    - The currently active API key.
    - Token usage per key and per model.
    - Keys that are currently rate-limited or have exhausted their daily quota.
    - Real-time charts visualizing token usage over the last hour.
- **Persistent Usage Tracking**: Saves usage statistics to `key_usage.json`, so state is maintained across application restarts.
- **Automatic Quota Reset**: Automatically resets token counters based on a configurable daily schedule.
- **Easy Configuration**: All settings are managed in a simple `config.json` file, which is created with default values on the first run.
- **API Key Tester**: An endpoint to test the validity of a Gemini API key.

## How It Works

GeminiLooper runs a local server that acts as a reverse proxy. You send your standard Gemini API requests to this local server instead of directly to `generativelanguage.googleapis.com`.

1.  **Request Interception**: The proxy intercepts requests to `/v1beta/models/:model_name`.
2.  **Key Selection**: The `KeyManager` selects the best available API key from the pool defined in `config.json`. It prioritizes available, non-rate-limited keys.
3.  **Request Forwarding**: The proxy forwards the original request to the Gemini API, adding the selected API key.
4.  **Response Handling**:
    - If the request is successful, the response is streamed back to the client, and the token usage is recorded.
    - If the API returns a `429 Too Many Requests` error, the `KeyManager` marks the key as rate-limited and retries the request with a different key.
5.  **Status Monitoring**: The `KeyManager` continuously tracks usage and updates the status dashboard in real-time.

## Getting Started

### Prerequisites

- Go (version 1.18 or later)

### Installation & Running

1.  **Clone the repository or download the source code.**

2.  **Configure your API keys:**
    On the first run, a `config.json` file will be created automatically. Open it and replace the placeholder keys with your actual Gemini API keys.

    ```json
    {
        "priority_keys": [
            "Your-Priority-Gemini-API-Key-1",
            "Your-Priority-Gemini-API-Key-2"
        ],
        "secondary_keys": [
            "Your-Secondary-Gemini-API-Key-1"
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

3.  **Run the application:**
    Open your terminal in the project directory and run:
    ```bash
    go run .
    ```
    The server will start on port `48888`.

### Usage

Update your application to send requests to the GeminiLooper proxy instead of the official Gemini API endpoint.

-   **Original URL**: `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent`
-   **New URL**: `http://localhost:48888/v1beta/models/gemini-1.5-pro-latest:generateContent`

You no longer need to include the `?key=...` query parameter in your requests, as the proxy handles it.

## API Endpoints

-   **Proxy Endpoint**: `POST /v1beta/models/:model_name`
    -   This is the main endpoint that proxies requests to the Gemini API. `:model_name` can be a model like `gemini-1.5-pro-latest` and can include an action like `:generateContent`.
-   **Status Page**: `GET /status`
    -   View the real-time monitoring dashboard in your browser.
-   **Status Data API**: `GET /api/status_data`
    -   Get the raw JSON data used by the status page.
-   **API Key Tester**: `POST /api/test_key`
    -   Test if a Gemini API key is valid.
    -   **Request Body**:
        ```json
        {
          "api_key": "YOUR_API_KEY_TO_TEST",
          "model_name": "gemini-1.5-pro-latest"
        }
        ```

## Configuration Details

The `config.json` file has the following fields:

-   `priority_keys`: A list of your primary Gemini API keys.
-   `secondary_keys`: A list of fallback keys to use when priority keys are unavailable.
-   `models`: A map of model configurations.
    -   `tpm_limit`: The Tokens-Per-Minute limit for the model.
    -   `tpd_limit`: The Tokens-Per-Day limit for the model. Set to `null` if there is no daily limit.
-   `reset_after`: The time of day (in HH:MM format) to reset the daily token counters.
-   `next_quota_reset_datetime`: (Internal use) Stores the next scheduled reset time.
-   `timezone`: The timezone for the `reset_after` time (e.g., "UTC", "America/Los_Angeles").
-   `default_model`: The model to use if the requested model is not found in the `models` map.