"""QueryRewriter factory.

Unlike get_embedding_provider()/get_llm_provider(), this factory needs an
LLMProvider instance to wrap (see lmstudio.py's module docstring for why a
QueryRewriter isn't a separate configured backend) - so it takes one as a
plain parameter rather than reading its own settings block, and is wired
via a nested FastAPI Depends() at the route level, same as any other
dependency composition.
"""

from app.rag.llm.base import LLMProvider
from app.rag.query_rewrite.base import QueryRewriter
from app.rag.query_rewrite.lmstudio import LMStudioQueryRewriter


def get_query_rewriter(llm_provider: LLMProvider) -> QueryRewriter:
    return LMStudioQueryRewriter(llm_provider)
