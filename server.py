import io
import logging
import os
import re
import secrets
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import pytesseract
import requests
import uvicorn
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from markdownify import markdownify as html_to_markdown
from fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from PIL import Image
from pypdf import PdfReader
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from youtube_transcript_api import YouTubeTranscriptApi

# ------------------------------
# 基础配置
# ------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mcp-server")

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
PORT = int(os.getenv("PORT", "59795"))

# ADMIN_TOKEN 加载逻辑：环境变量 > 持久化文件 > 自动生成并写入文件
_TOKEN_FILE = os.getenv("TOKEN_FILE", "/data/admin_token")
_token_auto_generated = False

def _load_or_generate_token() -> str:
    env_token = os.getenv("ADMIN_TOKEN", "").strip()
    if env_token:
        return env_token
    if os.path.isfile(_TOKEN_FILE):
        token = open(_TOKEN_FILE).read().strip()
        if token:
            return token
    token = f"Bearer {secrets.token_urlsafe(32)}"
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w") as f:
        f.write(token)
    global _token_auto_generated
    _token_auto_generated = True
    return token

ADMIN_TOKEN = _load_or_generate_token()

# 搜索后端相关配置
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:18080/search")
SEARCH_BACKENDS = [item.strip() for item in os.getenv("SEARCH_BACKENDS", "tavily,searxng,duckduckgo").split(",") if item.strip()]
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
TAVILY_TOPIC = os.getenv("TAVILY_TOPIC", "general").strip() or "general"

# Jina 相关配置，可选
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()

# OCR 相关配置
OCR_LANG = os.getenv("OCR_LANG", "eng+chi_sim")
OCR_BACKEND = os.getenv("OCR_BACKEND", "paddleocr").strip().lower()
PADDLEOCR_LANG = os.getenv("PADDLEOCR_LANG", "ch").strip()

# 百度 OCR 配置
BAIDU_OCR_API_KEY = os.getenv("BAIDU_OCR_API_KEY", "").strip()
BAIDU_OCR_SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "").strip()
_baidu_access_token = None
_baidu_token_expire_time = 0

# 外部视觉服务配置，可选
VISION_API_URL = os.getenv("VISION_API_URL", "").strip()
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip()

# PaddleOCR 实例（延迟加载）
_paddleocr_instance = None

# GitHub 可选 Token，提高匿名访问速率限制
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

HEADERS = {"User-Agent": USER_AGENT}

mcp = FastMCP("Web-Tools-Server")
# SSE 模式下，客户端通过 /sse 建立长连接，再通过 /messages/ 发起请求
transport = SseServerTransport("/messages/")
# Streamable HTTP 模式：单端点 /mcp，支持 Claude Code 等新版客户端
session_manager = StreamableHTTPSessionManager(
    app=mcp._mcp_server,
    event_store=None,
    json_response=False,
    stateless=False,
)


