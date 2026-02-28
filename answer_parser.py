#!/usr/bin/env python3
"""
信息点拆解器 V2（基于5W1H原则）
将答案拆解成独立的、可验证的原子信息点
"""

import json
import logging
import os
from typing import Dict, List
from datetime import datetime
from openai import OpenAI


def load_prompt(prompt_name: str) -> str:
    """从prompts目录加载prompt文件"""
    prompt_path = os.path.join(os.path.dirname(__file__), 'prompts', f'{prompt_name}.txt')
    if os.path.exists(prompt_path):
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read()
    return None


class AnswerParser:
    """信息点拆解器 - 基于5W1H原则的完整信息点拆解"""

    # 主观词汇表（用于统计，不再用于prompt）
    SUBJECTIVE_WORDS = [
        "重要", "精良", "惨重", "惨败", "成功", "失败", "策应", "牵制", "显著",
        "关键", "悲壮", "英勇", "卓越", "优秀", "糟糕", "出色", "严重",
        "很好", "非常", "极其", "适合", "不适合", "建议", "推荐", "应该",
        "最好", "最差", "较好", "较差", "更好", "更差", "优于", "劣于",
        "容易", "困难", "简单", "复杂", "强大", "弱小", "先进", "落后"
    ]

    def __init__(self, client: OpenAI, model: str, temperature: float = 0.1):
        """
        初始化信息点拆解器

        Args:
            client: OpenAI客户端
            model: 使用的模型
            temperature: 温度参数
        """
        self.client = client
        self.model = model
        self.temperature = temperature
        self.logger = logging.getLogger("AnswerParser")

    def _get_max_tokens(self) -> int:
        """
        根据模型自动确定max_tokens限制

        Returns:
            max_tokens值
        """
        # 不同模型的max_tokens限制
        model_limits = {
            'deepseek': 8192,
            'kimi': 16384,  # Kimi K2.5支持更大的输出
            'gpt-4': 16384,
            'gpt-5': 16384,
            'qwen': 8192,
            'glm': 8192,
        }

        # 尝试匹配模型名称
        model_lower = self.model.lower()
        for key, limit in model_limits.items():
            if key in model_lower:
                self.logger.debug(f"模型 {self.model} 使用 max_tokens={limit}")
                return limit

        # 默认使用8192（保守值）
        self.logger.debug(f"模型 {self.model} 使用默认 max_tokens=8192")
        return 8192

    def _clean_answer(self, answer: str) -> str:
        """
        清理答案内容，避免JSON解析错误

        Args:
            answer: 原始答案

        Returns:
            清理后的答案
        """
        # 移除可能导致JSON解析错误的字符
        # 1. 统一换行符
        answer = answer.replace('\r\n', '\n').replace('\r', '\n')

        # 2. 移除零宽字符和特殊控制字符
        import re
        answer = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', answer)

        # 3. 限制长度（避免超长答案导致LLM输出不完整）
        # 保持合理长度，但不要太激进截取
        if len(answer) > 5000:
            self.logger.warning(f"答案过长({len(answer)}字符)，截取前5000字符")
            answer = answer[:5000] + "..."

        return answer.strip()

    def parse(self, user_question: str, answer: str) -> Dict:
        """
        全面拆分答案为原子信息点（基于5W1H原则）

        Args:
            user_question: 用户问题
            answer: 待拆分的答案

        Returns:
            {
                "total_claims": 总信息点数,
                "claims": [
                    {
                        "id": "C1",
                        "type": "objective|subjective|mixed",
                        "claim": "完整信息点文本",
                        "query": "验证搜索查询",
                        "time": "时间",
                        "location": "地点",
                        "critical": true|false,
                        "attribution": "来源（subjective必需）",
                        "sub_claims": []  # 仅用于mixed类型
                    }
                ],
                "stats": {
                    "objective": 客观信息点数,
                    "subjective": 主观信息点数,
                    "mixed": 混合信息点数,
                    "has_time": 包含时间的信息点数,
                    "has_location": 包含地点的信息点数
                }
            }
        """

        # 清理答案内容
        answer = self._clean_answer(answer)

        # 从外部文件加载prompt
        system_prompt_template = load_prompt("pipeline/s2a_parse_claims")
        if not system_prompt_template:
            raise ValueError("Prompt文件未找到: prompts/pipeline/s2a_parse_claims.txt")
        
        # 数据通过 user_prompt 传入，system_prompt 保持纯指令
        system_prompt = system_prompt_template

        user_prompt = f"""用户问题：{user_question}

答案：
{answer}

请将这个答案全面拆解为原子信息点。"""

        try:
            # 根据模型自动确定max_tokens
            max_tokens = self._get_max_tokens()

            last_exc = None
            response = None
            for _attempt in range(2):  # 超时自动重试一次
                try:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=self.temperature,
                        max_tokens=max_tokens,
                        timeout=150,
                        response_format={'type': 'json_object'}
                    )
                    break
                except Exception as _e:
                    last_exc = _e
                    if 'timed out' in str(_e).lower() or 'timeout' in str(_e).lower():
                        self.logger.warning(f"S2a LLM 超时，第{_attempt+1}次重试...")
                        continue
                    raise _e
            if response is None:
                raise last_exc

            content = response.choices[0].message.content.strip()

            # 记录原始响应（用于调试）
            self.logger.info(f"LLM 原始响应长度: {len(content)} 字符")
            self.logger.debug(f"原始响应前200字符: {content[:200]}")

            # 尝试解析JSON，如果失败则尝试修复
            try:
                result = json.loads(content)
                self.logger.info(f"✅ JSON 解析成功，total_claims={result.get('total_claims', 0)}")
            except json.JSONDecodeError as e:
                self.logger.warning(f"❌ JSON解析失败，尝试修复: {e}")
                self.logger.debug(f"原始内容长度: {len(content)}字符")
                self.logger.debug(f"原始内容前500字符: {content[:500]}")
                self.logger.debug(f"原始内容后500字符: {content[-500:]}")

                # 尝试修复常见的JSON错误
                # 1. 移除可能的markdown代码块标记
                content = content.replace('```json', '').replace('```', '').strip()

                # 2. 检查是否被截断（缺少结尾的 }）
                open_braces = content.count('{')
                close_braces = content.count('}')
                open_brackets = content.count('[')
                close_brackets = content.count(']')

                if open_braces > close_braces or open_brackets > close_brackets:
                    self.logger.warning(f"检测到JSON被截断：{{={open_braces}/}}={close_braces}, [={open_brackets}/]={close_brackets}")

                    # 尝试修复：移除最后一个不完整的元素
                    # 找到最后一个完整的 claim 对象
                    last_complete = content.rfind('},')
                    if last_complete > 0:
                        # 截取到最后一个完整的对象
                        content = content[:last_complete + 1]
                        # 补全缺失的括号
                        if open_brackets > close_brackets:
                            content += ']'
                        if '"stats"' not in content:
                            # 如果没有stats，添加一个空的
                            content += ', "stats": {"objective": 0, "subjective": 0, "mixed": 0, "has_time": 0, "has_location": 0}'
                        content += '}'
                        self.logger.info("尝试修复截断的JSON")

                # 3. 尝试找到第一个 { 和最后一个 }
                import re
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    content = json_match.group(0)
                    try:
                        result = json.loads(content)
                        self.logger.info("JSON修复成功")
                    except json.JSONDecodeError as e2:
                        self.logger.error(f"JSON修复失败: {e2}")
                        # 保存错误内容到文件以便调试
                        error_file = f"/tmp/answer_parser_error_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                        with open(error_file, 'w', encoding='utf-8') as f:
                            f.write(f"原始错误: {e}\n\n")
                            f.write(f"修复后错误: {e2}\n\n")
                            f.write(f"内容:\n{content}")
                        self.logger.error(f"错误内容已保存到: {error_file}")
                        raise e
                else:
                    raise e

            # 计算统计数据
            result = self._calculate_stats(result)

            # 统计
            stats = result.get('stats', {})
            total = result.get('total_claims', 0)

            self.logger.info(f"信息点拆解完成：共{total}个信息点")
            self.logger.info(f"  - 客观（objective）: {stats.get('objective', 0)}个")
            self.logger.info(f"  - 主观（subjective）: {stats.get('subjective', 0)}个")
            self.logger.info(f"  - 混合（mixed）: {stats.get('mixed', 0)}个")
            self.logger.info(f"  - 包含时间: {stats.get('has_time', 0)}个")
            self.logger.info(f"  - 包含地点: {stats.get('has_location', 0)}个")

            return result

        except Exception as e:
            self.logger.error(f"信息点拆解失败: {e}")
            return {
                "error": str(e),
                "total_claims": 0,
                "claims": [],
                "stats": {
                    "objective": 0,
                    "subjective": 0,
                    "mixed": 0,
                    "has_time": 0,
                    "has_location": 0
                }
            }

    def _calculate_stats(self, result: Dict) -> Dict:
        """
        计算统计数据

        Args:
            result: 拆解结果

        Returns:
            添加了统计数据的结果
        """
        claims = result.get('claims', [])

        # 统计
        stats = {
            "objective": 0,
            "subjective": 0,
            "mixed": 0,
            "has_time": 0,
            "has_location": 0
        }

        for claim in claims:
            claim_type = claim.get('type', 'objective')

            # 统计类型
            if claim_type in stats:
                stats[claim_type] += 1

            # 统计时间和地点
            if claim.get('time'):
                stats['has_time'] += 1
            if claim.get('location'):
                stats['has_location'] += 1

            # 处理mixed类型的sub_claims
            if claim_type == 'mixed':
                sub_claims = claim.get('sub_claims', [])
                for sub in sub_claims:
                    # 统计sub_claim的时间和地点
                    if sub.get('time'):
                        stats['has_time'] += 1
                    if sub.get('location'):
                        stats['has_location'] += 1

        # 更新结果
        result['stats'] = stats
        result['total_claims'] = len(claims)

        return result

    def get_all_verifiable_claims(self, parsed_result: Dict) -> List[Dict]:
        """
        获取所有可验证的信息点（展开mixed类型的sub_claims）

        Args:
            parsed_result: parse()方法的返回结果

        Returns:
            所有可验证信息点的列表
        """
        all_claims = []
        claims = parsed_result.get('claims', [])

        for claim in claims:
            if not claim:
                continue
            claim_type = claim.get('type', 'objective')

            if claim_type == 'mixed':
                # mixed类型：添加所有sub_claims
                sub_claims = claim.get('sub_claims', []) or []
                for sub in sub_claims:
                    if not sub:
                        continue
                    # 继承父claim的时间和地点（如果sub_claim没有）
                    if not sub.get('time') and claim.get('time'):
                        sub['time'] = claim.get('time')
                    if not sub.get('location') and claim.get('location'):
                        sub['location'] = claim.get('location')
                    all_claims.append(sub)
            else:
                # objective或subjective类型：直接添加
                all_claims.append(claim)

        return all_claims


