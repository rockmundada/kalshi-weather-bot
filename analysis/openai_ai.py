"""
OpenAI analysis using the Responses API.
Synthesizes all weather data sources into trading recommendations.
"""
import json
import logging

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from config import (
    OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MAX_OUTPUT_TOKENS,
    OPENAI_TIMEOUT_SECONDS, OPENAI_MAX_RETRIES,
)
from analysis.claude_ai import build_analysis_prompt, build_global_prompt

log = logging.getLogger(__name__)

_OPENAI_DISABLED = False


def _extract_first_json(text: str) -> dict | None:
    """Extract the first valid JSON object from arbitrary text."""
    if not text:
        return None

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    in_str = False
    escape = False
    depth = 0
    start = None

    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            continue

        if ch == "\"":
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = None
                        continue
    return None


def _disable_openai(reason: str) -> None:
    global _OPENAI_DISABLED
    if not _OPENAI_DISABLED:
        _OPENAI_DISABLED = True
        log.warning(f"Disabling OpenAI analysis for this run: {reason}")


def analyze_with_openai(city_key: str,
                        weather_data: dict,
                        market_data: dict,
                        prompt_mode: str = "full",
                        edges: list[dict] | None = None,
                        max_edges: int = 6) -> dict | None:
    """
    Send data to OpenAI for analysis.
    Returns parsed JSON recommendations or None on failure.
    """
    if _OPENAI_DISABLED:
        return None

    if not OPENAI_API_KEY:
        _disable_openai("No OPENAI_API_KEY set")
        return None

    if OpenAI is None:
        _disable_openai("openai package not installed (pip install openai)")
        return None

    prompt = build_analysis_prompt(
        city_key,
        weather_data,
        market_data,
        prompt_mode=prompt_mode,
        edges=edges,
        max_edges=max_edges,
    )

    try:
        try:
            client = OpenAI(
                api_key=OPENAI_API_KEY,
                timeout=OPENAI_TIMEOUT_SECONDS,
                max_retries=OPENAI_MAX_RETRIES,
            )
        except TypeError:
            client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
        )

        result_text = getattr(response, "output_text", "") or ""

        parsed = _extract_first_json(result_text)
        if parsed is not None:
            parsed["_provider"] = "openai"
            parsed["_raw_response"] = result_text[:1000]
            return parsed
        log.error(f"No JSON found in OpenAI response: {result_text[:500]}")
        return None

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse OpenAI JSON: {e}")
        return None
    except Exception as e:
        log.error(f"OpenAI API error: {e}")
        _disable_openai(f"OpenAI API error: {e}")
        return None


def analyze_with_openai_global(analysis_bundle: list[dict],
                               prompt_mode: str = "full",
                               max_edges: int = 6) -> dict | None:
    """
    Send all-city data to OpenAI in a single request.
    Returns parsed JSON recommendations or None on failure.
    """
    if _OPENAI_DISABLED:
        return None

    if not OPENAI_API_KEY:
        _disable_openai("No OPENAI_API_KEY set")
        return None

    if OpenAI is None:
        _disable_openai("openai package not installed (pip install openai)")
        return None

    prompt = build_global_prompt(analysis_bundle, prompt_mode=prompt_mode, max_edges=max_edges)

    try:
        try:
            client = OpenAI(
                api_key=OPENAI_API_KEY,
                timeout=OPENAI_TIMEOUT_SECONDS,
                max_retries=OPENAI_MAX_RETRIES,
            )
        except TypeError:
            client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
        )

        result_text = getattr(response, "output_text", "") or ""

        parsed = _extract_first_json(result_text)
        if parsed is not None:
            parsed["_provider"] = "openai"
            parsed["_raw_response"] = result_text[:1000]
            return parsed
        log.error(f"No JSON found in OpenAI response: {result_text[:500]}")
        return None

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse OpenAI JSON: {e}")
        return None
    except Exception as e:
        log.error(f"OpenAI API error: {e}")
        _disable_openai(f"OpenAI API error: {e}")
        return None