def _get_baidu_access_token():
    """获取百度 OCR access_token，自动刷新。"""
    global _baidu_access_token, _baidu_token_expire_time
    import time
    
    if not BAIDU_OCR_API_KEY or not BAIDU_OCR_SECRET_KEY:
        return None
    
    if _baidu_access_token and time.time() < _baidu_token_expire_time:
        return _baidu_access_token
    
    try:
        response = requests.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={
                "grant_type": "client_credentials",
                "client_id": BAIDU_OCR_API_KEY,
                "client_secret": BAIDU_OCR_SECRET_KEY,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        _baidu_access_token = data.get("access_token")
        _baidu_token_expire_time = time.time() + data.get("expires_in", 86400) - 300
        logger.info("Baidu OCR access token refreshed")
        return _baidu_access_token
    except Exception as exc:
        logger.warning(f"Failed to get Baidu OCR token: {exc}")
        return None


def _baidu_ocr(img_bytes: bytes, language_type: str = "CHN_ENG") -> str:
    """调用百度 OCR API 识别图片。"""
    import base64
    import urllib.parse
    
    token = _get_baidu_access_token()
    if not token:
        raise RuntimeError("Baidu OCR not configured or token fetch failed")
    
    img_base64 = base64.b64encode(img_bytes).decode("utf-8")
    response = requests.post(
        f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic?access_token={token}",
        data={"image": img_base64, "language_type": language_type},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    
    if "error_code" in result:
        raise RuntimeError(f"Baidu OCR error: {result.get('error_msg', result)}")
    
    words = result.get("words_result", [])
    return "\n".join(item.get("words", "") for item in words)


def _get_paddleocr():
    """延迟加载 PaddleOCR 实例。"""
    global _paddleocr_instance
    if _paddleocr_instance is None and OCR_BACKEND == "paddleocr":
        try:
            from paddleocr import PaddleOCR
            _paddleocr_instance = PaddleOCR(
                lang=PADDLEOCR_LANG,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                show_log=False,
            )
            logger.info(f"PaddleOCR initialized with lang={PADDLEOCR_LANG}")
        except Exception as exc:
            logger.warning(f"Failed to initialize PaddleOCR: {exc}")
            _paddleocr_instance = False
    return _paddleocr_instance if _paddleocr_instance else None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _request(url: str, **kwargs) -> requests.Response:
    """统一的 GET 请求封装，网络错误自动重试最多 3 次（指数退避）。"""
    merged_headers = dict(HEADERS)
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        merged_headers.update(extra_headers)
    return requests.get(url, headers=merged_headers, timeout=TIMEOUT, **kwargs)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    """统一的 JSON POST 请求封装，网络错误自动重试最多 3 次（指数退避）。"""
    merged_headers = dict(HEADERS)
    merged_headers["Content-Type"] = "application/json"
    if headers:
        merged_headers.update(headers)
    return requests.post(url, json=payload, headers=merged_headers, timeout=TIMEOUT)


def _clean_html(html: str) -> BeautifulSoup:
    """把 HTML 清理后转成 BeautifulSoup，便于复用。"""
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "noscript", "iframe"]):
        bad.decompose()
    return soup


def _clean_text(html: str) -> str:
    """抽取网页文本正文。"""
    return _clean_html(html).get_text(separator="\n", strip=True)


def _normalize_whitespace(text: str) -> str:
    """压缩连续空白字符，避免返回内容太乱。"""
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, max_chars: int = 50000) -> str:
    """统一截断返回内容，避免响应过大。"""
    text = text or ""
    return text[:max_chars]


def _absolute_url(base_url: str, href: str) -> str:
    """把相对链接转成绝对链接。"""
    return urllib.parse.urljoin(base_url, href)


def _html_to_markdown(html: str) -> str:
    """把 HTML 转成更适合模型阅读的 Markdown。"""
    soup = _clean_html(html)
    markdown = html_to_markdown(str(soup), heading_style="ATX")
    return _normalize_whitespace(markdown)


def _extract_common_metadata(html: str, url: str) -> Dict[str, str]:
    """提取常见 meta / OpenGraph / Twitter 元信息。"""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    metas: Dict[str, str] = {}
    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name") or meta.get("http-equiv")
        value = meta.get("content")
        if key and value:
            metas[key.strip()] = value.strip()

    icon = ""
    icon_tag = soup.find("link", rel=lambda x: x and "icon" in str(x).lower())
    if icon_tag and icon_tag.get("href"):
        icon = _absolute_url(url, icon_tag["href"])

    result = {
        "title": title,
        "description": metas.get("description", ""),
        "keywords": metas.get("keywords", ""),
        "author": metas.get("author", ""),
        "og:title": metas.get("og:title", ""),
        "og:description": metas.get("og:description", ""),
        "og:url": metas.get("og:url", ""),
        "og:image": metas.get("og:image", ""),
        "twitter:title": metas.get("twitter:title", ""),
        "twitter:description": metas.get("twitter:description", ""),
        "twitter:image": metas.get("twitter:image", ""),
        "favicon": icon,
    }
    return result


