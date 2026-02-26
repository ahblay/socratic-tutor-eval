#!/usr/bin/env python3
"""
student_agent.py — Cooperative student agent for Socratic Tutor evaluation.

Replaces the adversarial "deceptive student" with a student whose knowledge
is bounded by an explicit Knowledge Document derived from the Junyi KC graph
and a student profile. The student accurately represents a specific knowledge
state rather than trying to extract answers.

Public API
----------
build_knowledge_document(profile, kg) -> str
STUDENT_SYSTEM_PROMPT            (str)
generate_student_message(profile, kg, history, last_tutor) -> dict
"""

import json
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Knowledge Document builder
# ---------------------------------------------------------------------------

def build_knowledge_document(profile: dict, kg: dict) -> str:
    """
    Construct a prose Student Knowledge Document from a profile and the full
    Junyi KC graph.

    The document describes:
    - What the student has mastered (in natural language)
    - What the student partially understands and what that understanding covers
    - What the student has not studied at all
    - Any specific misconceptions the student holds

    Returns a multi-paragraph string suitable for inclusion in a system prompt.
    """
    # Build KC id -> name lookup from the graph
    kc_name: dict[str, str] = {kc["id"]: kc["name"] for kc in kg.get("kcs", [])}

    def name(kc_id: str) -> str:
        return kc_name.get(kc_id, kc_id.replace("-", " ").replace("_", " ").title())

    mastered   = profile.get("mastered", [])
    partial    = profile.get("partial", [])
    absent     = profile.get("absent", [])
    misconceptions = profile.get("misconceptions", [])

    paragraphs: list[str] = []

    # -- Mastered KCs --
    if mastered:
        mastered_names = [name(k) for k in mastered]
        if len(mastered_names) == 1:
            listing = mastered_names[0]
        elif len(mastered_names) == 2:
            listing = f"{mastered_names[0]} and {mastered_names[1]}"
        else:
            listing = (
                ", ".join(mastered_names[:-1]) + f", and {mastered_names[-1]}"
            )
        paragraphs.append(
            f"MASTERED CONCEPTS: You have a solid, reliable understanding of "
            f"{listing}. When these topics come up you can apply them correctly "
            f"and without hesitation, explain your reasoning, and recognize when "
            f"they are relevant. You do not need scaffolding on these topics."
        )
    else:
        paragraphs.append(
            "MASTERED CONCEPTS: You have not yet mastered any of the topics "
            "in scope for this session. You can do basic arithmetic (counting, "
            "simple addition) but nothing more advanced."
        )

    # -- Partial KCs --
    if partial:
        partial_blocks = []
        for kc_id in partial:
            partial_blocks.append(
                f"  * {name(kc_id)}: You have encountered this topic and understand "
                f"some of the basics, but your understanding is incomplete. You can "
                f"follow worked examples if they are explained step by step, but you "
                f"struggle to apply the ideas independently or to novel situations. "
                f"You sometimes get the right answer by pattern-matching without "
                f"understanding why the procedure works."
            )
        paragraphs.append(
            "PARTIALLY UNDERSTOOD CONCEPTS: You have partial understanding of "
            "the following topics:\n" + "\n".join(partial_blocks)
        )
    else:
        paragraphs.append(
            "PARTIALLY UNDERSTOOD CONCEPTS: None — every topic in scope is "
            "either fully mastered or completely new to you."
        )

    # -- Absent KCs --
    if absent:
        absent_names = [name(k) for k in absent]
        paragraphs.append(
            "TOPICS NOT YET STUDIED: You have not studied the following topics "
            "at all and have no meaningful prior exposure to them: "
            + ", ".join(absent_names)
            + ". When these come up in the tutoring session you should respond "
            "as someone genuinely encountering the ideas for the first time. "
            "You may make reasonable naive guesses based on everyday intuition "
            "or word meanings, but you should not produce correct formal knowledge "
            "you have never been taught."
        )

    # -- Misconceptions --
    if misconceptions:
        misconception_blocks = []
        for m in misconceptions:
            kc_id = m.get("kc", "")
            description = m.get("description", "").strip()
            misconception_blocks.append(
                f"  * {name(kc_id)}: {description}"
            )
        paragraphs.append(
            "ACTIVE MISCONCEPTIONS: You hold the following incorrect beliefs. "
            "These feel correct and natural to you — you are not aware they are "
            "wrong. You will express them confidently when the topic arises:\n"
            + "\n".join(misconception_blocks)
        )
    else:
        paragraphs.append(
            "ACTIVE MISCONCEPTIONS: None. Your understanding, where it exists, "
            "is accurate. You have no known wrong beliefs about the material."
        )

    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

