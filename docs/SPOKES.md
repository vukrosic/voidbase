# Spokes — the integration map (how the platform is composed)

> Captured 2026-06-18. The single page that says what every spoke is, where it
> lives, and how it integrates with the spine. Per-spoke detail lives in
> `VOIDRUNNER.md`, `VOIDMIND.md`, `VOIDCREDIT.md`.

## The one rule

voidbase is built as **separable spokes around one seam: the voidbase HTTP API.**
Swap the Postgres/Neon spine and no spoke changes. Every spoke is exactly one of
two shapes — and which shape it is falls out of the **trust model**, not taste:

| | **Write client** | **Pure policy library** |
|---|---|---|
| Runs where | a donor's hardware | imported into the API / daemons |
| Talks to | the **HTTP API only**, bearer-token auth | nothing — pure functions, no I/O |
| Job | mutate state (push work / results) | derive / judge from rows it's given |
| Why this shape | can't hold DB creds on a stranger's box | read-and-derive needs no I/O at all |
| Lives in | own package, **extracts to its own repo** | a package **in voidbase** (API imports it) |

- Anything that runs on a machine we don't control **cannot hold DB creds** ⇒ it
  is an HTTP + token client.
- Anything that only **reads and derives** has no reason to do I/O ⇒ it is a pure
  library the trusted API edge calls.

## The four spokes

```
WRITE CLIENTS (donor hardware, HTTP+token)     PURE LIBS (imported, no I/O)
├─ Voidrunner  compute → runs        ✓ main    ├─ Voidcheck   is it real?    ✓ main
└─ Voidmind    tokens  → ideas/queue ✓ main    ├─ Voidcredit  who gets credit? ✓ main
        └──────────── voidbase API ◀── one seam ──────────┐ └─ voidconfig  config shape + dedup ✓ main
                            │  Postgres                    │
                       voidspark (UI reads API)            └─ (imported by API + re-exported by feeder)
```

| Spoke | Kind | Package | Status | Integrates via |
|---|---|---|---|---|
| **Voidrunner** | write client | `runner/` | ✓ main | `/register /claim /runs /release` (token) |
| **Voidcheck** | pure lib | `voidcheck/` | ✓ main | imported by API + `confirm_daemon` |
| **Voidmind** | write client | `voidmind/` | ✓ main | reads `/threads/public` `/runs`; `POST /ideas` `/queue_items` (token) |
| **Voidcredit** | pure lib | `voidcredit/` | ✓ main | imported by read endpoints `/leaderboard` `/contributor` `/lineage` |
| **voidconfig** | pure lib (shared) | `voidconfig/` | ✓ main | config-row shape + dedup `content_hash`; imported by API `POST /queue_items`, re-exported by `scripts/feeder` |

> **All four spokes are on `main`.** `voidconfig` is not a contributor-facing
> donation rail — it's a shared pure lib that pins the one thing every queue-row
> writer (feeder, hand-enqueue, the API, Voidmind) must agree on, so they can't
> drift. It earns its place by the same rule as the other pure libs: read-and-
> derive needs no I/O.

## Voidmind — integration reasoning

Proposes work (writes `ideas` + `queue_items`) on a **token donor's own LLM keys**.
Runs on their box ⇒ HTTP-only + bearer token (the model Voidrunner already built).
Its writes are **low-trust proposals, not results** ⇒ no integrity gate, can never
move the champion; dedups on the existing `content_hash`. **Zero schema change** —
`ideas` and `queue_items` exist. Package `voidmind/` parallels `runner/`; the two
donor clients likely share a small `voidclient` HTTP/auth core once both exist.

## Voidcredit — integration reasoning

Only **reads and derives** credit + lineage (compute-seconds, tokens-donated,
"your run promoted champion X", idea→run→champion chains) ⇒ a **pure library like
Voidcheck**, not a service. Credit is **derived on read, never stored**, so it
can't drift — the source of truth stays `runs`/`confirmations`/`champions`. New
**read** endpoints (`/leaderboard`, `/contributor/<handle>`, `/lineage?run=`) do
the SQL and call `voidcredit`; voidspark renders. **Zero schema change** for v0.

## Build order

1. ✓ **Voidrunner** — unlocked compute donation (the GPU bottleneck).
2. ✓ **Voidcheck** — the trust core, bulletproof + reusable before going public.
3. ✓ **Voidcredit** — Voidrunner already stamps `contributor_id` on every run;
   Voidcredit surfaces it (the low-risk pure-lib + read-only pattern).
4. ✓ **Voidmind** — the second donation rail (tokens). Pulled in `voidconfig` to
   pin the config/dedup shape so the idea-loop can't drift from the feeder; the
   spam gate is deliberately deferred until there's spam to measure (see
   VOIDMIND.md). Both donation rails (compute + tokens) are now open.

**All four spokes shipped.** Open follow-ups (not new spokes): deploy the new
endpoints to the live 8787 API (a server restart — the operator's call), wire
voidspark to `/leaderboard` `/contributor`, and add a Voidmind quality gate if/when
open token-donation produces spam.