def _format_search_results(results: List[Dict[str, str]]) -> str:
    """把标准化后的搜索结果列表渲染成可读文本。"""
    if not results:
        return "No results."

    blocks = []
    for item in results:
        blocks.append(
            "\n".join(
                [
                    f"Title: {item.get('title', '')}",
                    f"URL: {item.get('url', '')}",
                    f"Snippet: {item.get('snippet', '')}",
                    f"Source: {item.get('source', '')}",
                ]
            )
        )
    return "\n---\n".join(blocks)


# ------------------------------
# 搜索后端：SearXNG
# ------------------------------
def search_searxng(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """通过 SearXNG 检索网页。"""
    response = requests.get(
        SEARXNG_URL,
        params={"q": query, "format": "json"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("results", [])[:max_results]
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "source": "searxng",
        }
        for item in results
    ]


# ------------------------------
# 搜索后端：DuckDuckGo HTML（无需 API Key）
# ------------------------------
def search_duckduckgo_html(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """DuckDuckGo HTML 搜索，无需 API Key。"""
    response = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []
    for node in soup.select(".result"):
        link = node.select_one(".result__title a")
        snippet = node.select_one(".result__snippet")
        if not link:
            continue
        href = link.get("href", "")
        results.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": href,
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
                "source": "duckduckgo",
            }
        )
        if len(results) >= max_results:
            break
    return results


# ------------------------------
# 搜索后端：Tavily
# ------------------------------
def search_tavily(query: str, max_results: int = 5, topic: str = "general") -> List[Dict[str, str]]:
    """通过 Tavily API 搜索。未配置 API Key 时直接报错，由上层决定是否回退。"""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY 未配置")

    payload = {
        "query": query,
        "topic": topic,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
    }
    response = _post_json(
        "https://api.tavily.com/search",
        payload,
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
    )
    response.raise_for_status()
    data = response.json()
    results: List[Dict[str, str]] = []
    for item in data.get("results", [])[:max_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "source": "tavily",
            }
        )
    return results


def tavily_extract_urls(urls: List[str]) -> str:
    """调用 Tavily Extract 抽取指定 URL 内容。"""
    if not TAVILY_API_KEY:
        return "Tavily 未启用：请先配置 TAVILY_API_KEY。"
    response = _post_json(
        "https://api.tavily.com/extract",
        {"urls": urls, "extract_depth": "basic"},
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
    )
    response.raise_for_status()
    return _truncate(_normalize_whitespace(str(response.json())), 50000)




# ------------------------------
# 统一搜索入口
# ------------------------------
def perform_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """统一搜索入口，按 SEARCH_BACKENDS 的顺序依次尝试。"""
    errors: List[str] = []
    for backend in SEARCH_BACKENDS:
        try:
            if backend == "tavily":
                results = search_tavily(query, max_results=max_results, topic=TAVILY_TOPIC)
            elif backend == "searxng":
                results = search_searxng(query, max_results=max_results)
            elif backend in {"duckduckgo", "duckduckgo-html", "ddg"}:
                results = search_duckduckgo_html(query, max_results=max_results)
            else:
                errors.append(f"未知搜索后端: {backend}")
                continue

            if results:
                return results
        except Exception as exc:
            errors.append(f"{backend}: {exc}")

    raise RuntimeError("; ".join(errors) or "没有可用的搜索后端")


# ------------------------------
# YouTube 工具函数
# ------------------------------
def _youtube_video_id(url_or_id: str) -> str:
    """从 YouTube 链接或视频 ID 中提取 video_id。"""
    text = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text

    parsed = urllib.parse.urlparse(text)
    if parsed.netloc in {"youtu.be"}:
        return parsed.path.strip("/")[:11]
    if "youtube.com" in parsed.netloc:
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0][:11]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1][:11]

    raise ValueError("无法识别 YouTube 视频 ID")


# ------------------------------
# GitHub 工具函数
# ------------------------------
def _github_headers() -> Dict[str, str]:
    """GitHub API 请求头。"""
    headers = dict(HEADERS)
    headers["Accept"] = "application/vnd.github+json"
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


