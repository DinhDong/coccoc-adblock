# Sends the assembled prompt to the OpenAI API and returns the raw text response:
# select model (default: gpt-4o-mini, fallback: gpt-4o)
# send chat completion request with system + user messages
# handle retries on rate-limit or timeout errors
# return raw response text to rule_parser.py
#
# Input:  prompt string + system message from prompt_builder.py
# Output: raw LLM response text

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default model for rule generation. Falls back to stronger model on failure.
DEFAULT_MODEL = "gpt-4o-mini"
FALLBACK_MODEL = "gpt-4o"


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
        RuntimeError: If the API call fails after retries.
    """
    raise NotImplementedError


def call_llm_with_fallback(prompt: str, system_message: str = "") -> str:
    """
    Try DEFAULT_MODEL first; if it fails or returns empty, retry with FALLBACK_MODEL.

    Use for complex pages where the cheaper model struggles (dynamic ad scripts,
    unclear DOM patterns).
    """
    raise NotImplementedError
