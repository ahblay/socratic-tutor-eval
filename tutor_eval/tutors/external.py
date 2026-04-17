"""
tutor_eval/tutors/external.py

GenericAPITutor — wraps any OpenAI-compatible chat completion endpoint.

Covers: GPT-4o (OpenAI), open-source models via OpenRouter/vLLM/Ollama,
and Claude via LiteLLM. For direct Anthropic API use, see SocraticTutor.

System prompt rendering (caller's responsibility before passing in):
  Available substitution keys: {topic}, {domain_map_json}
  Use str.replace() or simulate.py's render_system_prompt() helper.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from tutor_eval.tutors.base import AbstractTutor

_COMPLETE_SENTINEL = "[session_complete]"


class GenericAPITutor(AbstractTutor):
    """
    AbstractTutor adapter for any OpenAI-compatible API.

    Parameters
    ----------
    model : str
        Model identifier, e.g. "gpt-4o", "meta-llama/Meta-Llama-3-70B-Instruct".
    system_prompt : str
        Fully rendered system prompt. Substitution (topic, domain_map_json) must
        be done by the caller before instantiation.
    base_url : str | None
        Override the API endpoint. None → OpenAI default.
        Examples: "https://openrouter.ai/api/v1", "http://localhost:11434/v1"
    api_key : str | None
        API key. Falls back to the OPENAI_API_KEY environment variable.
    max_tokens : int
        Maximum tokens in the response. Default 2048.
    temperature : float
        Sampling temperature. Default 1.0.
    extra_kwargs : dict | None
        Additional keyword arguments passed to chat.completions.create().
    """

    def __init__(
        self,
        model: str,
        system_prompt: str,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 1.0,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "GenericAPITutor requires the openai package: "
                "pip install 'openai>=1.0.0'"
            ) from exc

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "No API key provided and OPENAI_API_KEY is not set. "
                "Pass api_key= or set the environment variable."
            )

        self._client = OpenAI(api_key=resolved_key, base_url=base_url)
        self._model = model
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._extra_kwargs = extra_kwargs or {}

    def respond(self, student_message: str, history: list[dict]) -> str:
        """
        Generate the tutor's next message.

        history: list of {"role": "student"|"tutor", "text": str}
        Maps tutor → "assistant", student → "user" for the OpenAI format.
        """
        messages: list[dict] = [{"role": "system", "content": self._system_prompt}]

        for entry in history:
            role = "assistant" if entry["role"] == "tutor" else "user"
            messages.append({"role": role, "content": entry["text"]})

        # OpenAI requires the first non-system message to be from the user
        while len(messages) > 1 and messages[1]["role"] == "assistant":
            messages.pop(1)

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                **self._extra_kwargs,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            print(f"  [GenericAPITutor] API call failed: {exc}", file=sys.stderr)
            return "(tutor response unavailable)"

    def session_state(self) -> dict | None:
        return None
