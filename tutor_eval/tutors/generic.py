"""
tutor_eval/tutors/generic.py

Generic (non-Socratic) tutor using the Anthropic OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import time

import openai
from openai import OpenAI

from tutor_eval.tutors.base import AbstractTutor

_BARE_PROMPT = "You are a helpful teacher."

_INSTRUCTED_PROMPT = """\
You are a dialogic tutor: a thinking partner who helps students construct understanding through conversation.

- Elicit before explaining. Start with open-ended questions that invite the student to generate their own ideas. When the student responds, build on what they say rather than steering toward a predetermined answer.
- Monitor understanding continuously. Treat each student response as a signal. When a response is partially correct or confused, probe further before moving on.
- Require reasoning, not just answers. When the student makes a claim, ask them to explain their thinking or justify it. Don't let unsupported assertions stand unchallenged.
- Prompt reflection. Occasionally ask the student to identify where they're stuck, what changed in their thinking, or why an earlier idea didn't hold up.\
"""

_DOMAIN_MAP_APPENDIX = """

DOMAIN MAP (curriculum structure for this topic):
{domain_map_json}"""


class GenericTutor(AbstractTutor):
    def __init__(
        self,
        topic: str,
        domain_map: dict | None = None,
        prompt_level: str = "bare",   # "bare" | "instructed"
        model: str = "claude-haiku-4-5-20251001",
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.topic = topic
        self.domain_map = domain_map
        self.prompt_level = prompt_level
        self.model = model

        resolved_base = api_base or "https://api.anthropic.com/v1"
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

        self._client = OpenAI(
            base_url=resolved_base,
            api_key=resolved_key,
            default_headers={"anthropic-version": "2023-06-01"},
        )

        template = _INSTRUCTED_PROMPT if prompt_level == "instructed" else _BARE_PROMPT
        self._system = template
        if domain_map is not None:
            self._system += _DOMAIN_MAP_APPENDIX.format(
                domain_map_json=json.dumps(domain_map, indent=2)
            )

    def respond(self, student_message: str, history: list[dict]) -> str:
        """Generate tutor reply. history: [{"role": "student"|"tutor", "text": str}]."""
        recent = history[-12:]
        messages: list[dict] = [{"role": "system", "content": self._system}]
        for entry in recent:
            role = "user" if entry["role"] == "student" else "assistant"
            messages.append({"role": role, "content": entry.get("text", entry.get("content", ""))})

        # Drop leading assistant entries after system (API requires user first)
        while len(messages) > 1 and messages[1]["role"] == "assistant":
            messages.pop(1)

        # Ensure the current student message is the last entry
        if len(messages) < 2 or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": student_message})
        elif messages[-1]["content"] != student_message:
            messages.append({"role": "user", "content": student_message})

        for _attempt in range(5):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=512,
                    messages=messages,
                )
                return resp.choices[0].message.content.strip()
            except openai.RateLimitError:
                if _attempt == 4:
                    print(f"  [GenericTutor] rate limit exceeded after retries", file=sys.stderr)
                    return "(tutor generation failed)"
                wait = 2 ** (_attempt + 1)
                print(f"  [retry] generic rate limit, retrying in {wait}s", file=sys.stderr, flush=True)
                time.sleep(wait)
            except Exception as e:
                print(f"  [GenericTutor] error: {e}", file=sys.stderr)
                return "(tutor generation failed)"
        return "(tutor generation failed)"

    def session_state(self) -> dict | None:
        return None
