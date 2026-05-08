# -*- coding: utf-8 -*-
"""
泛型注册器基类。

提供可复用的键值注册模式，用于实现工厂和策略模式。
整个项目中所有注册器都继承自此类。
"""

from typing import TypeVar, Generic, Optional, Iterator, FrozenSet


K = TypeVar('K')
V = TypeVar('V')


class Registry(Generic[K, V]):
    """泛型注册器基类，支持以任意可哈希类型为键注册值。

    使用示例:
        >>> reg = Registry[str, type]()
        >>> reg.register("md5", MD5Hash)
        >>> reg.get("md5")
        <class 'MD5Hash'>
    """

    _items: dict[K, V]

    def __init__(self) -> None:
        """初始化空的注册表。"""
        self._items = {}

    def register(self, key: K, value: V) -> None:
        """注册一个键值对。

        参数:
            key: 注册键（常为字符串或枚举）。
            value: 注册值（常为类或实例）。

        异常:
            KeyError: 如果键已存在。
        """
        if key in self._items:
            raise KeyError(f"注册键 '{key}' 已存在，不允许重复注册")
        self._items[key] = value

    def unregister(self, key: K) -> None:
        """移除一个已注册的键。

        参数:
            key: 要移除的注册键。
        """
        self._items.pop(key, None)

    def get(self, key: K) -> Optional[V]:
        """获取键对应的值。

        参数:
            key: 注册键。

        返回:
            注册的值，如果键不存在则返回 None。
        """
        return self._items.get(key)

    def get_all(self) -> dict[K, V]:
        """返回所有已注册项的浅拷贝。"""
        return dict(self._items)

    @property
    def registered_keys(self) -> FrozenSet[K]:
        """返回所有已注册键的不可变集合。"""
        return frozenset(self._items.keys())

    @property
    def count(self) -> int:
        """返回已注册项的数量。"""
        return len(self._items)

    def __iter__(self) -> Iterator[tuple[K, V]]:
        """迭代所有键值对。"""
        return iter(self._items.items())

    def __contains__(self, key: K) -> bool:
        """判断键是否已注册。"""
        return key in self._items

    def __len__(self) -> int:
        """返回已注册项的数量。"""
        return len(self._items)

    def __repr__(self) -> str:
        return f"<Registry keys={sorted(self._items.keys())}>"
