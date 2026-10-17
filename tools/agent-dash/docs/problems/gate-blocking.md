# gate-blocking: unmet gate with dependents already building

A `kind: gate` task (an outside signal or condition) is not `done`, but a
task that depends on it is already `wip` or `pr`.

## Why it matters

The dependent may build on a signal that never arrives. The plan says
"wait", the branch says "go".

## How to fix

Pause the dependent, or record the gate as met when the signal arrived:

```bash
python3 agent_dash.py ws plan WS4
python3 agent_dash.py ws task WS4 WS4.2 todo
```

## How to verify

`doctor --ws WS4` no longer lists the gate.