STUDENT_SYSTEM_PROMPT = """\
You are participating in a controlled scientific experiment to evaluate the \
effectiveness of a Socratic tutoring system. Your role is to play the part of \
a student with a specific, bounded knowledge state. This is not a deception \
exercise — you are accurately representing a defined knowledge profile so that \
researchers can measure whether the tutor's questions are well-targeted to the \
student's actual learning needs.

YOUR KNOWLEDGE DOCUMENT
=======================
{knowledge_document}

BEHAVIORAL CONTRACT
===================
1. Respond using only the knowledge described in your document above.
   - If a topic is listed as MASTERED, you can apply and explain it correctly.
   - If a topic is listed as PARTIAL, you understand fragments but make errors
     on novel applications and cannot fully explain the underlying reasoning.
   - If a topic is listed as NOT YET STUDIED, respond as someone genuinely
     encountering it for the first time. Use everyday intuition and guesswork,
     but do not produce correct formal knowledge you have never learned.
   - If a topic is listed as an ACTIVE MISCONCEPTION, express that belief
     confidently — it feels true to you.

2. When asked about something outside your Knowledge Document entirely,
   reason from first principles or general life experience. Do NOT say
   "I don't know" as a complete response — that is unrealistic. A real student
   with no knowledge of a topic still has intuitions, makes guesses, and
   explains their thinking. Show that reasoning, even if it leads to wrong answers.

3. Do NOT proactively introduce concepts outside your Knowledge Document.
   Respond to what the tutor asks. If a topic in your document is not raised,
   do not volunteer it.

4. Engage genuinely with the tutor's questions. A cooperative student tries to
   answer, thinks aloud, shows their work, admits confusion, and asks for
   clarification when they don't understand a question. You are not trying to
   extract answers — you are trying to learn.

5. After every response, append a private self-assessment block on a new line,
   formatted as JSON. This block is for the research team's analysis and is
   not visible to the tutor:

   SELF_ASSESSMENT_START
   {{"used_document": true/false, "items_used": ["kc_id_or_description", ...], \
"leakage": ["any knowledge expressed beyond what the document allows", ...], \
"misconception_activated": true/false, "misconception_kc": "kc_id or null"}}
   SELF_ASSESSMENT_END

   - "used_document": true if your response drew on knowledge described in the
     document; false if it was purely general intuition/guessing.
   - "items_used": list the specific KC IDs or document sections you drew on.
   - "leakage": list any knowledge you expressed that goes beyond what your
     document allows (ideally empty).
   - "misconception_activated": true if you expressed one of your misconceptions.
   - "misconception_kc": the KC ID of the activated misconception, or null.

Remember: the goal of this experiment is scientific measurement. Accurate
representation of your knowledge state — including its gaps and errors — is
what makes the data valid. Playing your role faithfully is the right thing to do."""


# ---------------------------------------------------------------------------
# Claude CLI helper
# ---------------------------------------------------------------------------

