#!/usr/bin/env python3
"""
速率限制器 - 控制API请求速率
每个API：每秒最多5个请求，3秒冷却期
"""

import time
import threading
from typing import Dict
import logging


class APIRateLimiter:
    """单个API的速率限制器"""

    def __init__(self, api_name: str, max_requests: int = 5, cooldown_seconds: float = 3.0):
        """
        初始化速率限制器

        Args:
            api_name: API名称（如'bailian', 'kimi'）
            max_requests: 每个时间窗口最多请求数
            cooldown_seconds: 冷却时间（秒）
        """
        self.api_name = api_name
        self.max_requests = max_requests
        self.cooldown_seconds = cooldown_seconds

        self.request_times = []  # 请求时间戳列表
        self.lock = threading.Lock()
        self.logger = logging.getLogger(f"RateLimiter-{api_name}")

    def acquire(self, timeout: float = 60.0) -> bool:
        """
        获取请求许可（会等待直到可以发送请求）

        Args:
            timeout: 最长等待时间（秒）

        Returns:
            是否成功获取许可
        """
        start_time = time.time()

        while True:
            # 每次循环重新加锁，不在锁内睡眠，允许其他线程并发检查
            with self.lock:
                now = time.time()

                # 清除过期的请求记录（超过冷却期的）
                cutoff_time = now - self.cooldown_seconds
                self.request_times = [t for t in self.request_times if t > cutoff_time]

                # 检查是否可以发送请求
                if len(self.request_times) < self.max_requests:
                    # 可以发送，记录时间
                    self.request_times.append(now)
                    self.logger.debug(
                        f"✓ {self.api_name} 许可获取成功 "
                        f"({len(self.request_times)}/{self.max_requests})"
                    )
                    return True

                # 已达上限，在锁内计算需要等待的时间，但锁外再睡眠
                if time.time() - start_time > timeout:
                    self.logger.warning(f"✗ {self.api_name} 获取许可超时")
                    return False

                oldest_request = min(self.request_times)
                wait_until = oldest_request + self.cooldown_seconds
                wait_time = max(0.0, wait_until - now)

            # 锁已释放，睡眠期间其他线程可以正常 acquire
            if wait_time > 0:
                self.logger.info(
                    f"⏳ {self.api_name} 速率限制，等待 {wait_time:.1f}秒 "
                    f"(当前窗口已满 {self.max_requests}/{self.max_requests})"
                )
                time.sleep(min(wait_time, 0.1))  # 最多睡眠0.1秒，然后重新检查

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self.lock:
            now = time.time()
            cutoff_time = now - self.cooldown_seconds
            active_requests = [t for t in self.request_times if t > cutoff_time]

            return {
                "api_name": self.api_name,
                "active_requests": len(active_requests),
                "max_requests": self.max_requests,
                "cooldown_seconds": self.cooldown_seconds,
                "available_slots": self.max_requests - len(active_requests)
            }


class MultiAPIRateLimiter:
    """多个API的速率限制器管理器"""

    def __init__(self, provider_configs: Dict[str, Dict] = None):
        """
        初始化多API速率限制器

        Args:
            provider_configs: 提供商配置
            {
                'bailian': {'max_requests': 5, 'cooldown_seconds': 3.0},
                'kimi': {'max_requests': 5, 'cooldown_seconds': 3.0},
                'zhipu': {'max_requests': 5, 'cooldown_seconds': 3.0}
            }
        """
        self.limiters = {}
        self.logger = logging.getLogger("MultiAPIRateLimiter")

        # 默认配置
        if provider_configs is None:
            provider_configs = {
                'aliyun': {'max_requests': 5, 'cooldown_seconds': 1.0},
                'kimi': {'max_requests': 5, 'cooldown_seconds': 1.0},
                'zhipu': {'max_requests': 5, 'cooldown_seconds': 1.0}
            }

        # 为每个提供商创建限制器
        for provider_name, config in provider_configs.items():
            self.limiters[provider_name] = APIRateLimiter(
                api_name=provider_name,
                max_requests=config.get('max_requests', 5),
                cooldown_seconds=config.get('cooldown_seconds', 3.0)
            )
            self.logger.info(
                f"✓ 创建速率限制器: {provider_name} "
                f"({config.get('max_requests', 5)}请求/{config.get('cooldown_seconds', 3.0)}秒)"
            )

    def acquire(self, provider_name: str, timeout: float = 60.0) -> bool:
        """
        为指定提供商获取请求许可

        Args:
            provider_name: 提供商名称（如'aliyun', 'kimi', 'zhipu'）
            timeout: 最长等待时间

        Returns:
            是否成功获取许可
        """
        # 标准化提供商名称
        provider_name = self._normalize_provider_name(provider_name)

        if provider_name not in self.limiters:
            self.logger.warning(f"未知提供商: {provider_name}，跳过速率限制")
            return True

        return self.limiters[provider_name].acquire(timeout)

    def _normalize_provider_name(self, name: str) -> str:
        """标准化提供商名称"""
        name_lower = name.lower()

        # 映射各种可能的名称
        if name_lower in ['aliyun', 'bailian', '阿里云', 'tongyi']:
            return 'aliyun'
        elif name_lower in ['kimi', 'moonshot', '月之暗面']:
            return 'kimi'
        elif name_lower in ['zhipu', 'glm', '智谱']:
            return 'zhipu'
        else:
            return name_lower

    def get_all_stats(self) -> Dict[str, Dict]:
        """获取所有API的统计信息"""
        return {
            name: limiter.get_stats()
            for name, limiter in self.limiters.items()
        }

    def print_stats(self):
        """打印统计信息"""
        stats = self.get_all_stats()
        self.logger.info("\n" + "="*60)
        self.logger.info("API速率限制状态")
        self.logger.info("="*60)
        for provider, stat in stats.items():
            self.logger.info(
                f"{provider}: {stat['active_requests']}/{stat['max_requests']} "
                f"(可用: {stat['available_slots']})"
            )


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "="*60)
    print("测试速率限制器")
    print("="*60)

    # 创建限制器（测试用：每秒2个请求，3秒冷却）
    limiter = MultiAPIRateLimiter({
        'test_api': {'max_requests': 2, 'cooldown_seconds': 3.0}
    })

    print("\n测试场景：连续发送5个请求")
    print("预期：前2个立即通过，后3个需要等待3秒")

    for i in range(5):
        print(f"\n请求 {i+1}:")
        start = time.time()
        success = limiter.acquire('test_api', timeout=10)
        elapsed = time.time() - start

        if success:
            print(f"  ✓ 获取许可成功，耗时: {elapsed:.2f}秒")
        else:
            print(f"  ✗ 获取许可失败，耗时: {elapsed:.2f}秒")

    print("\n" + "="*60)
    limiter.print_stats()
