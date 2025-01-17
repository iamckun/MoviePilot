import inspect
import os
from abc import ABC, abstractmethod
from collections import defaultdict
from functools import wraps
from typing import Any, Dict, Optional

import redis
from cachetools import TTLCache
from cachetools.keys import hashkey

# 默认缓存区
DEFAULT_CACHE_REGION = "DEFAULT"


class CacheBackend(ABC):
    """
    缓存后端基类，定义通用的缓存接口
    """

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int, region: str = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区
        :param kwargs: 其他参数
        """
        pass

    @abstractmethod
    def get(self, key: str, region: str = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        pass

    @abstractmethod
    def delete(self, key: str, region: str = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        pass

    @abstractmethod
    def clear(self, region: Optional[str] = None) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区
        """
        pass

    @staticmethod
    def get_region(region: str = DEFAULT_CACHE_REGION):
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:default"


class CacheToolsBackend(CacheBackend):
    """
    基于 `cachetools.TTLCache` 实现的缓存后端，支持动态 TTL 和 Maxsize
    """

    def __init__(self, maxsize: int = 1000, ttl: int = 1800):
        """
        初始化缓存实例

        :param maxsize: 缓存的最大条目数
        :param ttl: 默认缓存存活时间，单位秒
        """
        self.maxsize = maxsize
        self.ttl = ttl
        # 存储各个 region 的缓存实例，region -> {key -> TTLCache}
        self._region_caches: Dict[str, Dict[str, TTLCache]] = defaultdict(dict)

    def set(self, key: str, value: Any, ttl: int = None, region: str = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存值支持每个 key 独立配置 TTL 和 Maxsize

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒如果未传入则使用默认值
        :param region: 缓存的区
        :param kwargs: maxsize: 缓存的最大条目数如果未传入则使用默认值
        """
        ttl = ttl or self.ttl
        maxsize = kwargs.get("maxsize", self.maxsize)
        region = self.get_region(region)
        # 如果该 key 尚未有缓存实例，则创建一个新的 TTLCache 实例
        region_cache = self._region_caches[region]
        if key not in region_cache:
            region_cache[key] = TTLCache(maxsize=maxsize, ttl=ttl)
        # 为每个 key 获取独立的缓存实例
        cache = region_cache[key]
        # 设置缓存值
        cache[key] = value

    def get(self, key: str, region: str = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        region = self.get_region(region)
        region_cache = self._region_caches[region]
        if key not in region_cache:
            return None
        # 获取缓存实例并返回缓存值
        cache = region_cache[key]
        return cache.get(key)

    def delete(self, key: str, region: str = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        region = self.get_region(region)
        region_cache = self._region_caches[region]
        if key not in region_cache:
            return None
        # 获取缓存实例并删除指定的缓存
        cache = region_cache[key]
        del cache[key]

    def clear(self, region: Optional[str] = None) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区
        """
        if region:
            region = self.get_region(region)
            region_cache = self._region_caches[region]
            for cache in region_cache.values():
                cache.clear()
        else:
            for region_cache in self._region_caches.values():
                for cache in region_cache.values():
                    cache.clear()


class RedisBackend(CacheBackend):
    """
    基于 Redis 实现的缓存后端，支持通过 Redis 存储缓存
    """

    def __init__(self, redis_url: str = "redis://localhost", ttl: int = 1800):
        """
        初始化 Redis 缓存实例

        :param redis_url: Redis 服务的 URL
        :param ttl: 缓存的存活时间，单位秒
        """
        self.redis_url = redis_url
        self.ttl = ttl
        self.client = redis.StrictRedis.from_url(redis_url)

    @staticmethod
    def get_redis_key(region, key):
        """
        获取缓存 Key
        """
        # 使用 region 作为缓存键的一部分
        return f"region:{region}:key:{key}"

    def set(self, key: str, value: Any, ttl: int = None, region: str = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒如果未传入则使用默认值
        :param region: 缓存的区
        :param kwargs: kwargs
        """
        ttl = ttl or self.ttl
        redis_key = self.get_redis_key(region, key)
        self.client.setex(redis_key, ttl, value)

    def get(self, key: str, region: str = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        redis_key = self.get_redis_key(region, key)
        value = self.client.get(redis_key)
        return value

    def delete(self, key: str, region: str = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        redis_key = self.get_redis_key(region, key)
        self.client.delete(redis_key)

    def clear(self, region: Optional[str] = None) -> None:
        """
        清除 Redis 中指定区域的缓存或全部缓存

        :param region: 缓存的区
        """
        if region:
            # 清除指定区域的所有键
            pattern = f"{region}:*"
            keys = list(self.client.keys(pattern))
            if keys:
                self.client.delete(*keys)
        else:
            # 清除所有缓存
            self.client.flushdb()


def get_cache_backend(maxsize: int = 1000, ttl: int = 1800) -> CacheBackend:
    """
    根据配置获取缓存后端实例

    :param maxsize: 缓存的最大条目数
    :param ttl: 缓存的默认存活时间，单位秒
    :return: 返回缓存后端实例
    """
    cache_type = os.getenv("CACHE_TYPE", "cachetools").lower()

    if cache_type == "redis":
        return RedisBackend(redis_url=os.getenv("REDIS_URL", "redis://localhost"))
    return CacheToolsBackend(maxsize=maxsize, ttl=ttl)


def cached(region: str = DEFAULT_CACHE_REGION, maxsize: int = 1000, ttl: int = 1800,
           skip_none: bool = True, skip_empty: bool = False):
    """
    自定义缓存装饰器，支持为每个 key 动态传递 maxsize 和 ttl

    :param region: 缓存的区
    :param maxsize: 缓存的最大条目数，默认值为 1000
    :param ttl: 缓存的存活时间，单位秒，默认值为 1800
    :param skip_none: 跳过 None 缓存，默认为 True
    :param skip_empty: 跳过空值缓存（如 [], {}, "", set()），默认为 False
    :return: 装饰器函数
    """

    def should_cache(value: Any) -> bool:
        """
        判断是否应该缓存结果，如果返回值是 None 或空值则不缓存

        :param value: 要判断的缓存值
        :return: 是否缓存结果
        """
        if skip_none and value is None:
            return False
        # if disable_empty and value in [[], {}, "", set()]:
        if skip_empty and not value:
            return False
        return True

    def get_cache_key(func, args, kwargs):
        """
        获取缓存的键，通过哈希函数对函数的参数进行处理
        :param func: 被装饰的函数
        :param args: 位置参数
        :param kwargs: 关键字参数
        :return: 缓存键
        """
        # 获取方法签名
        signature = inspect.signature(func)
        resolved_kwargs = {}
        # 获取默认值并结合传递的参数（如果有）
        for param, value in signature.parameters.items():
            if param in kwargs:
                # 使用显式传递的参数
                resolved_kwargs[param] = kwargs[param]
            elif value.default is not inspect.Parameter.empty:
                # 没有传递参数时使用默认值
                resolved_kwargs[param] = value.default
        # 构造缓存键
        return f"{func.__name__}_{hashkey(*args, **resolved_kwargs)}"

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 获取缓存键
            cache_key = get_cache_key(func, args, kwargs)
            # 尝试获取缓存
            cached_value = cache_backend.get(cache_key, region=region)
            if should_cache(cached_value):
                return cached_value
            # 执行函数并缓存结果
            result = func(*args, **kwargs)
            # 判断是否需要缓存
            if not should_cache(result):
                return result
            # 设置缓存（如果有传入的 maxsize 和 ttl，则覆盖默认值）
            cache_backend.set(cache_key, result, ttl=ttl, maxsize=maxsize, region=region)
            return result

        return wrapper

    return decorator


# 缓存后端实例
cache_backend = get_cache_backend()
