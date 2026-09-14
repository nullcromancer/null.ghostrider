# Ghostrider Protocol

## Seats

Claude is the default independent auditor/orchestrator. The implementing seat can be **Codex, Cline, or GitHub Copilot CLI**. The selected implementer works in an isolated worktree and reports what changed. The auditor does not quietly repair the implementer's failed work; failures become explicit repair packets or a blocked outcome.

## Durable local channel

The queue is a Git commit chain at `refs/pairing/queue`. Writes use compare-and-swap through `git update-ref`, so competing claims cannot both win. Queue operations never check out the queue ref and therefore do not dirty the user's working tree.

## Lifecycle

`briefed -> worktree-ready -> implementing -> reported -> verification -> audit-ready -> audit-passed -> landed -> cleaned`

Failure/recovery states include `verification-failed`, `repair-ready`, `blocked`, `max-rounds`, and `rolled-back`.

## Implementer routing

A run may select `codex`, `cline`, `copilot`, or `auto`. The chosen implementer is written into queue state and kept for repair rounds unless the operator explicitly overrides it. Branch names are implementation-seat specific, for example `cline/pair-12-...`.

## Repair loop

Deterministic verification failures are automatically eligible for another implementation round up to the profile's `max_rounds`. Claude's semantic audit can also produce a compact failure packet and request redispatch. Prior history remains durable, but only the latest relevant delta is sent on the wire.

## Landing

The implementer commits on its dedicated branch. Claude audits that branch/worktree. `land` refuses non-audit-passed work unless explicitly forced, creates a backup ref, merges with `--no-ff`, records the merge commit, then cleans the worktree/branch if enabled.

## Recovery

Claims record seat, lease expiry, PID, host, dispatch ID, round, worktree and branch. `resume` does not automatically take over a process that still appears alive. Stale claims can be requeued or redispatched after inspection.

## Token discipline

The queue stores full durable history, but the live wire packet contains the brief plus only the latest report and latest audit/verification failure. Detailed CLI output is logged locally and represented on the wire by command, exit status, and digest.
