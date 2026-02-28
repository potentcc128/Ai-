#!/usr/bin/env python3
"""
多源投票验证器 - 基于"取众数"的验证机制
每个搜索源单独投票，按多数决定结果
"""

import json
import logging
import re
from typing import Dict, List
from openai import OpenAI


def load_prompt(prompt_name: str) -> str:
    """从prompts目录加载prompt文件"""
    prompt_file = f"prompts/{prompt_name}.txt"
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.warning(f"Prompt文件未找到: {prompt_file}，将使用内置默认prompt")
        return None


class MultiSourceVotingVerifier:
    """多源投票验证器"""

    def __init__(self, client: OpenAI, model: str, temperature: float = 0.3):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.logger = logging.getLogger("MultiSourceVotingVerifier")

    def verify_with_voting(
        self,
        user_question: str,
        claim: Dict,
        search_results: List[Dict]  # 每个搜索源的独立结果
    ) -> Dict:
        """
        合并所有搜索结果，用模型做一次总体验证

        Args:
            user_question: 用户问题
            claim: 信息点 {claim, type, critical, ...}
            search_results: 多个搜索源的结果列表
                [
                    {"provider": "aliyun", "content": "...", "sources": [...]},
                    {"provider": "zhipu", "content": "...", "sources": [...]},
                    {"provider": "kimi", "content": "...", "sources": [...]}
                ]

        Returns:
            {
                "verified": true/false/null,
                "confidence": "high/medium/low",
                "reason": "验证理由",
                "quoted_evidence": [...],
                "providers_used": ["aliyun", "kimi"],
                "total_sources": 5
            }
        """

        claim_text = claim.get('claim', '')
        claim_type = claim.get('type', 'objective')
        is_critical = claim.get('critical', False)

        self.logger.info(f"开始验证信息点: {claim_text[:50]}...")

        # 步骤1: 合并所有搜索结果（只保留 provider + content，不需要 sources 详情）
        combined_content = ""
        providers_used = []

        for search_result in search_results:
            provider = search_result.get('provider', 'unknown')
            content = search_result.get('content', '')

            if content:
                combined_content += f"\n\n【{provider}搜索结果】\n{content}"
                providers_used.append(provider)

        if not combined_content.strip():
            self.logger.warning("所有搜索源都没有返回内容")
            return {
                "verified": None,  # 无法验证（不是错误）
                "confidence": "low",
                "reason": "搜索未返回相关内容，无法验证",
                "quoted_evidence": [],
                "providers_used": [],
                "total_sources": 0
            }

        # 步骤2: 用模型做一次总体判断
        result = self._verify_combined(
            user_question,
            claim_text,
            claim_type,
            combined_content,
            is_critical
        )

        result['providers_used'] = providers_used

        self.logger.info(
            f"验证结果: {'✅ 通过' if result['verified'] else ('❓ 无法验证' if result['verified'] is None else '❌ 未通过')} "
            f"(置信度: {result['confidence']})"
        )

        return result

    def _verify_combined(
        self,
        user_question: str,
        claim_text: str,
        claim_type: str,
        combined_content: str,
        is_critical: bool
    ) -> Dict:
        """对合并后的搜索结果做总体验证"""

        # 从文件加载prompt
        system_prompt = load_prompt("pipeline/s2c_verify_claim")
        
        if not system_prompt:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s2c_verify_claim.txt")

        user_prompt = f"""用户问题：{user_question}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【待验证信息点】（来自原始答案）
{claim_text}

信息点类型：{claim_type}
是否关键：{'是' if is_critical else '否'}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【联网搜索结果】（外部参考依据）
{combined_content[:6000]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

请根据联网搜索结果，判断原始答案中的信息点是否有外部支持：
- 搜索结果明确支持该信息点 → verified: true
- 搜索结果明确与该信息点矛盾 → verified: false
- 搜索结果不足以判断（搜不到、无关、证据不充分）→ verified: null"""

        import time

        api_params = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=self.temperature,
            timeout=60,
            response_format={'type': 'json_object'}
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(**api_params)

                content = response.choices[0].message.content
                if not content or not content.strip():
                    return {"verified": None, "confidence": "low",
                            "reason": "LLM返回空内容", "quoted_evidence": []}
                raw = content.strip()
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    # 模型可能在JSON前输出了文字（如执行步骤），尝试提取JSON块
                    m = re.search(r'\{[\s\S]*\}', raw)
                    if m:
                        result = json.loads(m.group(0))
                    else:
                        raise
                if not isinstance(result, dict):
                    return {"verified": None, "confidence": "low",
                            "reason": f"LLM响应格式错误: {type(result).__name__}", "quoted_evidence": []}
                return result

            except Exception as e:
                err_str = str(e)
                is_rate_limit = '429' in err_str or 'rate_limit' in err_str.lower() or 'RateLimitError' in type(e).__name__
                if is_rate_limit and attempt < max_retries - 1:
                    wait = 10 * (2 ** attempt)  # 10s, 20s
                    self.logger.warning(f"⏳ 触发限流(429)，{wait}秒后重试 (第{attempt+1}/{max_retries-1}次)...")
                    time.sleep(wait)
                    continue
                self.logger.error(f"验证失败: {e}")
                return {
                    "verified": None,
                    "confidence": "low",
                    "reason": f"验证过程出错: {err_str}",
                    "quoted_evidence": []
                }



# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 模拟数据
    claim = {
        "claim": "光的三原色是红、绿、蓝",
        "type": "objective",
        "critical": True
    }

    search_results = [
        {
            "provider": "aliyun",
            "content": "光的三原色（RGB）指红色、绿色、蓝色，是显示设备的基础...",
            "sources": [
                {"title": "光学基础知识", "source": "physics.edu"},
                {"title": "色彩学原理", "source": "color.org"}
            ]
        },
        {
            "provider": "zhipu",
            "content": "在光学中，三原色是红（Red）、绿（Green）、蓝（Blue）...",
            "sources": [
                {"title": "光学百科", "source": "encyclopedia.com"}
            ]
        },
        {
            "provider": "kimi",
            "content": "三原色有不同定义，色料三原色是红、黄、蓝...",
            "sources": [
                {"title": "色彩理论", "source": "art.blog"}
            ]
        }
    ]

    # 加载配置
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 获取评估器配置
    evaluators = config.get('evaluators') or config.get('answer_comparison', {}).get('evaluators', [])
    evaluator_config = None
    for ev in evaluators:
        if ev.get('enabled', True):
            evaluator_config = ev
            break

    if evaluator_config:
        api_key_name = evaluator_config.get('api_key_name') or evaluator_config.get('provider')
        api_key = config['api_keys'].get(api_key_name)
        base_url = evaluator_config.get('base_url', 'https://api.openai.com/v1')

        client = OpenAI(api_key=api_key, base_url=base_url)
        verifier = MultiSourceVotingVerifier(client, evaluator_config['model'])

        result = verifier.verify_with_voting(
            "光的三原色是什么？",
            claim,
            search_results
        )

        print("\n" + "="*60)
        print("投票验证结果")
        print("="*60)
        print(f"验证结果: {'✅ 通过' if result['verified'] else '❌ 未通过'}")
        print(f"置信度: {result['confidence']}")
        print(f"共识: {result['consensus']}")
        print(f"投票: 支持{result['votes']['support']}/反对{result['votes']['oppose']}/不确定{result['votes']['uncertain']}")
        print(f"理由: {result['reason']}")
        print("\n详细投票:")
        for detail in result['vote_details']:
            print(f"  [{detail['provider']}] {detail['vote']} ({detail['confidence']}): {detail['reason']}")