# 测试代码
if __name__ == "__main__":
    import json as json_module

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 加载配置
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json_module.load(f)

    # 获取第一个启用的评估器配置
    evaluators = config.get('evaluators') or config.get('answer_comparison', {}).get('evaluators', [])
    evaluator_config = None
    for ev in evaluators:
        if ev.get('enabled', True):
            evaluator_config = ev
            break

    if not evaluator_config:
        print("未找到启用的评估器")
        exit(1)

    # 获取API密钥
    api_key_name = evaluator_config.get('api_key_name') or evaluator_config.get('provider')
    api_key = config['api_keys'].get(api_key_name)

    if not api_key:
        print(f"未找到API密钥: {api_key_name}")
        exit(1)

    # 创建客户端
    base_url = evaluator_config.get('base_url', 'https://api.openai.com/v1')
    client = OpenAI(api_key=api_key, base_url=base_url)

    # 创建信息点拆解器
    parser = AnswerParser(
        client=client,
        model=evaluator_config['model'],
        temperature=0.1
    )

    # 测试拆分
    test_question = "什么是六欲？"
    test_answer = """六欲包括：色欲、形貌欲、威仪姿态欲、言语音声欲、细滑欲、人相欲。这是佛教中的概念，指人的六种基本欲望。"""

    print("\n" + "="*60)
    print("测试信息点拆解（V2 - 基于5W1H原则）")
    print("="*60)
    print(f"\n问题: {test_question}")
    print(f"\n答案:\n{test_answer}")

    result = parser.parse(test_question, test_answer)

    print("\n" + "="*60)
    print("拆解结果")
    print("="*60)
    print(json_module.dumps(result, ensure_ascii=False, indent=2))

    print("\n" + "="*60)
    print("所有可验证的信息点")
    print("="*60)
    all_claims = parser.get_all_verifiable_claims(result)
    print(f"共 {len(all_claims)} 个可验证信息点:")
    for i, claim in enumerate(all_claims, 1):
        print(f"\n{i}. [{claim.get('type')}] {claim.get('claim')}")
        print(f"   搜索查询: {claim.get('query')}")
        if claim.get('time'):
            print(f"   时间: {claim.get('time')}")
        if claim.get('location'):
            print(f"   地点: {claim.get('location')}")
