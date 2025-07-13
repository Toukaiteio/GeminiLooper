#!/bin/bash

# 切换到项目目录
cd "$(dirname "$0")"

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Virtual environment does not exist, creating..."
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "Failed to create virtual environment. Please make sure Python3 is installed and added to environment variables."
        exit 1
    fi
    echo "Virtual environment created successfully"
fi

# Activate virtual environment
source ./.venv/bin/activate
if [ $? -ne 0 ]; then
    echo "Failed to activate virtual environment"
    exit 1
fi

# 安装依赖
pip install -r requirements.txt

# 运行Python应用
python app.py