#!/usr/bin/env python3
"""
全自动评分流水线 V4 - 递进式筛选
5阶段评分流程：
  阶段1: 红线检查（是否0分？）
  阶段2: 事实错误检查（拆分+搜索+验证，是否0分？）
  阶段3: 满足度+完整性评估（1分还是2分？）
  阶段4: 格式检查（是否升为3分？）
  阶段5: 反思检查（最终确认）
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime
from openai import OpenAI

# 导入模块
from answer_parser import AnswerParser
from multi_source_voting_verifier import MultiSourceVotingVerifier
from rate_limiter import MultiAPIRateLimiter


# ============================================================
# 核心数据结构：流水线上下文（append-only，各阶段只写不删）
# ============================================================

@dataclass
class VerificationRecord:
    """一个信息点从拆解到验证的完整记录"""
    claim: Dict                    # 原始 claim（text/type/critical/query 等）
    search_content: str            # 多 provider AI总结内容合并（不含 URL/sources）
    verified: Optional[bool]       # true=搜索支持 / false=明确矛盾 / None=无法验证
    statement_type: str            # assertion（断言）/ speculation（推测）
    confidence: str                # high / medium / low
    reason: str                    # 一句话判断理由
    quoted_evidence: List[str] = field(default_factory=list)  # 搜索结果关键引用句
    claim_index: int = 0           # 原始搜索编号（与 stage1_searches 对齐，用于 UI 来源匹配）


@dataclass
class ScoringContext:
    """评分流水线的统一上下文，贯穿所有阶段"""
    question: str
    answer: str

    # ── 阶段2-1：信息点拆解 ──────────────────────────────
    claims: Optional[List[Dict]] = None          # AnswerParser 输出

    # ── 阶段2-2/3：搜索+验证（并行） ─────────────────────
    verifications: Optional[List[VerificationRecord]] = None

    # ── 阶段2-并行：答案内部一致性 ───────────────────────
    consistency: Optional[Dict] = None

    # ── 阶段结果（各 LLM 输出，原始保留） ────────────────
    redline: Optional[Dict] = None               # s1
    fact_check: Optional[Dict] = None            # s2d
    hallucination: Optional[Dict] = None         # s2e
    satisfaction: Optional[Dict] = None          # s3
    format_check: Optional[Dict] = None          # s4
    reflection: Optional[Dict] = None            # s5

    # ── 评分状态 ─────────────────────────────────────────
    preliminary_score: Optional[int] = None      # 反思前分数
    score: Optional[int] = None                  # 最终分数
    reasoning: str = ""
    terminated_at: Optional[str] = None          # 在哪个阶段提前终止

    # ── 硬规则上限 ───────────────────────────────────────
    critical_false_cap: Optional[int] = None     # 关键信息点被推翻 → 最高1分
    critical_null_cap: Optional[int] = None      # 关键信息点大量无法验证 → 最高2分

    # ── 元数据 ───────────────────────────────────────────
    is_not_recall: bool = False
    query_time: str = ""                             # 用户发起查询的时间（用于时效性判断）
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    duration: float = 0.0

    def to_legacy_result(self) -> Dict:
        """转换为旧版 result dict，保持与 app.py / excel_auto_scorer_v3.py 的兼容性"""
        return {
            "user_question": self.question,
            "original_answer": self.answer,
            "query_time": self.query_time,
            "score": self.score,
            "reasoning": self.reasoning,
            "preliminary_score": self.preliminary_score,
            "is_not_recall": self.is_not_recall,
            "timestamp": self.timestamp,
            "duration": self.duration,
            "terminated_at": self.terminated_at,
            # 各阶段结果
            "layer1_baseline": self.redline,
            "stage0_parsing": {"claims": self.claims, "total_claims": len(self.claims) if self.claims else 0,
                               "stats": self._compute_claim_stats()},
            "layer2_verification": self._build_layer2(),
            "stage2_fact_check": self.fact_check,
            "stage2b_hallucination": self.hallucination,
            "stage_consistency": self.consistency,
            "stage3_satisfaction": self.satisfaction,
            "stage4_format": self.format_check,
            "layer3_quality": {
                "satisfaction": self.satisfaction,
                "quality": {"final_score": self.format_check.get("final_score") if self.format_check else None,
                            "reasoning": self.satisfaction.get("reasoning", "") if self.satisfaction else ""}
            } if self.satisfaction else {},
            "reflection": self.reflection,
            "critical_false_cap": {"triggered": self.critical_false_cap is not None,
                                   "cap": self.critical_false_cap},
            "critical_null_cap": {"triggered": self.critical_null_cap is not None,
                                  "cap": self.critical_null_cap},
        }

    def _compute_claim_stats(self) -> Dict:
        if not self.claims:
            return {}
        from collections import Counter
        counts = Counter(c.get("type", "objective") for c in self.claims)
        return {"objective": counts.get("objective", 0),
                "subjective": counts.get("subjective", 0),
                "mixed": counts.get("mixed", 0)}

    def _build_layer2(self) -> Dict:
        if not self.verifications:
            return {}
        vlist = self.verifications
        total = len(vlist)
        true_count  = sum(1 for v in vlist if v.verified is True)
        false_count = sum(1 for v in vlist if v.verified is False)
        null_count  = sum(1 for v in vlist if v.verified is None)
        critical    = [v for v in vlist if v.claim.get("critical")]
        return {
            "all_verifications": [
                {"claim_index": v.claim_index, "claim": v.claim,
                 "search_result": {"content": v.search_content},
                 "verify_result": {"verified": v.verified, "statement_type": v.statement_type,
                                   "confidence": v.confidence, "reason": v.reason,
                                   "quoted_evidence": v.quoted_evidence}}
                for v in vlist
            ],
            "stats": {
                "total": total,
                "verified_true": true_count,
                "verified_false": false_count,
                "verified_null": null_count,
                "critical_total": len(critical),
                "critical_true": sum(1 for v in critical if v.verified is True),
                "critical_false": sum(1 for v in critical if v.verified is False),
            }
        }


def load_prompt(prompt_name: str) -> str:
    """
    从prompts目录加载prompt文件
    
    Args:
        prompt_name: prompt文件名（不含路径和.txt后缀）
    
    Returns:
        prompt内容
    """
    prompt_file = f"prompts/{prompt_name}.txt"
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.warning(f"Prompt文件未找到: {prompt_file}，将使用内置默认prompt")
        return None


class LLMAgent:
    """基础LLM Agent类（支持 reasoning model）"""

    def __init__(self, name: str, config: Dict, api_key: str):
        self.name = name
        self.config = config
        self.model = config['model']
        self.temperature = config.get('temperature', 0.3)
        self.is_reasoning_model = config.get('is_reasoning_model', False)

        base_url = config.get('base_url', 'https://api.openai.com/v1')
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.logger = logging.getLogger(f"Agent-{name}")

    def call(self, system_prompt: str, user_prompt: str, timeout: int = 60) -> Dict:
        """调用LLM（支持 reasoning model，429自动重试）"""
        import time

        # Reasoning model 需要更长超时时间
        if self.is_reasoning_model:
            timeout = max(timeout, 120)

        # Reasoning model 使用单消息格式
        if self.is_reasoning_model:
            messages = [{
                "role": "user",
                "content": f"""# System Instructions

{system_prompt}

---

# User Request

