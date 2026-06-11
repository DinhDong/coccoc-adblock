# Sends the assembled prompt to the OpenAI API and returns the raw text response
# plus token usage metadata.
#
# Input:  prompt string + system message from prompt_builder.py
# Output: LLMResponse(text=..., usage=..., model=..., fallback_used=...)

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from dataclasses import dataclass
@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class LLMResponse:
    text: str
    usage: TokenUsage | None = None
    model: str = ""
    fallback_used: bool = False

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class LLMResponse:
    text: str
    usage: Optional[TokenUsage] = None
    model: str = ""
    fallback_used: bool = False


# Model IDs — override via OPENAI_DEFAULT_MODEL / OPENAI_FALLBACK_MODEL in .env
DEFAULT_MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.4-mini")
FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-5.5")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds; multiplied by attempt number on rate-limit


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to your .env file."
        )

    return OpenAI(api_key=api_key)


def call_llm(
    prompt: str,
    system_message: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> LLMResponse:
    """
    Send a prompt to the OpenAI chat completions API and return the response text
    plus token usage metadata.

    Args:
        prompt:         The user message / rule-generation request.
        system_message: Optional system role instruction.
        model:          OpenAI model ID to use.
        max_tokens:     Cap on response length.
        temperature:    Lower = more deterministic output.

    Returns:
        LLMResponse:
            - text
            - usage.prompt_tokens
            - usage.completion_tokens
            - usage.total_tokens
            - model
            - fallback_used

    Raises:
        RuntimeError: If the API call fails after all retries.
    """
    client = _get_client()

    # gpt-5 series requires content as an array of content objects.
    messages = []

    if system_message:
        messages.append(
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system_message,
                    }
                ],
            }
        )

    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                }
            ],
        }
    )

    last_error: Exception = Exception("No attempts made")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(
                "LLM call attempt %s/%s (model=%s)",
                attempt,
                MAX_RETRIES,
                model,
            )

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_completion_tokens=max_tokens,
                temperature=temperature,
            )

            text = (response.choices[0].message.content or "").strip()
            usage = None
            if response.usage:
                usage = TokenUsage(
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                )

                logger.info(
                    "LLM token usage | model=%s | prompt=%s | completion=%s | total=%s",
                    model,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                )

            logger.debug("LLM response received: %s chars", len(text))

            return LLMResponse(
                text=text,
                usage=usage,
                model=model,
                fallback_used=False,
            )

            return LLMResponse(
                text=text,
                usage=usage,
                model=model,
                fallback_used=False,
            )
        except RateLimitError as exc:
            logger.warning(
                "Rate limit (attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )
            last_error = exc
            time.sleep(RETRY_BASE_DELAY * attempt)

        except (APITimeoutError, APIConnectionError) as exc:
            logger.warning(
                "API connection error (attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )
            last_error = exc
            time.sleep(RETRY_BASE_DELAY)

        except APIStatusError as exc:
            # 4xx errors usually won't be fixed by retrying.
            logger.error("OpenAI API error %s: %s", exc.status_code, exc.message)
            raise RuntimeError(
                f"OpenAI API error {exc.status_code}: {exc.message}"
            ) from exc

    raise RuntimeError(
        f"LLM call failed after {MAX_RETRIES} attempts: {last_error}"
    ) from last_error


def call_llm_with_fallback(
    prompt: str,
    system_message: str = "",
) -> LLMResponse:
    """
    Try DEFAULT_MODEL first; if it fails or returns empty, retry with FALLBACK_MODEL.

    Returns:
        LLMResponse with fallback_used=True if fallback model was used.
    """
    try:
        result = call_llm(
            prompt,
            system_message=system_message,
            model=DEFAULT_MODEL,
        )

        if result.text:
            result.fallback_used = False
            return result

        logger.warning("Default model returned empty response — trying fallback model")

    except RuntimeError as exc:
        logger.warning(
            "Default model failed (%s) — trying fallback model",
            exc,
        )

    fallback_result = call_llm(
        prompt,
        system_message=system_message,
        model=FALLBACK_MODEL,
    )

    fallback_result.fallback_used = True

    if fallback_result.usage:
        logger.info(
            "Fallback LLM token usage | model=%s | prompt=%s | completion=%s | total=%s",
            fallback_result.model,
            fallback_result.usage.prompt_tokens,
            fallback_result.usage.completion_tokens,
            fallback_result.usage.total_tokens,
        )

    return fallback_result