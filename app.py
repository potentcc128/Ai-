#!/usr/bin/env python3
"""
AI 评分工作台 - 独立全功能 Streamlit 应用
4个顶部 Tab：评分 / 调试 / 提示词 / 设置
"""

import json
import os
import io
import re
import time
import traceback
import tempfile
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────
# 页面配置（必须第一行）
# ──────────────────────────────────────────────
st.set_page_config(
    page_title="AI 评分工作台",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────
CONFIG_FILE   = Path(__file__).parent / "config.json"
HISTORY_FILE      = Path(__file__).parent / "history" / "single_scoring.jsonl"
BATCH_HISTORY_DIR = Path(__file__).parent / "history" / "batch"

_MODEL_OPTS = {
    "gpt5": "GPT-5.2 普通",
    "gpt5_thinking": "GPT-5.2 Thinking",
    "kimi": "Kimi K2.5 普通",
    "kimi_thinking": "Kimi K2.5 Thinking",
}
_MODEL_KEYS = list(_MODEL_OPTS.keys())

_STAGE_DEFAULTS = {
    "baseline_checker": "kimi",
    "answer_consistency_checker": "kimi",
    "answer_parser": "kimi",
    "voting_verifier": "kimi_thinking",
    "fact_error_checker": "kimi_thinking",
    "hallucination_checker": "kimi_thinking",
    "satisfaction_evaluator": "kimi",
    "format_checker": "kimi",
    "reflection_checker": "kimi_thinking",
}


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def append_history(result: dict, question: str, answer: str):
    """追加一条单条评分记录到 JSONL 文件"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "saved_at": datetime.now().isoformat(),
        "question": question,
        "answer": answer,
        "result": result,
    }
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history() -> list:
    """从 JSONL 文件加载所有历史记录，最新在前"""
    if not HISTORY_FILE.exists():
        return []
    records = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return list(reversed(records))


def save_batch_history(jsonl_bytes: bytes, fname: str):
    """将批量评分 JSONL 保存到 history/batch/"""
    BATCH_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(BATCH_HISTORY_DIR / fname, "wb") as f:
        f.write(jsonl_bytes)


def load_batch_jsonl(path) -> list:
    """加载一个批量 JSONL 文件，返回 result list"""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def get_stage_defaults(cfg: dict) -> dict:
    """从 config 读取各阶段默认模型，回退到内置默认值"""
    return {**_STAGE_DEFAULTS, **cfg.get("stage_models_default", {})}


def render_model_config(prefix: str, cfg: dict) -> dict:
    """渲染各阶段模型配置 expander，返回 stage_models dict"""
    defaults = get_stage_defaults(cfg)

    def _idx(key):
        v = defaults.get(key, "gpt5")
        return _MODEL_KEYS.index(v) if v in _MODEL_KEYS else 0

    with st.expander("⚙️ 各阶段模型配置", expanded=False):
        st.caption("Thinking 模式推理更严谨但速度较慢，建议关键判断阶段使用。")
        c1, c2, c3 = st.columns(3)
        with c1:
            bl = st.selectbox("🔴 S1 红线检查", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("baseline_checker"), key=f"{prefix}_bl")
            co = st.selectbox("🔍 S1b 内部一致性", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("answer_consistency_checker"), key=f"{prefix}_co")
            pr = st.selectbox("📋 S2a 信息点拆解", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("answer_parser"), key=f"{prefix}_pr")
        with c2:
            vr = st.selectbox("🔍 S2c 信息点验证", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("voting_verifier"), key=f"{prefix}_vr")
            fc = st.selectbox("⚖️ S2d 事实错误判定", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("fact_error_checker"), key=f"{prefix}_fc")
            ha = st.selectbox("🌀 S2e 幻觉检测", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("hallucination_checker"), key=f"{prefix}_ha")
        with c3:
            sa = st.selectbox("📊 S3 满足度评估", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("satisfaction_evaluator"), key=f"{prefix}_sa")
            fm = st.selectbox("✨ S4 格式检查", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("format_checker"), key=f"{prefix}_fm")
            rf = st.selectbox("🔄 S5 反思检查", _MODEL_KEYS,
                              format_func=lambda x: _MODEL_OPTS[x],
                              index=_idx("reflection_checker"), key=f"{prefix}_rf")

    return {
        "baseline_checker": bl,
        "answer_consistency_checker": co,
        "answer_parser": pr,
        "voting_verifier": vr,
        "fact_error_checker": fc,
        "hallucination_checker": ha,
        "satisfaction_evaluator": sa,
        "format_checker": fm,
        "reflection_checker": rf,
    }


def build_pipeline(cfg: dict, enable_search: bool, stage_models: dict):
    from auto_scoring_pipeline import AutoScoringPipeline
    return AutoScoringPipeline(
        str(CONFIG_FILE),
        enable_search=enable_search,
        stage_models=stage_models,
    )


def format_result_for_excel(result: dict) -> dict:
    """将 pipeline.score() 结果格式化为 Excel 行"""
    layer1 = result.get("layer1_baseline") or {}
    stage0 = result.get("stage0_parsing") or {}
    stage1 = result.get("stage1_searches") or {}
    layer2 = result.get("layer2_verification") or {}
    layer3 = result.get("layer3_quality") or {}
    reflection = result.get("reflection") or {}
    fact_check = result.get("stage2_fact_check") or {}

    s0_stats = stage0.get("stats") or {}
    l2_stats = layer2.get("stats") or {}

    details = []
    for v in layer2.get("all_verifications", []):
        vr = v.get("verify_result") or {}
        if vr.get("verified") is False:
            details.append(f"❌{v.get('claim',{}).get('claim','')[:50]} ({vr.get('reason','')[:80]})")
        elif vr.get("verified") is None:
            details.append(f"⚠️{v.get('claim',{}).get('claim','')[:50]} ({vr.get('reason','')[:80]})")

    return {
        "AI评分": result.get("score"),
        "AI评分理由": (result.get("reasoning") or "")[:800],
        "阶段1_红线检查": ("通过" if not layer1.get("has_fatal_issue")
                        else f"失败: {layer1.get('issue_type', '')}") if layer1 else "未检查",
        "信息点总数": stage0.get("total_claims", 0),
        "客观信息点": s0_stats.get("objective", 0),
        "主观信息点": s0_stats.get("subjective", 0),
        "搜索次数": len(stage1.get("searches", [])),
        "验证通过": l2_stats.get("verified_true", 0),
        "验证失败": l2_stats.get("verified_false", 0),
        "无法验证": l2_stats.get("verified_null", 0),
        "关键信息点": f"{l2_stats.get('critical_true',0)}/{l2_stats.get('critical_total',0)}",
        "验证失败详情": "\n".join(details[:5]) + (f"\n...还有{len(details)-5}个" if len(details) > 5 else ""),
        "满足度": (layer3.get("satisfaction") or {}).get("satisfaction_level", ""),
        "质量评分": (layer3.get("quality") or {}).get("final_score", ""),
        "阶段5_反思检查": (("确认" if not reflection.get("needs_correction")
                        else f"修正: {reflection.get('correction_reason','')[:80]}") if reflection else ""),
        "处理时间": result.get("timestamp", datetime.now().isoformat()),
        "耗时(秒)": round(result.get("duration", 0), 1),
        "状态": "✓ 已完成" if result.get("score") is not None else "❌ 失败",
    }


def build_single_export(result: dict) -> io.BytesIO:
    """构建单条评分的多-Sheet Excel 报告"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Sheet1: 摘要
        pd.DataFrame([format_result_for_excel(result)]).to_excel(writer, sheet_name="摘要", index=False)

        # Sheet2: 信息点验证明细
        layer2 = (result.get("layer2_verification") or {})
        claim_rows = []
        for v in layer2.get("all_verifications", []):
            c = v.get("claim") or {}
            vr = v.get("verify_result") or {}
            sr = v.get("search_result") or {}
            sources = sr.get("sources") or []
            vv = vr.get("verified")
            claim_rows.append({
                "ID": c.get("id", ""),
                "类型": c.get("type", ""),
                "关键信息点": "是" if c.get("critical") else "否",
                "信息点全文": c.get("claim", ""),
                "搜索查询": sr.get("query", ""),
                "搜索提供商": ", ".join(sr.get("providers", [])),
                "来源数量": len(sources),
                "来源URL": "\n".join([s.get("url", "") for s in sources[:5]]),
                "验证结果": "通过" if vv is True else ("失败" if vv is False else "无法验证"),
                "置信度": vr.get("confidence", ""),
                "验证理由": vr.get("reason", ""),
                "引用证据": "\n".join((vr.get("quoted_evidence") or [])[:3]),
                "搜索内容摘要": (sr.get("content", "")[:500] + "…") if len(sr.get("content", "")) > 500 else sr.get("content", ""),
            })
        if claim_rows:
            pd.DataFrame(claim_rows).to_excel(writer, sheet_name="信息点验证", index=False)

        # Sheet3: 各阶段 LLM 输出
        stage_rows = []
        l1 = result.get("layer1_baseline") or {}
        stage_rows.append({
            "阶段": "1. 红线检查",
            "结论": "通过" if not l1.get("has_fatal_issue") else f"失败: {l1.get('issue_type','')}",
            "详细输出": l1.get("reasoning", ""),
            "关键字段": f"pass={l1.get('pass')}, issue_type={l1.get('issue_type')}",
        })
        s0 = result.get("stage0_parsing") or {}
        if s0.get("error"):
            stage_rows.append({
                "阶段": "2a. 信息点拆解",
                "结论": f"❌ 失败: {s0['error'][:80]}",
                "详细输出": s0.get("error", ""),
                "关键字段": "pipeline早退，后续阶段均未运行",
            })
        else:
            fc = result.get("stage2_fact_check")
            stage_rows.append({
                "阶段": "2-4. 事实错误判定",
                "结论": ("有事实错误" if fc.get("has_factual_error") else "无事实错误") if fc else "未运行",
                "详细输出": fc.get("reasoning", "") if fc else "",
                "关键字段": json.dumps(fc.get("factual_errors", []), ensure_ascii=False) if fc else "",
            })
            sat = result.get("stage3_satisfaction")
            stage_rows.append({
                "阶段": "3. 满足度评估",
                "结论": f"{sat.get('satisfaction_level','?')} → {sat.get('score','?')}分" if sat else "未运行",
                "详细输出": (sat.get("reasoning") or sat.get("score_reason", "")) if sat else "",
                "关键字段": f"score={sat.get('score')}, level={sat.get('satisfaction_level')}" if sat else "",
            })
            fmt = result.get("stage4_format")
            stage_rows.append({
                "阶段": "4. 格式检查",
                "结论": f"{fmt.get('format_quality','?')} → upgrade={fmt.get('upgrade_to_3')}" if fmt else "未运行",
                "详细输出": fmt.get("reasoning", "") if fmt else "",
                "关键字段": json.dumps(fmt.get("format_details", {}), ensure_ascii=False) if fmt else "",
            })
            ref = result.get("reflection")
            stage_rows.append({
                "阶段": "5. 反思检查",
                "结论": (f"最终{ref.get('final_confirmed_score','?')}分" + (" (修正)" if ref.get("needs_correction") else " (确认)")) if ref else "未运行",
                "详细输出": ref.get("reasoning", "") if ref else "",
                "关键字段": f"needs_correction={ref.get('needs_correction')}, final_score={ref.get('final_confirmed_score')}" if ref else "",
            })
        pd.DataFrame(stage_rows).to_excel(writer, sheet_name="各阶段输出", index=False)

    buf.seek(0)
    return buf


# ──────────────────────────────────────────────
# 信息点详情弹窗
# ──────────────────────────────────────────────

@st.dialog("信息点详情", width="large")
def _claim_detail_dialog():
    d = st.session_state.get("_claim_dialog_data") or {}
    c_query     = d.get("c_query", "")
    reason      = d.get("reason", "")
    quoted      = d.get("quoted") or []
    ind_results = d.get("ind_results") or []
    header      = d.get("header", "")

    st.markdown(f"**{header}**")
    st.divider()
    if c_query:
        st.caption(f"🔍 {c_query}")
    if reason:
        st.markdown(f"> {reason}")
    if quoted:
        for q in quoted:
            st.markdown(f"📌 「{q}」")
    if ind_results:
        st.markdown("**🔗 搜索来源**")
        for ir in ind_results:
            provider   = ir.get("provider", "")
            content    = ir.get("content", "")
            ir_sources = ir.get("sources") or []
            valid_srcs = [s for s in ir_sources if s.get("url")][:3]
            st.caption(f"【{provider}】")
            if content:
                st.markdown(content[:400] + ("…" if len(content) > 400 else ""))
            for s in valid_srcs:
                title = s.get("title") or "（无标题）"
                url   = s.get("url", "")
                st.markdown(f"　↗ [{title}]({url})")



# ──────────────────────────────────────────────
# 单条评分结果面板
# ──────────────────────────────────────────────

def _render_result_panel(result: dict, key_prefix: str = "r"):
    """评分结果面板：总览卡片 → 输入上下文 → 左右分栏（理由+阶段 | 信息点） → 下载"""

    score        = result.get("score")
    preliminary  = result.get("preliminary_score")
    terminated_at= result.get("terminated_at")
    duration     = result.get("duration", 0)
    question     = result.get("user_question", "")
    answer       = result.get("original_answer", "")
    query_time   = result.get("query_time", "")

    layer2   = result.get("layer2_verification") or {}
    l2_stats = layer2.get("stats") or {}
    vt = l2_stats.get("verified_true", 0)
    vf = l2_stats.get("verified_false", 0)
    vn = l2_stats.get("verified_null", 0)
    cf = l2_stats.get("critical_false", 0)
    ct = l2_stats.get("critical_total", 0)
    total_claims = vt + vf + vn

    # ── score=None：流水线异常终止 ──
    if score is None:
        err_reason = result.get("reasoning") or "未知错误"
        s0_err = (result.get("stage0_parsing") or {}).get("error", "")
        err_detail = s0_err if s0_err else err_reason
        st.error(f"**⚠️ 评分未完成**　　耗时 {duration:.1f}s\n\n{err_detail}")

    # ── 评分总览卡片 ──
    else:
        _labels = {0: "0分（错误）", 1: "1分（较差）", 2: "2分（良好）", 3: "3分（优秀）"}
        _emojis = {0: "❌", 1: "⚠️", 2: "✅", 3: "🌟"}
        label = _labels.get(score, str(score))
        emoji = _emojis.get(score, "❓")

        pre_str = (f"反思前: {preliminary}分  →  最终: {score}分"
                   if preliminary is not None and preliminary != score
                   else f"最终: {score}分")
        _term_labels = {
            "s1_redline":        "S1 红线检查",
            "s1b_consistency":    "S1b 内部一致性严重问题",
            "s2c_fast_exit":     "S2c 搜索发现矛盾断言",
            "s2d_fact_error":    "S2d 事实错误判定",
            "s2e_hallucination": "S2e 幻觉检测",
            "s3_satisfaction":   "S3 满足度不满足",
        }
        term_str = f"终止于 {_term_labels.get(terminated_at, terminated_at)} → 直接0分" if terminated_at else "正常完成"

        card_md = (f"**{emoji}  {label}**　　耗时 {duration:.1f}s\n\n"
                   f"{pre_str}　　{term_str}\n\n"
                   f"信息点 {total_claims} 条　　✅ {vt}  ❌ {vf}  ❓ {vn}　　关键失败: {cf}/{ct}")

        if score == 3:
            st.success(card_md)
        elif score == 2:
            st.info(card_md)
        elif score == 1:
            st.warning(card_md)
        else:
            st.error(card_md)

    # ── 各阶段详情（折叠，默认收起；有严重问题时自动展开） ──
    _has_any_error = bool(
        (result.get("layer1_baseline") or {}).get("has_fatal_issue")
        or (result.get("stage_consistency") or {}).get("critical_issues")
        or (result.get("stage2_fact_check") or {}).get("has_factual_error")
        or (result.get("stage2b_hallucination") or {}).get("has_hallucination")
        or (result.get("reflection") or {}).get("needs_correction")
    )
    with st.expander("📋 各阶段详情", expanded=(_has_any_error or score is None)):
        # S1 红线
        l1 = result.get("layer1_baseline") or {}
        if l1:
            passed = not l1.get("has_fatal_issue")
            s1_status = "✅ 通过" if passed else f"❌ {l1.get('issue_type', '失败')}"
            st.markdown(f"**1️⃣ S1 红线　　{s1_status}**")
            st.markdown(l1.get("reasoning") or l1.get("detail") or "无详情")
            st.markdown("---")

        # S2a 解析（只在失败时显示）
        s0 = result.get("stage0_parsing") or {}
        if s0.get("error"):
            st.error(f"**📋 S2a 信息点拆解　　❌ 失败**")
            st.markdown(f"错误原因：`{s0['error']}`")
            st.caption("后续搜索、验证、满足度、格式、反思阶段均未运行")
            st.markdown("---")

        # S2_ 内部一致性
        cons = result.get("stage_consistency") or {}
        if cons:
            has_issue = cons.get("has_consistency_issue") or cons.get("has_critical_issue")
            st.markdown(f"**2️⃣ S1b 一致性　　{'❌ 有问题' if has_issue else '✅ 无问题'}**")
            for x in (cons.get("critical_issues") or []):
                st.error(x.get("problem", str(x)) if isinstance(x, dict) else str(x))
            for x in (cons.get("minor_issues") or []):
                st.warning(x.get("problem", str(x)) if isinstance(x, dict) else str(x))
            if cons.get("reasoning"):
                st.markdown(cons["reasoning"])
            if not cons.get("critical_issues") and not cons.get("minor_issues"):
                st.caption("无一致性问题")
            st.markdown("---")

        # S2d 事实错误
        fc = result.get("stage2_fact_check") or {}
        if fc:
            has_err = fc.get("has_factual_error")
            err_count = len(fc.get("factual_errors") or [])
            st.markdown(f"**2️⃣ S2d 事实　　{'❌ ' + str(err_count) + '个错误' if has_err else '✅ 无错误'}**")
            if fc.get("reasoning"):
                st.markdown(fc["reasoning"])
            for err in (fc.get("factual_errors") or []):
                st.error(str(err))
            for ne in (fc.get("non_errors") or []):
                st.caption(f"排除: {ne}")
            st.markdown("---")

        # S2e 幻觉
        hall = result.get("stage2b_hallucination") or {}
        if hall:
            has_hall = hall.get("has_hallucination")
            st.markdown(f"**2️⃣ S2e 幻觉　　{'❌ 发现幻觉' if has_hall else '✅ 无幻觉'}**")
            ev_q = hall.get("evidence_quality", "")
            if ev_q:
                st.caption(f"证据质量: {ev_q}　已检查: {hall.get('checked_claims_count', '?')} 条")
            if hall.get("reasoning"):
                st.markdown(hall["reasoning"])
            st.markdown("---")

        # S3 满足度
        sat = result.get("stage3_satisfaction") or {}
        if sat:
            sat_level = sat.get("satisfaction_level", "?")
            sat_score = sat.get("score", "?")
            st.markdown(f"**3️⃣ S3 满足度　　📊 {sat_level} → {sat_score}分**")
            reason_text = sat.get("score_reason") or sat.get("reasoning") or ""
            if reason_text:
                st.markdown(reason_text)
            for issue in (sat.get("completeness_issues") or []):
                st.warning(str(issue))
            ratio = sat.get("main_content_ratio")
            if ratio is not None:
                st.caption(f"主内容占比: {ratio:.0%}")
            st.markdown("---")

        # S4 格式
        fmt = result.get("stage4_format") or {}
        if fmt:
            fq = fmt.get("format_quality", "?")
            upgrade = fmt.get("upgrade_to_3")
            st.markdown(f"**4️⃣ S4 格式　　✨ {fq}" + (" → 升3分**" if upgrade else "**"))
            fd = fmt.get("format_details") or {}
            if fd:
                checks = [f"{zh}: {'✅' if fd.get(k) else '❌'}"
                          for k, zh in [("has_bold","加粗"),("has_list","列表"),("has_heading","标题")]
                          if fd.get(k) is not None]
                if checks:
                    st.caption("  ".join(checks))
            if fmt.get("reasoning"):
                st.markdown(fmt["reasoning"])
            st.markdown("---")

        # S5 反思
        ref = result.get("reflection") or {}
        if ref:
            corrected   = ref.get("needs_correction")
            final_score = ref.get("final_confirmed_score")
            conf        = ref.get("confidence", "")
            s5_label    = "修正" if corrected else "确认"
            st.markdown(f"**5️⃣ S5 反思　　🔄 {s5_label} {final_score}分" + (f"（{conf}）**" if conf else "**"))
            if ref.get("reasoning"):
                st.markdown(ref["reasoning"])
            if corrected:
                st.warning(f"修正原因: {ref.get('correction_reason', '')}")
            for issue in (ref.get("potential_issues") or []):
                st.caption(f"潜在问题: {issue}")

    # ── 左右分栏：AI 评分理由 | 信息点验证 ──
    col_left, col_right = st.columns([1, 2])

    # ════ 左列：AI 评分理由 ════
    with col_left:
        reasoning = result.get("reasoning") or ""
        if reasoning:
            st.markdown("**📝 AI 评分理由**")
            parts = [p.strip() for p in reasoning.split("|") if p.strip()]
            for p in parts:
                p_fmt = re.sub(r'(【[^】]+】)', r'**\1**', p)
                st.markdown(f"- {p_fmt}")

    # ════ 右列：信息点验证 ════
    with col_right:
        all_verif = layer2.get("all_verifications") or []
        if all_verif:
            st.markdown("**📊 信息点验证**")
            st.caption(
                f"共 {total_claims} 条　✅ {vt}  ❌ {vf}  ❓ {vn}　关键失败: {cf}/{ct}"
            )

            _type_zh = {"objective": "客观", "subjective": "主观",
                        "mixed": "混合", "implicit": "隐含"}
            _stmt_zh = {"assertion": "断言", "speculation": "推测"}

            # 用 stage1_searches 补全 sources / individual_results
            _s1_searches = result.get("stage1_searches") or {}
            _search_map = {
                s.get("claim_index"): s.get("search_result") or {}
                for s in (_s1_searches.get("searches") or [])
            }

            def _claim_sort_key(v):
                cid = (v.get("claim") or {}).get("id", "")
                m = re.search(r'\d+', cid)
                return int(m.group()) if m else 0
            all_verif_sorted = sorted(all_verif, key=_claim_sort_key)
            false_list = [v for v in all_verif_sorted if (v.get("verify_result") or {}).get("verified") is False]
            true_list  = [v for v in all_verif_sorted if (v.get("verify_result") or {}).get("verified") is True]
            null_list  = [v for v in all_verif_sorted if (v.get("verify_result") or {}).get("verified") is None]

            tab_f, tab_t, tab_n = st.tabs([
                f"❌ 矛盾 {len(false_list)}",
                f"✅ 已验证 {len(true_list)}",
                f"❓ 无法验证 {len(null_list)}",
            ])

            def _claim_cards(items, default_expanded=False):
                for v in items:
                    c_obj = v.get("claim") or {}
                    vr    = v.get("verify_result") or {}
                    sr    = _search_map.get(v.get("claim_index")) or v.get("search_result") or {}

                    c_id       = c_obj.get("id", "?")
                    c_type     = _type_zh.get(c_obj.get("type", ""), c_obj.get("type", ""))
                    c_critical = c_obj.get("critical", False)
                    c_text     = c_obj.get("claim", "")
                    c_query    = c_obj.get("query", "")

                    verified    = vr.get("verified")
                    reason      = vr.get("reason", "")
                    confidence  = vr.get("confidence", "")
                    stmt_type   = _stmt_zh.get(vr.get("statement_type", ""), vr.get("statement_type", ""))
                    quoted      = vr.get("quoted_evidence") or []
                    ind_results = sr.get("individual_results") or []

                    v_icon    = "✅" if verified is True else ("❌" if verified is False else "❓")
                    crit_mark = " ⭐" if c_critical else ""
                    preview   = (c_text[:48] + "…") if len(c_text) > 48 else c_text
                    tags      = "  ".join(filter(None, [c_type, stmt_type, confidence]))
                    header    = f"{c_id}{crit_mark}  {v_icon}  {preview}  ({tags})"

                    card_key = f"{key_prefix}_card_{v.get('claim_index', c_id)}"
                    if st.button(header, key=card_key, use_container_width=True):
                        st.session_state["_claim_dialog_data"] = {
                            "header": header, "c_query": c_query,
                            "reason": reason, "quoted": quoted,
                            "ind_results": ind_results,
                        }
                        _claim_detail_dialog()

            with tab_f:
                if false_list:
                    _claim_cards(false_list, default_expanded=True)
                else:
                    st.caption("无矛盾信息点")
            with tab_t:
                if true_list:
                    _claim_cards(true_list)
                else:
                    st.caption("无已验证信息点")
            with tab_n:
                if null_list:
                    _claim_cards(null_list)
                else:
                    st.caption("无无法验证的信息点")
        else:
            st.caption("无信息点验证数据")

    # ── 下载 ──
    st.markdown("---")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dl1, dl2, dl3 = st.columns(3)
    with dl1:
        summary_buf = io.BytesIO()
        pd.DataFrame([format_result_for_excel(result)]).to_excel(
            summary_buf, index=False, engine="openpyxl")
        summary_buf.seek(0)
        st.download_button("📥 摘要 Excel", data=summary_buf,
                           file_name=f"scoring_summary_{ts}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, key=f"{key_prefix}_dl_summary")
    with dl2:
        detail_buf = build_single_export(result)
        st.download_button("📄 详细报告", data=detail_buf,
                           file_name=f"scoring_detail_{ts}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, key=f"{key_prefix}_dl_detail")
    with dl3:
        with st.expander("📋 完整 JSON"):
            st.json(result)


# ──────────────────────────────────────────────
# Tab 1: 评分
# ──────────────────────────────────────────────

def render_scoring_tab():
    cfg = load_config()
    sub1, sub2 = st.tabs(["📝 单条评分", "📊 批量评分（Excel）"])

    # ── 单条评分 ──
    with sub1:

        # ① 输入
        st.markdown("#### 📝 输入")
        in1, in2, in3 = st.columns([4, 5, 3])
        with in1:
            query = st.text_area("用户问题", key="s_query", height=120,
                                 placeholder="输入用户的问题...")
        with in2:
            original = st.text_area("AI答案（待评分）", key="s_original", height=120,
                                    placeholder="输入待评分的答案...")
        with in3:
            query_time = st.text_input(
                "查询时间（可选）",
                key="s_query_time",
                placeholder="如：2025年6月1日",
            )
            st.write("")
            btn1, btn2 = st.columns(2)
            with btn1:
                run_clicked = st.button("🚀 开始评分", type="primary",
                                        use_container_width=True, key="s_run")
            with btn2:
                if st.button("🗑️ 清除", use_container_width=True, key="s_clear"):
                    for k in ["s_query", "s_original", "single_result"]:
                        st.session_state.pop(k, None)
                    st.rerun()

        enable_search = True
        stage_models = get_stage_defaults(cfg)
        if run_clicked:
            if not query or not original:
                st.error("❌ 请输入问题和答案")
            else:
                with st.spinner("⏳ 评分中，请稍候..."):
                    try:
                        pipeline = build_pipeline(cfg, enable_search, stage_models)
                        result = pipeline.score(user_question=query, original=original,
                                                query_time=query_time.strip() or None)
                        st.session_state["single_result"] = result
                        append_history(result, query, original)
                    except Exception as e:
                        st.error(f"❌ 评分失败: {e}")
                        st.code(traceback.format_exc())

        # ③ 评分结果
        if st.session_state.get("single_result"):
            st.markdown("---")
            st.markdown("#### 📊 评分结果")
            _render_result_panel(st.session_state["single_result"])
        else:
            st.caption("填写内容后点击「开始评分」")

    # ── 批量评分 ──
    with sub2:
        with st.expander("📖 Excel 格式说明", expanded=False):
            st.markdown("""
**必需列：** `用户问题`（或 `query` / `问题`）、`Original`（或 `original` / `答案`）

**可选列：** `查询时间`（或 `query_time`）— 填写则用该时间作为搜索时间上下文，不填则不传入

**输出列：** `AI评分`、`AI评分理由`、`阶段1_红线检查`、`信息点总数`、`客观信息点`、
`主观信息点`、`搜索次数`、`验证通过`、`验证失败`、`无法验证`、`关键信息点`、
`验证失败详情`、`满足度`、`质量评分`、`阶段5_反思检查`、`处理时间`、`耗时(秒)`、`状态`
""")

        uploaded = st.file_uploader("上传 Excel 文件", type=["xlsx", "xls"],
                                    key="batch_file")

        if uploaded:
            try:
                from io import BytesIO
                df_pre = pd.read_excel(BytesIO(uploaded.getvalue()), engine="calamine")
                st.success(f"✓ **{uploaded.name}**  共 {len(df_pre)} 行")

                with st.expander("👁 数据预览（前10行）", expanded=False):
                    st.dataframe(df_pre.head(10), use_container_width=True)

                st.markdown("**处理范围**")
                rc1, rc2 = st.columns(2)
                with rc1:
                    start_row = st.number_input("起始行", min_value=0,
                                                max_value=max(0, len(df_pre) - 1),
                                                value=0, key="batch_start")
                with rc2:
                    max_rows = st.number_input("最大行数", min_value=1,
                                               max_value=max(1, len(df_pre)),
                                               value=min(100, len(df_pre)), key="batch_max")

                st.markdown("**性能设置**")
                pc1, pc2, pc3 = st.columns(3)
                with pc1:
                    parallel_workers = st.number_input("并行数", min_value=1,
                                                       max_value=100, value=1,
                                                       help="1=顺序，推荐 10-30（注意API速率限制）",
                                                       key="batch_workers")
                with pc2:
                    chunk_size = st.number_input("分批大小（条/批）", min_value=10,
                                                 max_value=1000, value=100,
                                                 help="每批完成后立即保存一个独立 JSONL 文件，崩溃只丢当前批",
                                                 key="batch_chunk")
                with pc3:
                    save_interval = st.number_input("Excel 中间保存间隔", min_value=1,
                                                    max_value=500, value=20,
                                                    help="每N行写一次 Excel（批内崩溃保护）",
                                                    key="batch_save")

                rc3, rc4 = st.columns(2)
                with rc3:
                    enable_search_b = st.checkbox("🔍 启用搜索验证", value=True,
                                                  key="batch_search")
                with rc4:
                    max_retries = st.number_input("失败重试次数", min_value=0, max_value=5, value=2,
                                                  help="每批结束后对 API 异常行最多重试 N 次（数据缺失不重试）",
                                                  key="batch_retries")
                stage_models_b = get_stage_defaults(cfg)

                if parallel_workers > 1:
                    st.info(f"⚡ 并行模式：同时处理 {parallel_workers} 行")

                if st.button("🚀 开始批量评分", type="primary",
                             use_container_width=True, key="batch_run"):
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
                        tmp.write(uploaded.getvalue())
                        tmp_in = tmp.name

                    out_dir = Path(__file__).parent / "results"
                    out_dir.mkdir(exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    out_path = str(out_dir / f"batch_result_{ts}.xlsx")

                    try:
                        pipeline = build_pipeline(cfg, enable_search_b, stage_models_b)

                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        detail_text = st.empty()

                        df = pd.read_excel(tmp_in, engine="calamine")
                        end_row = min(int(start_row) + int(max_rows), len(df))

                        out_cols = [
                            "AI评分", "AI评分理由", "阶段1_红线检查",
                            "信息点总数", "客观信息点", "主观信息点", "搜索次数",
                            "验证通过", "验证失败", "无法验证", "关键信息点", "验证失败详情",
                            "满足度", "质量评分", "阶段5_反思检查",
                            "处理时间", "耗时(秒)", "状态",
                        ]
                        for col in out_cols:
                            if col not in df.columns:
                                df[col] = None

                        def process_row(index):
                            """返回 (index, fmt, ok, raw, retryable)"""
                            row = df.iloc[index]
                            uq = str(row.get("用户问题") or row.get("query") or row.get("问题") or "")
                            orig = str(row.get("Original") or row.get("original") or row.get("答案") or "")
                            qt_raw = row.get("查询时间") or row.get("query_time")
                            qt = str(qt_raw).strip() if qt_raw and str(qt_raw).strip() not in ("", "nan", "None") else None
                            if not uq or not orig:
                                return index, {"AI评分": None, "AI评分理由": "缺少数据", "状态": "❌ 数据缺失"}, False, None, False
                            try:
                                r = pipeline.score(user_question=uq, original=orig, query_time=qt)
                                return index, format_result_for_excel(r), r.get("score") is not None, r, False
                            except Exception as e:
                                return index, {"AI评分": None, "AI评分理由": f"失败: {str(e)[:100]}", "状态": "❌ 异常"}, False, None, True

                        success_count = error_count = completed = 0
                        all_raw_map = {}
                        chunk_fnames = []
                        t0 = time.time()
                        total_rows = end_row - int(start_row)
                        chunk_sz = int(chunk_size)
                        chunk_ranges = list(range(int(start_row), end_row, chunk_sz))
                        total_chunks = len(chunk_ranges)

                        def _apply_row_result(idx, fmt, ok, raw, chunk_raw_map):
                            """将单行结果写入 df 和 chunk_raw_map，返回 (ok, retryable)"""
                            for col, val in fmt.items():
                                df.at[idx, col] = val
                            if raw:
                                chunk_raw_map[idx] = raw

                        for chunk_idx, chunk_start in enumerate(chunk_ranges, 1):
                            chunk_end = min(chunk_start + chunk_sz, end_row)
                            chunk_raw_map = {}
                            chunk_done = 0
                            retry_queue = []   # 待重试行的 index 列表

                            if parallel_workers <= 1:
                                for i in range(chunk_start, chunk_end):
                                    q_p = str(df.iloc[i].get("用户问题") or df.iloc[i].get("query") or "")[:30]
                                    status_text.text(
                                        f"第 {chunk_idx}/{total_chunks} 批  |  行 {i+1}/{end_row}  |  {q_p}…"
                                    )
                                    idx, fmt, ok, raw, retryable = process_row(i)
                                    _apply_row_result(idx, fmt, ok, raw, chunk_raw_map)
                                    if ok:
                                        success_count += 1
                                    else:
                                        error_count += 1
                                        if retryable:
                                            retry_queue.append(idx)
                                    completed += 1
                                    progress_bar.progress(completed / total_rows)
                                    if completed % int(save_interval) == 0:
                                        df.to_excel(out_path, index=False)
                                        elapsed = time.time() - t0
                                        avg = elapsed / completed
                                        detail_text.text(
                                            f"💾 Excel 已保存  |  均 {avg:.1f}s/条  |  剩余约 {(total_rows - completed) * avg / 60:.1f}min"
                                        )
                            else:
                                status_text.text(
                                    f"第 {chunk_idx}/{total_chunks} 批（行 {chunk_start+1}–{chunk_end}）⚡ 并行 {parallel_workers} 线程…"
                                )
                                with ThreadPoolExecutor(max_workers=int(parallel_workers)) as executor:
                                    futures = {executor.submit(process_row, i): i
                                               for i in range(chunk_start, chunk_end)}
                                    for future in as_completed(futures):
                                        chunk_done += 1
                                        completed += 1
                                        progress_bar.progress(completed / total_rows)
                                        idx, fmt, ok, raw, retryable = future.result()
                                        elapsed = time.time() - t0
                                        avg = elapsed / completed
                                        remain = (total_rows - completed) * avg / max(1, int(parallel_workers))
                                        status_text.text(
                                            f"第 {chunk_idx}/{total_chunks} 批  |  总进度 {completed}/{total_rows}"
                                        )
                                        detail_text.text(
                                            f"⚡ 批内 {chunk_done}/{chunk_end - chunk_start}  |  均 {avg:.1f}s/条  |  剩余约 {remain/60:.1f}min"
                                        )
                                        _apply_row_result(idx, fmt, ok, raw, chunk_raw_map)
                                        if ok:
                                            success_count += 1
                                        else:
                                            error_count += 1
                                            if retryable:
                                                retry_queue.append(idx)
                                        if completed % int(save_interval) == 0:
                                            df.to_excel(out_path, index=False)

                            # ── 批内重试 ──
                            for attempt in range(1, int(max_retries) + 1):
                                if not retry_queue:
                                    break
                                status_text.text(
                                    f"第 {chunk_idx}/{total_chunks} 批  |  重试第 {attempt} 轮，共 {len(retry_queue)} 条失败行…"
                                )
                                time.sleep(3)
                                still_failed = []
                                if parallel_workers <= 1:
                                    for i in retry_queue:
                                        idx, fmt, ok, raw, retryable = process_row(i)
                                        _apply_row_result(idx, fmt, ok, raw, chunk_raw_map)
                                        if ok:
                                            success_count += 1
                                            error_count -= 1
                                        elif retryable:
                                            still_failed.append(idx)
                                else:
                                    with ThreadPoolExecutor(max_workers=int(parallel_workers)) as executor:
                                        futures = {executor.submit(process_row, i): i for i in retry_queue}
                                        for future in as_completed(futures):
                                            idx, fmt, ok, raw, retryable = future.result()
                                            _apply_row_result(idx, fmt, ok, raw, chunk_raw_map)
                                            if ok:
                                                success_count += 1
                                                error_count -= 1
                                            elif retryable:
                                                still_failed.append(idx)
                                retry_queue = still_failed

                            # 每批结束：立即保存 Excel + 独立 JSONL
                            df.to_excel(out_path, index=False)
                            chunk_jsonl = "\n".join(
                                json.dumps(chunk_raw_map[i], ensure_ascii=False)
                                for i in sorted(chunk_raw_map)
                            ).encode("utf-8")
                            chunk_fname = f"batch_{ts}_part{chunk_idx:03d}.jsonl"
                            save_batch_history(chunk_jsonl, chunk_fname)
                            chunk_fnames.append(chunk_fname)
                            all_raw_map.update(chunk_raw_map)
                            retry_info = f"，其中 {len(retry_queue)} 条重试后仍失败" if retry_queue else ""
                            detail_text.text(
                                f"✅ 第 {chunk_idx}/{total_chunks} 批完成，已保存 {chunk_fname}（{len(chunk_raw_map)} 条{retry_info}）"
                            )

                        # 全部完成：合并 JSONL
                        total_time = time.time() - t0
                        progress_bar.progress(1.0)
                        status_text.text("✅ 全部完成！")
                        detail_text.empty()

                        jsonl_bytes = "\n".join(
                            json.dumps(all_raw_map[i], ensure_ascii=False)
                            for i in sorted(all_raw_map)
                        ).encode("utf-8")
                        jsonl_fname = f"batch_{ts}_all.jsonl"
                        save_batch_history(jsonl_bytes, jsonl_fname)

                        score_counts = {
                            f"{s}分": int(len(df[(df["AI评分"] == s) &
                                                  (df.index >= int(start_row)) &
                                                  (df.index < end_row)]))
                            for s in [0, 1, 2, 3]
                        }
                        score_counts["失败"] = int(len(df[df["AI评分"].isna() &
                                                          (df.index >= int(start_row)) &
                                                          (df.index < end_row)]))

                        st.session_state["batch_result"] = {
                            "out_path": out_path,
                            "out_fname": f"batch_{ts}.xlsx",
                            "success": success_count,
                            "error": error_count,
                            "total": total_rows,
                            "total_time": total_time,
                            "score_counts": score_counts,
                            "jsonl_bytes": jsonl_bytes,
                            "jsonl_fname": jsonl_fname,
                            "chunk_fnames": chunk_fnames,
                        }

                    except Exception as e:
                        st.error(f"❌ 批量评分失败: {e}")
                        st.code(traceback.format_exc())
                    finally:
                        if os.path.exists(tmp_in):
                            os.unlink(tmp_in)

            except Exception as e:
                st.error(f"❌ 读取文件失败: {e}")

        if st.session_state.get("batch_result"):
            b = st.session_state["batch_result"]
            st.markdown("---")
            st.markdown("### ✅ 评分完成")
            st.caption(f"成功 {b['success']} / 失败 {b['error']}  |  总耗时 {b['total_time']/60:.1f} 分钟  |  平均 {b['total_time']/max(1,b['total']):.1f} 秒/条")

            cols = st.columns(5)
            for col, (k, v) in zip(cols, b["score_counts"].items()):
                col.metric(k, v)

            dl1, dl2 = st.columns(2)
            with dl1:
                if os.path.exists(b["out_path"]):
                    with open(b["out_path"], "rb") as f:
                        st.download_button("📥 下载结果 Excel（全量）", data=f,
                                           file_name=b["out_fname"],
                                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                           use_container_width=True, key="batch_dl_excel")
                else:
                    st.warning(f"文件已移动: {b['out_path']}")
            with dl2:
                if b.get("jsonl_bytes"):
                    st.download_button("📄 下载合并 JSONL（全量）", data=b["jsonl_bytes"],
                                       file_name=b["jsonl_fname"],
                                       mime="application/x-ndjson",
                                       use_container_width=True, key="batch_dl_jsonl")

            # 各批次独立下载
            chunk_fnames = b.get("chunk_fnames", [])
            if chunk_fnames:
                with st.expander(f"📦 各批次 JSONL（共 {len(chunk_fnames)} 批）", expanded=False):
                    for i, fname in enumerate(chunk_fnames, 1):
                        fpath = BATCH_HISTORY_DIR / fname
                        if fpath.exists():
                            with open(fpath, "rb") as f:
                                st.download_button(
                                    f"第 {i:03d} 批  {fname}",
                                    data=f, file_name=fname,
                                    mime="application/x-ndjson",
                                    use_container_width=True,
                                    key=f"batch_dl_chunk_{i}",
                                )

            try:
                df_res = pd.read_excel(b["out_path"], engine="calamine")
                with st.expander("📋 结果预览（前20行）", expanded=False):
                    qcol = next((c for c in ["用户问题", "query", "问题"] if c in df_res.columns), None)
                    show_cols = ([qcol] if qcol else []) + [c for c in ["AI评分", "AI评分理由", "状态"] if c in df_res.columns]
                    st.dataframe(df_res[show_cols].head(20) if show_cols else df_res.head(20),
                                 use_container_width=True)
            except Exception:
                pass

            # ── 重跑失败行 ──
            error_cnt = b.get("error", 0)
            if error_cnt > 0:
                st.markdown("---")
                st.markdown(f"#### 🔄 重跑失败行（{error_cnt} 条）")
                st.caption("从已保存的 JSONL 中读取失败记录，重新评分后保存为新文件。")

                rr1, rr2 = st.columns(2)
                with rr1:
                    retry_workers_b = st.number_input("并行数", min_value=1, max_value=30,
                                                      value=5, key="batch_retry_workers")
                with rr2:
                    retry_rounds_b = st.number_input("最多重试轮数", min_value=1, max_value=5,
                                                     value=2, key="batch_retry_rounds")

                if st.button("🚀 开始重跑失败行", type="primary",
                             use_container_width=True, key="batch_retry_run"):
                    # 从 all JSONL 里读取 score=None 的记录
                    jsonl_path = BATCH_HISTORY_DIR / b["jsonl_fname"]
                    if not jsonl_path.exists():
                        st.error("找不到 JSONL 文件，无法重跑")
                    else:
                        all_records = load_batch_jsonl(jsonl_path)
                        failed_items = [
                            (i, rec) for i, rec in enumerate(all_records)
                            if rec.get("score") is None
                        ]
                        if not failed_items:
                            st.info("没有找到 score=None 的记录")
                        else:
                            cfg_r = load_config()
                            pipeline_r = build_pipeline(cfg_r, True, get_stage_defaults(cfg_r))
                            updated = list(all_records)
                            prog = st.progress(0)
                            stat_txt = st.empty()
                            queue = list(failed_items)

                            def _retry_one_b(item):
                                orig_idx, rec = item
                                uq   = rec.get("user_question", "")
                                orig = rec.get("original_answer", "")
                                qt   = rec.get("query_time") or None
                                if not uq or not orig:
                                    return orig_idx, rec, False
                                try:
                                    new_r = pipeline_r.score(
                                        user_question=uq, original=orig, query_time=qt)
                                    return orig_idx, new_r, new_r.get("score") is not None
                                except Exception:
                                    return orig_idx, rec, False

                            success_cnt = 0
                            for rnd in range(1, int(retry_rounds_b) + 1):
                                if not queue:
                                    break
                                stat_txt.text(f"第 {rnd} 轮，共 {len(queue)} 条…")
                                still_failed = []
                                with ThreadPoolExecutor(max_workers=int(retry_workers_b)) as ex:
                                    futs = {ex.submit(_retry_one_b, item): item for item in queue}
                                    done = 0
                                    for fut in as_completed(futs):
                                        done += 1
                                        prog.progress(done / len(queue))
                                        orig_idx, new_r, ok = fut.result()
                                        updated[orig_idx] = new_r
                                        if ok:
                                            success_cnt += 1
                                        else:
                                            still_failed.append((orig_idx, new_r))
                                        stat_txt.text(
                                            f"第 {rnd} 轮  {done}/{len(queue)}  成功 {success_cnt} 条…"
                                        )
                                queue = still_failed
                                if queue and rnd < int(retry_rounds_b):
                                    time.sleep(3)

                            stat_txt.text(
                                f"✅ 完成：成功 {success_cnt}/{len(failed_items)} 条"
                                + (f"，仍失败 {len(queue)} 条" if queue else "")
                            )
                            ts_r = datetime.now().strftime("%Y%m%d_%H%M%S")
                            new_fname = f"batch_retry_{ts_r}.jsonl"
                            new_bytes = "\n".join(
                                json.dumps(rec, ensure_ascii=False) for rec in updated
                            ).encode("utf-8")
                            save_batch_history(new_bytes, new_fname)
                            st.session_state["batch_retry_result"] = {
                                "bytes": new_bytes, "fname": new_fname}
                            st.rerun()

                retry_res = st.session_state.get("batch_retry_result")
                if retry_res:
                    st.download_button(
                        f"📄 下载重跑后的完整 JSONL",
                        data=retry_res["bytes"],
                        file_name=retry_res["fname"],
                        mime="application/x-ndjson",
                        use_container_width=True,
                        key="batch_retry_dl",
                    )

            if st.button("🗑️ 清除结果", key="batch_clear"):
                st.session_state.pop("batch_result", None)
                st.session_state.pop("batch_retry_result", None)
                st.rerun()



# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# Tab 3: 设置
# ──────────────────────────────────────────────

def render_home_tab():
    # ── 介绍区 ──
    st.markdown("""
<div style="padding:1.2rem 0 0.5rem 0">
<h3 style="margin:0 0 0.3rem 0">AI 评分工作台</h3>
<p style="color:#888;margin:0">基于多阶段 LLM 流水线，通过联网搜索 + 多模型交叉验证，对 AI 回答进行事实核查和质量评分。</p>
</div>
""", unsafe_allow_html=True)

    # 评分标准四格
    s0, s1, s2, s3 = st.columns(4)
    s0.markdown("""**❌ 0 分**
事实错误 / 红线问题 / 完全未满足用户意图""")
    s1.markdown("""**🔶 1 分**
内容基本正确，但不完整或存在小问题""")
    s2.markdown("""**✅ 2 分**
内容正确，满足用户意图""")
    s3.markdown("""**🌟 3 分**
内容正确、满足意图、格式优秀""")

    st.markdown("---")

    # 流水线概览
    with st.expander("📋 评分流水线概览", expanded=False):
        st.markdown("""
| 阶段 | 名称 | 说明 |
|------|------|------|
| S1 | 🔴 红线检查 | 政治敏感、违规、危险内容 |
| S1b | 🔍 内部一致性 | 答案自身逻辑矛盾（与 S1 并行） |
| S2a | 📋 信息点拆解 | 按 5W1H 原则拆解为原子信息点 |
| S2b | 🌐 并行联网搜索 | 每条信息点独立搜索（aliyun + kimi） |
| S2c | ✅ 信息点验证 | 多源投票，判定 true / false / null |
| S2d | ⚖️ 事实错误判定 | 汇总验证结果，判定是否有事实错误 |
| S2e | 🌀 幻觉检测 | 全局对照搜索内容，补充验证盲点 |
| S3 | 📊 满足度评估 | 判断答案是否满足用户意图，给出 1 或 2 分 |
| S4 | ✨ 格式检查 | 判断格式是否优秀，决定是否升为 3 分 |
| S5 | 🔄 反思复核 | 独立二次审查，只能维持或降分 |
""")

    st.markdown("---")

    # ── 设置区 ──
    st.markdown("#### ⚙️ 系统设置")
    st.caption("修改后点击底部「💾 保存设置」生效，写入 config.json。")
    render_settings_content()


def render_settings_content():
    cfg = load_config()

    # ── 1. LLM 评估模型 API ──
    with st.expander("▼ LLM 评估模型 API", expanded=True):
        st.markdown("**GPT-5.2 (gpt5)**")
        evaluators = cfg.get("answer_comparison", {}).get("evaluators", [])
        ev_map = {ev["name"]: ev for ev in evaluators}

        gpt5_ev = ev_map.get("gpt5", {})
        kimi_ev = ev_map.get("kimi", {})

        g1, g2 = st.columns(2)
        with g1:
            gpt5_key = st.text_input("GPT-5 API Key",
                                     value=cfg.get("api_keys", {}).get("gpt5", ""),
                                     type="password", key="cfg_gpt5_key")
        with g2:
            gpt5_url = st.text_input("GPT-5 Base URL",
                                     value=gpt5_ev.get("base_url", "http://10.225.31.12/v1"),
                                     key="cfg_gpt5_url")

        st.markdown("**Kimi K2.5 (kimi)**")
        k1, k2 = st.columns(2)
        with k1:
            kimi_key = st.text_input("Kimi API Key",
                                     value=cfg.get("api_keys", {}).get("kimi", ""),
                                     type="password", key="cfg_kimi_key")
        with k2:
            kimi_url = st.text_input("Kimi Base URL",
                                     value=kimi_ev.get("base_url", "http://10.225.31.12/v1"),
                                     key="cfg_kimi_url")

    # ── 2. 各阶段默认模型 ──
    with st.expander("▼ 各阶段默认模型", expanded=False):
        st.caption("这里设置的是每次打开评分/调试页面时的默认值。")
        defaults = get_stage_defaults(cfg)

        def _idx(key):
            v = defaults.get(key, "gpt5")
            return _MODEL_KEYS.index(v) if v in _MODEL_KEYS else 0

        sd1, sd2, sd3 = st.columns(3)
        with sd1:
            sd_bl = st.selectbox("🔴 S1 红线检查", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("baseline_checker"), key="sd_bl")
            sd_co = st.selectbox("🔍 S1b 内部一致性", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("answer_consistency_checker"), key="sd_co")
            sd_pr = st.selectbox("📋 S2a 信息点拆解", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("answer_parser"), key="sd_pr")
        with sd2:
            sd_vr = st.selectbox("🔍 S2c 信息点验证", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("voting_verifier"), key="sd_vr")
            sd_fc = st.selectbox("⚖️ S2d 事实错误判定", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("fact_error_checker"), key="sd_fc")
            sd_ha = st.selectbox("🌀 S2e 幻觉检测", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("hallucination_checker"), key="sd_ha")
        with sd3:
            sd_sa = st.selectbox("📊 S3 满足度评估", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("satisfaction_evaluator"), key="sd_sa")
            sd_fm = st.selectbox("✨ S4 格式检查", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("format_checker"), key="sd_fm")
            sd_rf = st.selectbox("🔄 S5 反思检查", _MODEL_KEYS, format_func=lambda x: _MODEL_OPTS[x],
                                 index=_idx("reflection_checker"), key="sd_rf")

    # ── 4. 批量处理 ──
    with st.expander("▼ 批量处理默认设置", expanded=False):
        batch_cfg = cfg.get("batch_processing", {})
        bp1, bp2 = st.columns(2)
        with bp1:
            default_workers = st.number_input("默认并发数",
                                              min_value=1, max_value=100,
                                              value=batch_cfg.get("max_workers", 1),
                                              key="cfg_batch_workers")
        with bp2:
            default_save = st.number_input("默认保存间隔（行）",
                                           min_value=1, max_value=100,
                                           value=batch_cfg.get("save_interval", 10),
                                           key="cfg_batch_save")

    # ── 保存按钮 ──
    st.markdown("---")
    if st.button("💾 保存设置", type="primary", use_container_width=True, key="cfg_save"):
        # 更新 api_keys
        if "api_keys" not in cfg:
            cfg["api_keys"] = {}
        cfg["api_keys"]["gpt5"] = gpt5_key
        cfg["api_keys"]["kimi"] = kimi_key

        # 更新 evaluators 的 base_url
        for ev in cfg.get("answer_comparison", {}).get("evaluators", []):
            if ev["name"] in ("gpt5", "gpt5_thinking"):
                ev["base_url"] = gpt5_url
            elif ev["name"] in ("kimi", "kimi_thinking"):
                ev["base_url"] = kimi_url

        # 更新各阶段默认模型
        cfg["stage_models_default"] = {
            "baseline_checker": sd_bl,
            "answer_consistency_checker": sd_co,
            "answer_parser": sd_pr,
            "voting_verifier": sd_vr,
            "fact_error_checker": sd_fc,
            "hallucination_checker": sd_ha,
            "satisfaction_evaluator": sd_sa,
            "format_checker": sd_fm,
            "reflection_checker": sd_rf,
        }

        # 更新批量处理默认值
        if "batch_processing" not in cfg:
            cfg["batch_processing"] = {}
        cfg["batch_processing"]["max_workers"] = int(default_workers)
        cfg["batch_processing"]["save_interval"] = int(default_save)

        save_config(cfg)
        st.success("✅ 设置已保存到 config.json")
        st.rerun()


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main():
    st.markdown("""
    <style>
    /* 隐藏默认 sidebar 切换按钮和 header */
    [data-testid="stSidebarNav"] { display: none; }
    #MainMenu { display: none; }
    header { display: none; }
    .block-container { padding-top: 1rem; }
    /* 信息点按钮靠左对齐 */
    [data-testid="stButton"] > button {
        text-align: left;
        justify-content: flex-start;
    }
    </style>
    """, unsafe_allow_html=True)

    st.title("🎯 AI 评分工作台")

    tab0, tab1, tab2 = st.tabs(["🏠 首页", "📊 评分", "📜 历史记录"])

    with tab0:
        render_home_tab()

    with tab1:
        render_scoring_tab()

    with tab2:
        render_history_tab()


def render_history_tab():
    """Tab — 历史记录（单条 + 批量）"""
    h1, h2 = st.tabs(["📝 单条评分", "📊 批量评分"])

    _score_emoji = {0: "❌", 1: "⚠️", 2: "✅", 3: "🌟"}

    # ── 单条历史 ──
    with h1:
        records = load_history()
        if not records:
            st.info("暂无历史记录，完成单条评分后会自动保存。")
        else:
            col_cnt, col_clear = st.columns([3, 1])
            with col_cnt:
                st.caption(f"共 {len(records)} 条记录（最新在前）")
            with col_clear:
                if st.button("🗑️ 清空历史", use_container_width=True, key="hist_clear"):
                    if HISTORY_FILE.exists():
                        HISTORY_FILE.unlink()
                    st.rerun()

            table_rows = []
            for i, rec in enumerate(records):
                r = rec.get("result") or {}
                score = r.get("score")
                table_rows.append({
                    "#": i + 1,
                    "时间": rec.get("saved_at", "")[:19].replace("T", " "),
                    "分数": f"{_score_emoji.get(score, '❓')} {score}分" if score is not None else "—",
                    "耗时": f"{r.get('duration', 0):.1f}s",
                    "终止": r.get("terminated_at") or "正常",
                    "问题": rec.get("question", "")[:50],
                })
            st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            idx = st.selectbox(
                "查看详情",
                options=list(range(1, len(records) + 1)),
                format_func=lambda i: f"#{i}  {table_rows[i-1]['时间']}  {table_rows[i-1]['分数']}  {table_rows[i-1]['问题'][:40]}",
                key="hist_select",
            )
            if idx:
                rec = records[idx - 1]
                st.caption(f"**问题：** {rec.get('question', '')}")
                with st.expander("原始答案", expanded=False):
                    st.text(rec.get("answer", ""))
                _render_result_panel(rec.get("result") or {}, key_prefix=f"hist_{idx}")

    # ── 批量历史 ──
    with h2:
        # 上传 JSONL
        uploaded_jsonl = st.file_uploader(
            "上传 JSONL 文件查看", type=["jsonl", "json"],
            key="batch_hist_upload",
            help="支持直接上传批量评分导出的 .jsonl 文件"
        )

        # 本地已保存的批次文件
        batch_files = sorted(BATCH_HISTORY_DIR.glob("*.jsonl"), reverse=True) \
            if BATCH_HISTORY_DIR.exists() else []

        if not uploaded_jsonl and not batch_files:
            st.info("暂无批量评分历史。完成批量评分后会自动保存，也可上传 JSONL 文件查看。")
        else:
            # 选择数据源
            options = []
            if uploaded_jsonl:
                options.append({"label": f"📤 {uploaded_jsonl.name}（上传）", "type": "upload"})
            for f in batch_files:
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                size_kb = f.stat().st_size // 1024
                options.append({"label": f"📄 {f.name}  {mtime}  {size_kb}KB", "type": "file", "path": f})

            sel = st.selectbox("选择批次", range(len(options)),
                               format_func=lambda i: options[i]["label"],
                               key="batch_hist_sel")

            opt = options[sel]
            if opt["type"] == "upload":
                # 保存到本地，避免每次刷新重新解析
                save_batch_history(uploaded_jsonl.getvalue(), uploaded_jsonl.name)
                saved_path = BATCH_HISTORY_DIR / uploaded_jsonl.name
                batch_records = load_batch_jsonl(saved_path)
                st.success(f"已保存到本地：{uploaded_jsonl.name}，下次可直接从列表选择")
            else:
                batch_records = load_batch_jsonl(opt["path"])

            if not batch_records:
                st.warning("文件为空或格式不正确")
            else:
                total = len(batch_records)

                # 构建轻量汇总行（仅取少量字段，速度快）
                b_rows = []
                for i, r in enumerate(batch_records):
                    score = r.get("score")
                    b_rows.append({
                        "#": i + 1,
                        "分数": f"{_score_emoji.get(score, '❓')} {score}分" if score is not None else "—",
                        "耗时": f"{r.get('duration', 0):.1f}s",
                        "终止": r.get("terminated_at") or "正常",
                        "问题": str(r.get("user_question") or "")[:60],
                    })

                # 分数分布统计
                scores = [r.get("score") for r in batch_records]
                dist = {s: scores.count(s) for s in [3, 2, 1, 0]}
                none_cnt = scores.count(None)
                st.caption(
                    f"共 **{total}** 条　·　"
                    f"🌟3分: {dist[3]}　✅2分: {dist[2]}　"
                    f"🔶1分: {dist[1]}　❌0分: {dist[0]}"
                    + (f"　⚠️未完成: {none_cnt}" if none_cnt else "")
                )

                # ── 分页控制 ──
                PAGE_SIZE = 50
                total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                page_key = f"bhist_page_{sel}"
                # 切换批次时重置到第1页
                if st.session_state.get("_bhist_last_sel") != sel:
                    st.session_state[page_key] = 1
                    st.session_state["_bhist_last_sel"] = sel
                if page_key not in st.session_state:
                    st.session_state[page_key] = 1

                cur_page = st.session_state[page_key]
                pc1, pc2, pc3 = st.columns([1, 3, 1])
                with pc1:
                    if st.button("◀ 上一页", key="bhist_prev",
                                 disabled=cur_page <= 1):
                        st.session_state[page_key] -= 1
                        st.rerun()
                with pc2:
                    st.markdown(
                        f"<div style='text-align:center;padding-top:6px'>"
                        f"第 {cur_page} / {total_pages} 页　每页 {PAGE_SIZE} 条"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with pc3:
                    if st.button("下一页 ▶", key="bhist_next",
                                 disabled=cur_page >= total_pages):
                        st.session_state[page_key] += 1
                        st.rerun()

                # 只渲染当前页的行
                start = (cur_page - 1) * PAGE_SIZE
                end = min(start + PAGE_SIZE, total)
                st.dataframe(pd.DataFrame(b_rows[start:end]),
                             use_container_width=True, hide_index=True)

                st.markdown("---")
                # 用 number_input 代替 selectbox，避免渲染上千个选项
                bidx = st.number_input(
                    f"查看详情（输入序号 1–{total}）",
                    min_value=1, max_value=total, value=start + 1,
                    step=1, key="batch_hist_detail_num",
                )
                r = batch_records[bidx - 1]
                st.caption(f"**问题：** {r.get('user_question', '')}")
                with st.expander("原始答案", expanded=False):
                    st.text(r.get("original_answer", ""))
                _render_result_panel(r, key_prefix=f"bhist_{bidx}")


if __name__ == "__main__":
    main()
