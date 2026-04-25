"""
convolearn/import_to_webapp.py

Import sim_transcripts.json + sim_results.json into the webapp via POST /api/import/sessions.
Sessions already imported (409 Conflict) are skipped automatically.

Usage:
    python -m convolearn.import_to_webapp \\
        --results convolearn/results/sim/sim_results.json \\
        --transcripts convolearn/results/sim/sim_transcripts.json \\
        --token <superuser-jwt-token> \\
        --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Import simulation sessions into the webapp")
    parser.add_argument("--results", default="convolearn/results/sim/sim_results.json")
    parser.add_argument("--transcripts", default="convolearn/results/sim/sim_transcripts.json")
    parser.add_argument("--token", required=True, help="Superuser JWT token")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    with open(args.results) as f:
        results_by_id = {r["session_id"]: r for r in json.load(f)}
    with open(args.transcripts) as f:
        transcripts_by_id = {t["session_id"]: t for t in json.load(f)}

    headers = {"Authorization": f"Bearer {args.token}"}
    imported = skipped = failed = 0

    for session_id, transcript in transcripts_by_id.items():
        result = results_by_id.get(session_id)
        if result is None:
            print(f"  [warn] no result record for {session_id} — skipping", file=sys.stderr)
            continue

        full_result = transcript.get("full_result") or result
        clean_transcript = {k: v for k, v in transcript.items() if k != "full_result"}
        payload = {"transcript": clean_transcript, "result": full_result}
        try:
            resp = httpx.post(
                f"{args.base_url}/api/import/sessions",
                json=payload,
                headers=headers,
                timeout=30,
            )
        except Exception as e:
            print(f"  [error] {session_id}: request failed — {e}", file=sys.stderr)
            failed += 1
            continue

        if resp.status_code == 200:
            imported += 1
            print(f"  [imported] {session_id}")
        elif resp.status_code == 409:
            skipped += 1
            print(f"  [skip] {session_id} (already exists)")
        else:
            failed += 1
            print(f"  [error] {session_id}: HTTP {resp.status_code} — {resp.text[:120]}", file=sys.stderr)

    print(f"\nDone: {imported} imported, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
