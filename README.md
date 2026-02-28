# AI 评分工作台

基于多阶段 LLM 流水线的 AI 答案质量评分系统。通过**联网搜索 + 多模型交叉验证**，对 AI 回答进行事实核查和质量评分，输出 0-3 分制评分结果。

---

## 核心能力

- **事实核查**：将答案拆解为独立信息点，逐条联网搜索验证
- **多源验证**：aliyun + kimi 双搜索引擎，避免单一来源偏差
- **多阶段评分**：红线 → 一致性 → 事实 → 满足度 → 格式 → 反思，层层把关
- **快速失败**：任意阶段发现严重问题立即终止，节省后续计算资源
- **批量处理**：支持 Excel 批量上传，并发处理，自动保存进度
- **历史记录**：自动保存每次评分，支持随时回溯查看详情

---

## 评分标准

| 分数 | 含义 |
|------|------|
| 3分 | 内容正确、满足意图、格式优秀 |
| 2分 | 内容正确、满足意图 |
| 1分 | 内容基本正确，但不完整或存在小问题 |
| 0分 | 存在事实错误、红线问题、或完全未满足用户意图 |

---

## 评分流水线

```
S1  红线检查          ← 政治敏感、违规、危险内容
S1b 内部一致性        ← 答案自身逻辑矛盾（并行执行）
S2a 信息点拆解        ← 按 5W1H 原则拆解为原子信息点
S2b 并行联网搜索      ← 每条信息点独立搜索（aliyun + kimi）
S2c 信息点验证        ← 多源投票，判定 true / false / null
S2d 事实错误判定      ← 汇总验证结果，判定是否有事实错误
S2e 幻觉检测          ← 全局对照搜索内容，补充单条验证盲点
S3  满足度评估        ← 判断答案是否满足用户意图，给出 1 或 2 分
S4  格式检查          ← 判断格式是否优秀，决定是否升为 3 分
S5  反思复核          ← 独立二次审查，只能维持或降分
```

详细流程图见 [`doc/pipeline_lifecycle.md`](doc/pipeline_lifecycle.md)。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

编辑 `config.json`，填入以下密钥：

```json
{
  "api_keys": {
    "gpt5":   "sk-xxx",   // GPT-5 或中转 API（用于 S1b/S2a/S2c/S2d/S2e）
    "kimi":   "sk-xxx",   // Kimi K2.5（用于 S1/S3/S4/S5 及搜索）
    "aliyun": "sk-xxx"    // 阿里云百炼（用于搜索）
  }
}
```

也可在「⚙️ 设置」页面通过 UI 配置后保存。

### 3. 启动

```bash
streamlit run app.py
```

浏览器打开 `http://localhost:8501`。

---

## 界面功能

### 📊 评分

**单条评分**：输入问题和答案，点击「开始评分」，查看详细评分结果。结果包含：
- 总分卡片（分数、耗时、终止阶段）
- 各阶段详情折叠面板
- 信息点验证明细（每条 claim 的搜索结果和验证结论）
- 下载 Excel 详细报告（含摘要、信息点验证、各阶段输出三个 Sheet）

**批量评分**：上传 Excel 文件，配置并发数，批量处理后下载结果。
- 支持列名：`用户问题` / `query`、`Original` / `答案`、`查询时间`（可选）
- 自动保存进度，支持中途恢复

### ⚙️ 设置

- LLM API Key 和 Base URL 配置
- 各阶段默认使用的模型（可独立配置每个阶段）
- 批量处理默认参数

### 📜 历史记录

查看历史单条评分和批量评分记录，支持重新查看详情。

---

## 文件结构

```
AI评分工作台/
├── app.py                          # Streamlit 主程序（3 个 Tab）
├── auto_scoring_pipeline.py        # V4 评分流水线核心
├── answer_parser.py                # S2a 信息点拆解器（5W1H 原则）
├── multi_source_voting_verifier.py # S2c 多源投票验证器
├── search_integration.py           # 搜索集成（聚合多个搜索提供商）
├── search_providers.py             # 搜索服务封装（aliyun / kimi / zhipu）
├── rate_limiter.py                 # API 速率限制器（1s滑动窗口）
├── excel_auto_scorer.py            # Excel 批量评分工具
├── config.json                     # 配置文件（API Key、模型、参数）
├── requirements.txt
├── prompts/
│   └── pipeline/                   # 各阶段 LLM Prompt
│       ├── s1_redline.txt
│       ├── s1b_consistency.txt
│       ├── s2a_parse_claims.txt
│       ├── s2b_search_query.txt
│       ├── s2c_verify_claim.txt
│       ├── s2d_fact_error.txt
│       ├── s2e_hallucination.txt
│       ├── s3_satisfaction.txt
│       ├── s4_format.txt
│       └── s5_reflection.txt
├── history/                        # 历史记录（自动生成）
│   ├── single_scoring.jsonl
│   └── batch/
├── doc/                            # 技术文档
│   ├── pipeline_lifecycle.md       # 流水线生命周期详解
│   └── search_flow.md              # 搜索阶段详细流程
└── results/                        # 批量结果输出（自动生成）
```

---

## 各阶段模型配置

每个阶段可以独立配置使用不同的模型，在「⚙️ 设置」页面调整默认值。

| 阶段 | 默认模型 | 说明 |
|------|----------|------|
| S1 红线检查 | kimi | 需要较强的安全意识判断 |
| S1b 内部一致性 | gpt5 | 逻辑分析能力 |
| S2a 信息点拆解 | gpt5 | 结构化输出，需要 JSON 格式 |
| S2c 信息点验证 | gpt5 | 快速批量判断 |
| S2d 事实错误判定 | gpt5 | 综合推理 |
| S2e 幻觉检测 | gpt5 | 全局对照 |
| S3 满足度评估 | kimi | 意图理解 |
| S4 格式检查 | kimi | 格式感知 |
| S5 反思复核 | kimi_thinking | 深度思考模式，二次审查 |

可用模型：`gpt5`、`gpt5_thinking`、`kimi`、`kimi_thinking`

---

## 性能参考

以 10 条信息点为例（正常完成全流程）：

| 阶段 | 耗时 | 说明 |
|------|------|------|
| S1 + S1b | ~4s | 并行执行 |
| S2a | ~9-14s | 单次 LLM 调用 |
| S2b 搜索 | ~30-60s | **瓶颈**，10条并发，取决于最慢那条 |
| S2c 验证 | ~7s | 10条并发 LLM 调用 |
| S2d + S2e | ~8s | 各1次 LLM 调用 |
| S3 + S4 + S5 | ~21s | 顺序执行 |
| **总计** | **~80-110s** | |

批量处理建议并发数：**5-10**（受搜索 API 速率限制，过高收益递减）。
