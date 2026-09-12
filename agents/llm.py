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

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_llm(max_tokens: int = 64):
    """Return a chat model that tries Groq, then OpenRouter, then Claude.

    Each provider is included only if its API key is set in the
    environment. Raises if none of the three keys are configured.
    """
    providers = []

    if os.environ.get("GROQ_API_KEY"):
        providers.append(ChatGroq(model=GROQ_MODEL, max_tokens=max_tokens))

    if os.environ.get("OPENROUTER_API_KEY"):
        providers.append(
            ChatOpenAI(
                model=OPENROUTER_MODEL,
                base_url=OPENROUTER_BASE_URL,
                api_key=os.environ["OPENROUTER_API_KEY"],
                max_tokens=max_tokens,
            )
        )

    if os.environ.get("ANTHROPIC_API_KEY"):
        providers.append(ChatAnthropic(model=ANTHROPIC_MODEL, max_tokens=max_tokens))

    if not providers:
        raise RuntimeError(
            "No LLM provider configured. Set at least one of GROQ_API_KEY, "
            "OPENROUTER_API_KEY, or ANTHROPIC_API_KEY in .env."
        )

    primary, *fallbacks = providers
    return primary.with_fallbacks(fallbacks) if fallbacks else primary
