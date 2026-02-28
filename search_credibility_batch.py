#!/usr/bin/env python3
"""
搜索+可信度批量流水线
流程：搜索 query → 多评估模型投票 → 输出 final_credibility
"""

import json
import logging
import time
from typing import Dict, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from openai import OpenAI

from search_providers import SearchProviderFactory


# ── 评估 Prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是内容质量评估专家。

你将收到：
- 用户问题
- 原始答案（待评估）
- 搜索参考内容（来自联网搜索，可能包含多个来源）

你的任务是：判断原始答案与搜索参考内容是否一致。

判断标准：
- high：原始答案的核心事实与搜索内容基本一致，无明显错误
- low：原始答案与搜索内容有明显矛盾，或包含搜索内容明确否定的事实
- uncertain：搜索内容不足以判断，或答案部分正确、部分存疑

输出JSON格式：
{"credibility": "high|low|uncertain", "reasoning": "一句话理由"}"""


def _build_user_prompt(query: str, original_answer: str, search_contents: Dict[str, str]) -> str:
    parts = [
        f"## 用户问题\n{query}",
        f"\n## 原始答案\n{original_answer}",
        "\n## 搜索参考内容",
    ]
    for provider, content in search_contents.items():
        parts.append(f"\n【{provider}搜索结果】\n{content[:2000]}")
    return "\n".join(parts)


# ── 单个评估器 ────────────────────────────────────────────────────────────────

class CredibilityEvaluator:
    def __init__(self, name: str, config: Dict, api_key: str):
        self.name = name
        self.model = config['model']
        self.temperature = config.get('temperature', 1)
        self.client = OpenAI(
            api_key=api_key,
            base_url=config.get('base_url', 'https://api.openai.com/v1')
        )
        self.logger = logging.getLogger(f"CredEval-{name}")

    def evaluate(self, query: str, original_answer: str, search_contents: Dict[str, str]) -> Dict:
        user_prompt = _build_user_prompt(query, original_answer, search_contents)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self.temperature,
                    timeout=60,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content.strip()
                result = json.loads(content)
                return {
                    "credibility": result.get("credibility", "uncertain"),
                    "reasoning": result.get("reasoning", ""),
                }
            except Exception as e:
                err = str(e)
                is_rate = '429' in err or 'rate_limit' in err.lower()
                if is_rate and attempt < max_retries - 1:
                    wait = 10 * (2 ** attempt)
                    self.logger.warning(f"限流，{wait}s 后重试...")
                    time.sleep(wait)
                    continue
                self.logger.error(f"评估失败: {e}")
                return {"credibility": "uncertain", "reasoning": f"评估失败: {err}"}


# ── 主流水线 ──────────────────────────────────────────────────────────────────

class SearchCredibilityBatch:
    """搜索+可信度批量流水线"""

    def __init__(self, config: Dict):
        self.config = config
        self.logger = logging.getLogger("SearchCredibilityBatch")

        # 初始化搜索 providers
        self.search_providers = self._init_search_providers()

        # 初始化评估器
        self.evaluators = self._init_evaluators()

        self.logger.info(
            f"初始化完成：{len(self.search_providers)} 个搜索源，{len(self.evaluators)} 个评估器"
        )

    def _init_search_providers(self) -> List[Dict]:
        providers = []
        search_cfg = self.config.get('search', {})
        enabled = search_cfg.get('enabled_providers', [])
        available = search_cfg.get('available_providers', {})
        api_keys = self.config.get('api_keys', {})

        for name in enabled:
            pcfg = available.get(name, {})
            if not pcfg.get('enabled', True):
                continue
            api_key = api_keys.get(name, '')
            if not api_key:
                self.logger.warning(f"搜索源 {name} 缺少 API Key，跳过")
                continue
            try:
                provider = SearchProviderFactory.create_provider(name, api_key, pcfg)
                providers.append({'name': name, 'provider': provider})
                self.logger.info(f"✓ 搜索源: {name}")
            except Exception as e:
                self.logger.error(f"初始化搜索源 {name} 失败: {e}")

        return providers

    def _init_evaluators(self) -> List[CredibilityEvaluator]:
        evaluators = []
        api_keys = self.config.get('api_keys', {})
        for ev in self.config.get('evaluators', []):
            if not ev.get('enabled', True):
                continue
            api_key_name = ev.get('api_key_name') or ev.get('provider', '')
            api_key = api_keys.get(api_key_name, '')
            if not api_key:
                continue
            evaluators.append(CredibilityEvaluator(ev['name'], ev, api_key))
            self.logger.info(f"✓ 评估器: {ev['name']} ({ev['model']})")
        return evaluators

    # ── 单行处理 ──────────────────────────────────────────────────────────────

    def _search(self, query: str) -> Dict[str, str]:
        """并行搜索所有 provider，返回 {provider_name: content}"""
        results = {}
        with ThreadPoolExecutor(max_workers=len(self.search_providers) or 1) as ex:
            futures = {
                ex.submit(p['provider'].search, query): p['name']
                for p in self.search_providers
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    r = future.result(timeout=90)
                    content = r.get('content', '')
                    if content:
                        results[name] = content
                except Exception as e:
                    self.logger.error(f"搜索 {name} 失败: {e}")
        return results

    def _vote(self, evaluator_results: Dict[str, Dict]) -> str:
        """多数派投票"""
        counts = {"high": 0, "low": 0, "uncertain": 0}
        for r in evaluator_results.values():
            cred = r.get("credibility", "uncertain")
            if cred in counts:
                counts[cred] += 1
        total = sum(counts.values())
        if total == 0:
            return "uncertain"
        # 超过一半才算多数派
        for verdict in ("low", "high", "uncertain"):
            if counts[verdict] / total > 0.5:
                return verdict
        return "uncertain"

    def process_row(self, query: str, original_answer: str) -> Dict:
        start = time.time()

        # 1. 搜索
        search_contents = self._search(query)
        if not search_contents:
            return {
                "final_credibility": "uncertain",
                "evaluator_results": {},
                "search_providers_used": [],
                "duration": round(time.time() - start, 1),
                "error": "所有搜索源均无返回",
            }

        # 2. 多评估器并行投票
        evaluator_results = {}
        with ThreadPoolExecutor(max_workers=len(self.evaluators) or 1) as ex:
            futures = {
                ex.submit(ev.evaluate, query, original_answer, search_contents): ev.name
                for ev in self.evaluators
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    evaluator_results[name] = future.result(timeout=90)
                except Exception as e:
                    evaluator_results[name] = {"credibility": "uncertain", "reasoning": f"失败: {e}"}

        # 3. 投票
        final = self._vote(evaluator_results)

        return {
            "final_credibility": final,
            "evaluator_results": evaluator_results,
            "search_providers_used": list(search_contents.keys()),
            "duration": round(time.time() - start, 1),
        }

    # ── 批量处理 ──────────────────────────────────────────────────────────────

    def process_batch(
        self,
        df: pd.DataFrame,
        query_col: str,
        answer_col: str,
        max_workers: int = 3,
        progress_callback: Optional[Callable] = None,
    ) -> pd.DataFrame:
        """
        批量处理 DataFrame，返回带结果列的新 DataFrame

        progress_callback(completed, total) 用于更新进度
        """
        result_df = df.copy()
        total = len(df)
        completed = 0

        # 收集评估器名称（用于建列）
        evaluator_names = [ev.name for ev in self.evaluators]
        for col in ["final_credibility"] + [f"{n}_credibility" for n in evaluator_names] + [f"{n}_reasoning" for n in evaluator_names] + ["搜索来源", "耗时(秒)"]:
            result_df[col] = ""

        results_map: Dict[int, Dict] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(
                    self.process_row,
                    str(row[query_col]),
                    str(row[answer_col]),
                ): idx
                for idx, row in df.iterrows()
            }

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    r = future.result(timeout=120)
                    results_map[idx] = r
                except Exception as e:
                    results_map[idx] = {
                        "final_credibility": "uncertain",
                        "evaluator_results": {},
                        "search_providers_used": [],
                        "duration": 0,
                        "error": str(e),
                    }
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

        # 写回 DataFrame
        for idx, r in results_map.items():
            result_df.at[idx, "final_credibility"] = r.get("final_credibility", "")
            result_df.at[idx, "搜索来源"] = ", ".join(r.get("search_providers_used", []))
            result_df.at[idx, "耗时(秒)"] = r.get("duration", "")
            for ev_name, ev_r in r.get("evaluator_results", {}).items():
                result_df.at[idx, f"{ev_name}_credibility"] = ev_r.get("credibility", "")
                result_df.at[idx, f"{ev_name}_reasoning"] = ev_r.get("reasoning", "")

        return result_df
