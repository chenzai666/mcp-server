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

# 显式关闭 pip 的 hash 强校验，避免宿主机或基础环境继承的配置影响构建
ENV PIP_REQUIRE_HASHES=0

# 关闭 pip 版本检查，减少无意义输出
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# 更新 pip 到最新版本
RUN python3 -m pip install --upgrade pip

# 添加非 root 用户，避免权限问题
RUN useradd -m myuser
USER myuser

# 设置工作目录
WORKDIR /app

# 先复制依赖文件，最大化利用缓存
COPY requirements.txt /app/requirements.txt

# 升级 pip 基础工具，清理缓存，再安装项目依赖
RUN python3 -m pip install --upgrade pip setuptools wheel --break-system-packages \
    && python3 -m pip install --no-cache-dir --isolated \
       -i https://pypi.tuna.tsinghua.edu.cn/simple \
       -r /app/requirements.txt \
       --break-system-packages

# 再复制代码
COPY . /app

EXPOSE 59795
CMD ["python3", "server.py"]
