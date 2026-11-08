# geo-agent-bench

A descriptive experimental harness for studying agent web behavior, identity, tools and access. It is not yet a general benchmark of GEO effectiveness. GEO means Generative Engine Optimization. The harness compares three kinds of web surface while holding the underlying facts fixed: legacy HTML built for people, agent-readable sites with `llms.txt` and markdown copies, and agent-native interfaces built from MCP tools or identity-aware responses. The site runs on one small VM. A runner sends questions to an agent, the site writes its own request log, and a scorer checks each answer against exact ground truth and each claim against the log.

The project asks three groups of questions. First, how do agents and crawlers navigate the three surfaces? Second, do agents follow hidden instructions, ask for more data than the task needs, or cross a denied boundary? Third, can a site distinguish a named agent from a copycat with OAuth permissions and signed requests? Qwen and Claude ran as agents in the harness. Anthropic and OpenAI crawlers appeared in the public logs, and OpenAI hosted search was tested in a separate probe; those observations are not a three-agent comparison. The docs record where the real build differed from the plan. Every decision has an ADR, and every ADR lists what it gave up.

## Results at a glance

All results are descriptive. Most cells have one run, so read them as direction, not as measured size. [FINDINGS.md](FINDINGS.md) says what each result means and what it does not show.

| Part | What ran | Main result |
|---|---|---|
| Pilot | 60 runs, Claude Sonnet 5.5, 9-page site, `legacy` and `ai` | 60 of 60 correct. `llms.txt` was read in 6 of 30 `ai` runs and cost more tokens. No markdown copy was fetched. |
| Phase A | 36 runs, Claude and Qwen3-8B, 100 KB pages, planted files | Claude 24 of 24. Qwen 0 of 12 with direct fetch, 12 of 12 with fixed navigation. No planted file was hit. |
| Phase B and C | 54 runs, MCP tools, hidden tool text, signatures | Broad tools returned 18 to 28 times the bytes and gave the most correct runs. Claude followed a hidden tool-description sentence in 2 of 2 exposed runs. A spoofer with a valid token read the protected page. |
| Phase D | Interop test, signature gate, model-run OAuth | One verifier bug found and fixed. The gate blocked 6 of 6 spoofed or unsigned runs and passed 3 of 3 signed. Claude ran OAuth alone 3 of 3. Qwen 0 of 3. |
| Phase E | 101-page site, three variants, 24 initial runs plus an 8-run corrected rerun (32 total) | `legacy-l` 7 of 8, `ai-l` 7 of 8, `geo-l` 8 of 8 after one bug fix. `geo-l` used about one third of the input tokens on the 6 shared tasks. |
| Phase F | One probe of OpenAI's hosted web search | It fetches live as `ChatGPT-User`, from IPs in OpenAI's published file. A repeat page came from a cache. |
| Real visitors | The public log of the test sites | ClaudeBot and GPTBot took every page and every markdown copy. Named AI crawlers were about 4% of 3,170 stranger requests. |

## What this does not show

- One agent family for most runs, and one run per cell in Phase E. The intervals in the tables collapse when every run succeeds. A Wilson interval is a planned fix.
- The sites are fictional, so the model cannot know the answers. The tasks are mine, and I wrote the ground truth.
- Marketing value is not measured. Whether markdown for agents raises how often an engine cites a site needs a public, indexed site and weeks.
- Serving different content by identity has the shape of cloaking. I did not test how search engines treat it.

## Run it

You can run the tests and a local copy of the site without any account:

```bash
uv sync
uv run pytest -q                                   # 190 pass, 2 skipped
uv run geobench serve --default-variant legacy     # site on http://127.0.0.1:8000
```

A full benchmark run needs a domain in Cloudflare, one small VM, and an API key for each cloud agent. [deploy/README.md](deploy/README.md) lists the steps. Copy `.env.example` to `.env` and fill it in. Plans name the VM as `$GEOBENCH_VM`, so no real host name is in the repo.

## Where to read

| You want | Read |
|---|---|
| What the results mean, and their limits | [FINDINGS.md](FINDINGS.md) |
| The design, the web concepts (DNS, TLS, Caddy) and the plan-versus-result table | [docs/architecture.md](docs/architecture.md), last section |
| Why each decision was made, and what it gave up | [docs/adr.md](docs/adr.md) |
| Execution paths and failure behavior | [docs/logic_flow.md](docs/logic_flow.md) |
| Commands, file formats and interfaces | [docs/api.md](docs/api.md) |
| One run, step by step, as a diagram | [docs/visuals/exp-a-pilot-run-flow.html](docs/visuals/exp-a-pilot-run-flow.html) |
| The Phase A result tables | [reports/portfolio-phase-a/results.csv](reports/portfolio-phase-a/results.csv), [summary.csv](reports/portfolio-phase-a/summary.csv), [gaps.csv](reports/portfolio-phase-a/gaps.csv) |
| The Qwen robustness follow-up | [reports/phase-a-qwen-robust/results.csv](reports/phase-a-qwen-robust/results.csv), [summary.csv](reports/phase-a-qwen-robust/summary.csv), [gaps.csv](reports/phase-a-qwen-robust/gaps.csv) |
| The Phase B, C and E result tables | [reports/phase-b-claude/](reports/phase-b-claude/), [reports/phase-c/](reports/phase-c/), [reports/large-dry/](reports/large-dry/), [reports/large-dry-geo/](reports/large-dry-geo/) |
| How the site is deployed | [deploy/README.md](deploy/README.md) |

## Layout

```
geo-agent-bench/
├── docs/          design docs (architecture, ADRs, logic flow, API) and visuals
├── src/geobench/  server, site content, runner, log parser, metrics, agents, interceptor, probe
├── tasks/         tasks.yaml (small site) and tasks_large.yaml (101-page site): questions and ground truth
├── plans/         one YAML file per run: agents, variants, tasks, budget
├── reports/       result tables per plan (aggregates only, no visitor IPs)
├── deploy/        Caddyfile and systemd unit for the VM
├── FINDINGS.md    the seven findings and their limits
├── runs/          run and request logs (git-ignored)
└── tests/
```

## Known gaps

- `identity_reference.json` is on the laptop only. The VM server does not use it yet, so it cannot label verified agents.
- Phase A vendor rows have `identity_match: unchecked` because the earlier probe reference is keyed as `claude`, not the new `claude-vendor` arm. The result was not relabeled after the run.
