# GeminiLooper

一个基于Python的应用程序，集成了Flask，用于管理Gemini API交互和配置，专为拥有多个Gemini API密钥的用户设计，可实现多个API密钥的负载均衡使用。

## 项目结构
```
├── .gitignore
├── .venv/                # 虚拟环境
├── __pycache__/
├── app.py                # 主应用入口点
├── config.json           # 配置文件（已忽略git跟踪）
├── config.json.template  # 配置模板
├── key_manager.py        # 密钥管理工具
├── requirements.txt      # 项目依赖
├── start_app.bat         # Windows启动脚本
├── start_app.sh          # Linux/Mac启动脚本
└── templates/            # HTML模板
    └── prompt_test.html  # 测试提示模板
└── start_app.sh          # Linux/Mac启动脚本
```

## 安装说明

### 1. 克隆仓库
```bash
git clone https://github.com/Toukaiteio/GeminiLooper.git
cd GeminiLooper
```

### 2. 配置应用
1. 复制模板配置文件：
```bash
copy config.json.template config.json
```
2. 编辑`config.json`并添加您的特定设置

## 运行应用

### 在Windows上
```bash
start_app.bat
```

### 在Linux/Mac上
```bash
chmod +x start_app.sh
./start_app.sh
```

## 功能特点
- Gemini API集成
- 配置管理
- 密钥管理工具
- 自动创建和激活虚拟环境
- 自动安装依赖
- 跨平台支持（Windows、Linux、Mac）
- 多个Gemini API密钥的负载均衡机制