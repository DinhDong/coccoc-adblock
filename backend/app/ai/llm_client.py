# Sends the assembled prompt to the OpenAI API and returns the raw text response:
# select model (default: gpt-4o-mini, fallback: gpt-4o)
# send chat completion request with system + user messages
# handle retries on rate-limit or timeout errors
# return raw response text to rule_parser.py
#
# Input:  prompt string + system message from prompt_builder.py
# Output: raw LLM response text

import logging
import os
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Model IDs — override via OPENAI_DEFAULT_MODEL / OPENAI_FALLBACK_MODEL in .env
DEFAULT_MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4o")

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
) -> str:
    """
    Send a prompt to the OpenAI chat completions API and return the response text.

    Args:
        prompt:         The user message / rule-generation request.
        system_message: Optional system role instruction.
        model:          OpenAI model ID to use.
        max_tokens:     Cap on response length.
        temperature:    Lower = more deterministic output (0.2 works well for structured rules).

    Returns:
        Raw text content of the model's first choice message.

    Raises:
        RuntimeError: If the API call fails after all retries.
    """
    client = _get_client()

    messages = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    messages.append({"role": "user", "content": prompt})

    last_error: Exception = Exception("No attempts made")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"LLM call attempt {attempt}/{MAX_RETRIES} (model={model})")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = (response.choices[0].message.content or "").strip()
            logger.debug(f"LLM response received: {len(text)} chars")
            return text

        except RateLimitError as exc:
            logger.warning(f"Rate limit (attempt {attempt}/{MAX_RETRIES}): {exc}")
            last_error = exc
            time.sleep(RETRY_BASE_DELAY * attempt)

        except (APITimeoutError, APIConnectionError) as exc:
            logger.warning(f"API connection error (attempt {attempt}/{MAX_RETRIES}): {exc}")
            last_error = exc
            time.sleep(RETRY_BASE_DELAY)

        except APIStatusError as exc:
            # 4xx errors won't be fixed by retrying
            logger.error(f"OpenAI API error {exc.status_code}: {exc.message}")
            raise RuntimeError(f"OpenAI API error {exc.status_code}: {exc.message}") from exc

    raise RuntimeError(
        f"LLM call failed after {MAX_RETRIES} attempts: {last_error}"
    ) from last_error


def call_llm_with_fallback(prompt: str, system_message: str = "") -> str:
    """
    Try DEFAULT_MODEL first; if it fails or returns empty, retry with FALLBACK_MODEL.

    Use this for complex pages where the cheaper model struggles (dynamic ad scripts,
    unclear DOM patterns).
    """
    try:
        result = call_llm(prompt, system_message=system_message, model=DEFAULT_MODEL)
        if result:
            return result
        logger.warning("Default model returned empty response — trying fallback model")
    except RuntimeError as exc:
        logger.warning(f"Default model failed ({exc}) — trying fallback model")

    return call_llm(prompt, system_message=system_message, model=FALLBACK_MODEL)
