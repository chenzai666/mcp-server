# MCP 工具服务器（SSE + Streamable HTTP 双传输）

基于 Python + FastAPI + FastMCP 构建，同时支持 SSE 和 Streamable HTTP 两种传输协议，适合接入 Cherry Studio、Claude Desktop 等客户端。

## 已包含的工具

| 工具 | 说明 |
|------|------|
| `web_read` | 读取网页内容，支持 `markdown`（默认）/ `text` / `jina` 三种格式 |
| `web_search` | 统一联网搜索，支持 Tavily / SearXNG / DuckDuckGo，按优先级自动回退 |
| `search_agent` | 多源聚合搜索（网页、百科、论文、代码仓库） |
| `web_extract_links` | 提取网页中的所有链接 |
| `web_extract_metadata` | 提取网页元数据（标题、描述、关键词等） |
| `image_ocr` | 图片 OCR，支持 URL 或 base64，默认使用 PaddleOCR |
| `image_describe` | 图片描述，支持 URL 或 base64（可对接外部视觉服务） |
| `pdf_read` | 读取 PDF 文本内容 |
| `youtube_transcript` | 提取 YouTube 视频字幕 |
| `tavily_extract` | 调用 Tavily Extract 提取网页精华（需配置 `TAVILY_API_KEY`） |

## 目录说明

- `server.py`：主服务代码
- `Dockerfile`：镜像构建文件（生产环境）
- `Dockerfile.dev`：开发环境镜像，支持热加载
- `docker-compose.yml`：编排文件（生产环境）
- `docker-compose.dev.yml`：开发环境编排文件，支持代码热加载
- `.env.example`：环境变量模板
- `requirements.txt`：Python 依赖

## 快速部署

```bash
cp .env.example .env
# 按需修改 ADMIN_TOKEN、TAVILY_API_KEY、SEARXNG_SECRET 等

# 生产模式
docker compose up -d --build

# 开发模式（代码热加载）
docker compose -f docker-compose.dev.yml up -d --build
```

## 健康检查

```bash
# 公开端点，仅返回服务状态
curl http://127.0.0.1:59795/health

# 详细配置信息（需要 Authorization header）
curl -H "Authorization: Bearer 你的Token" http://127.0.0.1:59795/health/detail
```

## 客户端配置

### Cherry Studio（SSE）

- 类型：`SSE`
- URL：`http://你的服务器IP:59795/sse`
- Header：`Authorization: Bearer 你的Token`

### Claude Desktop / 支持 Streamable HTTP 的客户端

- URL：`http://你的服务器IP:59795/mcp`
- Header：`Authorization: Bearer 你的Token`

## 环境变量

### 基础配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PORT` | 服务端口 | `59795` |
| `ADMIN_TOKEN` | 鉴权 Token，格式：`Bearer xxx` | 必填 |
| `REQUEST_TIMEOUT` | HTTP 请求超时（秒） | `20` |
| `RATE_LIMIT_RPM` | 每 IP 每分钟最大请求数 | `120` |

### 搜索配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SEARCH_BACKENDS` | 搜索后端优先级，逗号分隔 | `tavily,searxng,duckduckgo` |
| `TAVILY_API_KEY` | Tavily API Key | - |
| `TAVILY_TOPIC` | Tavily 搜索主题 | `general` |
| `SEARXNG_URL` | SearXNG 搜索接口地址 | - |
| `SEARXNG_BASE_URL` | SearXNG 基础地址（用于结果拼接） | - |
| `SEARXNG_SECRET` | SearXNG 密钥 | - |

### OCR 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OCR_BACKEND` | OCR 引擎：`paddleocr` / `baidu` / `tesseract` | `paddleocr` |
| `PADDLEOCR_LANG` | PaddleOCR 语言：`ch`（中英文）、`en` 等 | `ch` |
| `OCR_LANG` | Tesseract 语言（仅 Tesseract 模式） | `eng+chi_sim` |
| `BAIDU_OCR_API_KEY` | 百度 OCR API Key | - |
| `BAIDU_OCR_SECRET_KEY` | 百度 OCR Secret Key | - |

### 可选增强

| 变量 | 说明 |
|------|------|
| `JINA_API_KEY` | Jina API Key，用于 `web_read` 的 `jina` 格式 |
| `VISION_API_URL` | 外部视觉服务地址，用于 `image_describe` |
| `VISION_API_KEY` | 外部视觉服务 API Key |
| `GITHUB_TOKEN` | GitHub Token，用于访问私有仓库 |

## 说明

1. `web_search` 按 `SEARCH_BACKENDS` 顺序尝试，某个后端失败自动切换下一个
2. 未配置 `TAVILY_API_KEY` 时，`tavily_extract` 工具不会注册，`web_search` 自动跳过 Tavily
3. `web_read` 的 `jina` 格式未配置 `JINA_API_KEY` 时仍可使用，但有速率限制
4. `image_ocr` 默认 PaddleOCR，中文识别效果好；可通过 `OCR_BACKEND=tesseract` 切换
5. `image_describe` 未配置 `VISION_API_URL` 时，回退为"基础信息 + OCR 文本"模式
6. `youtube_transcript` 依赖视频已开启公开字幕
7. SSE 连接设有 15 秒心跳保活，客户端断线后自动每 3 秒重连一次