{user_prompt}"""
            }]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

        # 构建API参数
        api_params = {
            'model': self.model,
            'messages': messages,
            'temperature': self.temperature,
            'timeout': timeout,
            'response_format': {'type': 'json_object'}
        }

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(**api_params)

                content = response.choices[0].message.content
                if content is None:
                    self.logger.error("LLM返回空内容(None)")
                    return {"error": "LLM返回空内容"}
                content = content.strip()
                try:
                    result = json.loads(content)
                except json.JSONDecodeError:
                    # 模型可能在JSON前输出了文字（如执行步骤），尝试提取JSON块
                    m = re.search(r'\{[\s\S]*\}', content)
                    if m:
                        result = json.loads(m.group(0))
                    else:
                        raise
                if not isinstance(result, dict):
                    self.logger.error(f"LLM返回非dict响应: {content[:100]}")
                    return {"error": f"LLM响应格式错误: {type(result).__name__}"}

                # 提取思考过程（如果是 reasoning model）
                if self.is_reasoning_model:
                    choice = response.choices[0]
                    if hasattr(choice.message, 'reasoning_content') and choice.message.reasoning_content:
                        result['thinking_process'] = choice.message.reasoning_content
                        self.logger.debug(f"思考过程长度: {len(choice.message.reasoning_content)} 字符")

                return result

            except Exception as e:
                err_str = str(e)
                is_rate_limit = '429' in err_str or 'rate_limit' in err_str.lower() or 'RateLimitError' in type(e).__name__
                if is_rate_limit and attempt < max_retries - 1:
                    wait = 10 * (2 ** attempt)  # 10s, 20s, 40s
                    self.logger.warning(f"⏳ 触发限流(429)，{wait}秒后重试 (第{attempt+1}/{max_retries-1}次)...")
                    time.sleep(wait)
                    continue
                self.logger.error(f"LLM调用失败: {e}")
                return {"error": err_str}


class BaselineChecker(LLMAgent):
    """LLM 1: 红线检查器（政治、违规、危险内容）"""

    def check(self, user_question: str, original_answer: str) -> Dict:
        """检查红线问题（严重违规内容）"""

        # 从文件加载prompt
        system_prompt = load_prompt("pipeline/s1_redline")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s1_redline.txt")

        user_prompt = f"""用户问题：{user_question}

待检查答案：
{original_answer}

请检查是否有红线问题。"""

        result = self.call(system_prompt, user_prompt)
        self.logger.info(f"红线检查结果: {result.get('has_fatal_issue', 'unknown')}")
        return result


class SearchQueryGenerator(LLMAgent):
    """搜索查询生成器 - 为每个信息点生成最佳搜索查询"""

    def generate_query(self, user_question: str, claim_type: str, claim_data: Dict) -> str:
        """
        为信息点生成最佳搜索查询

        Args:
            user_question: 用户问题
            claim_type: 信息点类型（objective/subjective）
            claim_data: 信息点数据

        Returns:
            最佳搜索查询
        """

        # 从外部文件加载prompt（纯系统指令，不含数据）
        system_prompt = load_prompt("pipeline/s2b_search_query")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s2b_search_query.txt")

        # 数据只通过 user_prompt 传递（避免双传）
        content_str = json.dumps(claim_data, ensure_ascii=False)
        user_prompt = f"""用户问题：{user_question}

信息点类型：{claim_type}
信息点内容：{content_str}

请生成最佳搜索查询。"""

        result = self.call(system_prompt, user_prompt, timeout=30)
        query = result.get('search_query', '')
        self.logger.info(f"生成搜索查询: {query}")
        return query


class SatisfactionEvaluator(LLMAgent):
    """满足度评估器 —— 接收 ScoringContext，直接从 ctx 读取所需数据"""

    def evaluate(self, ctx: 'ScoringContext') -> Dict:
        system_prompt = load_prompt("pipeline/s3_satisfaction")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s3_satisfaction.txt")

        user_prompt = self._build_prompt(ctx)
        result = self.call(system_prompt, user_prompt)
        self.logger.info(f"满足度: {result.get('satisfaction_level', 'unknown')}")
        return result

    def _build_prompt(self, ctx: 'ScoringContext') -> str:
        time_prefix = f"[查询时间: {ctx.query_time}]\n\n" if ctx.query_time else ""
        lines = [
            f"## 用户问题\n{time_prefix}{ctx.question}",
            f"\n## AI回答\n{ctx.answer}",
        ]

        # 答案内部一致性问题：直接从 ctx.consistency 读
        if ctx.consistency and ctx.consistency.get('has_consistency_issue'):
            critical_issues = ctx.consistency.get('critical_issues') or []
            minor_issues    = ctx.consistency.get('minor_issues') or []
            lines.append("\n## 答案内部一致性问题（已检测）")
            if critical_issues:
                lines.append(f"- 严重问题（影响核心答案）: {len(critical_issues)} 个")
                for issue in critical_issues[:3]:
                    if not issue:
                        continue
                    lines.append(f"  ✗ [{issue.get('type','')}] {issue.get('problem','')}")
                    lines.append(f"    原文：{issue.get('answer_text','')[:80]}")
            if minor_issues:
                lines.append(f"- 轻微问题（格式/表述瑕疵）: {len(minor_issues)} 个")
                for issue in minor_issues[:2]:
                    if not issue:
                        continue
                    lines.append(f"  △ [{issue.get('type','')}] {issue.get('problem','')}")

        # 信息点验证摘要（与 prompt 输入格式描述对齐）
        if ctx.verifications:
            vlist = ctx.verifications
            # assertion=断言 + verified=false → 矛盾
            false_assertion  = [v for v in vlist if v.verified is False and v.statement_type == 'assertion']
            # speculation=推测 + verified=false → 未得到支持
            false_speculation = [v for v in vlist if v.verified is False and v.statement_type == 'speculation']
            # verified=null
            null_all         = [v for v in vlist if v.verified is None]
            null_critical    = [v for v in null_all if v.claim.get('critical', False)]

            if false_assertion or false_speculation or null_all:
                lines.append("\n## 信息点验证摘要（仅当存在验证异常时才包含此节，如所有信息点验证通过则无此节）")
                if false_assertion:
                    lines.append(f"- 断言性表述与搜索结果矛盾: {len(false_assertion)} 个")
                    for v in false_assertion[:3]:
                        lines.append(f"  · {v.claim.get('claim', '')[:80]}")
                if false_speculation:
                    lines.append(f"- 推测性表述未得到搜索支持: {len(false_speculation)} 个")
                if null_all:
                    lines.append(f"- 无法验证（搜索证据不足）: {len(null_all)} 个（其中关键信息点 {len(null_critical)} 个）")
                if null_critical:
                    lines.append("- 关键信息点无法验证（存在幻觉风险）:")
                    for v in null_critical[:3]:
                        lines.append(f"  ? {v.claim.get('claim', '')[:80]}")

        lines.append("\n请评估满足度。")
        return "\n".join(lines)


class QualityScorer(LLMAgent):
    """质量评分器"""

    def score(self, user_question: str, original_answer: str,
              satisfaction_result: Dict, verified_results: List[Dict]) -> Dict:
        """
        质量评分
        
        Args:
            user_question: 用户问题
            original_answer: AI回答
            satisfaction_result: 满足度评估结果
            verified_results: 信息点验证结果列表
        """

        # 从文件加载prompt
        system_prompt = load_prompt("pipeline/s3_quality_scorer")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s3_quality_scorer.txt")

        # 传递验证结果，包含原始搜索的query和content
        simplified_results = []
        for item in verified_results:
            claim = item.get('claim', {})
            verify_result = item.get('verify_result', {})
            search_result = item.get('search_result', {})
            
            # 截取搜索内容（避免太长）
            search_content = search_result.get('content', '')
            if len(search_content) > 1500:
                search_content = search_content[:1500] + "...(截断)"
            
            simplified_results.append({
                "claim": claim.get('claim', ''),
                "critical": claim.get('critical', False),
                "verified": verify_result.get('verified'),
                "confidence": verify_result.get('confidence', ''),
                "reason": verify_result.get('reason', ''),
                "search_query": search_result.get('query', ''),  # 原始搜索查询
                "search_content": search_content  # 原始搜索结果内容
            })

        user_prompt = f"""## 用户问题
{user_question}

## AI回答
{original_answer}

## 满足度评估结果
{json.dumps(satisfaction_result, ensure_ascii=False, indent=2)}

## 信息点验证结果
{json.dumps(simplified_results, ensure_ascii=False, indent=2)}

请根据以上信息进行质量评分（1-3分）。"""

        result = self.call(system_prompt, user_prompt, timeout=90)
        self.logger.info(f"质量评分: {result.get('final_score', 'unknown')}分")
        return result


class FactErrorChecker(LLMAgent):
    """事实错误检查器 - 汇总验证结果判定是否有事实错误"""

    def check(self, user_question: str, original_answer: str,
              verified_results: List['VerificationRecord']) -> Dict:
        system_prompt = load_prompt("pipeline/s2d_fact_error")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s2d_fact_error.txt")

        # 只传 VotingVerifier 结论（statement_type + quoted_evidence），不重传搜索原文
        simplified = [
            {
                "claim":          v.claim.get('claim', ''),
                "claim_type":     v.claim.get('type', 'objective'),
                "critical":       v.claim.get('critical', False),
                "statement_type": v.statement_type,
                "verified":       v.verified,
                "confidence":     v.confidence,
                "reason":         v.reason,
                "quoted_evidence": v.quoted_evidence,
            }
            for v in verified_results
        ]

        user_prompt = f"""## 用户问题
{user_question}

