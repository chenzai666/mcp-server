FROM python:3.11-slim

# 安装系统依赖：
# 1. tesseract-ocr：本地 OCR 引擎
# 2. tesseract-ocr-eng / tesseract-ocr-chi-sim：英文与简体中文语言包
# 3. curl：方便容器内做简单调试
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements，利用 Docker 分层缓存加速重建
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 再复制项目代码
COPY server.py /app/server.py

EXPOSE 59795

# 直接启动 SSE 版 MCP 服务
CMD ["python", "/app/server.py"]