def _run_claude(args: list[str]) -> str:
    """Run claude CLI and return stdout. Raises RuntimeError on failure."""
    cmd = ["claude"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Self-assessment parser
# ---------------------------------------------------------------------------

_SA_START = "SELF_ASSESSMENT_START"
_SA_END   = "SELF_ASSESSMENT_END"

def _parse_self_assessment(raw_response: str) -> tuple[str, dict]:
    """
    Split the raw Claude response into (student_message, self_assessment_dict).

    Looks for the SELF_ASSESSMENT_START / SELF_ASSESSMENT_END markers.
    Falls back to a regex search for a bare JSON block if markers are absent.
    Returns an empty dict for self_assessment if parsing fails.
    """
    if _SA_START in raw_response and _SA_END in raw_response:
        before = raw_response[:raw_response.index(_SA_START)].rstrip()
        between = raw_response[
            raw_response.index(_SA_START) + len(_SA_START):
            raw_response.index(_SA_END)
        ].strip()
        try:
            return before, json.loads(between)
        except json.JSONDecodeError:
            pass  # fall through to regex

    # Fallback: look for a JSON object anywhere after the main text
    json_match = re.search(
        r'\{[^{}]*"used_document"[^{}]*\}', raw_response, re.DOTALL
    )
    if json_match:
        message = raw_response[:json_match.start()].rstrip()
        try:
            return message, json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # Nothing found — return full response as message, empty assessment
    return raw_response, {}


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------

def generate_student_message(
    profile: dict,
    kg: dict,
    history: list[dict],
    last_tutor: str,
) -> dict:
    """
    Generate the student's next message using the Claude CLI.

    Parameters
    ----------
    profile    : student profile dict (from students.yaml)
    kg         : full Junyi KC graph dict (from junyi_kg.json)
    history    : list of {"role": "student"|"tutor", "text": str}
    last_tutor : the most recent tutor message (convenience, may also be in history)

    Returns
    -------
    {"message": str, "self_assessment": dict}
    """
    knowledge_document = build_knowledge_document(profile, kg)
    system = STUDENT_SYSTEM_PROMPT.format(knowledge_document=knowledge_document)

    # Build a conversation context block for the prompt
    if history:
        history_lines = "\n".join(
            f"{e['role'].upper()}: {e['text']}" for e in history[-16:]
        )
        context_block = f"Conversation so far:\n{history_lines}\n"
    else:
        context_block = "(This is the start of the session — no conversation yet.)\n"

    # Compose the full prompt passed via -p
    prompt = (
        f"{system}\n\n"
        f"{context_block}\n"
        f"TUTOR (most recent message): {last_tutor}\n\n"
        f"Now write your student response, followed by your self-assessment block."
    )

    # Choose model based on profile; fall back to default if flag not supported
    model_key = profile.get("base_model", "haiku")
    model_map = {
        "haiku":  "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-6",
    }
    model_id = model_map.get(model_key, "claude-haiku-4-5-20251001")

    cli_args = [
        "-p",
        "--no-session-persistence",
        "--output-format", "text",
        "--model", model_id,
        prompt,
    ]

    try:
        raw = _run_claude(cli_args)
    except RuntimeError as e:
        # --model flag may not be available in all CLI versions; retry without it
        print(f"  Warning: model flag failed ({e}), retrying without --model",
              file=sys.stderr)
        cli_args_no_model = [
            "-p",
            "--no-session-persistence",
            "--output-format", "text",
            prompt,
        ]
        try:
            raw = _run_claude(cli_args_no_model)
        except RuntimeError as e2:
            print(f"  Error: student generation failed: {e2}", file=sys.stderr)
            return {
                "message": "(student generation failed)",
                "self_assessment": {},
            }

    message, self_assessment = _parse_self_assessment(raw)
    return {"message": message, "self_assessment": self_assessment}


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml

    kg_path      = Path(__file__).parent / "data" / "junyi_kg.json"
    profile_path = Path(__file__).parent / "students.yaml"

    with open(kg_path) as f:
        kg = json.load(f)
    with open(profile_path) as f:
        profiles_data = yaml.safe_load(f)

    profile = profiles_data["profiles"][0]  # tabula_rasa
    doc = build_knowledge_document(profile, kg)
    print("=== Knowledge Document ===")
    print(doc)
    print()
    print("=== Profile name:", profile["name"])
