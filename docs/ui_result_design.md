# 单条评分结果页面设计

## 整体布局（替换现有右列）

```
┌──────────────────────────────────────────────────────────────┐
│  SECTION 1：评分总览卡片                                      │
├──────────────────────────────────────────────────────────────┤
│  SECTION 2：AI 评分理由（各阶段结论拼接）                      │
├──────────────────────────────────────────────────────────────┤
│  SECTION 3：信息点验证（核心，逐条展开卡片 + 来源链接）          │
├──────────────────────────────────────────────────────────────┤
│  SECTION 4：各阶段结论折叠面板                                 │
├──────────────────────────────────────────────────────────────┤
│  SECTION 5：下载按钮                                          │
└──────────────────────────────────────────────────────────────┘
```

---

## SECTION 1 — 评分总览卡片

```
┌────────────────────────────────────────────────────────────┐
│                                                            │
│   🌟  3分（优秀）                      耗时 45.2s          │
│                                                            │
│   反思前: 3分  →  最终: 3分    终止于: 正常完成             │
│                                                            │
│   信息点: 5条    ✅ 4  ❌ 0  ❓ 1    关键失败: 0/5          │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

**颜色规则**：
- 0分 → 红色背景 `st.error`
- 1分 → 橙色 `st.warning`
- 2分 → 蓝色 `st.info`
- 3分 → 绿色 `st.success`

**字段来源**：
- `score` / `preliminary_score` / `terminated_at` / `duration`
- `layer2_verification.stats`（verified_true/false/null, critical_false/total）

---

## SECTION 2 — AI 评分理由

```
┌────────────────────────────────────────────────────────────┐
│ 📝 AI 评分理由                                              │
│                                                            │
│ 【S2d事实】无事实错误，4条信息点搜索支持，1条无法验证        │
│ 【S2e幻觉】证据覆盖充分，无明确幻觉                          │
│ 【S3满足】满足 → 答案直接回答了用户的问题                    │
│ 【S4格式】格式优秀，升3分                                    │
│ 【S5反思】确认3分，无新问题                                  │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

**实现**：把 `reasoning` 字段按 `|` 分割，每段一行，保留标签前缀加粗。

---

## SECTION 3 — 信息点验证（核心区域）

### 3.1 统计摘要行（始终展示）

```
📊 共 5 条信息点    ✅ 已验证 4    ❌ 矛盾 0    ❓ 无法验证 1
                   关键信息点: ✅ 4   ❌ 0   ❓ 1
```

### 3.2 逐条展开卡片

每条 claim 默认折叠，仅展示一行摘要；点击展开查看搜索详情和来源链接。

```
┌─ C1  ⭐[客观]  ✅ 已验证（high）─────────────────────────────┐
│  2025年6月1日是2025年的第22周                                 │
│  ▶ 展开搜索详情                                              │
└──────────────────────────────────────────────────────────────┘

▼ 展开后：

┌─ C1  ⭐[客观]  ✅ 已验证（high）─────────────────────────────┐
│  2025年6月1日是2025年的第22周                                 │
│                                                              │
│  🔍 搜索词：2025年6月1日是第几周？                            │
│                                                              │
│  💬 验证结论                                                  │
│     搜索结果明确支持：第22周为5月26日至6月1日，包含6月1日      │
│                                                              │
│  📌 关键引用                                                  │
│     「2025年第22周是从5月26日到6月1日」                       │
│                                                              │
│  🔗 参考来源                                                  │
│     · 百度百科 - 2025年周历                  [打开链接 ↗]    │
│     · 日历网 - 2025年日历                    [打开链接 ↗]    │
│     · 中国节假日网                           [打开链接 ↗]    │
│                                                              │
│  📋 搜索原文摘要（可折叠）                                    │
│     【阿里云】2025年6月1日（星期日）是2025年...               │
│     【Kimi】根据ISO 8601标准，2025年第22周...                │
│                                                              │
└──────────────────────────────────────────────────────────────┘

┌─ C4  ⭐[客观]  ❌ 矛盾（high）───────────────────────────────┐
│  2025年的第22周是2025年5月28日至2025年6月3日                  │
│  ▶ 展开搜索详情                                              │
└──────────────────────────────────────────────────────────────┘

▼ 展开后（红色边框/背景）：

┌─ C4  ⭐[客观]  ❌ 矛盾（high）───────────────────────────────┐
│  2025年的第22周是2025年5月28日至2025年6月3日                  │
│                                                              │
│  🔍 搜索词：2025年第22周是几月几日到几月几日？                │
│                                                              │
│  💬 验证结论                                                  │
│     搜索明确矛盾：实际第22周为5月26日至6月1日，非5月28日起    │
│                                                              │
│  📌 关键引用（矛盾证据）                                      │
│     「2025年第22周是从5月26日到6月1日」                       │
│                                                              │
│  🔗 参考来源                                                  │
│     · 日历网                                  [打开链接 ↗]   │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**卡片颜色规则**：
- `verified=True` → 绿色左边框
- `verified=False` → 红色左边框
- `verified=None` → 灰色左边框
- `critical=True` → 显示 ⭐ 标记

**字段来源**：
```
stage0_parsing.claims[i]           → claim文本、type、critical、query
stage1_searches.searches[i]        → search_result.sources[{title,url,snippet}]
                                     search_result.individual_results[{provider,content}]
