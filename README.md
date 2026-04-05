# Ombre Brain

一个给提供给Claude 用的长期情绪记忆系统。基于 Russell 效价/唤醒度坐标打标，Obsidian 做存储层，MCP 接入，带遗忘曲线。

A long-term emotional memory system for Claude. Tags memories using Russell's valence/arousal coordinates, stores them as Obsidian-compatible Markdown, connects via MCP, and has a forgetting curve.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/P0lar1zzZ/Ombre-Brain)
[![Deploy on Zeabur](https://zeabur.com/button.svg)](https://zeabur.com/deploy?repo=https://github.com/P0lar1zzZ/Ombre-Brain)

---

## 它是什么 / What is this

Claude 没有跨对话记忆。每次对话结束，之前聊过的所有东西都会消失。

Ombre Brain 给了它一套持久记忆——不是那种冷冰冰的键值存储，而是带情感坐标的、会自然衰减的、像人类记忆一样会遗忘和浮现的系统。

Claude has no cross-conversation memory. Everything from a previous chat vanishes once it ends.

Ombre Brain gives it persistent memory — not cold key-value storage, but a system with emotional coordinates, natural decay, and forgetting/surfacing mechanics that loosely mimic how human memory works.

核心特点 / Key features:

- **情感坐标打标 / Emotional tagging**: 每条记忆用 Russell 环形情感模型的 valence（效价）和 arousal（唤醒度）两个连续维度标记。不是"开心/难过"这种离散标签。
  Each memory is tagged with two continuous dimensions from Russell's circumplex model: valence and arousal. Not discrete labels like "happy/sad".

- **自然遗忘 / Natural forgetting**: 改进版艾宾浩斯遗忘曲线。不活跃的记忆自动衰减归档，高情绪强度的记忆衰减更慢。
  Modified Ebbinghaus forgetting curve. Inactive memories naturally decay and archive. High-arousal memories decay slower.

- **权重池浮现 / Weight pool surfacing**: 记忆不是被动检索的，它们会主动浮现——未解决的、情绪强烈的记忆权重更高，会在对话开头自动推送。
  Memories aren't just passively retrieved — they actively surface. Unresolved, emotionally intense memories carry higher weight and get pushed at conversation start.

- **Obsidian 原生 / Obsidian-native**: 每个记忆桶就是一个 Markdown 文件，YAML frontmatter 存元数据。可以直接在 Obsidian 里浏览、编辑、搜索。自动注入 `[[双链]]`。
  Each memory bucket is a Markdown file with YAML frontmatter. Browse, edit, and search directly in Obsidian. Wikilinks are auto-injected.

- **API 降级 / API degradation**: 脱水压缩和自动打标优先用廉价 LLM API（DeepSeek 等），API 不可用时自动降级到本地关键词分析——始终可用。
  Dehydration and auto-tagging prefer a cheap LLM API (DeepSeek etc.). When the API is unavailable, it degrades to local keyword analysis — always functional.

## 边界说明 / Design boundaries

官方记忆功能已经在做身份层的事了——你是谁，你有什么偏好，你们的关系是什么。那一层交给它，Ombre Brain不打算造重复的轮子。

Ombre Brain 的边界是时间里发生的事，不是你是谁。它记住的是：你们聊过什么，经历了什么，哪些事情还悬在那里没有解决。两层配合用，才是完整的。

每次新对话，Claude 从零开始——但它能从 Ombre Brain 里找回跟你有关的一切。不是重建，是接续。

---

Official memory already handles the identity layer — who you are, what you prefer, what your relationship is. That layer belongs there. Ombre Brain isn't trying to duplicate it.

Ombre Brain's boundary is *what happened in time*, not *who you are*. It holds conversations, experiences, unresolved things. The two layers together are what make it feel complete.

Each new conversation starts fresh — but Claude can reach back through Ombre Brain and find everything that happened between you. Not a rebuild. A continuation.

## 架构 / Architecture

```
Claude ←→ MCP Protocol ←→ server.py
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        bucket_manager   dehydrator     decay_engine
         (CRUD + 搜索)    (压缩 + 打标)   (遗忘曲线)
              │
        Obsidian Vault (Markdown files)
```

5 个 MCP 工具 / 5 MCP tools:

| 工具 Tool | 作用 Purpose |
|-----------|-------------|
| `breath` | 浮现或检索记忆。无参数=推送未解决记忆；有参数=关键词+情感检索 / Surface or search memories |
| `hold` | 存储单条记忆，自动打标+合并相似桶 / Store a single memory with auto-tagging |
| `grow` | 日记归档，自动拆分长内容为多个记忆桶 / Diary digest, auto-split into multiple buckets |
| `trace` | 修改元数据、标记已解决、删除 / Modify metadata, mark resolved, delete |
| `pulse` | 系统状态 + 所有记忆桶列表 / System status + bucket listing |

## 安装 / Setup

### 环境要求 / Requirements

- Python 3.11+
- 一个 Obsidian Vault（可选，不用也行，会在项目目录下自建 `buckets/`）
  An Obsidian vault (optional — without one, it uses a local `buckets/` directory)

### 步骤 / Steps

```bash
git clone https://github.com/P0lar1zzZ/Ombre-Brain.git
cd Ombre-Brain

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

复制配置文件并按需修改 / Copy config and edit as needed:

```bash
cp config.example.yaml config.yaml
```

如果你要用 API 做脱水压缩和自动打标（推荐，效果好很多），设置环境变量：
If you want API-powered dehydration and tagging (recommended, much better quality):

```bash
export OMBRE_API_KEY="your-api-key"
```

支持任何 OpenAI 兼容 API。在 `config.yaml` 里改 `base_url` 和 `model` 就行。
Supports any OpenAI-compatible API. Just change `base_url` and `model` in `config.yaml`.

### 接入 Claude Desktop / Connect to Claude Desktop

在 Claude Desktop 配置文件中添加（macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "ombre-brain": {
      "command": "python",
      "args": ["/path/to/Ombre-Brain/server.py"],
      "env": {
        "OMBRE_API_KEY": "your-api-key"
      }
    }
  }
}
```

### 接入 Claude.ai (远程) / Connect to Claude.ai (remote)

需要 HTTP 传输 + 隧道。可以用 Docker：
Requires HTTP transport + tunnel. Docker setup:

```bash
echo "OMBRE_API_KEY=your-api-key" > .env
docker-compose up -d
```

`docker-compose.yml` 里配好了 Cloudflare Tunnel。你需要自己在 `~/.cloudflared/` 下放凭证和路由配置。
The `docker-compose.yml` includes Cloudflare Tunnel. You'll need your own credentials under `~/.cloudflared/`.

### 指向 Obsidian / Point to Obsidian

在 `config.yaml` 里设置 `buckets_dir`：
Set `buckets_dir` in `config.yaml`:

```yaml
buckets_dir: "/path/to/your/Obsidian Vault/Ombre Brain"
```

不设的话，默认用项目目录下的 `buckets/`。
If not set, defaults to `buckets/` in the project directory.

## 配置 / Configuration

所有参数在 `config.yaml`（从 `config.example.yaml` 复制）。关键的几个：
All parameters in `config.yaml` (copy from `config.example.yaml`). Key ones:

| 参数 Parameter | 说明 Description | 默认 Default |
|---|---|---|
| `transport` | `stdio`（本地）/ `streamable-http`（远程）| `stdio` |
| `buckets_dir` | 记忆桶存储路径 / Bucket storage path | `./buckets/` |
| `dehydration.model` | 脱水用的 LLM 模型 / LLM model for dehydration | `deepseek-chat` |
| `dehydration.base_url` | API 地址 / API endpoint | `https://api.deepseek.com/v1` |
| `decay.lambda` | 衰减速率，越大越快忘 / Decay rate | `0.05` |
| `decay.threshold` | 归档阈值 / Archive threshold | `0.3` |
| `merge_threshold` | 合并相似度阈值 (0-100) / Merge similarity | `75` |

敏感配置用环境变量：
Sensitive config via env vars:
- `OMBRE_API_KEY` — LLM API 密钥
- `OMBRE_TRANSPORT` — 覆盖传输方式
- `OMBRE_BUCKETS_DIR` — 覆盖存储路径

## 衰减公式 / Decay Formula

$$final\_score = time\_weight \times base\_score$$

$$base\_score = Importance \times activation\_count^{0.3} \times e^{-\lambda \times days} \times (base + arousal \times boost)$$

时间系数（乘数，优先级最高）/ Time weight (multiplier, highest priority):

| 距今天数 Days since active | 时间系数 Weight |
|---|---|
| 0–1 天 | 1.0 |
| 第 2 天 | 0.9 |
| 之后每天约降 10% | `max(0.3, 0.9 × e^{-0.2197 × (days-2)})` |
| 7 天后稳定 | ≈ 0.3（不归零）|

- `importance`: 1-10，记忆重要性 / memory importance
- `activation_count`: 被检索的次数，越常被想起衰减越慢 / retrieval count; more recalls = slower decay
- `days`: 距上次激活的天数 / days since last activation
- `arousal`: 唤醒度，越强烈的记忆越难忘 / arousal; intense memories are harder to forget
- 已解决的记忆权重降到 5%，沉底等被关键词唤醒 / resolved memories drop to 5%, sink until keyword-triggered
- `pinned=true` 的桶：不衰减、不合并、importance 锁定 10 / `pinned` buckets: never decay, never merge, importance locked at 10

## 给 Claude 的使用指南 / Usage Guide for Claude

`CLAUDE_PROMPT.md` 是写给 Claude 看的使用说明。放到你的 system prompt 或 custom instructions 里就行。

`CLAUDE_PROMPT.md` is the usage guide written for Claude. Put it in your system prompt or custom instructions.

## 工具脚本 / Utility Scripts

| 脚本 Script | 用途 Purpose |
|---|---|
| `write_memory.py` | 手动写入记忆，绕过 MCP / Manually write memories, bypass MCP |
| `migrate_to_domains.py` | 迁移平铺文件到域子目录 / Migrate flat files to domain subdirs |
| `reclassify_domains.py` | 基于关键词重分类 / Reclassify by keywords |
| `reclassify_api.py` | 用 API 重打标未分类桶 / Re-tag uncategorized buckets via API |
| `test_smoke.py` | 冒烟测试 / Smoke test |

## 一键部署 / One-click Deploy

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/P0lar1zzZ/Ombre-Brain)

项目根目录已包含 `render.yaml`，点击按钮后：
1. （可选）在 Render dashboard → **Environment** 里设置 `OMBRE_API_KEY`（任何 OpenAI 兼容 API 的 key，不填则自动降级为本地关键词提取）
2. （可选）设置 `OMBRE_BASE_URL` 指定 API 地址（支持任意 OpenAI 化地址，如 `https://api.deepseek.com/v1` / `http://123.1.1.1:7689/v1` / `http://your-ollama:11434/v1` 等）
3. Render 会自动挂载持久化磁盘到 `/opt/render/project/src/buckets`
4. 部署完成后，MCP URL 为 `https://<你的服务名>.onrender.com/mcp`，填入 Claude Desktop / Claude.ai 的 MCP 设置

`render.yaml` is included. After clicking the button:
1. (Optional) Set `OMBRE_API_KEY` in Render dashboard → **Environment** (any OpenAI-compatible key; omit to fall back to local keyword extraction)
2. (Optional) Set `OMBRE_BASE_URL` to any OpenAI-compatible endpoint (e.g. `https://api.deepseek.com/v1`, `http://123.1.1.1:7689/v1`, `http://your-ollama:11434/v1`)
3. A persistent disk is auto-mounted at `/opt/render/project/src/buckets`
4. After deploy, your MCP URL is `https://<your-service>.onrender.com/mcp` — add it to Claude Desktop / Claude.ai MCP settings

### Zeabur

[![Deploy on Zeabur](https://zeabur.com/button.svg)](https://zeabur.com/deploy?repo=https://github.com/P0lar1zzZ/Ombre-Brain)

项目根目录已包含 `zbpack.json`，点击按钮后：
1. （可选）在 Zeabur 控制台设置环境变量 `OMBRE_API_KEY`（任何 OpenAI 兼容 API 的 key，不填则自动降级为本地关键词提取）
2. （可选）设置 `OMBRE_BASE_URL` 指定 API 地址（支持任意 OpenAI 化地址，如 `https://api.deepseek.com/v1` / `http://123.1.1.1:7689/v1` / `http://your-ollama:11434/v1` 等）
3. Volume `ombre-buckets` 会挂载到 `/app/buckets`
4. 部署完成后，MCP URL 为 `https://<你的域名>/mcp`，填入 Claude Desktop / Claude.ai 的 MCP 设置

`zbpack.json` is included. After clicking:
1. (Optional) Set `OMBRE_API_KEY` in the Zeabur console (omit to fall back to local keyword extraction)
2. (Optional) Set `OMBRE_BASE_URL` to any OpenAI-compatible endpoint (e.g. `https://api.deepseek.com/v1`, `http://123.1.1.1:7689/v1`, `http://your-ollama:11434/v1`)
3. Volume `ombre-buckets` mounts at `/app/buckets`
4. After deploy, your MCP URL is `https://<your-domain>/mcp` — add it to Claude Desktop / Claude.ai MCP settings

### Session Start Hook（自动 breath）

部署后，如果你使用 Claude Code，可以在项目内激活自动浮现 hook：
`.claude/settings.json` 已配置好 `SessionStart` hook，每次新会话或恢复会话时自动触发 `breath`，把最高权重未解决记忆推入上下文。

**仅在远程 HTTP 模式下有效**（`OMBRE_TRANSPORT=streamable-http`）。本地 stdio 模式下 hook 会安静退出，不影响正常使用。

可以通过 `OMBRE_HOOK_URL` 环境变量指定服务器地址（默认 `http://localhost:8000`），或者设置 `OMBRE_HOOK_SKIP=1` 临时禁用。

If using Claude Code, `.claude/settings.json` configures a `SessionStart` hook that auto-calls `breath` on each new or resumed session, surfacing your highest-weight unresolved memories as context. Only active in remote HTTP mode. Set `OMBRE_HOOK_SKIP=1` to disable temporarily.

## License

MIT