# ------------------------------
# 多源聚合搜索
# ------------------------------
def _search_wikipedia(query: str, max_results: int = 3) -> List[Dict[str, str]]:
    """使用 Wikipedia OpenSearch 接口做百科检索。"""
    response = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "opensearch",
            "search": query,
            "limit": max_results,
            "namespace": 0,
            "format": "json",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    titles = data[1] if len(data) > 1 else []
    descriptions = data[2] if len(data) > 2 else []
    urls = data[3] if len(data) > 3 else []
    results = []
    for title, desc, url in zip(titles, descriptions, urls):
        results.append({"title": title, "url": url, "snippet": desc, "source": "wikipedia"})
    return results


def _search_crossref(query: str, max_results: int = 3) -> List[Dict[str, str]]:
    """Crossref 检索论文/出版物。"""
    response = requests.get(
        "https://api.crossref.org/works",
        params={"query": query, "rows": max_results},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    items = response.json().get("message", {}).get("items", [])
    results = []
    for item in items[:max_results]:
        doi = item.get("DOI", "")
        title_list = item.get("title", [])
        results.append(
            {
                "title": title_list[0] if title_list else doi,
                "url": f"https://doi.org/{doi}" if doi else "",
                "snippet": "; ".join(
                    item.get("author", [{}])[0].get(k, "")
                    for k in ["given", "family"]
                    if item.get("author")
                ) if item.get("author") else "",
                "source": "crossref",
            }
        )
    return results


def _search_arxiv(query: str, max_results: int = 3) -> List[Dict[str, str]]:
    """通过 arXiv Atom API 搜索论文。"""
    response = requests.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": f"all:{query}", "start": 0, "max_results": max_results},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    results = []
    for entry in root.findall("atom:entry", ns)[:max_results]:
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        url = entry.findtext("atom:id", default="", namespaces=ns) or ""
        results.append({"title": title, "url": url, "snippet": summary, "source": "arxiv"})
    return results


def _search_github(query: str, max_results: int = 3) -> List[Dict[str, str]]:
    """GitHub 仓库搜索，适合查代码项目。"""
    response = requests.get(
        "https://api.github.com/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc", "per_page": max_results},
        headers=_github_headers(),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    items = response.json().get("items", [])
    results = []
    for item in items[:max_results]:
        results.append(
            {
                "title": item.get("full_name", ""),
                "url": item.get("html_url", ""),
                "snippet": item.get("description", ""),
                "source": "github",
            }
        )
    return results


# ------------------------------
# MCP 工具定义
# ------------------------------

@mcp.tool()
def web_read(url: str, format: str = "markdown") -> str:
    """读取网页内容。

    format 参数控制返回格式：
    - "markdown"（默认）：转为 Markdown，保留结构，适合大多数场景
    - "text"：纯文本，去除所有标签，适合只需正文的场景
    - "jina"：经由 Jina Reader 提取，对复杂/动态页面效果更好（需配置 JINA_API_KEY）
    """
    try:
        if format == "jina":
            headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
            target = url.removeprefix("http://").removeprefix("https://")
            r = requests.get(f"https://r.jina.ai/http://{target}", headers=headers, timeout=30)
            r.raise_for_status()
            return _truncate(r.text)
        r = _request(url)
        r.raise_for_status()
        if format == "text":
            return _truncate(_normalize_whitespace(_clean_text(r.text)))
        return _truncate(_html_to_markdown(r.text))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def web_extract_links(url: str, max_results: int = 100) -> str:
    """提取网页中的链接列表。"""
    try:
        r = _request(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True) or a["href"]
            links.append(f"- [{text[:120]}]({_absolute_url(url, a['href'])})")
            if len(links) >= max_results:
                break
        return "\n".join(links) if links else "No links found."
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def web_extract_metadata(url: str) -> str:
    """提取网页标题、描述、OpenGraph、Twitter 卡片等元数据。"""
    try:
        r = _request(url)
        r.raise_for_status()
        metadata = _extract_common_metadata(r.text, url)
        lines = [f"{key}: {value}" for key, value in metadata.items() if value]
        return "\n".join(lines) if lines else "No metadata found."
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """快速联网搜索。优先顺序由 SEARCH_BACKENDS 环境变量决定，支持 Tavily / SearXNG / DuckDuckGo。"""
    try:
        return _format_search_results(perform_search(query, max_results=max_results))
    except Exception as exc:
        return f"Search error: {exc}"


@mcp.tool()
def research_agent(query: str, max_results_per_source: int = 3) -> str:
    """深度多源调研：同时检索网页、Wikipedia、学术论文（arXiv/Crossref）、GitHub 代码仓库。
    适合学术研究、技术调研等需要全面信息的场景。日常快速搜索请使用 web_search。"""
    blocks: List[str] = []
    source_errors: List[str] = []

    search_jobs = [
        ("web", lambda: perform_search(query, max_results=max_results_per_source)),
        ("wikipedia", lambda: _search_wikipedia(query, max_results=max_results_per_source)),
        ("crossref", lambda: _search_crossref(query, max_results=max_results_per_source)),
        ("arxiv", lambda: _search_arxiv(query, max_results=max_results_per_source)),
        ("github", lambda: _search_github(query, max_results=max_results_per_source)),
    ]

    for source_name, func in search_jobs:
        try:
            results = func()
            if results:
                blocks.append(f"## {source_name}\n{_format_search_results(results)}")
        except Exception as exc:
            source_errors.append(f"{source_name}: {exc}")

    if source_errors:
        blocks.append("## errors\n" + "\n".join(source_errors))

    return "\n\n".join(blocks) if blocks else "No results."




def _get_image_bytes(image_url: Optional[str] = None, image_base64: Optional[str] = None) -> bytes:
    """从 URL 或 base64 获取图片字节。"""
    if image_base64:
        import base64
        return base64.b64decode(image_base64)
    if image_url:
        r = _request(image_url, stream=True)
        r.raise_for_status()
        return r.content
    raise ValueError("必须提供 image_url 或 image_base64")


def _ocr_image(img_bytes: bytes, lang: Optional[str] = None) -> str:
    """对图片执行 OCR，返回识别文本。"""
    if OCR_BACKEND == "baidu":
        language_type = "CHN_ENG"
        if lang:
            lang_map = {"eng": "ENG", "ch": "CHN_ENG", "jap": "JAP", "kor": "KOR"}
            language_type = lang_map.get(lang.lower(), "CHN_ENG")
        return _baidu_ocr(img_bytes, language_type)
    
    if OCR_BACKEND == "paddleocr":
        ocr = _get_paddleocr()
        if ocr:
            import tempfile
            import os as _os
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            try:
                result = ocr.ocr(tmp_path, cls=False)
                texts = []
                if result and result[0]:
                    for line in result[0]:
                        if line and len(line) >= 2:
                            texts.append(line[1][0])
                return "\n".join(texts) if texts else ""
            finally:
                _os.unlink(tmp_path)
    
    img = Image.open(io.BytesIO(img_bytes))
    return pytesseract.image_to_string(img, lang=lang or OCR_LANG).strip()


@mcp.tool()
def image_ocr(image_url: Optional[str] = None, image_base64: Optional[str] = None, lang: Optional[str] = None) -> str:
    """从图片中识别文字，支持 URL 或 base64 编码图片。默认使用 PaddleOCR（中文效果更好）。"""
    try:
        img_bytes = _get_image_bytes(image_url, image_base64)
        text = _ocr_image(img_bytes, lang)
        return text or "No text found in image."
    except Exception as exc:
        return f"OCR Error: {exc}"


@mcp.tool()
def image_describe(image_url: Optional[str] = None, image_base64: Optional[str] = None, prompt: str = "请描述这张图片的主要内容") -> str:
    """图片描述工具，支持 URL 或 base64 编码图片。优先调用外部视觉服务；未配置时回退到图片基础信息 + OCR。"""
    try:
        if VISION_API_URL:
            headers = {"Authorization": f"Bearer {VISION_API_KEY}"} if VISION_API_KEY else {}
            payload = {"prompt": prompt}
            if image_url:
                payload["image_url"] = image_url
            if image_base64:
                payload["image_base64"] = image_base64
            response = _post_json(VISION_API_URL, payload, headers=headers)
            response.raise_for_status()
            return _truncate(_normalize_whitespace(str(response.json())))

        img_bytes = _get_image_bytes(image_url, image_base64)
        img = Image.open(io.BytesIO(img_bytes))
        ocr_text = _ocr_image(img_bytes)
        
        return _truncate(
            _normalize_whitespace(
                f"当前未配置外部视觉服务。\n"
                f"图片格式: {img.format}\n"
                f"图片模式: {img.mode}\n"
                f"图片尺寸: {img.size[0]}x{img.size[1]}\n"
                f"OCR 文本:\n{ocr_text or '未识别到明显文字'}"
            )
        )
    except Exception as exc:
        return f"Image Describe Error: {exc}"


@mcp.tool()
def pdf_read(url: str, max_pages: int = 10) -> str:
    """读取 PDF 文本内容。适合在线文档、论文和说明书。"""
    try:
        r = _request(url)
        r.raise_for_status()
        reader = PdfReader(io.BytesIO(r.content))
        pages = []
        for index, page in enumerate(reader.pages[:max_pages]):
            text = page.extract_text() or ""
            pages.append(f"## Page {index + 1}\n{text.strip()}")
        return _truncate(_normalize_whitespace("\n\n".join(pages))) or "PDF 中没有提取到可读文本。"
    except Exception as exc:
        return f"PDF Error: {exc}"


@mcp.tool()
def youtube_transcript(video_url_or_id: str, languages: Optional[List[str]] = None) -> str:
    """提取 YouTube 视频字幕。可直接传链接或视频 ID。"""
    try:
        video_id = _youtube_video_id(video_url_or_id)
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id, languages=languages)
        text = "\n".join(item.text for item in transcript)
        return _truncate(_normalize_whitespace(text)) or "No transcript found."
    except Exception as exc:
        return f"YouTube Transcript Error: {exc}"


