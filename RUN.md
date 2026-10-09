# 5 分钟跑起来

本项目提供两条运行路径，**任选其一即可**，目标是在 5 分钟内完成启动并生成简报。

---

## 路径一：裸机运行（最快，无需 Docker）

适合：快速验证功能、本地演示。

### 1. 环境准备（约 2 分钟）

需要 Python 3.10+（本项目在 3.13 上开发验证）。

```bash
git clone <仓库地址>
cd mining-rights-agent

python -m venv .venv
```

### 2. 安装依赖（约 1-2 分钟）

**Windows（PowerShell）**
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux**
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

> 若 pip 报 `No matching distribution found`，多半是镜像源问题。
> 本项目已在 `requirements.txt` 中配置官方源作为回退；
> 如仍失败，手动指定：`pip install -r requirements.txt -i https://pypi.org/simple`

### 3. 生成简报（约 10 秒）

```bash
python agent/main.py "给我生成一份关于 Pilbara 锂矿的今日简报"
```

**加 `--explain` 可以看到 Agent 的路由决策过程**，这是本项目与固定流水线的核心区别所在：

```bash
python agent/main.py "今天锂价多少" --explain
# 输出：计划调用: get_price
# —— 只取价格，不拉新闻和储量
```

保存到文件：

```bash
python agent/main.py "Pilbara 锂矿风险简报" -o output/briefing.md
```

---

## 路径二：Docker（一键起全栈）

适合：标准化部署、快速切换环境。

```bash
docker compose up
```

首条命令会构建镜像并启动，结束后Agent 会自动生成简报到 `output/briefing.md`。

停止：`docker compose down`

### 挂到 Cursor / Claude Desktop

把项目根目录的 `mcp-config.json` 内容合并到你的客户端配置：

- **Cursor**：编辑 `~/.cursor/mcp.json`，加入 `mcpServers` 对象
- **Claude Desktop**：编辑 `claude_desktop_config.json`，加入 `mcpServers`

合并后客户端里应能看到三个 server 及其工具：

| server | 工具 |
|---|---|
| mining-news-mcp | `search`、`fetch_article` |
| mineral-pdf-mcp | `extract_resources` |
| lme-price-mcp | `get_price`、`get_trend` |

> 注意：配置里的 `command: "python"` 需指向项目 venv 的 Python 绝对路径，
> 或在已激活 venv 的终端中启动客户端。

---

## 跑测试

```bash
pytest tests/ -v
```

预期：**49 passed**。包含两类测试：

- `test_servers.py` —— 直接调用工具函数，验证业务逻辑与异常路径
- `test_mcp_protocol.py` —— **真实启动子进程走 stdio JSON-RPC**，验证 MCP 协议层连通性

---

## 项目结构

```
mining-rights-agent/
├── agent/                 Agent 编排层
│   ├── intent.py          意图解析与工具路由
│   ├── workflow.py        MCP 客户端（进程管理 / 并发 / 错误隔离）
│   ├── briefing.py        简报渲染（Markdown + 引用溯源）
│   └── main.py            入口
├── servers/               三个 MCP server
│   ├── mining_news/       search / fetch_article
│   ├── mineral_pdf/       extract_resources
│   └── lme_price/         get_price / get_trend
├── models.py              跨模块数据契约（pydantic）
├── utils.py               日志（走 stderr）与异常定义
├── data/                  本地样本数据
├── tests/                 49 个用例
├── mcp-config.json        MCP 客户端配置
├── docker-compose.yml     一键启动
└── requirements.txt       依赖（锁定版本）
```

---

## 常见问题

**Q: `mcp` 装不上，提示找不到版本**
用国内镜像 + 官方源双配置：`pip install -r requirements.txt -i https://pypi.org/simple`

**Q: server 启动报 `ModuleNotFoundError: models`**
在项目根目录运行，或确认 `PYTHONPATH=.` 已设置。

**Q: 简报里新闻段是空的**
默认用本地样本库，样本日期固定在 2026-10 前后。
若运行日期远晚于此，新闻召回会为空。设置 `MINING_NEWS_PROVIDER=remote` 可尝试实时 RSS（需外网）。

**Q: 想换成自己的数据**
三种方式，无需改代码：
1. 直接替换 `data/*.json`（字段结构见 `DATA_NOTES.md`）
2. 继承 Provider 基类实现新数据源，用环境变量切换
3. 设置 `MINING_PDF_PROVIDER=pdf` 走真实 PDF 解析