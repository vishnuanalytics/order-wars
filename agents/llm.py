"""Cost-aware LLM provider with a fallback chain.

Order: Groq -> OpenRouter -> Anthropic Claude.

Groq and OpenRouter both offer usable free tiers, so they're tried first;
Claude is the paid last resort, used only if both free providers are
unavailable (no key configured) or fail at call time. Built on LangChain's
`Runnable.with_fallbacks`, so callers just get back something with the usual
`.invoke()` — the fallback logic is invisible to node code.
"""

import os

from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_llm(max_tokens: int = 64, schema: type[BaseModel] | None = None):
    """Return a chat model that tries Groq, then OpenRouter, then Claude.

    Each provider is included only if its API key is set in the
    environment. Raises if none of the three keys are configured.

    `schema`, if given, binds `.with_structured_output(schema)` to each
    provider *before* they're combined into the fallback chain — binding
    afterward isn't possible, since `.with_fallbacks()` returns a
    `RunnableWithFallbacks`, which doesn't have `with_structured_output`
    (that's a `BaseChatModel`-only method). `.invoke()` on the result then
    returns a parsed `schema` instance directly instead of a chat message.
    """
    providers = []

    def _with_schema(model):
        return model.with_structured_output(schema) if schema is not None else model

    if os.environ.get("GROQ_API_KEY"):
        providers.append(_with_schema(ChatGroq(model=GROQ_MODEL, max_tokens=max_tokens)))

    if os.environ.get("OPENROUTER_API_KEY"):
        providers.append(
            _with_schema(
                ChatOpenAI(
                    model=OPENROUTER_MODEL,
                    base_url=OPENROUTER_BASE_URL,
                    api_key=os.environ["OPENROUTER_API_KEY"],
                    max_tokens=max_tokens,
                )
            )
        )

    if os.environ.get("ANTHROPIC_API_KEY"):
        providers.append(_with_schema(ChatAnthropic(model=ANTHROPIC_MODEL, max_tokens=max_tokens)))

    if not providers:
        raise RuntimeError(
            "No LLM provider configured. Set at least one of GROQ_API_KEY, "
            "OPENROUTER_API_KEY, or ANTHROPIC_API_KEY in .env."
        )

    primary, *fallbacks = providers
    return primary.with_fallbacks(fallbacks) if fallbacks else primary
