<div align="center">
  <img src="references/ghostrider.png" alt="Ghostrider" width="480" />
</div>

## Business Analyst Summary

- Ghostrider is a Claude Code Agent Skill that lets one AI seat (Claude) delegate hands-on implementation work to a second AI seat (Codex, Cline, or GitHub Copilot CLI), then independently audit the result before it is merged (SKILL.md:6-15).
- The goal is to cut the token/dollar cost of running two AI agents together: instead of repeatedly re-pasting source code, instructions, and history between the two seats, they exchange a compact wire format called CAVEMAN/1 (SKILL.md:17-27, references/caveman.md).
- The normal end-user workflow is a single instruction, `Pair on this.`, optionally naming the implementer (`Pair on this using Codex.`) (SKILL.md:46-60, README usage section below).
- Work happens in an isolated Git worktree per task so the implementing agent cannot collide with or corrupt the primary working tree (SKILL.md:8-15, references/protocol.md:7-9).
- Every implementation is deterministically verified (build/test commands discovered from the repository), then independently audited by Claude before merge; Claude's audit is never skipped or replaced by the implementer's own claim of success (SKILL.md:183-224, references/protocol.md:1-5).
- Failure handling is explicit and bounded: a fixed number of automatic repair rounds, then a human-visible `blocked` or `max-rounds` state rather than silent retries forever (references/protocol.md:11-23).
- All coordination state (task queue, history, audit trail) is stored as a Git commit chain (`refs/pairing/queue`) rather than a separate server or database, so it travels with the repository and survives machine restarts (references/protocol.md:7-9).
- Landing (merging) only happens after an explicit audit pass, with an automatic backup ref and a documented rollback path (SKILL.md:225-239, references/protocol.md:25-27).
- Operational impact for a team adopting this: it changes how AI-assisted changes are authored and reviewed (multi-agent, worktree-isolated, audit-gated) but does not require any change to the target repository's own code, build system, or branch structure beyond the merges it performs (references/migration.md).
- No user data, customer data, or business data flows through Ghostrider itself; it orchestrates local CLI tools and Git operations only (evidence: no network, database, or external-service code found in `scripts/pairctl.py` or `install.py` during this scan).

## Technical Summary

- Ghostrider is a single-directory Claude Code "Agent Skill": `SKILL.md` (policy, read by the agent) plus `scripts/pairctl.py` (the deterministic runtime CLI) (SKILL.md:1-15, scripts/pairctl.py).
- Implementation language is Python 3.8+, standard library only — no third-party dependencies, no `requirements.txt`/`pyproject.toml`/lockfile exists in the repository (install.py:2-5, readme-scan: `no_manifests: true`).
- `scripts/pairctl.py` (1,482 lines) is the whole runtime: config loading, Git-ref queue read/write via compare-and-swap `git update-ref`, CAVEMAN/1 encode/decode, worktree lifecycle, verification-command discovery and execution, and the `pair`/`report`/`audit`/`land`/`rollback`/`resume` subcommands (scripts/pairctl.py:121-953, references/cli.md).
- `install.py` (204 lines) copies the skill directory into the per-tool skill locations for Claude Code, Codex CLI, Cline, and GitHub Copilot CLI, at user or project scope, and writes initial shared config to `~/.pairing/config.json` (install.py:60-125).
- State and coordination live entirely in Git: a commit chain on `refs/pairing/queue` inside the target repository, written with compare-and-swap so two competing claims cannot both win (references/protocol.md:7-9, scripts/pairctl.py:403-460).
- Hosting model: none — this is a local CLI tool invoked by an AI coding agent on a developer's machine; there is no server component, no container, and no IaC in the repository (readme-scan: `no_ci: true`, `no_containers_or_iac: true`).
- Auth/security model: relies on the operating system's own file permissions and each implementing CLI's own sandbox flag (`codex_sandbox`, Cline auto-approval, `copilot_yolo`); Ghostrider itself performs no authentication (references/adapters.md).
- CI/CD: none present in the repository at scan time (readme-scan: `ci: []`). Verification is instead discovered per target repository at runtime (`detect_verification`, scripts/pairctl.py:696-740).
- Observability: a local, human-readable console plus a machine-readable event/history log per queue entry (`events ID --ndjson`), and full command output kept in `~/.pairing/logs/` rather than injected into agent context (README usage section below, references/cli.md:29-34).
- Test suite: `tests/test_smoke.py` (228 lines), standard-library `unittest`, exercising install/uninstall and a full pair/verify/audit/land/rollback cycle against a fake agent CLI (tests/test_smoke.py).
- Where to start reading: `SKILL.md` for the agent-facing policy the orchestrating seat follows; `scripts/pairctl.py` for the actual mechanics; `references/protocol.md` for the lifecycle state machine; `references/cli.md` for every subcommand.