if TAVILY_API_KEY:
    @mcp.tool()
    def tavily_extract(urls: List[str]) -> str:
        """批量抽取多个网页正文（via Tavily Extract API），适合同时处理多个 URL。
        仅在配置 TAVILY_API_KEY 后可用。"""
        try:
            return tavily_extract_urls(urls)
        except Exception as exc:
            return f"Tavily Extract Error: {exc}"


# ------------------------------
# HTTP 服务与鉴权
# ------------------------------

_RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "120"))  # 每 IP 每分钟最大请求数
_rate_counters: dict = defaultdict(list)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """滑动窗口速率限制，按客户端 IP 计数。"""

    async def dispatch(self, request: Request, call_next):
        ip = (request.client.host if request.client else "unknown")
        now = _time.monotonic()
        window_start = now - 60.0
        bucket = _rate_counters[ip]
        # 清理窗口外的旧记录
        while bucket and bucket[0] < window_start:
            bucket.pop(0)
        if len(bucket) >= _RATE_LIMIT_RPM:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded, retry after 60s"},
                headers={"Retry-After": "60"},
            )
        bucket.append(now)
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """简单 Bearer Token 鉴权。

    说明：
    - /health 和 / 允许匿名访问，方便健康检查。
    - /health/detail 及其余端点均要求 Authorization。
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/health", "/", "/docs", "/openapi.json"}:
            return await call_next(request)
        if request.headers.get("Authorization") != ADMIN_TOKEN:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动时输出关键信息，便于排查部署问题。"""
    async with session_manager.run():
        logger.info("MCP server starting on port %s", PORT)
        logger.info("SSE endpoint (legacy): /sse + /messages/")
        logger.info("Streamable HTTP endpoint (new): /mcp")
        if _token_auto_generated:
            logger.warning("=" * 60)
            logger.warning("ADMIN_TOKEN auto-generated (not set in env):")
            logger.warning("  %s", ADMIN_TOKEN)
            logger.warning("Token saved to: %s", _TOKEN_FILE)
            logger.warning("=" * 60)
        else:
            logger.info("Admin token: configured via env or file")
        logger.info("Search backends: %s", ", ".join(SEARCH_BACKENDS))
        logger.info("Tavily enabled: %s", "yes" if bool(TAVILY_API_KEY) else "no")
        yield


