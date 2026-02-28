#!/usr/bin/env python3
"""
搜索服务提供商模块 V2
按照"先搜索参考内容，再整理生成答案"的统一思路重构
所有提供商都返回：content（整理后的内容） + sources（参考内容）
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import requests
from openai import OpenAI
import logging
import time
import re

# 导入 DashScope SDK（用于阿里云）
try:
    import dashscope
    from dashscope import Generation
    DASHSCOPE_AVAILABLE = True
except ImportError:
    DASHSCOPE_AVAILABLE = False
    logging.warning("dashscope 未安装，阿里云搜索功能将受限。安装方法: pip install dashscope")


class BaseSearchProvider(ABC):
    """搜索服务提供商基类"""

    def __init__(self, api_key: str, config: Optional[Dict] = None):
        """
        初始化搜索提供商

        参数:
            api_key: API 密钥
            config: 提供商配置（如模型名称、超时时间等）
        """
        self.api_key = api_key
        self.config = config or {}
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def search(self, query: str, original_query: Optional[str] = None, 
               shallow_intent: Optional[str] = None, deep_intent: Optional[str] = None) -> Dict[str, Any]:
        """
        执行搜索 - 统一返回格式

        参数:
            query: 搜索查询（实际用于搜索的内容）
            original_query: 原始查询（可选）
            shallow_intent: 浅层意图（可选）
            deep_intent: 深层意图（可选）

        返回:
            {
                "provider": 提供商名称,
                "content": 模型整理后的综合答案（必须有）,
                "sources": [  # 参考内容列表（必须有）
                    {
                        "title": "标题",
                        "url": "URL",
                        "snippet": "摘要",
                        "index": 序号（可选）
                    }
                ]
            }
            或
            {
                "error": 错误信息
            }
        """
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """返回提供商名称"""
        pass

    def _retry_request(self, func, *args, **kwargs):
        """
        通用重试机制

        参数:
            func: 要执行的函数
            *args, **kwargs: 函数参数
        """
        max_retries = self.config.get('max_retries', 2)

        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt < max_retries:
                    self.logger.warning(f"请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                    time.sleep(self.config.get('retry_delay', 1))
                else:
                    raise


class AliyunSearchProvider(BaseSearchProvider):
    """
    阿里云百炼搜索服务

    端到端服务：一次调用返回"整理后的内容" + "参考内容"
    使用原生 DashScope SDK 以获取完整的信息源列表
    参考: https://help.aliyun.com/zh/model-studio/web-search
    """

    def get_provider_name(self) -> str:
        return "阿里云百炼"

    def search(self, query: str, original_query: Optional[str] = None, 
               shallow_intent: Optional[str] = None, deep_intent: Optional[str] = None) -> Dict[str, Any]:
        """执行阿里云百炼搜索（使用原生 SDK）"""

        if not DASHSCOPE_AVAILABLE:
            return {"error": "dashscope SDK 未安装，请运行: pip install dashscope"}

        try:
            def make_request():
                # 设置 API key
                dashscope.api_key = self.api_key

                # 构建 search_options
                search_options = {
                    'enable_source': True,      # 返回搜索来源
                    'enable_citation': True,    # 添加引用标记
                    'citation_format': '[ref_<number>]'  # 自定义引用格式
                }

                # 构建消息
                messages = [{'role': 'user', 'content': query}]

                # 调用 API
                response = Generation.call(
                    model=self.config.get('model', 'qwen-plus-latest'),
                    messages=messages,
                    enable_search=True,
                    search_options=search_options,
                    result_format='message'
                )

                if response.status_code != 200:
                    raise Exception(f"API 调用失败: {response.code} - {response.message}")

                return response

            response = self._retry_request(make_request)

            self.logger.info(f"阿里云搜索成功: {query[:50]}")

            # 提取整理后的内容
            content = response.output.choices[0].message.content

            # 提取参考内容
            sources = []
            if hasattr(response.output, 'search_info'):
                search_info = response.output.search_info
                # search_info 是字典类型，不是对象
                if isinstance(search_info, dict) and 'search_results' in search_info:
                    search_results = search_info['search_results']
                    for item in search_results:
                        sources.append({
                            'title': item.get('title', ''),
                            'url': item.get('url', ''),
                            'snippet': item.get('site_name', ''),
                            'index': item.get('index', 0)
                        })
                    self.logger.info(f"提取到 {len(sources)} 个参考来源")

            return {
                "provider": self.get_provider_name(),
                "content": content,
                "sources": sources
            }

        except Exception as e:
            self.logger.error(f"阿里云搜索失败: {e}")
            return {"error": f"阿里云搜索失败: {str(e)}"}


class ZhipuSearchProvider(BaseSearchProvider):
    """
    智谱 AI 搜索服务

    两步流程：
    1. 调用专用 Web Search API 获取参考内容（sources）
    2. 用参考内容调用 Chat API 生成整理后的答案（content）
    参考: https://docs.bigmodel.cn/cn/guide/tools/web-search
    """

    def get_provider_name(self) -> str:
        return "智谱AI"

    def search(self, query: str, original_query: Optional[str] = None, 
               shallow_intent: Optional[str] = None, deep_intent: Optional[str] = None) -> Dict[str, Any]:
        """执行智谱 AI 搜索（两步流程）"""
        try:
            # 第一步：调用专用搜索 API 获取参考内容
            def make_search_request():
                """调用 Web Search API"""
                url = "https://open.bigmodel.cn/api/paas/v4/web_search"

                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }

                data = {
                    "search_query": query,
                    "search_engine": self.config.get('search_engine', 'search_pro'),
                    "search_intent": True,  # 启用意图识别
                }

                # 添加可选参数
                if self.config.get('search_count'):
                    data['count'] = self.config['search_count']

                if self.config.get('search_recency_filter'):
                    data['search_recency_filter'] = self.config['search_recency_filter']

                if self.config.get('search_domain_filter'):
                    data['search_domain_filter'] = self.config['search_domain_filter']

                if self.config.get('content_size'):
                    data['content_size'] = self.config['content_size']

                response = requests.post(
                    url,
                    headers=headers,
                    json=data,
                    timeout=self.config.get('timeout', 60)
                )
                response.raise_for_status()
                return response.json()

            search_result = self._retry_request(make_search_request)

            self.logger.info(f"智谱 Web Search 成功: {query[:50]}")

            # 提取参考内容
            sources = []
            search_results = search_result.get('search_result', [])

            for i, item in enumerate(search_results, 1):
                sources.append({
                    'title': item.get('title', ''),
                    'url': item.get('link', ''),
                    'snippet': item.get('content', ''),
                    'media': item.get('media', ''),
                    'index': i
                })

            self.logger.info(f"提取到 {len(sources)} 个参考来源")

            # 第二步：用搜索结果生成整理后的答案
            if sources:
                def make_chat_request():
                    """使用搜索结果生成答案"""
                    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    }

                    # System Prompt
                    system_prompt = """你是一个专业的AI助手，能够通过联网搜索回答用户的问题。

