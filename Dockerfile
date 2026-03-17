FROM python:3.11-slim

# 安装 Tesseract OCR 引擎及其英文、简体中文语言包
RUN apt-get update && \
    apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash mcpadmin
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
RUN chown -R mcpadmin:mcpadmin /app
USER mcpadmin
EXPOSE 59795
CMD ["python", "server.py"]