app = FastAPI(title="MCP Advanced Web OSINT Server", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)


@app.get("/")
async def index():
    """首页，用于快速查看服务入口。"""
    return {
        "ok": True,
        "name": "Web-Tools-Server",
        "mcp": "/mcp",
        "sse": "/sse",
        "messages": "/messages/",
        "health": "/health",
    }


@app.get("/health")
def health_check():
    """公开健康检查，仅返回服务存活状态。"""
    return {"status": "ok"}


@app.get("/health/detail")
def health_check_detail():
    """详细健康信息（需鉴权，由 AuthMiddleware 保护）。"""
    return {
        "status": "ok",
        "port": PORT,
        "search_backends": SEARCH_BACKENDS,
        "tavily_enabled": bool(TAVILY_API_KEY),
        "ocr_backend": OCR_BACKEND,
        "paddleocr_lang": PADDLEOCR_LANG,
        "features": [
            "web_fetch",
            "web_to_markdown",
            "web_search",
            "search_agent",
            "web_extract_links",
            "web_extract_metadata",
            "image_ocr",
            "image_describe",
            "jina_reader",
            "jina_vision",
            "pdf_read",
            "youtube_transcript",
            "tavily_extract",
        ],
    }


import asyncio
import time as _time
from collections import defaultdict