回答要求：
1. 综合多个来源的信息，给出全面、准确、有价值的答案
2. 从参考资料中提取对用户有用的补充信息（如相关数据、背景知识、注意事项等）
3. 在答案中引用信息来源（可用序号标注），增强答案的可信度
4. 保持客观中立，不要编造信息
5. 直接给出答案内容，不要说"根据...搜索结果"等套话"""

                    # User Prompt - 只提供搜索查询和参考资料
                    user_prompt = f"用户问题: {query}\n\n"
                    user_prompt += "# 搜索到的参考资料\n\n"
                    for source in sources[:10]:  # 最多用前10个结果
                        user_prompt += f"{source['index']}. {source['title']}\n"
                        if source['media']:
                            user_prompt += f"   来源: {source['media']}\n"
                        user_prompt += f"   内容: {source['snippet'][:200]}...\n"
                        user_prompt += f"   链接: {source['url']}\n\n"
                    
                    user_prompt += "请根据以上信息直接回答用户的问题。"

                    data = {
                        "model": self.config.get('model', 'glm-4-air'),
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ]
                    }

                    response = requests.post(
                        url,
                        headers=headers,
                        json=data,
                        timeout=self.config.get('timeout', 60)
                    )
                    response.raise_for_status()
                    return response.json()

                chat_result = self._retry_request(make_chat_request)
                content = chat_result['choices'][0]['message']['content']
                self.logger.info("智谱答案生成成功")

            else:
                # 没有搜索结果，返回提示
                content = "未找到相关搜索结果，无法回答该问题。"

            return {
                "provider": self.get_provider_name(),
                "content": content,
                "sources": sources
            }

        except Exception as e:
            self.logger.error(f"智谱搜索失败: {e}")
            return {"error": f"智谱搜索失败: {str(e)}"}


class KimiSearchProvider(BaseSearchProvider):
    """
    Kimi (月之暗面) 搜索服务

    两轮Tool Calling流程 + URL提取优化：
    1. 第一轮：模型请求搜索工具
    2. 第二轮：获取整理后的答案（优化prompt让其包含来源）
    3. 从答案中提取URL作为参考内容
    参考: https://platform.moonshot.cn/docs/guide/web-search
    """

    def get_provider_name(self) -> str:
        return "Kimi"

    def search(self, query: str, original_query: Optional[str] = None, 
               shallow_intent: Optional[str] = None, deep_intent: Optional[str] = None) -> Dict[str, Any]:
        """执行 Kimi 搜索（完整的 Tool Calling 流程 + 来源提取）"""
        try:
            def make_request():
                client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.config.get('base_url', "https://api.moonshot.cn/v1")
                )

                # System Prompt
                system_prompt = """你是一个专业的AI助手，能够通过联网搜索回答用户的问题。

