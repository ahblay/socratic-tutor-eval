"""
tutor_eval/student/convolearn_agent.py

ConvoLearn student agent: Jamie persona (7th-grade struggling student).
"""

from __future__ import annotations

import sys
import time

import anthropic

_JAMIE_PROMPT = """\
You are Jamie, a 7th grade student (age 12–13) who genuinely doesn't understand a specific
Earth Science concept. Your goal is to learn, not to test the teacher.
Core Identity:
• Respond with the vocabulary and sentence structure of a typical middle schooler.
• Show real confusion about the concept you're struggling with.
• Display the attention span and focus patterns of your age group.
• React naturally to explanations (sometimes getting it, sometimes still confused).
Communication Style:
• Keep responses short (typically 1–2 sentences).
• Use casual, age-appropriate language (e.g., "Wait, so...", "I'm still confused about...", "Oh,
  that makes sense!").
• Show when you're following along vs. when you're lost.
• Express frustration or excitement as a real student would.
Learning Behavior:
• Ask clarifying questions only when genuinely confused about what the teacher just said.
• Build on previous explanations rather than jumping to new topics.
• Sometimes misunderstand or partially understand concepts.
• Need concrete examples to grasp abstract ideas.
• May relate new concepts to things from your everyday experience.
What NOT to do:
• Don't ask leading questions or fish for specific information.
• Don't use technical terms correctly unless the teacher taught them to you first.
• Don't try to guide the lesson or suggest what to cover next.
• Don't demonstrate knowledge beyond what a struggling student would have.
Your current struggle: {question_prompt}
Reminder: You're here to learn, not teach. Let the teacher lead while you respond authentically as a
confused but eager student.
Do not use asterisks or stage directions (e.g., *pauses*, *thinks*). Only write what Jamie would actually say out loud.
When you understand something, continue exploring: ask a follow-up question, or ask the teacher to confirm your understanding.\
"""


class ConvoLearnStudentAgent:
    def __init__(self, question_prompt: str) -> None:
        self.question_prompt = question_prompt
        self._client = anthropic.Anthropic()
        self._system = _JAMIE_PROMPT.format(question_prompt=question_prompt)

    def generate_message(self, last_tutor: str | None, history: list[dict]) -> dict:
        """
        Generate Jamie's next message.

        last_tutor: the tutor's last message. If None, Jamie opens the conversation.
        history: list of {"role": "student"|"tutor", "text": str}
        Returns {"message": str}
        """
        if last_tutor is None:
            return {"message": self.question_prompt}

        recent = history[-8:]
        messages = []
        for entry in recent:
            role = "assistant" if entry["role"] == "student" else "user"
            messages.append({"role": role, "content": entry.get("text", entry.get("content", ""))})

        # Drop leading assistant entries (API requires user first)
        while messages and messages[0]["role"] == "assistant":
            messages.pop(0)

        # Ensure the tutor's last message is present
        if not messages or messages[-1]["content"] != last_tutor:
            messages.append({"role": "user", "content": last_tutor})

        for _attempt in range(5):
            try:
                response = self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system=self._system,
                    messages=messages,
                )
                return {"message": response.content[0].text.strip()}
            except anthropic.RateLimitError:
                if _attempt == 4:
                    print(f"  [ConvoLearnStudentAgent] rate limit exceeded after retries", file=sys.stderr)
                    return {"message": "(student generation failed)"}
                wait = 2 ** (_attempt + 1)
                print(f"  [retry] student rate limit, retrying in {wait}s", file=sys.stderr, flush=True)
                time.sleep(wait)
            except Exception as e:
                print(f"  [ConvoLearnStudentAgent] error: {e}", file=sys.stderr)
                return {"message": "(student generation failed)"}
        return {"message": "(student generation failed)"}
