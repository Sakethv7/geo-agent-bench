# geo-agent-bench

A benchmark that measures how AI agents behave on a test website when the same content is published in different formats. GEO means Generative Engine Optimization.

Experiment A is the pilot. It runs one cloud agent (Claude Sonnet 5.5) on two variants of one fictional site (`legacy` and `ai`). A local Qwen model ran once and is recorded as a finding (ADR-010). It scores each run with an exact string match. It reads the site's own request log to see what the agent really fetched.

This study is also a case study. The docs explain each concept and record where the real build differed from the plan.

## Status (2026-11-03)

| Part | State |
|---|---|
| Design docs in `docs/` | ADR-009 and ADR-010 and their contracts: approved. ADR-011 (the `ai` variant) and its contracts: approved. ADR-012 (visible pointers for the `ai` variant): approved. The rest: still draft. |
| Test site code (`legacy` variant) | Written. Running on the VM. |
| `ai` variant | Built and live on 2026-11-03 (ADR-011). A first dry run found no shortcut use, because the shortcuts were hidden. Visible pointers (ADR-012) are built and live. The rerun found shortcut use in 1 of 6 runs (`llms.txt` only, no twin). |
| VM, static IP, domain, DNS, HTTPS | Live at `https://legacy.wanderingcodersv.dev` |
| Identity probe (Claude) | Ran on 2026-11-02. It found a fetch cache of at least 21 minutes. |
| Fix for the cache (ADR-009: one host name per run) | Built and live on 2026-11-03. 103 tests pass. The Claude dry run (2 runs) passed. The Qwen dry run worked end to end but scored 0 of 2: the model fetched one page and stopped. |
| The pilot | Ran on 2026-11-03: 60 runs, 60 correct. No gap on pages, over-fetch or tokens. `llms.txt` read in 6 of 30 `ai` runs, no markdown twin fetched. See the last section of docs/architecture.md. |

## Where to read

| You want | Read |
|---|---|
| The design, the web concepts (DNS, TLS, Caddy) and the plan-versus-result table | [docs/architecture.md](docs/architecture.md), last section |
| Why each decision was made, and what it gave up | [docs/adr.md](docs/adr.md) |
| Execution paths and failure behavior | [docs/logic_flow.md](docs/logic_flow.md) |
| Commands, file formats and interfaces | [docs/api.md](docs/api.md) |
| One run, step by step, as a diagram | [docs/visuals/exp-a-pilot-run-flow.html](docs/visuals/exp-a-pilot-run-flow.html) |
| How the site is deployed | [deploy/README.md](deploy/README.md) |

## Layout

```
geo-agent-bench/
├── docs/          design docs and visuals
├── src/geobench/  server, runner, log parser, metrics, agents, interceptor, probe
├── tasks/         tasks.yaml: questions, ground truth, minimum page sets
├── plans/         pilot-1.yaml: the run plan
├── deploy/        Caddyfile and systemd unit for the VM
├── runs/          run and request logs (git-ignored)
└── tests/
```

## Known gaps

- `identity_reference.json` is on the laptop only. The VM server does not use it yet, so it cannot label verified agents.
