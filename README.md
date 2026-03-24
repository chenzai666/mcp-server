# MCP SSE 工具服务器（已接入 Tavily）

这是一个基于 Python + FastAPI + FastMCP 的 SSE 版 MCP 服务，适合接入 Cherry Studio 等客户端。

## 已包含的工具

- `web_fetch`：读取网页正文
- `web_to_markdown`：网页转 Markdown
- `web_search`：统一联网搜索，支持 Tavily / SearXNG / DuckDuckGo
- `search_agent`：多源聚合搜索（网页、百科、论文、代码仓库）
- `web_extract_links`：提取网页链接
- `web_extract_metadata`：提取网页元数据
- `image_ocr`：图片 OCR
- `image_describe`：图片描述（可对接外部视觉服务）
- `jina_reader`：Jina 网页读取
- `jina_vision`：Jina 图片读取
- `pdf_read`：读取 PDF 文本
- `youtube_transcript`：提取 YouTube 字幕
- `tavily_extract`：直接调用 Tavily Extract

## 目录说明

- `server.py`：主服务代码，已尽量使用中文注释
- `Dockerfile`：镜像构建文件（生产环境）
- `Dockerfile.dev`：开发环境镜像，支持热加载
- `docker-compose.yml`：编排文件（生产环境）
- `docker-compose.dev.yml`：开发环境编排文件，支持代码热加载
- `.env.example`：环境变量模板
- `requirements.txt`：Python 依赖

## 快速部署

```bash
cp .env.example .env
# 按需修改 ADMIN_TOKEN、TAVILY_API_KEY、SEARXNG_SECRET

# 生产模式
docker compose up -d --build

# 开发模式（代码热加载，首次需要 build，后续修改代码自动重载）
docker compose -f docker-compose.dev.yml up -d --build
```

## 健康检查

```bash
curl http://127.0.0.1:59795/health
```

## Cherry Studio 配置

- 类型：`SSE`
- URL：`http://你的服务器IP:59795/sse`
- Header：`Authorization: Bearer 你的Token`

## 说明

1. `web_search` 会按 `SEARCH_BACKENDS` 顺序搜索，推荐：`tavily,searxng,duckduckgo`
2. 没有配置 `TAVILY_API_KEY` 时，会自动回退到后面的搜索后端
3. `image_describe` 没配置 `VISION_API_URL` 时，会回退为“图片基础信息 + OCR 文本”模式
4. `youtube_transcript` 依赖公开视频存在字幕