_SSE_KEEPALIVE_INTERVAL = 15  # 秒，缩短到 15s 防止 30s 超时的代理误杀连接

async def handle_sse(request: Request):
    """SSE 长连接入口。Cherry Studio 等客户端通过这里建立会话。"""
    _response_started = False
    original_send = request._send

    async def guarded_send(message):
        nonlocal _response_started
        if message["type"] == "http.response.start":
            if _response_started:
                return  # SSE 已发过响应，丢弃 Starlette 外层的重复响应
            _response_started = True
            await original_send(message)
            # 连接建立后立即告知客户端断线重连间隔（3 秒），
            # 符合 SSE 规范的客户端会在断线后自动重连
            await original_send({
                "type": "http.response.body",
                "body": b"retry: 3000\n\n",
                "more_body": True,
            })
            return
        await original_send(message)

    async with transport.connect_sse(request.scope, request.receive, guarded_send) as streams:
        in_stream, out_stream = streams

        async def _keepalive():
            """定期发送 SSE 注释行，防止代理/客户端因空闲断开连接。"""
            try:
                while True:
                    await asyncio.sleep(_SSE_KEEPALIVE_INTERVAL)
                    try:
                        await original_send({
                            "type": "http.response.body",
                            "body": b": keepalive\n\n",
                            "more_body": True,
                        })
                    except Exception:
                        break  # 连接已关闭
            except asyncio.CancelledError:
                pass

        keepalive_task = asyncio.create_task(_keepalive())
        try:
            await mcp._mcp_server.run(
                in_stream,
                out_stream,
                mcp._mcp_server.create_initialization_options(),
            )
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass


# /sse: 建立 SSE 长连接（兼容旧版客户端：Cherry Studio、Claude Desktop 等）
app.add_route("/sse", handle_sse, methods=["GET"])
# /messages/: 客户端实际投递 MCP 请求的入口（配合 /sse 使用）
app.mount("/messages/", transport.handle_post_message)


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def handle_mcp(request: Request):
    """Streamable HTTP 端点，支持新版 MCP 客户端（Claude Code、claude.ai 等）。

    - GET  /mcp : 建立 SSE 流，接收服务端推送（可选）
    - POST /mcp : 发送 MCP 请求，返回 JSON 或 SSE 流
    - DELETE /mcp : 终止会话
    会话通过 Mcp-Session-Id 响应头追踪。
    """
    await session_manager.handle_request(
        request.scope, request.receive, request._send
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