回答要求：
1. 综合多个来源的信息，给出全面、准确、有价值的答案
2. 从搜索结果中提取对用户有用的补充信息（如相关数据、背景知识、注意事项等）
3. 在答案中明确标注每个信息的来源，格式为：[来源标题](URL)
4. 保持客观中立，不要编造信息
5. 直接给出答案内容，不要说"根据...搜索结果"等套话"""

                # User Prompt - 只提供搜索查询
                user_prompt = f"用户问题: {query}\n\n请进行联网搜索后直接回答用户的问题。"

                # 构建初始消息
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]

                tools = [{
                    "type": "builtin_function",
                    "function": {"name": "$web_search"}
                }]

                # 第一轮：模型决定是否使用搜索
                self.logger.info("Kimi 第一轮调用：请求搜索（Instant Mode）")

                # 构建请求参数（使用 Instant Mode，显式禁用 thinking）
                create_params = {
                    "model": self.config.get('model', 'kimi-k2.5'),
                    "messages": messages,
                    "tools": tools,
                    "temperature": 1,  # Kimi 模型要求 temperature=1
                    "timeout": self.config.get('timeout', 60),
                    # 显式禁用 thinking 模式，避免 tool_calls 错误
                    "extra_body": {
                        "use_search": True,  # 启用搜索
                        "enable_thinking": False  # 禁用推理模式
                    }
                }

                response = client.chat.completions.create(**create_params)

                message = response.choices[0].message

                # 如果模型返回 tool_calls，需要继续对话
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    self.logger.info(f"Kimi 返回 {len(message.tool_calls)} 个工具调用")

                    # 🔧 新方法：不使用标准 tool calling 流程，直接构造新的 user 消息
                    # 提取搜索查询（从 tool_call 的 arguments 中）
                    search_info = ""
                    for tool_call in message.tool_calls:
                        if tool_call.function.name == "$web_search":
                            import json as json_module
                            try:
                                args = json_module.loads(tool_call.function.arguments)
                                search_query = args.get('query', query)
                                search_info = f"已进行网络搜索：{search_query}"
                            except:
                                search_info = f"已进行网络搜索：{query}"
                            break

                    # 构造新的 user 消息，让模型基于"已搜索"的前提直接回答
                    new_user_message = f"""{search_info}

