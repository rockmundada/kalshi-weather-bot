"""LLM fallback orchestration: Claude first, OpenAI as backup."""
import logging

from analysis.claude_ai import analyze_with_claude, analyze_with_claude_global
from analysis.openai_ai import analyze_with_openai, analyze_with_openai_global

log = logging.getLogger(__name__)

_FALLBACK_LOGGED = False


def analyze_with_llm(city_key: str,
                     weather_data: dict,
                     market_data: dict,
                     prompt_mode: str = "full",
                     edges: list[dict] | None = None,
                     max_edges: int = 6) -> dict | None:
    """
    Try Claude first; if unavailable or fails, fall back to OpenAI.
    Returns parsed JSON recommendations or None on failure.
    """
    global _FALLBACK_LOGGED

    import time
    edge_count = len(edges) if edges else 0
    log.info(f"LLM prompt_mode={prompt_mode} edges_for_llm={edge_count}")
    t0 = time.time()
    claude_result = analyze_with_claude(
        city_key,
        weather_data,
        market_data,
        prompt_mode=prompt_mode,
        edges=edges,
        max_edges=max_edges,
    )
    log.info(f"Claude call duration: {time.time() - t0:.1f}s")
    if claude_result:
        if "_provider" not in claude_result:
            claude_result["_provider"] = "claude"
        return claude_result

    if not _FALLBACK_LOGGED:
        log.info("Claude unavailable or failed; trying OpenAI fallback")
        _FALLBACK_LOGGED = True

    t1 = time.time()
    openai_result = analyze_with_openai(
        city_key,
        weather_data,
        market_data,
        prompt_mode=prompt_mode,
        edges=edges,
        max_edges=max_edges,
    )
    log.info(f"OpenAI call duration: {time.time() - t1:.1f}s")
    if openai_result:
        if "_provider" not in openai_result:
            openai_result["_provider"] = "openai"
        return openai_result

    return None


def analyze_with_llm_global(analysis_bundle: list[dict],
                            prompt_mode: str = "full",
                            max_edges: int = 6) -> dict | None:
    """Run a single LLM call across all cities."""
    import time
    log.info(f"LLM global prompt_mode={prompt_mode} cities={len(analysis_bundle)}")
    t0 = time.time()
    claude_result = analyze_with_claude_global(
        analysis_bundle,
        prompt_mode=prompt_mode,
        max_edges=max_edges,
    )
    log.info(f"Claude global call duration: {time.time() - t0:.1f}s")
    if claude_result:
        if "_provider" not in claude_result:
            claude_result["_provider"] = "claude"
        return claude_result

    if not _FALLBACK_LOGGED:
        log.info("Claude global unavailable or failed; trying OpenAI fallback")
    t1 = time.time()
    openai_result = analyze_with_openai_global(
        analysis_bundle,
        prompt_mode=prompt_mode,
        max_edges=max_edges,
    )
    log.info(f"OpenAI global call duration: {time.time() - t1:.1f}s")
    if openai_result:
        if "_provider" not in openai_result:
            openai_result["_provider"] = "openai"
        return openai_result
    return None
