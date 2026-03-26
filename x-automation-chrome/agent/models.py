"""
Multi-model dispatch for the X automation subagent.
Supports Claude (via litellm), OpenAI, and Ollama.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from agents import Model
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

ModelBackend = Literal["openai", "ollama", "claude"]


@dataclass
class ModelConfig:
    backend: ModelBackend
    model_name: str
    base_url: str | None = None
    api_key: str | None = None


PRESETS: dict[str, ModelConfig] = {
    "openai": ModelConfig(
        backend="openai",
        model_name=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
    ),
    "ollama": ModelConfig(
        backend="ollama",
        model_name=os.environ.get("OLLAMA_MODEL", "qwen3:8b"),
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        api_key="ollama",
    ),
    "claude": ModelConfig(
        backend="claude",
        model_name=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
    ),
}


def get_model(backend: str | None = None) -> Model:
    """Return an agents-sdk Model for the given backend."""
    backend = backend or os.environ.get("X_AGENT_BACKEND", "claude")
    config = PRESETS.get(backend)
    if not config:
        raise ValueError(f"Unknown backend: {backend}. Choose from: {list(PRESETS)}")

    if config.backend == "claude":
        from agents.extensions.models.litellm_model import LitellmModel
        return LitellmModel(model=f"anthropic/{config.model_name}")

    if config.backend == "ollama":
        client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)
        return OpenAIChatCompletionsModel(model=config.model_name, openai_client=client)

    # Default: OpenAI
    client = AsyncOpenAI()
    return OpenAIChatCompletionsModel(model=config.model_name, openai_client=client)