请现在基于你掌握的最新网络信息直接回答以下问题：{query}

要求：
1. 综合多个来源的信息给出全面、准确的答案
2. 在答案中标注信息来源，格式为：[来源标题](URL)
3. 保持客观中立，不要编造信息
4. 直接给出答案，不要说"根据搜索结果"等套话"""

                    # 使用全新的对话（只保留 system，不包含之前的 assistant tool_calls）
                    fresh_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": new_user_message}
                    ]

                    # 第二轮：获取最终答案（使用全新对话，避免 tool_calls 导致的 thinking 问题）
                    self.logger.info("Kimi 第二轮调用：使用简化流程获取答案")

                    # 不传 tools，使用简化的对话
                    create_params_2 = {
                        "model": self.config.get('model', 'kimi-k2.5'),
                        "messages": fresh_messages,
                        "temperature": 1,  # Kimi 模型要求 temperature=1
                        "timeout": self.config.get('timeout', 60),
                        # 同样禁用 thinking 模式
                        "extra_body": {
                            "use_search": True,
                            "enable_thinking": False
                        }
                    }

                    response = client.chat.completions.create(**create_params_2)

                return response

            response = self._retry_request(make_request)

            self.logger.info(f"Kimi 搜索成功: {query[:50]}")

            # 提取整理后的答案
            content = response.choices[0].message.content or ""

            # 从答案中提取参考内容（URL）
            sources = self._extract_sources_from_content(content)

            if sources:
                self.logger.info(f"从答案中提取到 {len(sources)} 个参考来源")

            return {
                "provider": self.get_provider_name(),
                "content": content,
                "sources": sources
            }

        except Exception as e:
            self.logger.error(f"Kimi 搜索失败: {e}")
            return {"error": f"Kimi 搜索失败: {str(e)}"}

    def _extract_sources_from_content(self, content: str) -> List[Dict[str, Any]]:
        """从 Kimi 返回的内容中提取来源"""
        sources = []

        # 方法1：提取 markdown 链接格式 [标题](URL)
        markdown_pattern = r'\[([^\]]+)\]\((https?://[^\)]+)\)'
        markdown_matches = re.findall(markdown_pattern, content)

        for i, (title, url) in enumerate(markdown_matches, 1):
            sources.append({
                'title': title,
                'url': url,
                'snippet': '',
                'index': i
            })

        # 方法2：如果没找到 markdown 链接，提取所有 URL
        if not sources:
            url_pattern = r'https?://[^\s\)>]+'
            urls = re.findall(url_pattern, content)

            for i, url in enumerate(urls, 1):
                sources.append({
                    'title': f'参考资料 {i}',
                    'url': url,
                    'snippet': '',
                    'index': i
                })

        return sources


class OpenAISearchProvider(BaseSearchProvider):
    """
    OpenAI 搜索服务

    端到端服务：使用 GPT-4o Search Preview 模型
    一次调用返回"整理后的内容" + "参考内容"（待验证）
    参考: https://platform.openai.com/docs/guides/tools-web-search
    """

    def get_provider_name(self) -> str:
        return "OpenAI"

    def search(self, query: str, original_query: Optional[str] = None, 
               shallow_intent: Optional[str] = None, deep_intent: Optional[str] = None) -> Dict[str, Any]:
        """执行 OpenAI 搜索"""
        try:
            def make_request():
                client = OpenAI(api_key=self.api_key)

                # 构建完整的查询内容（包含意图信息）
                full_query = ""
                if original_query or shallow_intent or deep_intent:
                    full_query = "# 用户查询信息\n\n"
                    if original_query:
                        full_query += f"原始查询: {original_query}\n"
                    if shallow_intent:
                        full_query += f"浅层意图: {shallow_intent}\n"
                    if deep_intent:
                        full_query += f"深层意图: {deep_intent}\n"
                    full_query += f"当前搜索: {query}\n\n"
                    full_query += "请基于以上用户查询信息进行搜索并回答，充分理解用户的原始查询和深浅层意图。"
                else:
                    full_query = query

                # 构建 web_search_options
                web_search_options = {}

                # 添加地理位置配置（如果有）
                if self.config.get('user_location'):
                    web_search_options['user_location'] = self.config['user_location']

                # 添加搜索上下文大小配置（如果有）
                if self.config.get('search_context_size'):
                    web_search_options['search_context_size'] = self.config['search_context_size']

                response = client.chat.completions.create(
                    model=self.config.get('model', 'gpt-4o-search-preview'),
                    web_search_options=web_search_options,
                    messages=[{"role": "user", "content": full_query}],
                    timeout=self.config.get('timeout', 60)
                )
                return response

            response = self._retry_request(make_request)

            self.logger.info(f"OpenAI 搜索成功: {query[:50]}")

            # 提取整理后的内容
            content = response.choices[0].message.content

            # 提取参考内容
            sources = []
            response_dict = response.model_dump()

            # 方法1: 从 response 的元数据中提取
            if 'sources' in response_dict:
                for source in response_dict.get('sources', []):
                    sources.append({
                        'title': source.get('title', ''),
                        'url': source.get('url', ''),
                        'snippet': source.get('snippet', '')
                    })

            # 方法2: 从 message 的 annotations 中提取（如果有）
            message = response.choices[0].message
            if hasattr(message, 'annotations'):
                for annotation in message.annotations or []:
                    if hasattr(annotation, 'url'):
                        sources.append({
                            'title': getattr(annotation, 'title', ''),
                            'url': annotation.url,
                            'snippet': getattr(annotation, 'text', '')
                        })

            self.logger.info(f"提取到 {len(sources)} 个参考来源")

            return {
                "provider": self.get_provider_name(),
                "content": content,
                "sources": sources
            }

        except Exception as e:
            self.logger.error(f"OpenAI 搜索失败: {e}")
            return {"error": f"OpenAI 搜索失败: {str(e)}"}


class SearchProviderFactory:
    """搜索服务提供商工厂类"""

    # 提供商类映射
    PROVIDER_CLASSES = {
        'aliyun': AliyunSearchProvider,
        'zhipu': ZhipuSearchProvider,
        'kimi': KimiSearchProvider,
        'openai': OpenAISearchProvider
    }

    @classmethod
    def create_provider(cls, provider_name: str, api_key: str, config: Optional[Dict] = None) -> BaseSearchProvider:
        """
        创建搜索提供商实例

        参数:
            provider_name: 提供商名称 ('aliyun', 'zhipu', 'kimi', 'openai')
            api_key: API 密钥
            config: 提供商配置

        返回:
            搜索提供商实例

        异常:
            ValueError: 如果提供商名称无效
        """
        provider_class = cls.PROVIDER_CLASSES.get(provider_name)

        if not provider_class:
            raise ValueError(f"未知的搜索提供商: {provider_name}")

        return provider_class(api_key, config)

    @classmethod
    def get_available_providers(cls) -> list:
        """返回所有可用的提供商名称"""
        return list(cls.PROVIDER_CLASSES.keys())

    @classmethod
    def register_provider(cls, provider_name: str, provider_class: type):
        """
        注册新的搜索提供商

        参数:
            provider_name: 提供商名称
            provider_class: 提供商类（必须继承自 BaseSearchProvider）
        """
        if not issubclass(provider_class, BaseSearchProvider):
            raise ValueError(f"{provider_class} 必须继承自 BaseSearchProvider")

        cls.PROVIDER_CLASSES[provider_name] = provider_class
