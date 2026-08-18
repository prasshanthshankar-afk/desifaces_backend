from __future__ import annotations

from desifaces_shared.v3.director_graph import CreativeDirectorRuntime, build_creative_director_graph

from .compiler import CanonicalStoryCompiler
from .config import settings
from .llm import OpenAICreativeCritic, OpenAICreativePlanner
from .retrieval import HybridCreativeRetriever


def create_director_graph(business_pool, checkpointer):
    if not settings.DF_DIRECTOR_LLM_MODEL:
        return None
    runtime = CreativeDirectorRuntime(
        retriever=HybridCreativeRetriever(business_pool),
        planner=OpenAICreativePlanner(),
        critic=OpenAICreativeCritic(),
        compiler=CanonicalStoryCompiler(business_pool),
        require_human_review=settings.DF_DIRECTOR_REVIEW_REQUIRED,
        max_revisions=max(0, settings.DF_DIRECTOR_MAX_REVISIONS),
    )
    return build_creative_director_graph(runtime, checkpointer=checkpointer)
