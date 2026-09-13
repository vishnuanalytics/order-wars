"""Adapts `agents.llm.build_llm`'s Groq -> OpenRouter -> Claude fallback
chain to DeepEval's model interface, so eval metrics use the same
cost-ordered chain the game itself does, instead of DeepEval's OpenAI
default (which would need a separate, unused API key configured).
"""

from typing import Any

from deepeval.models.base_model import DeepEvalBaseLLM
from pydantic import BaseModel

from agents.llm import build_llm

DEFAULT_MAX_TOKENS = 600  # see agents/graph.py's note: Groq's hidden
# reasoning tokens truncate small structured-output budgets before the
# visible answer — confirmed live in Phase 2, same risk applies here.


class DeepEvalLLM(DeepEvalBaseLLM):
    """`generate`'s optional `schema` param is what lets DeepEval's default
    `generate_with_schema` (see DeepEvalBaseLLM) get structured output
    through here — it calls `generate(prompt, schema=schema)` and falls
    back to a plain call only if that raises `TypeError`, which it won't
    since this signature accepts `schema` directly.
    """

    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.max_tokens = max_tokens
        super().__init__(model="order-wars-fallback-chain")

    def load_model(self) -> "DeepEvalLLM":
        return self  # build_llm() is called fresh per generate(), not cached

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        # method="json_mode": see agents/llm.py's build_llm docstring — GEval's
        # prompts collide with tool-calling-based structured output on Groq.
        llm = build_llm(max_tokens=self.max_tokens, schema=schema, structured_output_method="json_mode")
        response = llm.invoke(prompt)
        if schema is not None:
            return response  # already a parsed `schema` instance
        return response.content if isinstance(response.content, str) else str(response.content)

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        # No async LLM path anywhere in this project yet (agents/llm.py's
        # chain is sync-only) — fine at this project's scale, and GEval's
        # default async_mode just runs this inside asyncio.run_until_complete.
        return self.generate(prompt, schema=schema)

    def get_model_name(self) -> str:
        return "order-wars-fallback-chain"
