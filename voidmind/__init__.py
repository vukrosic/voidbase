"""Voidmind — the token-donation client (a write-client spoke, like Voidrunner).

Reads open research threads from voidbase, asks the donor's OWN LLM for candidate
structural experiments, and enqueues them as runnable jobs (POST /ideas +
/queue_items, bearer-token auth). Voidmind fills the queue with tokens; Voidrunner
drains it with compute.

Stdlib + voidconfig only, zero DB imports (enforced by a test). The LLM call lives
behind the proposer seam (voidmind/propose.py), so the core carries no vendor SDK
and the donor can swap in any model. See docs/VOIDMIND.md and docs/SPOKES.md.
"""
from voidmind.core import (  # noqa: F401
    DEFAULT_API,
    ApiError,
    build_context,
    enqueue,
    open_threads,
    post_idea,
    recent_runs,
    register,
    resolve_proposal,
    run_once,
    thread_goal,
)

__all__ = [
    "DEFAULT_API",
    "ApiError",
    "register",
    "open_threads",
    "thread_goal",
    "recent_runs",
    "build_context",
    "resolve_proposal",
    "post_idea",
    "enqueue",
    "run_once",
]
