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

# pip 网络相关设置：
# 1) 增大超时
# 2) 增加重试次数
# 3) 关闭进度条，减少长连接中断概率
# 4) 关闭版本检查
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_RETRIES=10
ENV PIP_PROGRESS_BAR=off
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_REQUIRE_HASHES=0

WORKDIR /app

# 先复制依赖文件，最大化利用缓存
COPY requirements.txt /app/requirements.txt

# 使用镜像源安装 Python 依赖；如果你想只用官方源，我下面也给了替换命令
RUN python3 -m pip install --upgrade pip && python3 -m pip install --upgrade pip setuptools wheel --break-system-packages \
    && python3 -m pip install --no-cache-dir --isolated \
       -i https://pypi.tuna.tsinghua.edu.cn/simple \
       -r /app/requirements.txt \
       --break-system-packages

# 再复制代码
COPY . /app

EXPOSE 59795
CMD ["python3", "server.py"]
