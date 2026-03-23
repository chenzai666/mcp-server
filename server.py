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
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from markdownify import markdownify as html_to_markdown
from fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
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
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or f"Bearer {secrets.token_urlsafe(24)}"

# 搜索后端相关配置
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:18080/search")
SEARCH_BACKENDS = [item.strip() for item in os.getenv("SEARCH_BACKENDS", "tavily,searxng,duckduckgo,serp").split(",") if item.strip()]
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
TAVILY_TOPIC = os.getenv("TAVILY_TOPIC", "general").strip() or "general"

# SERP 抓取引擎配置（无需 API Key）
# 逗号分隔，优先级按顺序，支持: google,baidu,bing,sogou,360,duckduckgo
SERP_ENGINES = [item.strip() for item in os.getenv("SERP_ENGINES", "google,baidu,bing,sogou,360,duckduckgo").split(",") if item.strip()]

# Jina 相关配置，可选
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()

# OCR 相关配置
OCR_LANG = os.getenv("OCR_LANG", "eng+chi_sim")

# 外部视觉服务配置，可选
VISION_API_URL = os.getenv("VISION_API_URL", "").strip()
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip()

# GitHub 可选 Token，提高匿名访问速率限制
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

HEADERS = {"User-Agent": USER_AGENT}

mcp = FastMCP("Web-Tools-Server")
# SSE 模式下，客户端通过 /sse 建立长连接，再通过 /messages/ 发起请求
transport = SseServerTransport("/messages/")


def _request(url: str, **kwargs) -> requests.Response:
    """统一的 GET 请求封装，自动合并公共请求头。"""
    merged_headers = dict(HEADERS)
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        merged_headers.update(extra_headers)
    return requests.get(url, headers=merged_headers, timeout=TIMEOUT, **kwargs)


def _post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    """统一的 JSON POST 请求封装。"""
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
# 搜索后端：SERP 直接抓取（无需 API Key）
# 支持 Google、Baidu、Bing、Sogou、360
# ------------------------------