## AI回答
{original_answer}

## 信息点验证结果
{json.dumps(simplified, ensure_ascii=False, indent=2)}

请判定是否存在事实错误。"""

        result = self.call(system_prompt, user_prompt, timeout=90)
        self.logger.info(f"事实错误检查: {'有错误' if result.get('has_factual_error') else '无错误'}")
        return result


class FormatChecker(LLMAgent):
    """格式检查器 - 判断是否从2分升为3分"""

    def check(self, user_question: str, original_answer: str, 
              stage3_result: Dict) -> Dict:
        """
        检查格式质量，判断是否升为3分
        
        Args:
            user_question: 用户问题
            original_answer: AI回答
            stage3_result: 阶段3评分结果
            
        Returns:
            {format_quality: str, upgrade_to_3: bool, final_score: int}
        """
        # 从文件加载prompt
        system_prompt = load_prompt("pipeline/s4_format")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s4_format.txt")

        user_prompt = f"""## 用户问题
{user_question}

## AI回答
{original_answer}

## 阶段3评分结果
{json.dumps(stage3_result, ensure_ascii=False, indent=2)}

请检查格式质量，判断是否升为3分。"""

        result = self.call(system_prompt, user_prompt, timeout=60)
        self.logger.info(f"格式检查: {result.get('format_quality', 'unknown')}, 升级={result.get('upgrade_to_3', False)}")
        return result


class AnswerConsistencyChecker(LLMAgent):
    """答案内部一致性检查器：不依赖搜索，直接检测答案内部的细节错误和自相矛盾"""

    def check(self, user_question: str, original_answer: str) -> Dict:
        system_prompt = load_prompt("pipeline/s1b_consistency")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s1b_consistency.txt")

        user_prompt = f"""## 用户问题
{user_question}

## AI回答原文
{original_answer}

请逐类检查答案内部的一致性和细节错误。"""

        result = self.call(system_prompt, user_prompt, timeout=60)
        has_issue = result.get('has_consistency_issue', False)
        critical = result.get('critical_issues', [])
        minor = result.get('minor_issues', [])
        self.logger.info(
            f"答案内部一致性: has_issue={has_issue}, "
            f"critical={len(critical)}个, minor={len(minor)}个"
        )
        return result


class HallucinationChecker(LLMAgent):
    """全局幻觉检测器：基于完整搜索内容+答案整体对照，补充 FactErrorChecker 的单条验证盲点"""

    def check(self, user_question: str, original_answer: str,
              verification_results: List['VerificationRecord']) -> Dict:
        system_prompt = load_prompt("pipeline/s2e_hallucination")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s2e_hallucination.txt")

        # 直接用 VerificationRecord.search_content（已去掉 URL/sources）
        total = len(verification_results)
        evidence_lines = []
        for i, v in enumerate(verification_results, 1):
            evidence_lines.append(
                f"[信息点{i}] {v.claim.get('claim', '')}\n"
                f"搜索内容：{v.search_content}"
            )

        evidence_text = "\n\n---\n\n".join(evidence_lines)

        user_prompt = f"""## 用户问题
{user_question}

## AI回答原文
{original_answer}

## 搜索证据（共{total}条信息点）
{evidence_text}

