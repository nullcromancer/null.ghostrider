# Migration from pairing.py

The original `pairing.py` proved the paired-seat workflow by installing managed instruction blocks into global `CLAUDE.md` and `AGENTS.md` files while also owning the queue runtime.

v4 separates those concerns:

- stable behavioral policy lives in `SKILL.md` and is loaded on demand;
- deterministic mechanics live in `scripts/pairctl.py`;
- no global `CLAUDE.md` or `AGENTS.md` surgery is required;
- CAVEMAN/1 replaces repeated long-form seat handoffs;
- implementation uses isolated worktrees;
- verification, repair rounds, leases, recovery, landing and rollback are first-class;
- the implementing seat is adapter-based and can be Codex, Cline, or Copilot;
- the installer can place the same skill in Claude, Codex, Cline, and Copilot skill locations.

Existing repository queue refs created by the old tool can be preserved. For a clean migration, install v4 first, inspect `pairctl.py status`, and do not delete an existing `refs/pairing/queue` unless its history is intentionally disposable.