layer2_verification.all_verifications[i]  → verified、reason、quoted_evidence、confidence
```

**链接实现**：`st.link_button(title, url)` 或 `st.markdown(f"[{title}]({url})")`

---

## SECTION 4 — 各阶段结论折叠面板

每个阶段一个 `st.expander`，默认折叠，有问题的自动展开。

```
▼ 1️⃣  S1 红线检查                               ✅ 通过
   reasoning: "问题类型判断：正常，非医疗/违法类..."

▼ 2️⃣  S2_ 内部一致性                            ✅ 无问题
   无严重问题，无轻微问题

▼ 2️⃣  S2d 事实错误判定                          ✅ 无事实错误
   reasoning: "逐条分析：C4 verified=false 但..."
   non_errors: ["C4: 证据冲突属于..."]

▼ 2️⃣  S2e 全局幻觉检测                          ✅ 无幻觉
   evidence_quality: sufficient
   checked_claims_count: 5

▼ 3️⃣  S3 满足度评估                             📊 满足 → 2分
   satisfaction_level: 满足
   main_content_ratio: 0.95
   completeness_issues: []
   score_reason: "答案直接回答了用户问题..."

▼ 4️⃣  S4 格式检查                               ✨ 优秀 → 升3分
   format_quality: 优秀
   has_bold: ✅  has_list: ❌  has_heading: ✅
   upgrade_to_3: true

▼ 5️⃣  S5 反思复核                               🔄 确认3分
   needs_correction: false
   confidence: 高
   potential_issues: []
```

---

## SECTION 5 — 下载按钮

```
[ 📥 摘要 Excel ]    [ 📄 详细报告（多Sheet） ]
```

---

## 数据拼接逻辑（claims ↔ searches ↔ verifications）

三组数据按 `claim_index` 对齐：

```python
# stage0_parsing.claims: 按顺序，index从0开始
# stage1_searches.searches: 每项有 claim_index（1-based）
# layer2_verification.all_verifications: 每项有 claim_index（1-based）

def build_claim_rows(result):
    claims = result["stage0_parsing"]["claims"]

    search_map = {
        s["claim_index"]: s["search_result"]
        for s in result["stage1_searches"]["searches"]
    }
    verify_map = {
        v["claim_index"]: v["verify_result"]
        for v in result["layer2_verification"]["all_verifications"]
    }

    rows = []
    for i, claim in enumerate(claims, 1):
        rows.append({
            "claim":  claim,
            "search": search_map.get(i, {}),
            "verify": verify_map.get(i, {}),
        })
    return rows
```

---

## 实现优先级

| 优先级 | 内容 | 工作量 |
|---|---|---|
| P0 | SECTION 1 评分总览卡片 | 小 |
| P0 | SECTION 3 claims 卡片（含来源链接） | 中 |
| P1 | SECTION 2 理由分段展示 | 小 |
| P1 | SECTION 4 各阶段折叠面板 | 中 |
| P2 | SECTION 3 搜索原文摘要折叠 | 小 |

总计约 150-200 行新代码，替换现有右列约 70 行。
