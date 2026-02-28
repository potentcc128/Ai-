#!/usr/bin/env python3
"""
搜索集成模块 - 用于自动评分流水线
提供全自动的搜索验证功能
支持并行搜索以提升性能
"""

import logging
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from search_providers import SearchProviderFactory


class SearchIntegration:
    """搜索集成类 - 为自动评分提供搜索验证"""

    def __init__(self, config: Dict):
        """
        初始化搜索集成

        Args:
            config: 配置字典，包含搜索服务配置
        """
        self.logger = logging.getLogger("SearchIntegration")
        self.config = config
        self.rate_limiter = None  # 由外部注入（auto_scoring_pipeline 传入）

        # 初始化搜索提供商（默认使用配置中的第一个）
        self.providers = []

        # 从config中加载搜索提供商配置（尝试多个路径）
        search_config = config.get('search_providers')

        # 如果没有专门的search_providers，尝试从search.available_providers中获取
        if not search_config and 'search' in config:
            search_section = config['search']
            enabled_providers = search_section.get('enabled_providers', [])
            available_providers = search_section.get('available_providers', {})

            # 构建搜索提供商配置
            search_config = []
            for provider_name in enabled_providers:
                if provider_name in available_providers:
                    provider_config = available_providers[provider_name]
                    if provider_config.get('enabled', True):
                        search_config.append({
                            'provider': provider_name,
                            'model': provider_config.get('model'),
                            'api_key_name': provider_name,
                            **provider_config
                        })

        if not search_config:
            search_config = []

        if not search_config:
            self.logger.warning("配置中未找到搜索提供商，尝试使用默认配置")
            # 使用评估器配置作为后备
            evaluators = config.get('evaluators') or config.get('answer_comparison', {}).get('evaluators', [])
            if evaluators:
                # 将评估器配置转换为搜索配置
                for evaluator in evaluators[:1]:  # 只用第一个
                    if not evaluator.get('enabled', True):
                        continue
                    provider_name = self._map_model_to_provider(evaluator.get('model', ''))
                    if provider_name:
                        # 获取API密钥（使用provider字段）
                        api_key_name = evaluator.get('api_key_name') or evaluator.get('provider')
                        api_key = config['api_keys'].get(api_key_name)
                        if api_key:
                            try:
                                provider = SearchProviderFactory.create_provider(
                                    provider_name=provider_name,
                                    api_key=api_key,
                                    config=evaluator
                                )
                                self.providers.append({
                                    'name': provider_name,
                                    'provider': provider
                                })
                                self.logger.info(f"✓ 加载搜索提供商: {provider_name}")
                            except Exception as e:
                                self.logger.warning(f"加载搜索提供商 {provider_name} 失败: {e}")
        else:
            # 使用专门的搜索提供商配置
            for sp_config in search_config[:3]:  # 最多3个
                provider_name = sp_config.get('provider')
                # 获取API密钥名（优先使用api_key_name，其次使用provider名）
                api_key_name = sp_config.get('api_key_name') or provider_name
                api_key = config['api_keys'].get(api_key_name)

                if not api_key:
                    self.logger.warning(f"搜索提供商 {provider_name} 缺少API密钥")
                    continue

                try:
                    provider = SearchProviderFactory.create_provider(
                        provider_name=provider_name,
                        api_key=api_key,
                        config=sp_config
                    )
                    self.providers.append({
                        'name': provider_name,
                        'provider': provider
                    })
                    self.logger.info(f"✓ 加载搜索提供商: {provider_name}")
                except Exception as e:
                    self.logger.warning(f"加载搜索提供商 {provider_name} 失败: {e}")

        if not self.providers:
            self.logger.warning("⚠️  未能加载任何搜索提供商，搜索功能将不可用")

    def _map_model_to_provider(self, model: str) -> Optional[str]:
        """将模型名映射到搜索提供商"""
        model_lower = model.lower()

        if 'qwen' in model_lower or 'tongyi' in model_lower:
            return 'aliyun'
        elif 'glm' in model_lower or 'zhipu' in model_lower:
            return 'zhipu'
        elif 'kimi' in model_lower or 'moonshot' in model_lower:
            return 'kimi'
        elif 'gpt' in model_lower or 'openai' in model_lower:
            return 'openai'

        return None

    def _search_single_provider(self, provider_info: Dict, query: str) -> Dict:
        """
        单个提供商搜索（用于并行调用）

        Args:
            provider_info: 提供商信息字典
            query: 搜索查询

        Returns:
            搜索结果或错误信息
        """
        provider_name = provider_info['name']
        provider = provider_info['provider']

        try:
            # 速率限制：紧贴实际API调用点
            if self.rate_limiter:
                self.rate_limiter.acquire(provider_name, timeout=60)

            self.logger.info(f"  使用 {provider_name} 搜索...")

            # 调用搜索
            result = provider.search(query=query)

            # 检查是否有错误
            if 'error' not in result:
                self.logger.info(f"  ✓ {provider_name} 搜索成功")
                return {
                    'success': True,
                    'provider': provider_name,
                    'content': result.get('content', ''),
                    'sources': result.get('sources', []),
                    'raw_response': result
                }
            else:
                error_msg = result.get('error', '未知错误')
                self.logger.warning(f"  ✗ {provider_name} 搜索失败: {error_msg}")
                return {
                    'success': False,
                    'provider': provider_name,
                    'error': error_msg
                }

        except Exception as e:
            error_detail = f"{type(e).__name__}: {str(e)}"
            self.logger.error(f"  ✗ {provider_name} 搜索异常: {error_detail}", exc_info=True)
            return {
                'success': False,
                'provider': provider_name,
                'error': error_detail
            }

    def search(self, query: str, max_providers: int = 2, parallel: bool = True) -> Dict:
        """
        执行搜索并聚合结果

        Args:
            query: 搜索查询
            max_providers: 最多使用几个搜索提供商
            parallel: 是否并行搜索（默认True，可提升3-5倍速度）

        Returns:
            聚合的搜索结果
        """
        if not self.providers:
            self.logger.warning("无可用的搜索提供商")
            return {
                'success': False,
                'error': '无可用的搜索提供商',
                'results': []
            }

        self.logger.info(f"搜索查询: {query} ({'并行' if parallel else '顺序'})")

        results = []
        errors = []

        # 选择要使用的提供商
        providers_to_use = self.providers[:max_providers]

        if parallel and len(providers_to_use) > 1:
            # 🚀 并行搜索
            with ThreadPoolExecutor(max_workers=len(providers_to_use)) as executor:
                # 提交所有搜索任务
                future_to_provider = {
                    executor.submit(self._search_single_provider, p, query): p['name']
                    for p in providers_to_use
                }

                # 收集结果
                for future in as_completed(future_to_provider):
                    provider_name = future_to_provider[future]
                    try:
                        result = future.result(timeout=90)  # 90秒超时

                        if result.get('success'):
                            results.append({
                                'provider': result['provider'],
                                'content': result['content'],
                                'sources': result['sources'],
                                'raw_response': result['raw_response']
                            })
                        else:
                            errors.append(f"{result['provider']}: {result.get('error', '未知错误')}")

                    except Exception as e:
                        error_detail = f"{type(e).__name__}: {str(e)}"
                        errors.append(f"{provider_name}: {error_detail}")
                        self.logger.error(f"  ✗ {provider_name} 异常: {error_detail}")
        else:
            # 顺序搜索（fallback或单个提供商）
            for provider_info in providers_to_use:
                result = self._search_single_provider(provider_info, query)

                if result.get('success'):
                    results.append({
                        'provider': result['provider'],
                        'content': result['content'],
                        'sources': result['sources'],
                        'raw_response': result['raw_response']
                    })
                else:
                    errors.append(f"{result['provider']}: {result.get('error', '未知错误')}")

        # 返回聚合结果
        if results:
            # 合并所有内容
            combined_content = "\n\n---\n\n".join([
                f"**来源 {i+1} ({r['provider']})**:\n{r['content']}"
                for i, r in enumerate(results)
            ])

            # 合并所有sources
            all_sources = []
            for r in results:
                all_sources.extend(r.get('sources', []))

            return {
                'success': True,
                'query': query,
                'results_count': len(results),
                'providers_used': [r['provider'] for r in results],
                'combined_content': combined_content,
                'sources': all_sources,
                'individual_results': results,
                'errors': errors if errors else None
            }
        else:
            return {
                'success': False,
                'error': f"所有搜索提供商均失败: {'; '.join(errors)}",
                'results': []
            }

    def multi_query_search(self, queries: List[str], max_providers: int = 2) -> Dict:
        """
        并行搜索多个查询（用于观点检查等需要多角度搜索的场景）

        Args:
            queries: 查询列表（如["官方观点", "主流媒体", "权威解读"]）
            max_providers: 每个查询使用几个搜索提供商

        Returns:
            {
                'success': True/False,
                'queries': ['query1', 'query2', ...],
                'all_results': [...],  # 所有查询的结果聚合
                'query_results': {  # 按查询分组
                    'query1': {...},
                    'query2': {...}
                },
                'total_sources': 总来源数,
                'combined_content': '所有内容合并'
            }
        """
        if not queries:
            return {
                'success': False,
                'error': '查询列表为空',
                'all_results': []
            }

        self.logger.info(f"多查询并行搜索: {len(queries)} 个查询")

        # 🚀 并行搜索所有查询
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            future_to_query = {
                executor.submit(self.search, q, max_providers, parallel=True): q
                for q in queries
            }

            query_results = {}
            all_results = []
            errors = []

            for future in as_completed(future_to_query):
                query = future_to_query[future]
                try:
                    result = future.result(timeout=120)  # 2分钟超时

                    query_results[query] = result

                    if result.get('success'):
                        all_results.extend(result.get('individual_results', []))
                        self.logger.info(f"  ✓ 查询 '{query[:30]}...' 完成")
                    else:
                        errors.append(f"查询'{query}': {result.get('error')}")
                        self.logger.warning(f"  ✗ 查询 '{query[:30]}...' 失败")

                except Exception as e:
                    error_detail = f"{type(e).__name__}: {str(e)}"
                    errors.append(f"查询'{query}': {error_detail}")
                    self.logger.error(f"  ✗ 查询 '{query[:30]}...' 异常: {error_detail}")

        # 聚合所有结果
        if all_results:
            # 合并所有内容
            combined_content = "\n\n---\n\n".join([
                f"**来源 {i+1} ({r['provider']} - 查询: {self._find_query_for_result(r, query_results)})**:\n{r['content']}"
                for i, r in enumerate(all_results)
            ])

            # 合并所有sources
            all_sources = []
            for r in all_results:
                all_sources.extend(r.get('sources', []))

            return {
                'success': True,
                'queries': queries,
                'results_count': len(all_results),
                'query_results': query_results,
                'all_results': all_results,
                'combined_content': combined_content,
                'total_sources': len(all_sources),
                'sources': all_sources,
                'errors': errors if errors else None
            }
        else:
            return {
                'success': False,
                'error': f"所有查询均失败: {'; '.join(errors)}",
                'all_results': []
            }

    def _find_query_for_result(self, result: Dict, query_results: Dict) -> str:
        """查找某个结果对应的查询"""
        for query, qr in query_results.items():
            if qr.get('success'):
                for r in qr.get('individual_results', []):
                    if r.get('provider') == result.get('provider'):
                        return query[:20] + '...' if len(query) > 20 else query
        return '未知查询'

    def extract_answer(self, search_result: Dict, question: str,
                      original_value: str, others_value: str) -> str:
        """
        从搜索结果中提取回答要点

        Args:
            search_result: 搜索结果
            question: 问题
            original_value: Original的值
            others_value: 其他答案的值

        Returns:
            提取的回答文本
        """
        if not search_result.get('success'):
            return f"搜索失败: {search_result.get('error', '未知错误')}"

        # 获取合并的内容
        content = search_result.get('combined_content', '')

        if not content:
            return "搜索结果为空"

        # 构建摘要
        providers = search_result.get('providers_used', [])
        sources_count = len(search_result.get('sources', []))

        summary = f"""搜索查询: {question}

争议点:
- Original说: {original_value}
- 其他答案说: {others_value}

搜索结果 (来自 {', '.join(providers)}，共 {sources_count} 个来源):

{content}"""

        return summary


# 测试代码
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 加载配置
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 创建搜索集成
    search = SearchIntegration(config)

    # 测试搜索
    print("\n" + "="*60)
    print("测试搜索集成")
    print("="*60)

    result = search.search("六欲 人想欲 人相欲 哪个正确")

    if result['success']:
        print(f"\n✓ 搜索成功")
        print(f"使用的提供商: {result['providers_used']}")
        print(f"结果数量: {result['results_count']}")
        print(f"\n内容预览:")
        print(result['combined_content'][:500] + "...")
    else:
        print(f"\n✗ 搜索失败: {result['error']}")
