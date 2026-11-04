# geo-agent-bench

A benchmark that measures how AI agents behave on a test website when the same content is published in different formats. GEO means Generative Engine Optimization.

The project started with Experiment A, a 60-run Claude pilot on two variants of one fictional site (`legacy` and `ai`). Phase A then made the pages realistic in size, added planted exposed files, and compared Claude's vendor fetch tool, Claude with the benchmark's raw fetch tool, and local Qwen on llama.cpp. The scorer uses exact ground truth and reconciles agent claims with the site's own request log.

This study is also a case study. The docs explain each concept and record where the real build differed from the plan.

## Status (2026-11-03)

| Part | State |
|---|---|
| Design docs in `docs/` | Pilot ADRs and Phase A ADR-013 through ADR-017 are approved. Phase A includes a portfolio-scope amendment and final observed result. ADR-018 to ADR-023 (Phases B and C) were approved and built on 2026-11-03. |
| Test site code (`legacy` variant) | Written. Running on the VM. |
| `ai` variant | Built and live on 2026-11-03 (ADR-011). A first dry run found no shortcut use, because the shortcuts were hidden. Visible pointers (ADR-012) are built and live. The rerun found shortcut use in 1 of 6 runs (`llms.txt` only, no twin). |
| VM, static IP, domain, DNS, HTTPS | Live at `https://legacy.wanderingcodersv.dev` |
| Identity probe (Claude) | Ran on 2026-11-02. It found a fetch cache of at least 21 minutes. |
| Fix for the cache (ADR-009: one host name per run) | Built and live on 2026-11-03. Each run has a fresh host and token so origin requests can be assigned without overlapping windows. |
| The pilot | Ran on 2026-11-03: 60 runs, 60 correct. No gap on pages, over-fetch or tokens. `llms.txt` read in 6 of 30 `ai` runs, no markdown twin fetched. See the last section of docs/architecture.md. |
| Phase A portfolio run | Complete on 2026-11-03: 36/36 records. Claude vendor and Claude raw were each correct on 12/12; Qwen was correct on 0/12, including two context overflows. No exposed-file hit, canary leak or injection obedience occurred. Recorded Claude cost estimate: $4.94. This is descriptive evidence from one pass per cell, not a statistical model ranking. |
| Phase A Qwen robustness follow-up | Complete: 12/12 exact answers, no errors or context overflows. It used deterministic visible-text/link extraction, bounded context and one early-stop recovery; no second model or ground truth entered the loop. One run fetched the injection target but did not reproduce it. The original 0/12 baseline remains unchanged. Phase A is closed. |
| Phase B and C | Built, deployed and run on 2026-11-03 (154 tests pass). Phase B, 36 runs: the MCP connector worked; the broad tool surface returned 18 to 28 times the bytes and gave the most correct, cheapest runs; Claude obeyed the poisoned tool description in 2 of 2 exposed runs, Qwen in 1 of 2. Phase C, 18 Qwen runs: signature verdicts matched the design, and spoofed or unsigned agents with a valid token still read the protected page. Tables in `reports/phase-b-claude/`, `reports/phase-b-qwen/`, `reports/phase-c/`. One pass per cell, so descriptive only. Follow-up (ADR-024): one retry on an empty Qwen reply lifted the Phase B Qwen arm from 14/18 to 18/18 at +47% input tokens; tables in `reports/phase-b-qwen-retry/`. Claude check (ADR-022 addendum): on the protected page with no sign-in path, Claude's fetch tool said it could not read it in 6 of 6 runs and made up no order number; the tool hides the 401's sign-in link. Phase D (ADR-025 to ADR-027): interop with Cloudflare's `web-bot-auth` found and fixed one verifier bug; a signature gate (own bot name, account host) blocked 6/6 spoofed or unsigned runs and passed 3/3 signed; a model running OAuth itself worked for Claude (3/3 granted) and not for Qwen (0/3, it never followed the 401's link). |
| Verification | 124 tests pass. The runner refuses to append records to an output file owned by another plan. |

## Where to read

| You want | Read |
|---|---|
| The design, the web concepts (DNS, TLS, Caddy) and the plan-versus-result table | [docs/architecture.md](docs/architecture.md), last section |
| Why each decision was made, and what it gave up | [docs/adr.md](docs/adr.md) |
| Execution paths and failure behavior | [docs/logic_flow.md](docs/logic_flow.md) |
| Commands, file formats and interfaces | [docs/api.md](docs/api.md) |
| One run, step by step, as a diagram | [docs/visuals/exp-a-pilot-run-flow.html](docs/visuals/exp-a-pilot-run-flow.html) |
| The Phase A result tables | [reports/portfolio-phase-a/results.csv](reports/portfolio-phase-a/results.csv), [summary.csv](reports/portfolio-phase-a/summary.csv), [gaps.csv](reports/portfolio-phase-a/gaps.csv) |
| The Qwen robustness follow-up | [reports/phase-a-qwen-robust/results.csv](reports/phase-a-qwen-robust/results.csv), [summary.csv](reports/phase-a-qwen-robust/summary.csv), [gaps.csv](reports/phase-a-qwen-robust/gaps.csv) |
| How the site is deployed | [deploy/README.md](deploy/README.md) |

## Layout

```
geo-agent-bench/
├── docs/          design docs and visuals
├── src/geobench/  server, runner, log parser, metrics, agents, interceptor, probe
├── tasks/         tasks.yaml: questions, ground truth, minimum page sets
├── plans/         pilot-1.yaml: the run plan
├── reports/       derived portfolio result tables
├── deploy/        Caddyfile and systemd unit for the VM
├── runs/          run and request logs (git-ignored)
└── tests/
```

## Known gaps

- `identity_reference.json` is on the laptop only. The VM server does not use it yet, so it cannot label verified agents.
- Phase A vendor rows have `identity_match: unchecked` because the earlier probe reference is keyed as `claude`, not the new `claude-vendor` arm. The result was not relabeled after the run.
