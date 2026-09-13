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
# meta-llama/llama-3.3-70b-instruct:free (the original default) was
# deprecated by OpenRouter — confirmed live, it now 404s and points at the
# paid slug instead. Replaced after testing several of OpenRouter's current
# free models directly against both this project's structured-output paths
# (agents/actions.py's FactionAction via default tool-calling, and
# eval/llm_wrapper.py's json_mode path) — this one handled both reliably
# across repeated calls; a couple of others either rate-limited immediately
# or weren't tested as thoroughly.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_llm(
    max_tokens: int = 64,
    schema: type[BaseModel] | None = None,
    structured_output_method: str | None = None,
):
    """Return a chat model that tries Groq, then OpenRouter, then Claude.

    Each provider is included only if its API key is set in the
    environment. Raises if none of the three keys are configured.

    `schema`, if given, binds `.with_structured_output(schema)` to each
    provider *before* they're combined into the fallback chain — binding
    afterward isn't possible, since `.with_fallbacks()` returns a
    `RunnableWithFallbacks`, which doesn't have `with_structured_output`
    (that's a `BaseChatModel`-only method). `.invoke()` on the result then
    returns a parsed `schema` instance directly instead of a chat message.

    `structured_output_method` overrides LangChain's default structured-
    output method (normally tool/function-calling) — left unset for
    `agents/graph.py`'s calls, which are verified working with the default.
    `eval/llm_wrapper.py` passes `"json_mode"`: DeepEval's GEval metric
    prompts end with an explicit natural-language "return this as JSON"
    instruction, and combining that with tool-calling confused Groq's
    openai/gpt-oss-120b into hallucinating a call to a tool literally named
    "json" that was never registered (confirmed live — a 400 from Groq,
    "attempted to call tool 'json' which was not in request.tools"; the
    identical prompt succeeded immediately once bound with
    `method="json_mode"` instead). Verified this override doesn't break the
    other providers either: OpenAI-compatible endpoints (OpenRouter) accept
    "json_mode" directly, and ChatAnthropic falls back to "json_schema" with
    a warning rather than erroring, since Anthropic has no native JSON-mode
    parameter.
    """
    providers = []

    def _with_schema(model):
        if schema is None:
            return model
        if structured_output_method is not None:
            return model.with_structured_output(schema, method=structured_output_method)
        return model.with_structured_output(schema)

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
