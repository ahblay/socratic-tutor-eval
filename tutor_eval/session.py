"""
tutor_eval/session.py

Transcript-collection session runner.

Produces a raw-transcript-v1 dict that can be passed directly to
ingest.py / prepare_analysis_input() for post-hoc evaluation.

Contrast with simulation.py (run_simulation):
  - simulation.py runs BKT live and saves to JSONL (old format)
  - session.py collects transcripts only; evaluation is post-hoc

Termination
-----------
A session ends when any of the following occur:
  1. max_turns is reached (ended_by="max_turns")
  2. Either party's response contains [SESSION_COMPLETE] case-insensitively
     (ended_by="tutor" or "student")
  3. KeyboardInterrupt — partial transcript is saved (ended_by="interrupted")
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tutor_eval.tutors.base import AbstractTutor
from tutor_eval.student.agent import StudentAgent

_COMPLETE_SENTINEL = "[session_complete]"


def _is_complete_signal(text: str) -> bool:
    return _COMPLETE_SENTINEL in text.lower()


def run_session(
    tutor: AbstractTutor,
    domain_map: dict,
    topic: str,
    *,
    student_type: str = "llm",
    student_agent: StudentAgent | None = None,
    profile: dict | None = None,
    kg: dict | None = None,
    bkt_initial_states: dict | None = None,
    opening_message: str | None = None,
    max_turns: int = 20,
    min_turns: int = 8,
    session_id: str | None = None,
    source: str | None = None,
    verbose: bool = True,
    output_file: str | None = None,
) -> dict:
    """
    Run a tutoring session and return a raw-transcript-v1 dict.

    Parameters
    ----------
    tutor : AbstractTutor
        Any tutor implementing AbstractTutor.respond().
    domain_map : dict
        Normalized domain map (core_concepts format).
    topic : str
        Human-readable topic name for the opening message and transcript metadata.
    student_type : str
        "llm" — use student_agent (StudentAgent required).
        "human" — read responses from stdin; student_agent is ignored.
    student_agent : StudentAgent | None
        Required when student_type="llm".
    profile : dict | None
        Student knowledge profile. Used to derive bkt_initial_states if
        bkt_initial_states is not provided explicitly.
    kg : dict | None
        KG dict ({kcs, edges}) matching profile's KC IDs. Required with profile.
    bkt_initial_states : dict | None
        Explicit BKT initial states. If provided, overrides profile-derived states.
    opening_message : str | None
        First student message. Default: "Hi, I'm trying to understand {topic}."
    max_turns : int
        Maximum number of tutor turns before the session ends.
    min_turns : int
        Sessions with fewer tutor turns than this are flagged is_valid=False.
    session_id : str | None
        Session identifier. Generated as UUID4 if not provided.
    source : str | None
        Tutor system identifier (e.g. "gpt-4o"). Stored in transcript metadata.
    verbose : bool
        Print each turn to stdout.
    output_file : str | None
        Path to write the completed transcript JSON. Not written if None.

    Returns
    -------
    dict
        A raw-transcript-v1 dict.
    """
    if student_type == "llm" and student_agent is None:
        raise ValueError("student_agent is required when student_type='llm'")

    session_id = session_id or str(uuid.uuid4())
    opening = opening_message or f"Hi, I'm trying to understand {topic}."

    turns: list[dict] = []
    history: list[dict] = []
    ended_by: str = "max_turns"

    if verbose:
        if student_type == "human":
            print(
                f"\n{'='*60}\n"
                f"Session started  |  max turns: {max_turns}\n"
                f"Type [SESSION_COMPLETE] to end the session early.\n"
                f"Press Ctrl+C to interrupt and save a partial transcript.\n"
                f"{'='*60}\n"
            )

    # --- Opening student message ---
    history.append({"role": "student", "text": opening})
    turns.append({"role": "student", "content": opening})
    if verbose:
        print(f"STUDENT: {opening}")

    try:
        for turn_num in range(1, max_turns + 1):

            # --- Tutor turn ---
            last_student = history[-1]["text"]
            tutor_response = tutor.respond(last_student, history)
            history.append({"role": "tutor", "text": tutor_response})
            turns.append({"role": "tutor", "content": tutor_response})

            if verbose:
                print(f"\n[Turn {turn_num}/{max_turns}]")
                print(f"TUTOR:   {tutor_response}")

            if _is_complete_signal(tutor_response):
                ended_by = "tutor"
                if verbose:
                    print(f"\n[Session complete — tutor signal at turn {turn_num}]")
                break

            # --- Student turn ---
            if student_type == "human":
                if verbose:
                    print()
                try:
                    student_msg = input("You: ").strip()
                except EOFError:
                    student_msg = "[SESSION_COMPLETE]"
                if not student_msg:
                    student_msg = "(no response)"
            else:
                result = student_agent.generate_message(tutor_response, history)
                student_msg = result["message"]
                if verbose:
                    print(f"STUDENT: {student_msg}")

            history.append({"role": "student", "text": student_msg})
            turns.append({"role": "student", "content": student_msg})

            if _is_complete_signal(student_msg):
                ended_by = "student"
                if verbose:
                    print(f"\n[Session complete — student signal at turn {turn_num}]")
                break

    except KeyboardInterrupt:
        ended_by = "interrupted"
        if verbose:
            print("\n\n[Interrupted — saving partial transcript]", file=sys.stderr)

    # --- Resolve BKT initial states ---
    if bkt_initial_states:
        resolved_bkt = bkt_initial_states
    elif profile and kg:
        from tutor_eval.student.domain_profile import bkt_states_from_profile
        resolved_bkt = bkt_states_from_profile(profile, kg)
    else:
        resolved_bkt = {}

    # --- Build transcript ---
    total_tutor_turns = sum(1 for t in turns if t["role"] == "tutor")
    transcript: dict = {
        "_schema": "raw-transcript-v1",
        "session_id": session_id,
        "topic": topic,
        "source": source,
        "date": datetime.now(timezone.utc).date().isoformat(),
        "domain_map": domain_map,
        "bkt_initial_states": resolved_bkt,
        "turns": turns,
        "_metadata": {
            "total_tutor_turns": total_tutor_turns,
            "ended_by": ended_by,
            "is_valid": total_tutor_turns >= min_turns,
        },
    }

    if output_file:
        Path(output_file).write_text(json.dumps(transcript, indent=2))
        if verbose:
            print(
                f"\nTranscript saved to {output_file} "
                f"({total_tutor_turns} tutor turns, ended_by={ended_by})"
            )

    return transcript
