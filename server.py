import os
import io
import urllib.parse
import requests
import uvicorn
import pytesseract
from PIL import Image
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

# ================= 1. 基础配置 =================
ADMIN_TOKEN = "Bearer mcp_admin_secret_2026"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
mcp = FastMCP("Web-Tools-Server")

# ================= 2. 基础 Web 工具 (保留) =================
@mcp.tool()
def web_fetch(url: str) -> str:
    """网页内容读取 (基础版)"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return BeautifulSoup(r.text, 'html.parser').get_text(separator='\n', strip=True)
    except Exception as e: return f"Error: {e}"

@mcp.tool()
def web_extract_links(url: str) -> str:
    """提取网页链接"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        links = [f"- [{a.get_text(strip=True)[:50]}]({urllib.parse.urljoin(url, a['href'])})" for a in soup.find_all('a', href=True)]
        return "\n".join(links) if links else "No links found."
    except Exception as e: return f"Error: {e}"

# ================= 3. 高阶聚合与深度工具 (新增) =================

@mcp.tool()
def searxng_search(query: str, max_results: int = 5) -> str:
    """使用本地 SearXNG 引擎进行聚合搜索 (取代普通联网搜索)"""
    try:
        # 默认调用 docker-compose 中配置的内网 searxng
        searxng_url = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")
        r = requests.get(searxng_url, params={"q": query, "format": "json"}, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])[:max_results]
        return "\n---\n".join([f"Title: {res.get('title')}\nURL: {res.get('url')}\nSnippet: {res.get('content')}" for res in results]) if results else "No results."
    except Exception as e:
        return f"SearXNG Error: {e}"

@mcp.tool()
def jina_reader(url: str) -> str:
    """使用 Jina Reader 进行网页深度沉浸式提取 (支持绕过反爬、清洗正文)"""
    try:
        jina_key = os.environ.get("JINA_API_KEY")
        headers = {"Authorization": f"Bearer {jina_key}"} if jina_key else {}
        r = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        return f"Jina Reader Error: {e}"

@mcp.tool()
def local_ocr(image_url: str) -> str:
    """使用本地 Tesseract 引擎提取图片中的中英文文字"""
    try:
        r = requests.get(image_url, stream=True, timeout=15)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        # 强制支持英文 (eng) 和简体中文 (chi_sim)
        text = pytesseract.image_to_string(img, lang='eng+chi_sim')
        return text.strip() or "No text found in image."
    except Exception as e:
        return f"Local OCR Error: {e}"

@mcp.tool()
def jina_vision(image_url: str) -> str:
    """使用 Jina Reader Vision 模式分析和理解图片内容细节"""
    try:
        jina_key = os.environ.get("JINA_API_KEY")
        headers = {"Authorization": f"Bearer {jina_key}"} if jina_key else {}
        # Jina 的视觉模式目前与普通模式共用入口，只要传入图片 URL 即可自动触发 VLM 模型
        r = requests.get(f"https://r.jina.ai/{image_url}", headers=headers, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        return f"Jina Vision Error: {e}"

# ================= 4. 传输层与鉴权 =================
transport = SseServerTransport("/messages/")

async def handle_sse(request: Request):
    async with transport.connect_sse(request.scope, request.receive, request._send) as (in_stream, out_stream):
        await mcp._mcp_server.run(in_stream, out_stream, mcp._mcp_server.create_initialization_options())

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health": return await call_next(request)
        if request.headers.get("Authorization") != ADMIN_TOKEN:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized."})
        return await call_next(request)

# ================= 5. 启动服务 =================
app = FastAPI(title="MCP Advanced Web OSINT Server")
app.add_middleware(AuthMiddleware)

@app.get("/health")
def health_check(): 
    return {"status": "ok", "port": 59795, "features": ["searxng", "jina", "ocr"]}

app.add_route("/sse", handle_sse, methods=["GET"])
app.mount("/messages/", transport.handle_post_message)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=59795)
