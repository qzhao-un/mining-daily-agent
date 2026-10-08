# 矿权日报 Agent

基于 **MCP（Model Context Protocol）** 协议的多源矿权信息汇总系统。

输入一句自然语言，输出一份带引用溯源的 Markdown 矿权日报。

```bash
python agent/main.py "给我生成一份关于 Pilbara 锂矿的今日简报"
```

---

## 这是什么

题目要求实现 3 个 MCP Server + 1 个 Agent Client，组合成「矿权日报」Agent。本仓库是该题目的完整实现：

| 组件 | 内容 |
|---|---|
| **mining-news-mcp** | `search(query, days)` · `fetch_article(url)` |
| **mineral-pdf-mcp** | `extract_resources(pdf_url)` —— NI 43-101 Indicated/Inferred 储量 |
| **lme-price-mcp** | `get_price(commodity, date)` · `get_trend(commodity, days)` |
| **Agent Client** | 意图解析 → 工具路由 → 并发取数 → 降级 → 简报渲染 |

Agent 输出包含题目要求的全部五部分：**新闻摘要 + 储量数据 + 价格走势 + 风险提示 + 引用源链接**。

---

## 快速开始

见 [RUN.md](RUN.md)。两条路径任选：

```bash
# 路径一：裸机
python -m venv .venv && pip install -r requirements.txt
python agent/main.py "Pilbara 锂矿今日简报"

# 路径二：Docker
docker compose up
```

测试：

```bash
pytest tests/ -v   # 49 passed
```

---

## 架构

```
用户输入
   ↓
[agent/intent.py] 意图解析 —— 识别矿种/地域/时间窗，决定调哪些工具
   ↓
[agent/workflow.py] MCP 客户端 —— 启动三个 server 子进程，并发调用
   ↓
   ├── mining-news-mcp  →  Provider 层 →  data/news_samples.json
   ├── mineral-pdf-mcp  →  Provider 层 →  data/resources_samples.json
   └── lme-price-mcp    →  Provider 层 →  data/price_samples.json
   ↓
[agent/briefing.py] 渲染 Markdown + 引用溯源 + 风险提示
```

### 三个关键设计

**1. Provider 抽象层 —— 数据源可替换**

三个 server 都不直接取数据，而是依赖 Provider 接口：

```
工具函数 → Provider 接口 → [样本数据] / [远端源] → 失败则标记 degraded
```

工具签名不随数据源变化。题目提到的 LME/SHFE 行情存在登录墙与频控限制，
24h 内无法完成合规接入 —— 本项目的处理是**把它抽象掉**而不是绕过它。
拿到正式数据授权后，只需新增一个 Provider 子类，上层工具与 Agent 完全不用改。

**2. 失败降级 —— 单点失效不阻断全局**

任一 server 崩溃、超时或无返回，编排层捕获异常并标记该数据源不可用，
简报对应段落输出「数据源暂不可用」，其余段落照常生成。
调用超时上限由 `MCP_CALL_TIMEOUT` 控制（默认 20s），避免一个挂死的 server 拖垮整个流程。

**3. 不编造数据 —— 置信度如实标注**

储量抽取时，每条记录带 `confidence` 字段。样本库数据为 `high`（已结构化校对）；
真实 PDF 正则解析为 `low`，并在简报中明确提示「以原始报告为准」。
趋势数据不足时会输出 `warning`，而非静默返回残缺结果。

> 这一点的设计动机：系统应该知道自己不知道。
> 一个诚实说「这个数字不可信」的系统，比一个永远给出精确数字的系统更有决策价值。

---

## Agent 与流水线的区别

这是本项目刻意设计的核心差异，也是面试高频追问点。

**流水线**：固定按序执行全部工具，把结果拼在一起。
**Agent**：先解析意图，再决定调用哪些工具。

| 输入 | 工具路由 | 说明 |
|---|---|---|
| `给我生成一份关于 Pilbara 锂矿的今日简报` | search → extract_resources → get_price → get_trend | 全维度简报 |
| `今天锂价多少` | **get_price** | 不拉新闻、不查储量 |
| `昨天镍价` | **get_price** | 单点查询 |
| `铁矿石近30天走势` | **get_trend** | 只看趋势 |
| `今天有什么新闻` | **search** | 只看新闻 |