# Ghostrider

A token-conscious multi-agent pairing engine for **Claude Code, Codex CLI, Cline, and GitHub Copilot CLI**.

## Last Updated

- Last Updated: 2026-09-14
- Last Commit Date: Insufficient Evidence (repository is not yet a Git work tree; `inventory/vcs-state.json` records `is_work_tree: false`)

## Table of Contents

- [Business Analyst Summary](#business-analyst-summary)
- [Technical Summary](#technical-summary)
- [Repository Overview](#repository-overview)
- [Components / Projects / Packages](#components-projects-packages)
- [Architecture Overview](#architecture-overview)
- [Tech Stack and Dependencies](#tech-stack-and-dependencies)
- [Project Layout](#project-layout)
- [Getting Started (Local Development)](#getting-started-local-development)
- [Configuration](#configuration)
- [Running the System](#running-the-system)
- [Deployment and CI/CD](#deployment-and-cicd)
- [Deep Code Reference (Wiki Section)](#deep-code-reference-wiki-section)
- [Data and Integrations](#data-and-integrations)
- [Security Notes](#security-notes)
- [Observability and Monitoring](#observability-and-monitoring)
- [Common Tasks and Troubleshooting](#common-tasks-and-troubleshooting)
- [Contributing / Coding Standards](#contributing-coding-standards)
- [License](#license)

## Repository Overview

Ghostrider is a Claude Code Agent Skill (single directory, distributed as-is) that adds multi-agent "paired programming" to Claude Code, Codex CLI, Cline, and GitHub Copilot CLI. Claude acts as the default independent auditor/orchestrator; one of Codex, Cline, or Copilot acts as the implementing seat inside an isolated Git worktree (SKILL.md:1-15). Seat-to-seat traffic uses the compact CAVEMAN/1 wire protocol instead of re-pasting source and history (SKILL.md:17-44). The repository ships the skill definition, the deterministic runtime, per-adapter integration notes, and a standard-library smoke-test suite (readme-scan `repository_shape`: "multi-component system"; 12 first-party files, 0 third-party — inventory `origin_breakdown`).

## Components / Projects / Packages

| Component | Type | Language/Framework | Runtime/Target | Path | Purpose |
| --- | --- | --- | --- | --- | --- |
| Skill definition | Agent Skill policy document | Markdown (YAML frontmatter) | Read by Claude Code, Codex, Cline, Copilot | `SKILL.md` | Behavioral policy the orchestrating agent follows (SKILL.md:1-15) |
| Runtime CLI | Command-line tool | Python 3.8+, stdlib only | `python scripts/pairctl.py <cmd>` | `scripts/pairctl.py` | Deterministic queue, worktree, verification, audit and landing mechanics (scripts/pairctl.py:1-15) |
| Installer | Command-line tool | Python 3.8+, stdlib only | `python install.py` | `install.py` | Copies the skill into Claude/Codex/Cline/Copilot skill directories, seeds config (install.py:1-20) |
| Codex agent descriptor | Configuration | YAML | Read by Codex CLI | `agents/openai.yaml` | Declares display name, prompt, and policy for Codex's agent registry (agents/openai.yaml) |
| Reference docs | Documentation | Markdown | Read on demand by the agent | `references/*.md` | Protocol, adapters, CAVEMAN packet format, CLI reference, migration notes (references/) |
| Banner image | Asset | PNG | N/A | `references/ghostrider.png` | Skill banner/artwork used in this README |
| Smoke tests | Test suite | Python 3 `unittest` | `python tests/test_smoke.py` | `tests/test_smoke.py` | End-to-end install and pair/verify/audit/land/rollback coverage (tests/test_smoke.py) |

## Architecture Overview

```mermaid
flowchart LR
    User[Developer] -->|"Pair on this."| Claude[Claude Code seat<br/>auditor/orchestrator]
    Claude -->|brief: CAVEMAN/1| Queue[(refs/pairing/queue<br/>Git commit chain)]
    Queue --> Dispatch{Implementer routing}
    Dispatch -->|codex| Codex[Codex CLI<br/>isolated worktree]
    Dispatch -->|cline| Cline[Cline CLI<br/>isolated worktree]
    Dispatch -->|copilot| Copilot[Copilot CLI<br/>isolated worktree]
    Codex -->|report: CAVEMAN/1| Queue
    Cline -->|report: CAVEMAN/1| Queue
    Copilot -->|report: CAVEMAN/1| Queue
    Queue --> Verify[Deterministic verification<br/>pairctl.py: detect_verification]
    Verify -->|pass| Audit[Claude independent audit]
    Verify -->|fail, rounds remain| Dispatch
    Audit -->|pass| Land[land: merge --no-ff<br/>backup ref]
    Audit -->|fail| Dispatch
    Land --> Repo[(Target repository<br/>main/default branch)]
```

Evidence: SKILL.md:8-15, SKILL.md:128-233, references/protocol.md:1-27, scripts/pairctl.py:696-953 (dispatch/verify), scripts/pairctl.py (land/rollback command handlers).

## Tech Stack and Dependencies

- Language: Python 3.8+ (install.py:4).
- Dependencies: none — standard library only; no `requirements.txt`, `pyproject.toml`, `setup.py`, or lockfile exists in the repository (readme-scan: `manifests_and_lockfiles: []`, `no_manifests: true`).
- Version control integration: Git, invoked as a subprocess (`git update-ref`, `git worktree`, `git log`, etc.) — no Git Python binding is used (scripts/pairctl.py:314-334, references/protocol.md:7-9).
- Configuration format: JSON (`~/.pairing/config.json`) and optional TOML/JSON per-repository overrides (`.pairing.toml` / `.pairing.json`) (SKILL.md:271-297, install.py:108-124).
- Test framework: `unittest` (Python standard library) (tests/test_smoke.py:10).
- Packaging: none — the skill is distributed as a plain directory and installed by copying (`install.py`), not via PyPI, npm, or any package manager (install.py:26-49).
- External CLIs orchestrated (not bundled): `codex`, `cline`, `copilot`, all invoked as subprocesses if present on `PATH` (references/adapters.md).

## Project Layout

```text
ghostrider/
  SKILL.md                   - agent-facing policy and workflow (304 lines)
  README.md                  - this file
  install.py                 - installer for Claude/Codex/Cline/Copilot (204 lines)
  agents/
    openai.yaml               - Codex CLI agent descriptor
  scripts/
    pairctl.py                - deterministic runtime CLI (1,482 lines)
  references/
    protocol.md                - lifecycle/state-machine reference
    adapters.md                 - per-CLI (Codex/Cline/Copilot) adapter details
    caveman.md                  - CAVEMAN/1 wire-format reference
    cli.md                       - full pairctl.py subcommand reference
    migration.md                 - migration notes from the prior "pairing.py" tool
    ghostrider.png               - banner image used in this README
  tests/
    test_smoke.py               - standard-library end-to-end smoke tests (228 lines)
```

Evidence: repository inventory (12 regular files, 4 directories; `inventory/inventory-summary.json`).

## Getting Started (Local Development)

Prerequisites: Python 3.8+ and Git on `PATH`. No third-party packages need to be installed (install.py:4-5).

Install the skill for every supported client:

```bash
python install.py --target all
```

Install for a single client:

```bash
python install.py --target claude
python install.py --target codex
python install.py --target cline
python install.py --target copilot
```

Project-local install instead of the user-level skill directory:

```bash
python install.py --target all --scope project --project /path/to/repo
```

Replace an existing installed copy:

```bash
python install.py --target all --force
```

Evidence: install.py:52-88, 142-196; README usage section (previously validated against `install.py --help`).

## Configuration

Shared configuration is a JSON file, `~/.pairing/config.json` by default, overridable with the `PAIRING_HOME` environment variable (install.py:108, SKILL.md:271-279 for profile-level config; references/cli.md:68-77 for keys). Known configuration keys (names only — this repository defines no secret values):

| Key | Purpose |
| --- | --- |
| `enabled` | global pairing on/off switch (install.py:93-94) |
| `implementer` | default implementing CLI: `auto`, `codex`, `cline`, `copilot` (install.py:95-96, references/cli.md:71) |
| `implementer_priority` | ordered list `auto` uses to pick the first installed CLI (references/cli.md:72, references/adapters.md:72-73) |
| `codex_sandbox` | Codex sandbox mode: `read-only`, `workspace-write`, `danger-full-access` (install.py:97-98, references/adapters.md:20) |
| `cline_thinking` | Cline reasoning level: `none`, `low`, `medium`, `high`, `xhigh` (install.py:99-100, references/adapters.md:39) |
| `copilot_model` | Copilot CLI model name, e.g. `auto` (install.py:101-102, references/adapters.md:61) |
| `copilot_yolo` | allow Copilot fully non-interactive `--yolo` runs; off by default (install.py:103-104, references/adapters.md:63-67) |
| `dispatch_output` | whether dispatched CLI output is logged only or inherited into context (references/cli.md:76, README CAVEMAN section) |

Repository-local overrides: a target repository may contain `.pairing.toml` or `.pairing.json` with `[pairing]` (profile, implementer), `[verification]` (explicit verification commands), and `[profile.<name>]` blocks (SKILL.md:281-297).

## Running the System

Everyday use is a single natural-language instruction inside Claude Code:

```text
Pair on this.
```

Naming the implementer explicitly:

```text
Pair on this using Codex.
Pair on this using Cline.
Pair on this using Copilot.
```

Direct runtime invocation (what the skill drives under the hood):

```bash
python scripts/pairctl.py status
python scripts/pairctl.py doctor
python scripts/pairctl.py pair "Implement feature X" --spec brief.json --implementer codex
python scripts/pairctl.py pair "Implement feature X" --spec brief.json --implementer cline
python scripts/pairctl.py pair "Implement feature X" --spec brief.json --implementer copilot
python scripts/pairctl.py config implementer auto
```

Full subcommand reference: `references/cli.md`. Lifecycle states a run passes through: `briefed -> worktree-ready -> implementing -> reported -> verification -> audit-ready -> audit-passed -> landed -> cleaned`, with failure states `verification-failed`, `repair-ready`, `blocked`, `max-rounds`, `rolled-back` (references/protocol.md:11-15).

Uninstall:

```bash
python install.py --target all --uninstall
```

Uninstall removes only the installed skill copies; shared config, logs, worktrees with in-progress work, and repository queue history are left in place intentionally (README uninstall notes; install.py:127-139).

## Deployment and CI/CD

Insufficient Evidence for a deployment pipeline: the repository contains no CI configuration, no Dockerfile, no container/IaC definitions, and no publish/release scripts (readme-scan: `no_ci: true`, `no_containers_or_iac: true`, `ci: []`). Distribution is manual: a user or agent runs `install.py` to copy the skill directory into a target CLI's skill location (install.py:26-49, 142-196).

## Deep Code Reference (Wiki Section)

### Cross-Reference Index

| Component/Module/Class/Function | File Path | Key Methods | Notes |
| --- | --- | --- | --- |
| `Console` | `scripts/pairctl.py:125` | output helpers used throughout the CLI | human-facing console output (scripts/pairctl.py:125) |
| Config loading | `scripts/pairctl.py:162-256` | `load_raw_config`, `load_config`, `save_raw_config`, `load_repo_settings`, `effective_config` | JSON global config + TOML/JSON repo overrides merged into one effective config |
| Git helpers | `scripts/pairctl.py:314-372` | `git`, `repo_root`, `common_repo_root`, `working_tree_clean`, `current_branch`, `channel_ready`, `has_github` | thin subprocess wrapper around the `git` binary |
| Queue (Git-ref) | `scripts/pairctl.py:403-460` | `queue_load`, `queue_commit`, `next_id`, `find_brief` | reads/writes the `refs/pairing/queue` commit chain with compare-and-swap |
| CAVEMAN/1 codec | `scripts/pairctl.py:492-577` | `cvm_encode`, `cvm_parse`, `cvm_escape`, `cvm_unescape`, `lint_brief` | encodes/decodes/validates the compact wire packets described in `references/caveman.md` |
| Worktree lifecycle | `scripts/pairctl.py:643-696` | `worktree_path`, `ensure_worktree`, `cleanup_worktree` | creates/removes the isolated Git worktree per brief |
| Verification | `scripts/pairctl.py:696-794` | `detect_verification`, `run_verification`, `verification_wire` | discovers and runs repository build/test commands, produces the compact wire summary |
| CLI subcommands | `scripts/pairctl.py:794+` | `cmd_init`, `cmd_brief`, `cmd_claim`, `cmd_requeue`, `cmd_report`, plus `pair`/`audit`/`land`/`rollback`/`resume` handlers | one function per `argparse` subcommand; full list in `references/cli.md` |
| Installer | `install.py` | `copytree_atomicish`, `user_destinations`, `project_destinations`, `set_initial_config`, `uninstall`, `main` | atomic-ish copy-install/uninstall for four target CLIs at user or project scope |
| Smoke tests | `tests/test_smoke.py` | `PairingV4Tests` (unittest) | drives install, a fake implementing-agent CLI, and a full pair/verify/audit/land/rollback cycle |

No web or API surface exists in this repository (it is a local CLI tool); the API Surface subsection is intentionally omitted (readme-scan: `entrypoints: []`, no HTTP framework detected).

## Data and Integrations

- Persistent state: a Git commit chain on `refs/pairing/queue` inside the target repository being paired on — no external database (references/protocol.md:7-9).
- Local files: `~/.pairing/config.json` (shared config), `~/.pairing/logs/` (full command output), worktrees under `~/.pairing/` by default (README runtime-examples section; install.py:108-124).
- External integrations: none over the network. Ghostrider shells out to locally installed CLIs (`git`, `codex`, `cline`, `copilot`) only; no HTTP client, SDK, or API key handling was found in `scripts/pairctl.py` or `install.py` during this scan (readme-scan: `ecosystems_detected: []`).

## Security Notes

- No secrets, credentials, or tokens were found in the repository; the secret scan returned zero candidates across all 11 scanned text files (secrets scan: `findings_total: 0`, `binary_sensitive_files: 0`).
- No authentication/authorization layer exists in this repository — it is a local developer tool, not a service (Repository Overview above).
- The implementing seat's sandboxing is delegated to the underlying CLI: `codex_sandbox` (default `workspace-write`), Cline's auto-approval setting, and Copilot's `copilot_yolo` flag (default `off`, since enabling it appends `--yolo` and grants broad permissions beyond the worktree) (references/adapters.md:56-67, SKILL.md:299-304).
- SKILL.md explicitly documents that Git worktree isolation "prevents edit collisions; it is not a security sandbox for arbitrary shell commands" (SKILL.md:304).
- Queue writes use Git compare-and-swap (`git update-ref`) so two competing claims cannot both succeed, which is a correctness/integrity control rather than an authentication control (references/protocol.md:7-9).

## Observability and Monitoring

- Structured, greppable lifecycle events are recorded per brief and can be read with `pairctl.py events ID --ndjson` (references/cli.md:29-34).
- Queue state itself is inspectable at any time: `pairctl.py queue`, `pairctl.py status [ID]`, `pairctl.py show ID --json` (references/cli.md:26-34).
- Full command/verification output is written to local log files under `~/.pairing/logs/` by default rather than being replayed into an agent's context window, keeping the CAVEMAN/1 wire packets small (README CAVEMAN section; scripts/pairctl.py:750-794).
- There is no metrics/tracing integration (no OpenTelemetry, Prometheus, or similar dependency found; `manifests_and_lockfiles: []`).

## Common Tasks and Troubleshooting

| Task | Command |
| --- | --- |
| Check runtime health and adapter availability | `python scripts/pairctl.py doctor` |
| Check current pairing status | `python scripts/pairctl.py status` |
| List/inspect the task queue | `python scripts/pairctl.py queue --repo <repo>` |
| Inspect one queue entry | `python scripts/pairctl.py status <id> --repo <repo>` / `show <id> --json` |
| Resume an interrupted round | `python scripts/pairctl.py resume <id> --repo <repo>` |
| Requeue a stale/dead claim | `python scripts/pairctl.py resume <id> --requeue --repo <repo>` |
| Redispatch after requeue | `python scripts/pairctl.py resume <id> --redispatch --repo <repo>` |
| Undo a landed merge (no history rewrite) | `python scripts/pairctl.py rollback <id> --repo <repo> --seat claude` |
| Switch the default implementer | `python scripts/pairctl.py config implementer auto` |
| Replace an existing skill install | `python install.py --target all --force` |

Evidence: SKILL.md:64-260, references/cli.md.

## Contributing / Coding Standards

- No `CONTRIBUTING.md`, linter configuration, or formatter configuration was found in the repository during this scan (Insufficient Evidence for a formal style guide).
- The codebase is standard-library-only Python; `scripts/pairctl.py` and `install.py` use `from __future__ import annotations` and full type hints on function signatures throughout (scripts/pairctl.py, install.py:7-15).
- The test suite (`tests/test_smoke.py`) is the closest thing to a contribution gate present in the repository; run it with `python tests/test_smoke.py` before proposing changes.
- SKILL.md itself encodes several hard rules for anyone extending the runtime, notably: do not collapse the auditing and implementing seat into one when an independent auditor is available (SKILL.md:13), and never let deterministic verification substitute for Claude's independent semantic audit (SKILL.md:147, 183-192).

## License

Insufficient Evidence — no `LICENSE` file exists in the repository at scan time.