请全局对照搜索证据，判断AI回答中是否存在明确的幻觉或事实错误。"""

        result = self.call(system_prompt, user_prompt, timeout=90)
        self.logger.info(
            f"幻觉检测: has_hallucination={result.get('has_hallucination')}, "
            f"证据质量={result.get('evidence_quality', 'unknown')}"
        )
        return result


class ReflectionChecker(LLMAgent):
    """反思检查器 —— 接收 ScoringContext，直接从 ctx 读取所有阶段信息"""

    def reflect(self, ctx: 'ScoringContext', current_score: int) -> Dict:
        system_prompt = load_prompt("pipeline/s5_reflection")
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s5_reflection.txt")

        user_prompt = self._build_prompt(ctx, current_score)
        result = self.call(system_prompt, user_prompt, timeout=120)
        self.logger.info(f"反思检查: {'需修正' if result.get('needs_correction') else '确认'}")
        return result

    def _build_prompt(self, ctx: 'ScoringContext', current_score: int) -> str:
        time_prefix = f"[查询时间: {ctx.query_time}]\n\n" if ctx.query_time else ""
        lines = [
            f"## 用户问题\n{time_prefix}{ctx.question}",
            f"\n## AI回答\n{ctx.answer}",
        ]

        # 搜索验证详情：直接从 ctx.verifications 读，保留完整信息
        if ctx.verifications:
            lines.append("\n## 搜索验证结果")
            for v in ctx.verifications:
                verified_str = "✓通过" if v.verified is True else ("✗矛盾" if v.verified is False else "?无法验证")
                claim_text = v.claim.get('claim', '')
                evidence = f"  引用：{v.quoted_evidence[0][:150]}" if v.quoted_evidence else ""
                lines.append(
                    f"- [{verified_str}][{v.statement_type}][critical={'是' if v.claim.get('critical') else '否'}] "
                    f"{claim_text[:120]}\n  理由：{v.reason}{evidence}"
                )

        lines.append(f"\n## 当前评分\n{current_score}分")
        lines.append("\n请进行独立复核，检查是否有遗漏或需要修正的地方。")
        return "\n".join(lines)


class AutoScoringPipeline:
    """全自动评分流水线 V4 - 5阶段递进式评分"""

    def __init__(self, config_file: str, enable_search: bool = True, stage_models: Dict = None):
        """
        初始化流水线

        Args:
            config_file: 配置文件路径
            enable_search: 是否启用搜索验证（默认True）
            stage_models: 各阶段模型配置，格式为 {stage_name: evaluator_name}
                可配置阶段：baseline_checker, answer_parser, voting_verifier,
                            fact_error_checker, satisfaction_evaluator,
                            format_checker, reflection_checker
        """
        self.logger = logging.getLogger("AutoScoringPipeline")

        # 加载配置
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # 获取评估器配置（兼容两种配置格式）
        if 'evaluators' in config:
            evaluators = config['evaluators']
        elif 'answer_comparison' in config and 'evaluators' in config['answer_comparison']:
            evaluators = config['answer_comparison']['evaluators']
        else:
            raise ValueError("配置文件中未找到 'evaluators' 或 'answer_comparison.evaluators'")

        # 找到第一个启用的评估器（作为全局默认）
        evaluator_config = None
        for ev in evaluators:
            if ev.get('enabled', True):
                evaluator_config = ev
                break

        if not evaluator_config:
            raise ValueError("配置文件中没有启用的评估器")

        # 构建所有评估器配置的查找表（含禁用的，供per-stage选择）
        all_evaluator_configs = {ev['name']: ev for ev in evaluators}

        def get_stage_cfg(stage_name: str) -> Dict:
            """获取指定阶段的模型配置，未配置时使用全局默认"""
            if stage_models and stage_name in stage_models:
                model_name = stage_models[stage_name]
                if model_name in all_evaluator_configs:
                    cfg = all_evaluator_configs[model_name]
                    self.logger.info(f"  [{stage_name}] 使用 {cfg['name']} ({cfg['model']})")
                    return cfg
                else:
                    self.logger.warning(f"  [{stage_name}] 未知模型 '{model_name}'，使用默认")
            return evaluator_config

        def make_api_key(cfg: Dict) -> str:
            """从配置提取API密钥"""
            key_name = cfg.get('api_key_name') or cfg.get('provider')
            key = config['api_keys'].get(key_name)
            if not key:
                raise ValueError(f"未找到API密钥: {key_name}")
            return key

        def make_client(cfg: Dict) -> OpenAI:
            """为指定配置创建 OpenAI 客户端"""
            return OpenAI(
                api_key=make_api_key(cfg),
                base_url=cfg.get('base_url', 'https://api.openai.com/v1')
            )

        if stage_models:
            self.logger.info("⚙️ 各阶段模型配置:")

        # 初始化所有Agent（V4流程），每个阶段使用各自的模型配置
        _baseline_cfg = get_stage_cfg('baseline_checker')
        self.baseline_checker = BaselineChecker("baseline", _baseline_cfg, make_api_key(_baseline_cfg))

        # SearchQueryGenerator 是内部辅助工具（AnswerParser已预生成query，很少调用），使用答案拆解阶段的模型
        _sqg_cfg = get_stage_cfg('answer_parser')
        self.search_query_generator = SearchQueryGenerator("search_query_gen", _sqg_cfg, make_api_key(_sqg_cfg))

        _satisfaction_cfg = get_stage_cfg('satisfaction_evaluator')
        self.satisfaction_evaluator = SatisfactionEvaluator("satisfaction", _satisfaction_cfg, make_api_key(_satisfaction_cfg))

        # QualityScorer: V4流程中未调用（已由 SatisfactionEvaluator + FormatChecker 替代）
        # 保留初始化仅供可能的外部调用，不在 score() 中使用
        self.quality_scorer = QualityScorer("quality", evaluator_config, make_api_key(evaluator_config))

        _fact_cfg = get_stage_cfg('fact_error_checker')
        self.fact_error_checker = FactErrorChecker("fact_error", _fact_cfg, make_api_key(_fact_cfg))

        _hallucination_cfg = get_stage_cfg('hallucination_checker')
        self.hallucination_checker = HallucinationChecker("hallucination", _hallucination_cfg, make_api_key(_hallucination_cfg))

        _consistency_cfg = get_stage_cfg('answer_consistency_checker')
        self.answer_consistency_checker = AnswerConsistencyChecker("consistency", _consistency_cfg, make_api_key(_consistency_cfg))

        _format_cfg = get_stage_cfg('format_checker')
        self.format_checker = FormatChecker("format", _format_cfg, make_api_key(_format_cfg))

        _reflection_cfg = get_stage_cfg('reflection_checker')
        self.reflection_checker = ReflectionChecker("reflection", _reflection_cfg, make_api_key(_reflection_cfg))

        # 初始化答案拆分器（独立客户端，支持per-stage模型）
        _parser_cfg = get_stage_cfg('answer_parser')
        self.answer_parser = AnswerParser(
            client=make_client(_parser_cfg),
            model=_parser_cfg['model'],
            temperature=_parser_cfg.get('temperature', 0.1)
        )

        # 初始化多源投票验证器（独立客户端，支持per-stage模型）
        _verifier_cfg = get_stage_cfg('voting_verifier')
        self.multi_source_verifier = MultiSourceVotingVerifier(
            client=make_client(_verifier_cfg),
            model=_verifier_cfg['model'],
            temperature=_verifier_cfg.get('temperature', 1)
        )
        self.logger.info("✓ 多源投票验证器已启用")

        # 初始化搜索集成
        self.enable_search = enable_search
        self.search_integration = None

        if enable_search:
            try:
                from search_integration import SearchIntegration
                self.search_integration = SearchIntegration(config)
                self.logger.info("✓ 搜索集成已启用")
            except Exception as e:
                self.logger.warning(f"⚠️  搜索集成初始化失败，将禁用搜索功能: {e}")
                self.enable_search = False
        else:
            self.logger.info("ℹ 搜索集成已禁用")

        # 初始化速率限制器（1秒内单个服务商最多5次）
        self.rate_limiter = MultiAPIRateLimiter({
            'aliyun': {'max_requests': 5, 'cooldown_seconds': 1.0},
            'kimi': {'max_requests': 5, 'cooldown_seconds': 1.0},
            'zhipu': {'max_requests': 5, 'cooldown_seconds': 1.0}
        })
        self.logger.info("✓ API速率限制器已启用（5请求/秒每服务商）")

        # 注入 rate_limiter 到 search_integration
        if self.search_integration:
            self.search_integration.rate_limiter = self.rate_limiter

        self.logger.info("流水线初始化完成（V4版本：5阶段递进式评分）")

    def _search_for_claim(self, user_question: str, claim: Dict) -> Dict:
        """
        为单个信息点搜索获取参考信息

        Args:
            user_question: 用户问题
            claim: 信息点数据（包含type, claim, query等字段）

        Returns:
            搜索结果
        """
        if not self.enable_search or not self.search_integration:
            return {
                "success": False,
                "error": "搜索功能未启用"
            }

        try:
            # 使用claim中已生成的query，如果没有则生成
            search_query = claim.get('query')
            if not search_query:
                claim_type = claim.get('type', 'objective')
                search_query = self.search_query_generator.generate_query(
                    user_question, claim_type, claim
                )

            if not search_query:
                return {
                    "success": False,
                    "error": "无法生成搜索查询"
                }

            # 执行搜索（速率限制由 search_integration 内部在实际调用时处理）
            self.logger.info(f"    🔍 搜索: {search_query}")
            search_result = self.search_integration.search(
                query=search_query,
                max_providers=2,  # 使用2个搜索提供商
                parallel=True     # 并行搜索（提供商级别）
            )

            if search_result.get('success'):
                self.logger.info(f"    ✓ 搜索成功 ({len(search_result.get('sources', []))} 个来源)")
                return {
                    "success": True,
                    "query": search_query,
                    "content": search_result.get('combined_content', ''),
                    "sources": search_result.get('sources', []),
                    "providers": search_result.get('providers_used', []),
                    "individual_results": search_result.get('individual_results', [])  # ⭐ 关键：传递独立结果
                }
            else:
                self.logger.warning(f"    ✗ 搜索失败: {search_result.get('error')}")
                return {
                    "success": False,
                    "query": search_query,
                    "error": search_result.get('error')
                }

        except Exception as e:
            self.logger.error(f"    ✗ 搜索异常: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def score(self, user_question: str, original: str,
              query_time: Optional[str] = None) -> Dict:
        """执行完整评分流程"""

        start_time = datetime.now()

        # 统一上下文：贯穿所有阶段，append-only
        ctx = ScoringContext(question=user_question, answer=original)
        ctx.query_time = query_time or ""

        # 有查询时间时才注入前缀，帮助模型进行时效性判断
        _q = f"[查询时间: {query_time}]\n\n{user_question}" if query_time else user_question

        # 兼容旧版调用方的 result dict（app.py / excel_auto_scorer 读取这个）
        result = {
            "user_question": user_question,
            "original_answer": original,
            "timestamp": start_time.isoformat(),
            "score": None,
            "reasoning": "",
        }

        try:
            # ========== 阶段1：S1 红线检查 + S1b 内部一致性 并行 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段1：S1 红线检查 + S1b 内部一致性（并行）")
            self.logger.info("="*60)

            with ThreadPoolExecutor(max_workers=2) as _pre_executor:
                _s1_future   = _pre_executor.submit(self.baseline_checker.check, _q, original)
                _s1b_future  = _pre_executor.submit(self.answer_consistency_checker.check, _q, original)
                layer1            = _s1_future.result()
                try:
                    consistency_result = _s1b_future.result(timeout=60)
                except Exception as _e:
                    self.logger.warning(f"S1b 内部一致性检查失败: {_e}")
                    consistency_result = {"has_consistency_issue": False, "critical_issues": [], "minor_issues": []}

            ctx.redline    = layer1
            ctx.consistency = consistency_result
            result['layer1_baseline']  = layer1
            result['stage_consistency'] = consistency_result

            if layer1.get('has_fatal_issue'):
                issue_type = layer1.get('issue_type', '未知')
                detail = layer1.get('detail', '')

                # 标记不召回类型（区别于0分红线违规）
                if '不召回' in (issue_type or ''):
                    result['is_not_recall'] = True
                    result['reasoning'] = f"不召回：{issue_type} - {detail}"
                # 如果是用户信息捏造，特别标注
                elif '捏造' in issue_type or 'fabricated_info' in layer1:
                    result['is_not_recall'] = False
                    fabricated = layer1.get('fabricated_info', [])
                    if fabricated:
                        result['reasoning'] = f"红线问题：{issue_type} - {', '.join(fabricated)}。{detail}"
                    else:
                        result['reasoning'] = f"红线问题：{issue_type} - {detail}"
                else:
                    result['is_not_recall'] = False
                    result['reasoning'] = f"红线问题：{issue_type} - {detail}"

                result['score'] = 0
                result['duration'] = (datetime.now() - start_time).total_seconds()

                self.logger.info(f"❌ 阶段1失败: {result['reasoning']}")
                self.logger.info(f"⚡ 快速失败，节省后续拆分和搜索时间")
                return result

            self.logger.info("✓ 阶段1通过")

            # ========== S1b 结果记录（不再硬终止，作为参考传入 S3）==========
            _critical_issues = consistency_result.get('critical_issues') or []
            if _critical_issues:
                self.logger.info(f"⚠️ S1b 发现 {len(_critical_issues)} 个严重一致性问题，将作为参考传入 S3 满足度评估")
            else:
                self.logger.info("✓ S1b 内部一致性通过")
            self.logger.info("进入阶段2")

            # ========== 阶段2-1：拆分信息点 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段2-1：拆分信息点（事实+观点+推理）")
            self.logger.info("="*60)

            parsed_result = self.answer_parser.parse(_q, original)
            result['stage0_parsing'] = parsed_result

            if 'error' in parsed_result:
                result['score'] = None
                result['reasoning'] = f"答案拆分失败: {parsed_result['error']}"
                return result

            # 获取所有可验证的信息点（展开mixed类型的sub_claims）
            all_claims = self.answer_parser.get_all_verifiable_claims(parsed_result)
            ctx.claims = parsed_result.get('claims', [])
            total_points = len(all_claims)

            stats = parsed_result.get('stats', {})
            self.logger.info(f"共拆分出 {total_points} 个可验证信息点")
            self.logger.info(f"  - 客观: {stats.get('objective', 0)}个")
            self.logger.info(f"  - 主观: {stats.get('subjective', 0)}个")
            self.logger.info(f"  - 混合(已拆分): {stats.get('mixed', 0)}个")

            # ========== 阶段2-2：并行搜索 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段2-2：为每个信息点并行搜索参考信息")
            self.logger.info("="*60)

            search_results = []

            # 🚀 并行搜索所有信息点（不限线程数，速率由 rate_limiter 在各 provider 调用点控制）
            with ThreadPoolExecutor() as executor:
                future_to_point = {}

                for idx, claim in enumerate(all_claims, 1):
                    claim_type = claim.get('type', 'objective')
                    claim_text = claim.get('claim', '')
                    desc = claim_text[:50]

                    self.logger.info(f"  [{idx}/{total_points}] 提交搜索任务: [{claim_type}] {desc}...")

                    future = executor.submit(
                        self._search_for_claim,
                        _q,
                        claim
                    )
                    future_to_point[future] = {
                        "index": idx,
                        "claim": claim,
                        "desc": desc
                    }

                self.logger.info(f"\n🚀 已提交全部 {total_points} 个搜索任务，等待完成...")

                # 收集结果
                completed = 0
                for future in as_completed(future_to_point):
                    claim_info = future_to_point[future]
                    try:
                        search_result = future.result(timeout=120)
                        completed += 1

                        search_results.append({
                            "claim_index": claim_info['index'],
                            "claim": claim_info['claim'],
                            "search_result": search_result
                        })

                        claim_type = claim_info['claim'].get('type', 'objective')
                        self.logger.info(
                            f"  ✓ [{completed}/{total_points}] [{claim_type}] "
                            f"{claim_info['desc'][:30]}... 完成"
                        )

                    except Exception as e:
                        self.logger.error(
                            f"  ✗ [{completed}/{total_points}] "
                            f"{claim_info['desc'][:30]}... 失败: {e}"
                        )
                        search_results.append({
                            "claim_index": claim_info['index'],
                            "claim": claim_info['claim'],
                            "search_result": {
                                "success": False,
                                "error": str(e)
                            }
                        })

            # 按index排序（确保顺序）
            search_results.sort(key=lambda x: x['claim_index'])

            result['stage1_searches'] = {
                "total_claims": total_points,
                "searches": search_results,
                "parallel_execution": True
            }

            self.logger.info(f"\n✓ 阶段2-2完成：已为所有 {total_points} 个信息点并行搜索获取参考信息")

            # ========== 阶段2-3：并行验证 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段2-3：并行验证每个信息点")
            self.logger.info("="*60)

            verification_results = []

            # 🚀 并行验证所有信息点
            MAX_CONCURRENT_VERIFICATIONS = 30  # 最多30个信息点并行验证

            def _verify_single_claim(idx, search_item):
                """验证单个信息点 → 返回 VerificationRecord"""
                claim = search_item['claim']
                claim_type = claim.get('type', 'objective')
                search_result = search_item['search_result']

                self.logger.info(f"  [{idx}/{total_points}] 验证 [{claim_type}] {claim.get('claim', '')[:50]}...")

                individual_results = search_result.get('individual_results', [])
                self.logger.info(f"    使用多源验证（{len(individual_results)}个搜索源）")

                # 合并搜索内容（只保留 AI总结文本，去掉 sources/URL）
                search_content = "\n\n".join(
                    f"【{r.get('provider', 'unknown')}】\n{r.get('content', '')}"
                    for r in individual_results if r.get('content')
                )

                verify_result = self.multi_source_verifier.verify_with_voting(
                    _q, claim, individual_results
                )

                return VerificationRecord(
                    claim=claim,
                    search_content=search_content,
                    verified=verify_result.get('verified'),
                    statement_type=verify_result.get('statement_type', 'assertion'),
                    confidence=verify_result.get('confidence', 'low'),
                    reason=verify_result.get('reason', ''),
                    quoted_evidence=verify_result.get('quoted_evidence', []),
                    claim_index=search_item['claim_index'],
                )

            with ThreadPoolExecutor(max_workers=min(total_points, MAX_CONCURRENT_VERIFICATIONS)) as executor:
                future_to_verify = {}

                for idx, search_item in enumerate(search_results, 1):
                    future = executor.submit(_verify_single_claim, idx, search_item)
                    future_to_verify[future] = idx

                self.logger.info(f"\n🚀 已提交 {total_points} 个并行验证任务，等待完成...")

                completed = 0
                for future in as_completed(future_to_verify):
                    idx = future_to_verify[future]
                    try:
                        record = future.result(timeout=120)
                        completed += 1
                        verification_results.append(record)

                        status = "✅ 通过" if record.verified is True else ("❓ 无法验证" if record.verified is None else "❌ 未通过")
                        self.logger.info(f"  {status} [{completed}/{total_points}] 信息点验证完成")

                    except Exception as e:
                        self.logger.error(f"  ✗ [{completed}/{total_points}] 验证失败: {e}")
                        # 构造失败记录
                        search_item = search_results[idx-1]
                        verification_results.append(VerificationRecord(
                            claim=search_item['claim'],
                            search_content="",
                            verified=None,
                            statement_type='assertion',
                            confidence='low',
                            reason=f"验证过程出错: {str(e)}",
                            claim_index=search_item['claim_index'],
                        ))
                        completed += 1
            # verification_results 现在是 List[VerificationRecord]
            # 统计（直接用 dataclass 属性，不再用 .get()）
            vlist: List[VerificationRecord] = verification_results
            verified_true  = sum(1 for v in vlist if v.verified is True)
            verified_false = sum(1 for v in vlist if v.verified is False)
            verified_null  = sum(1 for v in vlist if v.verified is None)
            critical_records    = [v for v in vlist if v.claim.get('critical')]
            critical_true       = sum(1 for v in critical_records if v.verified is True)
            critical_false_cnt  = sum(1 for v in critical_records if v.verified is False)
            critical_null_count = sum(1 for v in critical_records if v.verified is None)

            self.logger.info(f"\n验证结果统计:")
            self.logger.info(f"  - 总计: {total_points}个信息点")
            self.logger.info(f"  - 验证通过: {verified_true}个")
            self.logger.info(f"  - 验证失败: {verified_false}个")
            self.logger.info(f"  - 无法验证: {verified_null}个")
            self.logger.info(f"  - 关键信息点: {len(critical_records)}个 (通过:{critical_true}, 失败:{critical_false_cnt})")

            # 写入 ctx
            ctx.verifications = vlist

            # ========== 快速失败：assertion + false → 直接 0 分，跳过 s2d/s2e ==========
            assertion_false = [v for v in vlist if v.verified is False and v.statement_type == 'assertion']
            if assertion_false:
                v0 = assertion_false[0]
                evidence_str = f" | 证据：{v0.quoted_evidence[0][:150]}" if v0.quoted_evidence else ""
                ctx.score = 0
                ctx.reasoning = f"事实错误：{v0.claim.get('claim', '')} | 原因：{v0.reason}{evidence_str}"
                ctx.terminated_at = "s2c_fast_exit"
                ctx.duration = (datetime.now() - start_time).total_seconds()
                result.update({"score": 0, "reasoning": ctx.reasoning, "duration": ctx.duration})
                self.logger.info(f"❌ s2c 快速失败：{len(assertion_false)} 个 assertion+false，直接判 0 分")
                return {**result, **ctx.to_legacy_result()}

            self.logger.info("✓ 阶段2-3完成，进入阶段2-4事实错误判定")

            # ========== 阶段2-4：事实错误判定 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段2-4：事实错误判定（断言性+明确矛盾→0分）")
            self.logger.info("="*60)

            fact_check_result = self.fact_error_checker.check(
                _q, original, vlist
            )
            ctx.fact_check = fact_check_result
            result['stage2_fact_check'] = fact_check_result

            if fact_check_result.get('has_factual_error'):
                errors = [e for e in fact_check_result.get('factual_errors', []) if e]
                error_desc = "；".join([
                    f"{e.get('claim', '')} ({e.get('error_type', '')})"
                    for e in errors[:3]
                ])
                ctx.score = 0
                ctx.reasoning = f"事实错误：{error_desc}"
                ctx.terminated_at = "s2d_fact_error"
                ctx.duration = (datetime.now() - start_time).total_seconds()
                result.update({"score": 0, "reasoning": ctx.reasoning, "duration": ctx.duration})
                self.logger.info(f"❌ 阶段2失败: 发现{len(errors)}个事实错误，评为0分")
                return {**result, **ctx.to_legacy_result()}

            self.logger.info("✓ 阶段2通过（无事实错误），进入阶段3")

            # ========== 硬规则：关键信息点被推翻 → 直接0分 ==========
            if critical_false_cnt > 0:
                ctx.critical_false_cap = 0
                self.logger.info(
                    f"⚠️ 硬规则触发：{critical_false_cnt}个关键信息点被搜索结果推翻，最高分限制为0分"
                )

            # ========== 硬规则：关键信息点大量无法验证 → 最高2分 ==========
            if critical_null_count >= 2:
                ctx.critical_null_cap = 2
                self.logger.info(
                    f"⚠️ 硬规则触发：{critical_null_count}个关键信息点无法验证，最高分限制为2分（禁止升3分）"
                )
            else:
                result['critical_null_cap'] = {"triggered": False}

            # ========== 阶段2b：全局幻觉检测 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段2b：全局幻觉检测（基于完整搜索内容整体对照）")
            self.logger.info("="*60)

            hallucination_result = self.hallucination_checker.check(_q, original, verification_results)
            ctx.hallucination = hallucination_result
            result['stage2b_hallucination'] = hallucination_result

            if hallucination_result.get('has_hallucination'):
                details = [d for d in hallucination_result.get('hallucination_details', []) if d]
                detail_str = "; ".join(
                    d.get('contradiction', '') for d in details[:3]
                )
                ctx.score = 0
                ctx.reasoning = f"幻觉检测发现明确事实错误：{detail_str}"
                ctx.terminated_at = "s2e_hallucination"
                ctx.duration = (datetime.now() - start_time).total_seconds()
                result['score'] = 0
                result['reasoning'] = ctx.reasoning
                result['duration'] = ctx.duration
                self.logger.info(f"❌ 幻觉检测失败，判0分: {detail_str}")
                return {**result, **ctx.to_legacy_result()}

            self.logger.info(f"✓ 幻觉检测通过，证据质量={hallucination_result.get('evidence_quality', 'unknown')}")

            # ========== 阶段3：满足度+完整性评估（1分或2分）==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段3：满足度+完整性评估（判定1分或2分）")
            self.logger.info("="*60)

            satisfaction_result = self.satisfaction_evaluator.evaluate(ctx)
            ctx.satisfaction = satisfaction_result
            result['stage3_satisfaction'] = satisfaction_result

            satisfaction_level = satisfaction_result.get('satisfaction_level', 'unknown')
            stage3_score       = satisfaction_result.get('score', 2)
            completeness_issues = satisfaction_result.get('completeness_issues', [])

            self.logger.info(f"  满足度: {satisfaction_level}")
            self.logger.info(f"  完整性问题: {completeness_issues}")
            self.logger.info(f"  阶段3评分: {stage3_score}分")

            if satisfaction_level == "不满足":
                ctx.score = 0
                ctx.reasoning = f"答案完全未满足用户意图：{satisfaction_result.get('score_reason', '')}"
                ctx.terminated_at = "s3_satisfaction"
                ctx.duration = (datetime.now() - start_time).total_seconds()
                result.update({"score": 0, "reasoning": ctx.reasoning, "duration": ctx.duration})
                self.logger.info("❌ 满足度=不满足，直接判0分，终止评分")
                return {**result, **ctx.to_legacy_result()}

            # ========== 阶段4：格式检查（是否升为3分）==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段4：格式检查（是否升为3分）")
            self.logger.info("="*60)

            format_result = self.format_checker.check(
                _q, original,
                {"satisfaction_level": satisfaction_level,
                 "completeness_issues": completeness_issues,
                 "score": stage3_score}
            )
            ctx.format_check = format_result
            result['stage4_format'] = format_result

            if format_result.get('upgrade_to_3') and stage3_score == 2:
                stage4_score = 3
                self.logger.info("✓ 格式优秀，升级为3分")
            else:
                stage4_score = format_result.get('final_score', stage3_score)
                self.logger.info(f"  格式评估: {format_result.get('format_quality', 'unknown')}")
                self.logger.info(f"  阶段4评分: {stage4_score}分")

            result['layer3_quality'] = {
                "satisfaction": satisfaction_result,
                "quality": {"final_score": stage4_score, "reasoning": satisfaction_result.get('reasoning', '')}
            }

            # 应用硬规则上限
            preliminary_score = stage4_score
            if ctx.critical_false_cap is not None and preliminary_score > ctx.critical_false_cap:
                self.logger.info(f"⚠️ 硬规则生效：{preliminary_score}→{ctx.critical_false_cap}分（关键信息点被推翻）")
                preliminary_score = ctx.critical_false_cap
            if ctx.critical_null_cap is not None and preliminary_score > ctx.critical_null_cap:
                self.logger.info(f"⚠️ 硬规则生效：{preliminary_score}→{ctx.critical_null_cap}分（关键信息点无法验证）")
                preliminary_score = ctx.critical_null_cap

            ctx.preliminary_score = preliminary_score
            result['preliminary_score'] = preliminary_score
            self.logger.info(f"✓ 阶段4完成，初步评分: {preliminary_score}分")

            # ========== 阶段5：反思检查 ==========
            self.logger.info("\n" + "="*60)
            self.logger.info("阶段5：反思检查（独立复核）")
            self.logger.info("="*60)

            # 直接传 ctx，ReflectionChecker 自己从 ctx 取完整信息
            reflection = self.reflection_checker.reflect(ctx, preliminary_score)
            ctx.reflection = reflection
            result['reflection'] = reflection

            # 最终分数（反思只能维持或降分，代码层强制保证）
            reflected_score = reflection.get('final_confirmed_score', preliminary_score)
            if reflected_score > preliminary_score:
                self.logger.warning(
                    f"⚠️ 反思试图将{preliminary_score}分升为{reflected_score}分，已拦截，维持{preliminary_score}分"
                )
                reflected_score = preliminary_score
            final_score = reflected_score
            # 拼接各阶段关键结论
            reasoning_parts = []

            # S2d: 事实错误判定
            s2d_r = fact_check_result.get('reasoning', '')
            if s2d_r:
                reasoning_parts.append(f"【S2d事实】{s2d_r}")

            # S2e: 幻觉检测
            s2e_r = hallucination_result.get('reasoning', '') or hallucination_result.get('evidence_quality', '')
            if s2e_r:
                reasoning_parts.append(f"【S2e幻觉】{s2e_r}")

            # S3: 满足度
            s3_r = satisfaction_result.get('score_reason', '') or satisfaction_result.get('reasoning', '')
            if s3_r:
                reasoning_parts.append(f"【S3满足】{s3_r}")

            # S4: 格式
            s4_r = format_result.get('score_reason', '') or format_result.get('format_quality', '')
            if s4_r:
                reasoning_parts.append(f"【S4格式】{s4_r}")

            # S5: 反思
            s5_r = reflection.get('reasoning', '')
            if s5_r:
                reasoning_parts.append(f"【S5反思】{s5_r}")

            ctx.reasoning = ' | '.join(reasoning_parts) if reasoning_parts else ''

            if reflection.get('needs_correction'):
                ctx.reasoning += f" | 反思修正: {reflection.get('correction_reason')}"

            # 硬规则：反思发现事实错误或红线 → 强制0分
            if reflection.get('has_factual_error') or reflection.get('has_redline'):
                if final_score != 0:
                    self.logger.warning(
                        f"⚠️ 反思发现{'事实错误' if reflection.get('has_factual_error') else '红线问题'}，"
                        f"强制将{final_score}分降为0分"
                    )
                    final_score = 0
                    ctx.reasoning += " | 硬规则: 反思阶段发现事实错误/红线，强制0分"

            # 硬规则保护：反思不能突破 caps
            if ctx.critical_false_cap is not None and final_score > ctx.critical_false_cap:
                self.logger.warning(f"⚠️ 硬规则保护：{final_score}→{ctx.critical_false_cap}分（关键信息点失败上限）")
                ctx.reasoning += f" | 硬规则保护: 关键信息点验证失败，反思{final_score}分被限为{ctx.critical_false_cap}分"
                final_score = ctx.critical_false_cap
            if ctx.critical_null_cap is not None and final_score > ctx.critical_null_cap:
                self.logger.warning(f"⚠️ 硬规则保护：{final_score}→{ctx.critical_null_cap}分（无法验证上限）")
                ctx.reasoning += f" | 硬规则保护: 无法验证信息点过多，反思{final_score}分被限为{ctx.critical_null_cap}分"
                final_score = ctx.critical_null_cap

            ctx.score = final_score
            result['score'] = final_score
            result['reasoning'] = ctx.reasoning
            self.logger.info(f"✓ 最终评分: {final_score}分")

        except Exception as e:
            self.logger.error(f"评分失败: {e}", exc_info=True)
            ctx.score = None
            ctx.reasoning = f"处理失败: {str(e)}"

        # 计算耗时
        ctx.duration = (datetime.now() - start_time).total_seconds()

        # 合并 ctx 数据到 result（兼容 app.py / excel_auto_scorer 的旧接口）
        try:
            legacy = ctx.to_legacy_result()
            result.update(legacy)
        except Exception as e2:
            self.logger.error(f"to_legacy_result失败: {e2}", exc_info=True)
            result['score'] = ctx.score
            result['reasoning'] = ctx.reasoning or f"处理失败(legacy): {e2}"
        result['duration'] = ctx.duration

        return result

    def export_to_excel(self, result: Dict, output_file: str = None) -> str:
        """
        将单条评分结果导出为Excel文件
        
        Args:
            result: score()方法返回的评分结果
            output_file: 输出文件路径（可选，默认自动生成）
        
        Returns:
            输出文件路径
        """
        import pandas as pd
        from datetime import datetime
        
        # 自动生成文件名
        if not output_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"auto_scoring_result_{timestamp}.xlsx"
        
        # 构建基础数据
        data = {
            "用户问题": result.get('user_question', ''),
            "AI评分": result.get('score', None),
            "AI评分理由": result.get('reasoning', ''),
            "处理时间": result.get('timestamp', ''),
            "耗时(秒)": result.get('duration', 0),
        }
        
        # 添加各阶段结果摘要
        # 阶段1：红线检查
        layer1 = result.get('layer1_baseline', {})
        if layer1:
            data["阶段1_红线检查"] = "通过" if not layer1.get('has_fatal_issue') else f"失败: {layer1.get('issue_type', '')}"
        
        # 阶段2-1：信息点拆分
        stage0 = result.get('stage0_parsing', {})
        if stage0:
            data["信息点总数"] = stage0.get('total_claims', 0)
            stats = stage0.get('stats', {})
            data["客观信息点"] = stats.get('objective', 0)
            data["主观信息点"] = stats.get('subjective', 0)
        
        # 阶段2-2：搜索
        stage1 = result.get('stage1_searches', {})
        if stage1:
            data["搜索次数"] = len(stage1.get('searches', []))
        
        # 阶段2-3：信息点验证
        layer2 = result.get('layer2_verification', {})
        if layer2:
            stats = layer2.get('stats', {})
            total = stats.get('total', 0)
            verified_true = stats.get('verified_true', 0)
            verified_false = stats.get('verified_false', 0)
            data["验证通过"] = verified_true
            data["验证失败"] = verified_false
            data["无法验证"] = stats.get('verified_null', 0)
            data["关键信息点"] = f"{stats.get('critical_true', 0)}/{stats.get('critical_total', 0)}"
        
        # 阶段3+4：质量评估
        layer3 = result.get('layer3_quality', {})
        if layer3:
            satisfaction = layer3.get('satisfaction', {})
            quality = layer3.get('quality', {})
            data["满足度"] = satisfaction.get('satisfaction_level', '')
            data["质量评分"] = quality.get('final_score', '')
        
        # 阶段5：反思检查
        reflection = result.get('reflection', {})
        if reflection:
            data["阶段5_反思检查"] = "确认" if not reflection.get('needs_correction') else f"修正: {reflection.get('correction_reason', '')}"
        
        # 添加详细JSON（可选，用于调试）
        data["详细JSON"] = json.dumps(result, ensure_ascii=False, indent=2)
        
        # 创建DataFrame
        df = pd.DataFrame([data])
        
        # 导出Excel
        df.to_excel(output_file, index=False, engine='openpyxl')
        
        self.logger.info(f"✓ 评分结果已导出到: {output_file}")
        return output_file

    def score_batch(
        self,
        input_file: str,
        output_file: str = None,
        query_col: str = 'query',
        answer_col: str = 'answer',
        concurrent: bool = True,
        batch_workers: int = 3,
        save_interval: int = 10,
        checkpoint_dir: str = 'checkpoints'
    ) -> Dict:
        """
        批量评分
        
        Args:
            input_file: 输入文件路径（Excel或CSV）
            output_file: 输出文件路径（可选，默认自动生成）
            query_col: 问题列名
            answer_col: 答案列名
            concurrent: 是否并发处理
            batch_workers: 并发线程数
            save_interval: 每N条保存一次结果
            checkpoint_dir: 断点续传目录
            
        Returns:
            处理结果统计
        """
        import pandas as pd
        import os
        
        self.logger.info(f"开始批量评分: {input_file}")
        
        # 自动生成输出文件名
        if not output_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"auto_scoring_batch_{timestamp}.xlsx"
        
        # 读取输入文件
        if input_file.endswith('.xlsx'):
            df = pd.read_excel(input_file)
        else:
            df = pd.read_csv(input_file)
        
        total_rows = len(df)
        mode = "🚀 并发处理" if (concurrent and batch_workers > 1) else "📝 顺序处理"
        self.logger.info(f"读取 {total_rows} 条记录")
        self.logger.info(f"处理模式: {mode} (workers={batch_workers})")
        
        # 创建checkpoint目录
        os.makedirs(checkpoint_dir, exist_ok=True)
        
        # 加载已完成的checkpoint
        completed_indices = set()
        checkpoint_results = {}
        
        for f in os.listdir(checkpoint_dir):
            if f.startswith('row_') and f.endswith('.json'):
                try:
                    idx = int(f.replace('row_', '').replace('.json', ''))
                    with open(os.path.join(checkpoint_dir, f), 'r', encoding='utf-8') as fp:
                        checkpoint_results[idx] = json.load(fp)
                    completed_indices.add(idx)
                except Exception as e:
                    self.logger.warning(f"加载checkpoint失败: {f}, {e}")
        
        if completed_indices:
            self.logger.info(f"✓ 从checkpoint恢复 {len(completed_indices)} 条已完成记录")
        
        # 准备任务（跳过已完成的）
        tasks = []
        for idx, row in df.iterrows():
            if idx in completed_indices:
                continue
            query = str(row.get(query_col, ''))
            answer = str(row.get(answer_col, ''))
            if query and answer:
                tasks.append((idx, query, answer))
        
        pending_count = len(tasks)
        self.logger.info(f"待处理: {pending_count} 条, 已完成: {len(completed_indices)} 条")
        
        # 处理函数
        def process_single(idx: int, query: str, answer: str) -> Dict:
            """处理单条数据"""
            try:
                result = self.score(query, answer)
                result['_idx'] = idx
                
                # 保存checkpoint
                checkpoint_file = os.path.join(checkpoint_dir, f'row_{idx}.json')
                with open(checkpoint_file, 'w', encoding='utf-8') as fp:
                    json.dump(result, fp, ensure_ascii=False, indent=2)
                
                return result
            except Exception as e:
                self.logger.error(f"处理失败 [{idx}]: {e}")
                return {
                    '_idx': idx,
                    'user_question': query,
                    'original_answer': answer,
                    'score': None,
                    'reasoning': f"处理失败: {str(e)}",
                    'error': str(e)
                }
        
        # 执行处理
        results = list(checkpoint_results.values())  # 已完成的结果
        
        if concurrent and batch_workers > 1:
            # 并发处理
            with ThreadPoolExecutor(max_workers=batch_workers) as executor:
                futures = {
                    executor.submit(process_single, idx, query, answer): idx
                    for idx, query, answer in tasks
                }
                
                completed = len(completed_indices)
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        result = future.result(timeout=300)  # 5分钟超时
                        results.append(result)
                        completed += 1
                        
                        score = result.get('score', 'N/A')
                        self.logger.info(f"✓ [{completed}/{total_rows}] 评分: {score}")
                        
                        # 定期保存结果
                        if completed % save_interval == 0:
                            self._save_partial_results(results, output_file, df, query_col, answer_col)
                            self.logger.info(f"📁 已保存 {completed} 条结果")
                            
                    except Exception as e:
                        self.logger.error(f"✗ [{idx}] 失败: {e}")
        else:
            # 顺序处理
            completed = len(completed_indices)
            for idx, query, answer in tasks:
                result = process_single(idx, query, answer)
                results.append(result)
                completed += 1
                
                score = result.get('score', 'N/A')
                self.logger.info(f"✓ [{completed}/{total_rows}] 评分: {score}")
                
                # 定期保存结果
                if completed % save_interval == 0:
                    self._save_partial_results(results, output_file, df, query_col, answer_col)
                    self.logger.info(f"?? 已保存 {completed} 条结果")
        
        # 保存最终结果
        self._save_final_results(results, output_file, df, query_col, answer_col)
        
        # 统计
        scores = [r.get('score') for r in results if r.get('score') is not None]
        score_dist = {}
        for s in scores:
            score_dist[s] = score_dist.get(s, 0) + 1
        
        stats = {
            'total': total_rows,
            'processed': len(results),
            'success': len(scores),
            'failed': len(results) - len(scores),
            'score_distribution': score_dist,
            'output_file': output_file
        }
        
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"批量评分完成")
        self.logger.info(f"{'='*60}")
        self.logger.info(f"总数: {stats['total']}")
        self.logger.info(f"成功: {stats['success']}")
        self.logger.info(f"失败: {stats['failed']}")
        self.logger.info(f"分数分布: {score_dist}")
        self.logger.info(f"结果文件: {output_file}")
        
        return stats

    def _save_partial_results(self, results: List[Dict], output_file: str, 
                               df: 'pd.DataFrame', query_col: str, answer_col: str):
        """保存部分结果（用于断点续传）"""
        import pandas as pd
        
        # 按索引排序
        sorted_results = sorted(results, key=lambda x: x.get('_idx', 0))
        
        # 构建输出数据
        output_data = []
        for r in sorted_results:
            row_data = {
                query_col: r.get('user_question', ''),
                answer_col: r.get('original_answer', ''),
                'AI评分': r.get('score'),
                'AI评分理由': r.get('reasoning', ''),
                '耗时(秒)': r.get('duration', 0),
            }
            
            # 添加验证统计
            layer2 = r.get('layer2_verification', {})
            if layer2:
                stats = layer2.get('stats', {})
                row_data['验证通过'] = stats.get('verified_true', 0)
                row_data['验证失败'] = stats.get('verified_false', 0)
                row_data['无法验证'] = stats.get('verified_null', 0)
            
            # 添加满足度
            layer3 = r.get('layer3_quality', {})
            if layer3:
                satisfaction = layer3.get('satisfaction', {})
                row_data['满足度'] = satisfaction.get('satisfaction_level', '')
            
            output_data.append(row_data)
        
        # 保存
        result_df = pd.DataFrame(output_data)
        temp_file = output_file.replace('.xlsx', '_temp.xlsx')
        result_df.to_excel(temp_file, index=False, engine='openpyxl')

    def _save_final_results(self, results: List[Dict], output_file: str,
                            df: 'pd.DataFrame', query_col: str, answer_col: str):
        """保存最终结果"""
        import pandas as pd
        import os
        
        # 按索引排序
        sorted_results = sorted(results, key=lambda x: x.get('_idx', 0))
        
        # 构建输出数据
        output_data = []
        for r in sorted_results:
            row_data = {
                query_col: r.get('user_question', ''),
                answer_col: r.get('original_answer', ''),
                'AI评分': r.get('score'),
                'AI评分理由': r.get('reasoning', ''),
                '耗时(秒)': r.get('duration', 0),
            }
            
            # 红线检查
            layer1 = r.get('layer1_baseline', {})
            if layer1:
                row_data['红线检查'] = '通过' if not layer1.get('has_fatal_issue') else f"失败: {layer1.get('issue_type', '')}"
            
            # 信息点拆分
            stage0 = r.get('stage0_parsing', {})
            if stage0:
                row_data['信息点数'] = stage0.get('total_claims', 0)
            
            # 验证统计
            layer2 = r.get('layer2_verification', {})
            if layer2:
                stats = layer2.get('stats', {})
                row_data['验证通过'] = stats.get('verified_true', 0)
                row_data['验证失败'] = stats.get('verified_false', 0)
                row_data['无法验证'] = stats.get('verified_null', 0)
                row_data['关键信息点'] = f"{stats.get('critical_true', 0)}/{stats.get('critical_total', 0)}"
            
            # 质量评估
            layer3 = r.get('layer3_quality', {})
            if layer3:
                satisfaction = layer3.get('satisfaction', {})
                quality = layer3.get('quality', {})
                row_data['满足度'] = satisfaction.get('satisfaction_level', '')
                row_data['质量评分'] = quality.get('final_score', '')
            
            # 反思检查
            reflection = r.get('reflection', {})
            if reflection:
                row_data['反思检查'] = '确认' if not reflection.get('needs_correction') else f"修正: {reflection.get('correction_reason', '')}"
            
            output_data.append(row_data)
        
        # 保存最终结果
        result_df = pd.DataFrame(output_data)
        result_df.to_excel(output_file, index=False, engine='openpyxl')
        
        # 删除临时文件
        temp_file = output_file.replace('.xlsx', '_temp.xlsx')
        if os.path.exists(temp_file):
            os.remove(temp_file)
        
        self.logger.info(f"✓ 最终结果已保存到: {output_file}")


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    pipeline = AutoScoringPipeline("config.json")

    # 测试数据
    result = pipeline.score(
        user_question="什么是六欲？",
        original="六欲包括：色欲、形貌欲、威仪姿态欲、言语音声欲、细滑欲、人想欲。"
    )

    print("\n" + "="*60)
    print("最终结果")
    print("="*60)
    print(f"评分: {result['score']}")
    print(f"理由: {result['reasoning']}")
    print(f"耗时: {result['duration']:.1f}秒")

    # 输出详细信息
    if result.get('stage1_searches'):
        print(f"\n搜索信息点数: {result['stage1_searches']['total_claims']}")
    
    # 导出到Excel
    print("\n" + "="*60)
    print("导出结果")
    print("="*60)
    output_file = pipeline.export_to_excel(result)
    print(f"✅ 结果已导出到: {output_file}")
