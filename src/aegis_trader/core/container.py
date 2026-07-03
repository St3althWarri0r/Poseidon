"""Minimal dependency-injection container.

Services register under their type; consumers resolve by type. This keeps
construction order explicit in the kernel while letting tests swap any
component for a fake without monkeypatching.
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


class Container:
    def __init__(self) -> None:
        self._services: dict[type[Any], Any] = {}

    def register(self, interface: type[T], instance: T) -> None:
        if interface in self._services:
            raise RuntimeError(f"{interface.__name__} is already registered")
        self._services[interface] = instance

    def resolve(self, interface: type[T]) -> T:
        try:
            return self._services[interface]  # type: ignore[no-any-return]
        except KeyError:
            raise RuntimeError(f"{interface.__name__} is not registered") from None

    def try_resolve(self, interface: type[T]) -> T | None:
        return self._services.get(interface)

    def __contains__(self, interface: type[Any]) -> bool:
        return interface in self._services
