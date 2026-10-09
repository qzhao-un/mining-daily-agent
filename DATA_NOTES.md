# DATA_NOTES.md —— 数据说明

本文件说明项目使用的数据结构、字段含义、来源与主键策略。

---

## ⚠️ 数据来源声明（请先读这一节）

**本项目默认使用本地样本数据，均为合成数据，不冒充实时行情或真实矿权报告。**

| 数据源 | 默认来源 | 是否实时 | 切换方式 |
|---|---|---|---|
| 矿业新闻 | `data/news_samples.json` | ❌ 合成样本 | `MINING_NEWS_PROVIDER=remote` |
| NI 43-101 储量 | `data/resources_samples.json` | ❌ 合成样本 | `MINING_PDF_PROVIDER=pdf` + 本地 PDF |
| LME 价格 | `data/price_samples.json` | ❌ 合成样本 | `MINING_PRICE_PROVIDER=remote`（需授权） |

如此设计默认值的原因：LME/SHFE 官方行情接口存在**登录墙与频控限制**，
未授权情况下无法合规接入。本项目选择将数据源**抽象为 Provider 层接口**，
而非绕过访问限制。取得正式数据授权后，新增一个 Provider 子类即可，
上层工具与 Agent 编排无需改动。

每个工具返回值都带 `origin` 字段，简报顶部也会声明数据来源：

- `remote` —— 实时数据源
- `sample` —— 本地样本数据（合成）
- `degraded` —— 远端不可用，已回落或失败

---

## 1. 新闻数据 `data/news_samples.json`

### Schema

```json
{
  "_meta": {
    "description": "数据说明",
    "source_policy": "数据性质声明",
    "last_updated": "YYYY-MM-DD"
  },
  "articles": [
    {
      "title": "新闻标题",
      "url": "原文链接",
      "source": "来源媒体",
      "published_at": "YYYY-MM-DD",
      "summary": "摘要正文"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | string | ✅ | 新闻标题 |
| `url` | string | ✅ | 原文链接，必须以 `http://` / `https://` 开头 |
| `source` | string | ✅ | 来源媒体，如 `mining.com` |
| `published_at` | string | ✅ | 发布日期 ISO8601 |
| `summary` | string | — | 摘要 |

### 主键与去重策略

- **主键**：`url`
- **去重**：本项目未做跨源去重（单一数据源）。若扩展到多源，需按 URL 归一化后去重；
  标题相似度去重（如 SimHash）留作后续能力
- **容错**：单条记录解析失败会被跳过并记录 warning，不影响其余条目

### 检索规则

- 关键词按空格/逗号切分，采用 **OR 语义**（任一命中即收录）
  - 理由：采用 AND 会让召回率过低，短查询几乎必然返回空
- 时间窗按 `published_at` 过滤
- **新闻窗口下限 14 天**：矿产新闻低频，若严格按「今日」过滤 1 天，召回必然为空

---

## 2. 储量数据 `data/resources_samples.json`

### Schema

```json
{
  "_meta": { "spec": "NI 43-101 (JORC-aligned)" },
  "reports": {
    "<report_key>": {
      "source_url": "报告链接",
      "document_title": "报告标题",
      "page_count": 148,
      "resources": [
        {
          "category": "Indicated",
          "ore_tonnage": 78.4,
          "grade": 1.21,
          "grade_unit": "% Li2O",
          "contained_metal": 2380000,
          "metal_unit": "t Li",
          "commodity": "Lithium",
          "page": 87
        }
      ]
    }
  }
}
```

### 字段说明（对齐 NI 43-101 储量表）

| 字段 | 类型 | 单位 | 说明 |
|---|---|---|---|
| `category` | enum | — | `Measured` / `Indicated` / `Inferred` |
| `ore_tonnage` | float | Mt | 矿石量 |
| `grade` | float | `% Li2O` / `g/t Au` / `% Cu` | 品位 |
| `grade_unit` | string | — | 品位单位 |
| `contained_metal` | float | `t` / `oz` | 金属量 |
| `metal_unit` | string | — | 金属量单位 |
| `commodity` | string | — | 矿种 |
| `page` | int | — | 所在页码（用于溯源） |
| `confidence` | enum | — | `high` / `medium` / `low` |

### 主键策略

- **报告级主键**：`source_url`
- **记录级复合主键**：`(report_key, category, page, commodity)`
- **查询匹配顺序**（`SampleResourceProvider._match`）：
  1. 精确匹配 `source_url`
  2. `report_key` 包含关系双向匹配（支持 `pilbara_greenbushes` 或 `greenbushes`）
  3. 文档标题包含匹配
  - 全部失败返回 `found=False` 并列出可用 key

### 置信度约定

| 来源 | confidence | 含义 |
|---|---|---|
| 样本库 | `high` | 字段已结构化校对，可信 |
| 真实 PDF 正则解析 | `low` | 存在歧义，以原始报告为准 |

**抽不准就不填数字。** 这是本项目的核心原则之一：
返回空值 + 低置信度，好过返回一个看起来精确实则臆造的数值。

---

## 3. 价格数据 `data/price_samples.json`

### Schema

```json
{
  "_meta": {
    "currencies": { "lithium": "USD/t", "iron_ore": "USD/dmt" }
  },
  "series": {
    "lithium": [
      { "date": "2026-09-08", "price": 8420.0 }
    ]
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `date` | string | 交易日 `YYYY-MM-DD` |
| `price` | float | 价格 |

### 支持的矿种与单位

| key | 别名 | 单位 |
|---|---|---|
| `lithium` | `Li`, `锂` | USD/t |
| `copper` | `Cu`, `铜` | USD/t |
| `nickel` | `Ni`, `镍` | USD/t |
| `zinc` | `Zn`, `锌` | USD/t |
| `iron_ore` | `Fe`, `铁矿石` | USD/dmt |

### 主键与覆盖范围

- **主键**：`(commodity, date)`
- **覆盖**：5 个矿种 × 23 个交易日（2026-09-08 ~ 2026-10-08）
- **交易日**：已剔除周末。查询周末会明确返回「是周末或节假日，无交易数据」

### 数据不足时的行为

请求天数超过样本容量时，返回结果中会带 `warning`：

```
请求 30 个交易日，实际仅取到 23 个（2026-09-08 ~ 2026-10-08）。
```

**静默返回残缺数据是不允许的** —— 使用者必须知道结论的可靠性边界。

---

## 扩展数据源的方式

不需要改动工具函数或 Agent 编排：

1. **替换样本文件**：保持上述 JSON 结构，修改 `data/*.json` 即可
2. **实现新 Provider**：继承对应基类，实现接口方法，用环境变量切换

```python
class MyProvider(PriceProvider):
    def get_price(self, commodity, target_date): ...
    def get_series(self, commodity, days): ...
```

3. **接入真实 PDF**：设置 `MINING_PDF_PROVIDER=pdf`，传入本地 PDF 路径

生产环境还需要补充：凭证管理、请求重试、限流、缓存策略。