def _serp_google(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """通过 Google SERP 直接抓取搜索结果。"""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}&hl=zh-CN"
    response = _request(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []

    for item in soup.select(".g")[:max_results]:
        title_tag = item.select_one("h3")
        link_tag = item.select_one("a")
        snippet_tag = item.select_one(".VwiC3b") or item.select_one(".IsZvec")

        if not title_tag or not link_tag:
            continue

        href = link_tag.get("href", "")
        if not href or href.startswith("/") or "google.com" in href:
            continue

        title = title_tag.get_text(strip=True)
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        # 清理 URL 参数
        clean_url = re.sub(r"&sa=.*", "", href)
        clean_url = clean_url.split("&")[0] if "?" in clean_url else clean_url

        results.append({
            "title": title,
            "url": clean_url,
            "snippet": snippet,
            "source": "google",
        })

    return results


def _serp_baidu(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """通过百度 SERP 直接抓取搜索结果。"""
    encoded = urllib.parse.quote(query.encode("utf-8"))
    url = f"https://www.baidu.com/s?wd={encoded}&rn={max_results}"
    response = _request(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []

    for item in soup.select(".result, .result-op")[:max_results]:
        title_tag = item.select_one("h3 a") or item.select_one("h3")
        link_tag = item.select_one("h3 a") or item.select_one("a")
        snippet_tag = item.select_one(".c-abstract") or item.select_one(".content-right_8Zs40")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        href = ""
        if title_tag.name == "a":
            href = title_tag.get("href", "")
        else:
            link = item.select_one("a")
            if link:
                href = link.get("href", "")

        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        if not href:
            continue

        # 百度有时返回的是跳转链接，需要解跳转
        if "redirect" in href or "baidu.com" in href:
            try:
                r = _request(href, allow_redirects=False)
                href = r.headers.get("Location", href) if r.status_code == 302 else href
            except Exception:
                pass

        results.append({
            "title": title,
            "url": href,
            "snippet": snippet[:300] if snippet else "",
            "source": "baidu",
        })

    return results


def _serp_bing(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """通过 Bing 国际版 SERP 直接抓取搜索结果。"""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&ensearch=1"
    response = _request(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []

    for item in soup.select("li.b_algo")[:max_results]:
        title_tag = item.select_one("h2 a")
        link_tag = item.select_one("h2 a")
        snippet_tag = item.select_one(".b_paractl") or item.select_one("p")

        if not title_tag:
            continue

        href = link_tag.get("href", "") if link_tag else ""
        title = title_tag.get_text(strip=True) if title_tag else ""
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        if not href:
            continue

        results.append({
            "title": title,
            "url": href,
            "snippet": snippet[:300] if snippet else "",
            "source": "bing",
        })

    return results


def _serp_sogou(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """通过搜狗 SERP 直接抓取搜索结果。"""
    encoded = urllib.parse.quote(query.encode("utf-8"))
    url = f"https://www.sogou.com/web?query={encoded}&ie=utf8"
    response = _request(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []

    for item in soup.select(".vrwrap, .rb")[:max_results]:
        title_tag = item.select_one("h3 a") or item.select_one("h3")
        link_tag = item.select_one("h3 a") or item.select_one("a")
        snippet_tag = item.select_one(".space-txt") or item.select_one(".str-text-info")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        href = link_tag.get("href", "") if link_tag else ""
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        if not href or "sogou.com" in href:
            continue

        results.append({
            "title": title,
            "url": href,
            "snippet": snippet[:300] if snippet else "",
            "source": "sogou",
        })

    return results


def _serp_360(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """通过 360 搜索 SERP 直接抓取搜索结果。"""
    encoded = urllib.parse.quote(query.encode("utf-8"))
    url = f"https://www.so.com/s?q={encoded}&pn=1&rn={max_results}"
    response = _request(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []

    for item in soup.select(".res-list")[:max_results]:
        title_tag = item.select_one("h3 a") or item.select_one("h3")
        link_tag = item.select_one("h3 a")
        snippet_tag = item.select_one(".des")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        href = link_tag.get("href", "") if link_tag else ""
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        if not href:
            continue

        results.append({
            "title": title,
            "url": href,
            "snippet": snippet[:300] if snippet else "",
            "source": "360",
        })

    return results


def search_serp(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """SERP 抓取入口，按配置的引擎顺序依次尝试，返回第一个有结果的后端。"""
    engine_map = {
        "google": _serp_google,
        "baidu": _serp_baidu,
        "bing": _serp_bing,
        "sogou": _serp_sogou,
        "360": _serp_360,
        "duckduckgo": search_duckduckgo_html,
    }

    errors: List[str] = []
    for engine in SERP_ENGINES:
        if engine not in engine_map:
            continue
        try:
            results = engine_map[engine](query, max_results=max_results)
            if results:
                logger.info(f"SERP search succeeded with engine: {engine}")
                return results
        except Exception as exc:
            errors.append(f"{engine}: {exc}")
            logger.warning(f"SERP engine {engine} failed: {exc}")

    # 所有 SERP 引擎都失败了
    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("所有 SERP 引擎均无结果")


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
            elif backend == "serp":
                results = search_serp(query, max_results=max_results)
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
def web_fetch(url: str) -> str:
    """读取网页正文，返回清洗后的文本内容。"""
    try:
        r = _request(url)
        r.raise_for_status()
        return _truncate(_normalize_whitespace(_clean_text(r.text)))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def web_to_markdown(url: str) -> str:
    """读取网页并转换为 Markdown，便于模型理解页面结构。"""
    try:
        r = _request(url)
        r.raise_for_status()
        return _truncate(_html_to_markdown(r.text))
    except Exception as exc:
        return f"Markdown Error: {exc}"


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
    """统一联网搜索。优先顺序由 SEARCH_BACKENDS 环境变量决定，支持 Tavily / SearXNG / DuckDuckGo / SERP 直接抓取。"""
    try:
        return _format_search_results(perform_search(query, max_results=max_results))
    except Exception as exc:
        return f"Search error: {exc}"


@mcp.tool()
def search_agent(query: str, max_results_per_source: int = 3) -> str:
    """多源聚合搜索：网页、百科、论文、代码仓库一起查，适合做较全面的检索。"""
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


@mcp.tool()
def jina_reader(url: str) -> str:
    """使用 Jina Reader 提取网页内容。配置 JINA_API_KEY 后效果通常更稳定。"""
    try:
        headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
        target = url.removeprefix("http://").removeprefix("https://")
        r = requests.get(f"https://r.jina.ai/http://{target}", headers=headers, timeout=30)
        r.raise_for_status()
        return _truncate(r.text)
    except Exception as exc:
        return f"Jina Reader Error: {exc}"


@mcp.tool()
def image_ocr(image_url: str, lang: Optional[str] = None) -> str:
    """从图片中识别文字，适合截图、海报、扫描件。"""
    try:
        r = _request(image_url, stream=True)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        text = pytesseract.image_to_string(img, lang=lang or OCR_LANG)
        return text.strip() or "No text found in image."
    except Exception as exc:
        return f"OCR Error: {exc}"


@mcp.tool()
def image_describe(image_url: str, prompt: str = "请描述这张图片的主要内容") -> str:
    """图片描述工具。优先调用外部视觉服务；未配置时回退到图片基础信息 + OCR。"""
    try:
        if VISION_API_URL:
            headers = {"Authorization": f"Bearer {VISION_API_KEY}"} if VISION_API_KEY else {}
            response = _post_json(
                VISION_API_URL,
                {"image_url": image_url, "prompt": prompt},
                headers=headers,
            )
            response.raise_for_status()
            return _truncate(_normalize_whitespace(str(response.json())))

        # 没有视觉服务时，至少返回图片基本信息和 OCR 结果
        r = _request(image_url, stream=True)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        ocr_text = pytesseract.image_to_string(img, lang=OCR_LANG).strip()
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
def jina_vision(image_url: str) -> str:
    """使用 Jina 读取图片内容。适合作为外部视觉能力的简易补充。"""
    try:
        headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
        target = image_url.removeprefix("http://").removeprefix("https://")
        r = requests.get(f"https://r.jina.ai/http://{target}", headers=headers, timeout=40)
        r.raise_for_status()
        return _truncate(r.text)
    except Exception as exc:
        return f"Jina Vision Error: {exc}"


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


@mcp.tool()
def tavily_extract(urls: List[str]) -> str:
    """直接调用 Tavily Extract，适合批量抽取多个网页内容。"""
    try:
        return tavily_extract_urls(urls)
    except Exception as exc:
        return f"Tavily Extract Error: {exc}"


# ------------------------------
# HTTP 服务与鉴权
# ------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    """简单 Bearer Token 鉴权。

    说明：
    - /health 和 / 允许匿名访问，方便健康检查。
    - 其余包括 /sse 和 /messages/ 都要求带 Authorization。
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
    logger.info("MCP server starting on port %s", PORT)
    logger.info("SSE endpoint: /sse")
    logger.info("POST endpoint: /messages/")
    logger.info("Admin token configured: %s", "yes" if ADMIN_TOKEN else "no")
    logger.info("Search backends: %s", ", ".join(SEARCH_BACKENDS))
    logger.info("SERP engines (no API key): %s", ", ".join(SERP_ENGINES))
    logger.info("Tavily enabled: %s", "yes" if bool(TAVILY_API_KEY) else "no")
    yield


app = FastAPI(title="MCP Advanced Web OSINT Server", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


@app.get("/")
async def index():
    """首页，用于快速查看服务入口。"""
    return {
        "ok": True,
        "name": "Web-Tools-Server",
        "sse": "/sse",
        "messages": "/messages/",
        "health": "/health",
    }


@app.get("/health")
def health_check():
    """健康检查接口。"""
    return {
        "status": "ok",
        "port": PORT,
        "search_backends": SEARCH_BACKENDS,
        "serp_engines": SERP_ENGINES,
        "tavily_enabled": bool(TAVILY_API_KEY),
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


async def handle_sse(request: Request):
    """SSE 长连接入口。Cherry Studio 等客户端通过这里建立会话。"""
    async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
        in_stream, out_stream = streams
        await mcp._mcp_server.run(
            in_stream,
            out_stream,
            mcp._mcp_server.create_initialization_options(),
        )


# /sse: 建立 SSE 长连接
app.add_route("/sse", handle_sse, methods=["GET"])
# /messages/: 客户端实际投递 MCP 请求的入口
app.mount("/messages/", transport.handle_post_message)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
