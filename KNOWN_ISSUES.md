# Known Issues

---

## KI-001 — Article analysis timeout

**Area:** `webapp/api/articles.py`, `webapp/static/article.js`
**Severity:** Medium — recoverable by resubmitting the URL

### Description
Domain map generation makes two sequential LLM calls (Sonnet domain mapper + Haiku prerequisite fix pass). The frontend polls for completion and hard-rejects after 120 seconds. The backend has no timeout — it runs indefinitely regardless of what the frontend does.

### Failure modes
1. **Frontend times out while backend is still running.** Status stays `"pending"` in the DB. If the backend eventually succeeds, resubmitting the same URL will instantly return the completed result — the user has no way of knowing this.
2. **No backend timeout.** A slow Anthropic API response, rate limit, or large article can push total LLM time well past the 2-minute frontend window. The background task never terminates until the Anthropic SDK itself gives up.
3. **Silent failure.** `_compute_domain_map_bg` catches all exceptions and sets `domain_map_status = "failed"` with no logging. Root causes (timeout, malformed JSON, API error) are indistinguishable.
4. **Fix pass failure fails the whole map.** If `_fix_prerequisite_references` throws, the domain map is not saved at all, even though the primary domain mapper call succeeded. The fix pass is a quality improvement, not a correctness requirement — failure should fall back to the unmodified map.

### Suggested fixes
- Wrap the compute + fix calls in `asyncio.wait_for(timeout=180)` in `_compute_domain_map_bg`; set `domain_map_status = "failed"` and log the reason on expiry.
- Log all exceptions in `_compute_domain_map_bg` (`print` to stderr at minimum).
- Increase the frontend poll timeout to ~200 s (slightly above the backend timeout) and add a message advising the user to resubmit if it fails.
- In `_fix_prerequisite_references`, catch its own exceptions and return the original domain map on any failure rather than propagating.

---
