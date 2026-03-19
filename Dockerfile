FROM debian:bookworm-slim

# 安装系统依赖和 Python 运行环境
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

# pip 相关环境变量
ENV PIP_REQUIRE_HASHES=0
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_RETRIES=10
ENV PIP_PROGRESS_BAR=off

# 创建应用目录
WORKDIR /app

# 先创建虚拟环境
RUN python3 -m venv /opt/venv

# 让后续命令默认使用虚拟环境里的 python / pip
ENV PATH="/opt/venv/bin:$PATH"

# 先复制依赖文件，最大化利用缓存
COPY requirements.txt /app/requirements.txt

# 在虚拟环境里升级 pip 并安装依赖
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --isolated \
       -i https://pypi.tuna.tsinghua.edu.cn/simple \
       -r /app/requirements.txt

# 再复制代码
COPY . /app

# 创建非 root 用户
RUN useradd -m appuser \
    && chown -R appuser:appuser /app /opt/venv

# 切换到非 root 用户运行
USER appuser

EXPOSE 59795

# 启动服务
CMD ["python", "server.py"]
