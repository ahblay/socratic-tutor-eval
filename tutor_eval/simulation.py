"""
tutor_eval/simulation.py

Generic simulation loop: tutor <-> student agent with BKT evaluation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tutor_eval.tutors.base import AbstractTutor
from tutor_eval.student.agent import StudentAgent
from tutor_eval.evaluation.bkt import BKTEvaluator


def run_simulation(
    tutor: AbstractTutor,
    profile: dict,
    kg: dict,
    topic: str,
    turns: int,
    output_file: str | None = None,
    verbose: bool = True,
) -> list[dict]:
    """
    Run a simulation between the given tutor and a student defined by profile/kg.

    Turn 0: student sends opening message "Hi, I'm trying to understand {topic}."
    Turns 1..N: student responds to tutor, tutor responds to student,
                evaluator updates BKT.

    Returns the full log as a list of dicts (one per turn, starting from turn 1).
    """
    student   = StudentAgent(profile, kg)
    evaluator = BKTEvaluator(profile, kg)
    target_kcs = profile.get("target_kcs", [])

    history: list[dict] = []
    log: list[dict]     = []

    # ── Turn 0: opening ──
    opening = f"Hi, I'm trying to understand {topic}."
    history.append({"role": "student", "text": opening})
    if verbose:
        print(f"STUDENT (opening): {opening}")

    opening_response = tutor.respond(opening, history)
    history.append({"role": "tutor", "text": opening_response})
    if verbose:
        print(f"TUTOR: {opening_response[:200]}...\n")

    # ── Turns 1..N ──
    for turn_num in range(1, turns + 1):
        if verbose:
            print(f"\u2500\u2500\u2500 Turn {turn_num}/{turns} " + "\u2500" * 40)

        last_tutor = history[-1]["text"]

        # Generate student response
        student_result  = student.generate_message(last_tutor, history)
        student_msg     = student_result["message"]
        self_assessment = student_result.get("self_assessment", {})
        history.append({"role": "student", "text": student_msg})

        if verbose:
            preview = student_msg[:300]
            suffix  = "..." if len(student_msg) > 300 else ""
            print(f"  STUDENT: {preview}{suffix}")

        # Get tutor response
        tutor_response = tutor.respond(student_msg, history)
        history.append({"role": "tutor", "text": tutor_response})

        if verbose:
            preview = tutor_response[:300]
            suffix  = "..." if len(tutor_response) > 300 else ""
            print(f"  TUTOR:   {preview}{suffix}")

        # Evaluate turn
        evaluator_snapshot = evaluator.evaluate_turn(student_msg)
        frontier = evaluator_snapshot.get("knowledge_frontier", [])

        if verbose:
            print(f"  Frontier: {frontier}")
            for kc in target_kcs:
                p = evaluator_snapshot["updated_bkt"].get(kc, 0)
                print(f"  {kc}: {p:.3f}")

        # Build log entry
        entry: dict = {
            "turn":             turn_num,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "student_message":  student_msg,
            "self_assessment":  self_assessment,
            "tutor_response":   tutor_response,
            "evaluator_snapshot": evaluator_snapshot,
        }

        state = tutor.session_state()
        if state:
            entry["session_snapshot"] = state

        log.append(entry)

        # Write log incrementally (JSONL)
        if output_file:
            with open(output_file, "w") as f:
                for e in log:
                    f.write(json.dumps(e) + "\n")

    # ── Summary ──
    if verbose and log:
        print("\n" + "\u2550" * 60)
        print("SIMULATION COMPLETE")
        print("\u2550" * 60)
        last_snap = log[-1]["evaluator_snapshot"]
        bkt       = last_snap.get("updated_bkt", {})
        for kc in target_kcs:
            p   = bkt.get(kc, 0)
            bar = "\u2588" * int(p * 20) + "\u2591" * (20 - int(p * 20))
            print(f"  {kc:50s} {bar} {p:.3f}")
        print(f"  Final frontier: {last_snap.get('knowledge_frontier', [])}")

    return log
