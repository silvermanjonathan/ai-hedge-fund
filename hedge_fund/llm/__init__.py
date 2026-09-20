"""v2 LLM layer — provider protocol, provider clients, prompt cache."""

from hedge_fund.llm.anthropic_client import AnthropicLLM
from hedge_fund.llm.cache import prompt_key, PromptCache
from hedge_fund.llm.client import (
    ChatLLM,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    extract_json,
    LLMClient,
    LLMParseError,
    LLMRefusal,
    make_llm,
)
from hedge_fund.llm.registry import (
    env_var_for,
    is_supported,
    load_api_models,
    PROVIDER_ENV_VARS,
    provider_for,
    SUPPORTED_PROVIDERS,
)
from hedge_fund.llm.watch import ThesisStream

__all__ = [
    "AnthropicLLM",
    "ChatLLM",
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "LLMClient",
    "LLMParseError",
    "LLMRefusal",
    "PROVIDER_ENV_VARS",
    "PromptCache",
    "SUPPORTED_PROVIDERS",
    "ThesisStream",
    "env_var_for",
    "extract_json",
    "is_supported",
    "load_api_models",
    "make_llm",
    "prompt_key",
    "provider_for",
]
