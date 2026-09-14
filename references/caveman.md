# CAVEMAN/1 Wire Protocol

CAVEMAN/1 is deliberately boring: one short header, then `KEY|value` lines. It is optimized for model-to-model handoff where both seats can read the repository themselves.

It is not compression, encryption, or a private language. It is a compact shared schema that avoids repeated natural-language framing.

## Escaping

`\\n` = newline, `\\p` = literal pipe, `\\\\` = literal backslash.

## Brief

```text
CVM1|B
G|goal
S|scope item
C|constraint
A|acceptance criterion
F|path:symbol
T|verification hint
X|explicit exclusion
U|important unknown
```

`G`, `A`, and at least one `S` or `F` are required by the strict brief quality gate.

## Implementation report

```text
CVM1|R|P
D|delta
M|commit sha
T|command => exit
E|path:symbol
Q|uncertainty
B|blocker
```

Verdict codes are `P` pass, `F` fail, `B` blocked.

## Audit

```text
CVM1|A|P
E|independent evidence
T|auditor command => exit
B|finding requiring repair
Q|remaining uncertainty
```

## Verification delta

The runtime emits compact verification packets like:

```text
CVM1|V|F
T|dotnet test => 1 [5d91d8c33f98a51d]
```

Full command output stays in `~/.pairing/logs/...` and is loaded only when a seat needs it.

## Token discipline

1. Reference repository paths/symbols instead of copying source.
2. During repair send only latest report + latest failing verification/audit packet.
3. Use hashes for large logs; retrieve the log from disk only if diagnosing it.
4. Do not restate the stable role contract in each dispatch; `$ghostrider` supplies it.
5. Keep one fact per line so a later round can preserve only the facts that changed.
