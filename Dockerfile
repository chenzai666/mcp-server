# syntax=docker/dockerfile:1
FROM debian:bookworm-slim

# 安装系统依赖和 Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    curl \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-eng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只复制依赖文件
COPY requirements.txt /app/requirements.txt

# 先安装依赖，这一层最值得缓存
RUN pip3 install --no-cache-dir -r /app/requirements.txt --break-system-packages

# 最后再复制业务代码
COPY . /app

EXPOSE 59795
CMD ["python3", "server.py"]
