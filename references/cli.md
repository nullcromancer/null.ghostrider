# CLI Reference

All examples use `python scripts/pairctl.py` from the skill directory; an installed agent should resolve the absolute runtime path.

Shared optional selector:

```text
--implementer auto|codex|cline|copilot
```

## Core

```text
status [ID]             config + lifecycle status
doctor                  prerequisite/adapter check
on | off                global pairing switch
config [KEY [VALUE]]    show/set global config
profile list|show|use|set
protocol ...            CAVEMAN templates/lint/decode/stats
```

## Queue / lifecycle

```text
channel --ensure
init
brief TITLE --spec brief.json --strict
queue [--json]
show ID [--wire|--json]
events ID [--ndjson]
claim [ID]
requeue ID [--note TEXT]
report ID --verdict pass|fail|blocked --spec report.json
```

## Orchestration

```text
pair TITLE --spec brief.json [--profile fast|standard|deep|release] [--implementer ...]
pair --id ID             resume deterministic implementation/verify loop
dispatch ID [--implementer ...]
verify [ID] [--list|--json]
audit ID --verdict pass|fail|block --spec audit.json [--redispatch]
resume ID [--requeue|--redispatch] [--implementer ...]
```

## Landing / recovery

```text
land ID [--keep-worktree]
rollback ID
cleanup ID [--keep-branch]
purge-queue --yes
```

## Profiles

Built-ins: `fast`, `standard`, `deep`, `release`.

```text
profile list
profile show standard
profile use fast
profile set my-profile max_rounds 2
profile set my-profile implementer cline
```

## Useful multi-agent configuration

```text
config implementer auto
config implementer-priority '["codex","cline","copilot"]'
config cline-thinking low
config copilot-model auto
config copilot-yolo off
config dispatch-output log
```

## CAVEMAN helpers

```text
protocol brief-template
protocol report-template
protocol audit-template
protocol lint --file packet.cvm
protocol decode --file packet.cvm
protocol stats --file packet.cvm
```

`stats` reports character/word/line proxies. It intentionally does not claim an exact tokenizer count.