加 `--explain` 可直接看到决策过程：

```bash
$ python agent/main.py "今天锂价多少" --explain
[Agent] 路由决策:
矿种=lithium | 地域=未指定 | 价格窗口=days_7(7天)
新闻窗口=14天
计划调用: get_price
```

### 为什么不用 LLM 做意图解析

24h 交付里引入 LLM 会带来 key、网络、超时三重不确定性，且无法在离线环境验证。
规则方案行为可预测、易测试、易解释。

若后续需要更强的语义理解，`parse_request()` 是唯一需要替换的函数 ——
它返回 `MiningRequest`，上层 `workflow.py` 不依赖具体实现。这是刻意的解耦。

---

## 项目结构

```
mining-rights-agent/
├── agent/
│   ├── intent.py          意图解析与工具路由
│   ├── workflow.py        MCP 客户端：进程管理 / 并发 / 错误隔离
│   ├── briefing.py        简报渲染 + 引用溯源 + 风险提示
│   └── main.py            CLI 入口
├── servers/
│   ├── mining_news/server.py
│   ├── mineral_pdf/server.py
│   └── lme_price/server.py
├── models.py              跨模块数据契约（pydantic）
├── utils.py               日志（stderr）与异常定义
├── data/                  本地样本数据（详见 DATA_NOTES.md）
├── tests/
│   ├── test_servers.py         工具逻辑与异常路径（43 例）
│   └── test_mcp_protocol.py    MCP 协议层连通性（6 例）
├── samples/               真实 PDF 样本（可选）
├── output/                生成的简报
├── mcp-config.json
├── docker-compose.yml
├── RUN.md
└── DATA_NOTES.md
```

---

## 测试

```bash
pytest tests/ -v
```

**49 个用例**，两类测试：

- **单元测试**（`test_servers.py`）直接调用工具函数，覆盖 happy path 与异常路径
  （未知矿种、周末无数据、日期格式错误、未知报告、脏数据条目）
- **集成测试**（`test_mcp_protocol.py`）**真实启动子进程走 stdio JSON-RPC**，
  验证工具能被 MCP 客户端发现、参数 schema 正确

刻意覆盖的一类用例：`test_never_raise_on_bad_input` ——
任何非法输入都必须返回结构化结果而非抛异常。因为 MCP server 一旦崩溃，
Agent 的降级能力就完全失效。

---

## 数据说明

**默认使用本地样本数据，均为合成数据，非实时行情或真实报告。**
每个工具返回值都带 `origin` 字段（`remote` / `sample` / `degraded`），
简报顶部也会声明数据来源。详见 [DATA_NOTES.md](DATA_NOTES.md)。

切到真实数据源：

```bash
MINING_NEWS_PROVIDER=remote    # 实时 RSS
MINING_PDF_PROVIDER=pdf        # 真实 PDF 解析
MINING_PRICE_PROVIDER=remote   # 远端行情（需正式授权）
```

---

## 技术栈

- **Python 3.10+**（开发验证于 3.13）
- **MCP 官方 Python SDK** `mcp==1.30.0`
  - ⚠️ 注意：不要装第三方 `fastmcp` 包，与官方 SDK **同名不同包**，
    导入写法与装饰器语法均不兼容（官方 `@mcp.tool()` 带括号，第三方不带）
- **pydantic 2** 数据契约校验
- **pdfplumber** PDF 表格解析
- **pytest** 测试

---

## 已知局限

诚实列出，便于评估：

1. **意图解析是规则式的**，覆盖中英文常见问法，复杂多意图或复杂指代会漏判
2. **无多轮对话**，用户追问需重新输入完整上下文
3. **数据样本有限**，覆盖 5 个矿种、3 份储量报告
4. **真实 PDF 解析准确率有限**，正则抽取存在歧义，故一律标 `confidence: low`
5. **远端数据源未做认证与限流**，生产环境需要补充凭证管理与重试策略