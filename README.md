# GeminiLooper

A Python-based application with Flask integration for managing Gemini API interactions and configurations, specifically designed to load balance multiple Gemini API keys for users who have several keys.

## Project Structure
```
├── .gitignore
├── .venv/                # Virtual environment
├── __pycache__/
├── app.py                # Main application entry point
├── config.json           # Configuration file (gitignored)
├── config.json.template  # Template for configuration
├── key_manager.py        # Key management utilities
├── requirements.txt      # Project dependencies
├── start_app.bat         # Windows startup script
├── start_app.sh          # Linux/Mac startup script
└── templates/            # HTML templates
    └── prompt_test.html  # Test prompt template
└── start_app.sh          # Linux/Mac startup script
```

## Setup Instructions

### 1. Clone the Repository
```bash
git clone https://github.com/Toukaiteio/GeminiLooper.git
cd GeminiLooper
```

### 2. Configure Application
1. Copy the template configuration file:
```bash
copy config.json.template config.json
```
2. Edit `config.json` with your specific settings

## Running the Application

### On Windows
```bash
start_app.bat
```

### On Linux/Mac
```bash
chmod +x start_app.sh
./start_app.sh
```

## Features
- Gemini API integration
- Configuration management
- Key management utilities
- Automatic virtual environment creation and activation
- Automatic dependency installation
- Cross-platform support (Windows, Linux, Mac)
- Load balancing mechanism for multiple Gemini API keys

## Features
- Gemini API integration
- Configuration management
- Key management utilities