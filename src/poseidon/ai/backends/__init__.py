"""Pluggable LLM backends for the AI layer.

``build_backend`` selects the concrete backend from config. The Anthropic path
is the default and reads its key from the vault; the OpenAI-compatible path
targets a local endpoint and needs no secret.
"""
from __future__ import annotations

from collections.abc import Callable

from ...core.config import AIConfig
from .anthropic_backend import AnthropicBackend
from .base import ChatBackend, LLMResponse, ToolCall, ToolResult
from .openai_backend import OpenAICompatibleBackend

__all__ = ["ChatBackend", "LLMResponse", "ToolCall", "ToolResult",
           "build_backend", "build_backends"]


def build_backend(cfg: AIConfig, resolve_secret: Callable[[str], str]) -> ChatBackend:
    if cfg.backend == "anthropic":
        return AnthropicBackend(cfg, api_key=resolve_secret(cfg.api_key_credential))
    return OpenAICompatibleBackend(cfg)


def build_backends(cfg: AIConfig,
                   resolve_secret: Callable[[str], str]) -> tuple[ChatBackend, ChatBackend]:
    """(primary, utility) backends. Utility IS the primary object unless
    ``cfg.utility_model`` is set, in which case it is the same backend/endpoint
    with the model swapped — for the tolerant auxiliary roles (chat, reflection).
    The trading decision must always use the primary."""
    primary = build_backend(cfg, resolve_secret)
    if not cfg.utility_model:
        return primary, primary
    utility = build_backend(cfg.model_copy(update={"model": cfg.utility_model}), resolve_secret)
    return primary, utility
