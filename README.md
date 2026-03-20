<div align="center">

# RepoFix

**Clone any GitHub repo, detect the stack, install dependencies, run it, and recover from failures — without a three-hour README scavenger hunt.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)]()

[Installation](#installation) · [Quick start](#quick-start) · [Examples](#examples) · [Bring your own LLM](#bring-your-own-llm) · [CLI reference](#cli-reference) · [Contributing](#contributing)

</div>

---

## Why RepoFix exists

Every developer knows the loop: star a repo → clone → guess the Node version → fight `pnpm` vs `yarn` → discover a missing `.env` → port already in use → give up. READMEs are written for humans, not machines — and **machines are better at grinding through setup errors than you are at 11pm.**

RepoFix automates that grind: **one command** takes you from a URL (or local path) to a running app, with **rule-based fixes**, **optional on-device LLM**, and **optional cloud LLM fallbacks** (Gemini, OpenAI, Anthropic) when heuristics run out. It remembers what worked so the *next* repo costs less.

### Pain points we optimize for

| You’ve been here before | What RepoFix does |
|------------------------|-------------------|
| “Works on my machine” in the README, not on yours | Detects stack and picks install/run commands from manifests (`package.json`, `Makefile`, `Procfile`, `docker-compose`, README hints) |
| Cryptic errors after install | Classifies failures, applies safe fixes (deps, ports, env), retries |
| Novel or project-specific failures | Escalates to local LLM and/or cloud APIs, with provider fallback |
| Re-cloning the same repo for every branch | **Branch cache** — reuses installed environments per branch |
| Long-running dev servers you lose track of | **`repofix ps` / `logs` / `stop` / `start` / `restart`** — process registry with log files |
| Global `npm install -g` in a README | Prompts for **isolated** vs system install; documents where binaries land |

---

## Features

- **One command to run a repo** — GitHub URL or local directory.
- **Multi-stack stack detection** — Node, Python, Go, Rust, Java/Kotlin, PHP, Ruby, Docker / Compose, and more.
- **Command discovery** — `package.json`, `Makefile`, `Procfile`, `docker-compose.yml`, README-driven deploy hints.
- **Self-healing pipeline** — Retries with fixes for common classes of failures (missing dependencies, ports, environment).
- **AI layer (optional)** — On-device **Qwen2.5-Coder-3B** via `llama-cpp-python`, plus **Gemini / OpenAI / Anthropic** with automatic fallback when configured.
- **Bring your own LLM** — Pin **any model id** each vendor supports, or point the OpenAI integration at an **OpenAI-compatible** base URL (local gateway, proxy, or enterprise endpoint); see [Bring your own LLM](#bring-your-own-llm).
- **Fix memory** — Persists successful fixes and run history for faster repeat runs.
- **Safety controls** — Command allow/block lists and **assist mode** for approval before applying fixes.
- **Deploy mode hints** — When a README describes both self-hosted and local dev paths, RepoFix can prompt (or use **`--prod`** / **`--dev`** to skip).
- **Release binaries** — Optional install from GitHub Releases when available (**`--binary`** / **`--source`**).
- **Branch environment cache** — `repofix branches`, `repofix branch-clean`.
- **Process lifecycle** — `ps`, `logs`, `stop`, `start`, `restart` with persisted metadata and log paths.

Default clone and config locations: **`~/.repofix/repos/`**, **`~/.repofix/config.toml`**, local model under **`~/.repofix/models/`**.

---

## Installation

**Requirements:** Python **3.10+**, **`git`** on your `PATH`. For Docker-based projects, Docker must be available when the stack needs it.

```bash
pip install repofix
```

**Install from source** (contributors):

```bash
git clone https://github.com/YOUR_ORG/repofix.git
cd repofix
pip install -e ".[dev]"
```

---

## Quick start

```bash
# Run from a GitHub URL (first run may prompt for local LLM + optional cloud API key)
repofix run https://github.com/user/repo

# Specific branch
repofix run https://github.com/user/repo --branch develop

# Local checkout
repofix run ./my-project

# Safer: confirm each fix before applying
repofix run https://github.com/user/repo --mode assist

# Inject env vars from a file
repofix run https://github.com/user/repo --env-file ./secrets.env
```

### First-run setup

On the first `repofix run`, you may be offered:

1. **On-device AI** — Installs `llama-cpp-python` (prebuilt wheel when available) and downloads **~2 GB** model weights to `~/.repofix/models/`. You can skip and rely on cloud keys only, or disable later via `repofix config`.
2. **Cloud API key (optional)** — Gemini, OpenAI, or Anthropic for harder errors.

Manage these anytime:

```bash
repofix config show
repofix config set-key --provider gemini
repofix config set-default --local-llm    # or --no-local-llm
repofix model download                  # pre-fetch model before going offline
repofix model status
```

---

## Examples

```bash
# Prefer production/self-hosting path from README (skip prompt)
repofix run https://github.com/org/app --prod

# Force local development path
repofix run https://github.com/org/app --dev

# Use a GitHub Release binary when detected (skip source build)
repofix run https://github.com/org/cli-tool --binary

# Always build from source
repofix run https://github.com/org/cli-tool --source

# Override port and cap retry cycles
repofix run https://github.com/org/api --port 8080 --retries 8

# No automated fixes (observe raw errors)
repofix run ./local-service --no-fix

# CI / non-interactive: skip confirmation prompts
repofix run https://github.com/org/app --auto-approve

# Override install or run commands explicitly
repofix run ./legacy-app --install "pip install -r requirements.txt" --command "uvicorn main:app --reload"
```

### After the app is running

```bash
repofix ps                          # List processes started by RepoFix
repofix logs my-app --lines 100     # Tail log file
repofix logs my-app --follow        # Stream logs
repofix stop my-app                 # Graceful stop (SIGTERM)
repofix stop my-app --force         # SIGKILL
repofix start                       # Resume last stopped process
repofix start my-app
repofix restart my-app              # Stop + relaunch same command (no reinstall)
```

### Branch cache

```bash
repofix branches                              # List cached branch environments
repofix branches https://github.com/user/app
repofix branch-clean https://github.com/user/app --branch feature-x
repofix branch-clean --yes                    # Clear all branch caches (destructive)
```

---

## Execution modes

| Mode | Behaviour |
|------|-----------|
| `auto` (default) | Applies fixes automatically within safety rules |
| `assist` | Prompts before applying each fix |
| `debug` | Verbose output for detection and fix decisions |

---

## CLI reference

### `repofix run`

```
repofix run <REPO> [OPTIONS]

Arguments:
  REPO                     GitHub URL or local path

Options:
  -b, --branch TEXT        Git branch
  -m, --mode TEXT          auto | assist | debug (default: auto)
  -p, --port INT           Override listening port
  -r, --retries INT        Max fix/retry cycles (default: from config, else 5)
  -e, --env-file PATH      .env file to load
  --no-fix                 Disable automated fixes
  --auto-approve           Skip confirmation prompts (e.g. npm global install choice)
  -c, --command TEXT       Override run command
  -i, --install TEXT       Override install command
  --binary                 Prefer prebuilt release binary when available
  --source                 Always run from source (skip binary)
  --prod                   Use production/self-hosting path when README offers both
  --dev                    Use local development path when README offers both
```

### Other commands

| Command | Purpose |
|---------|---------|
| `repofix config show` | Current settings (keys shown as set/not set) |
| `repofix config set-key [--provider gemini\|openai\|anthropic]` | Store API key |
| `repofix config set-default ...` | Defaults: AI provider, models, `OPENAI_BASE_URL`, retries, local LLM, etc. |
| `repofix history [--limit N]` | Recent runs and fixes |
| `repofix clear-memory [--yes]` | Wipe fix memory + run history |
| `repofix ps` | Processes + PIDs + log paths |
| `repofix logs <name> [--lines N] [--follow]` | Show or follow logs |
| `repofix stop <name> [--force]` | Stop a process |
| `repofix start [name]` | Start stopped/crashed process (last one if name omitted) |
| `repofix restart <name>` | Stop + start same command |
| `repofix branches [repo]` | List cached branch environments |
| `repofix branch-clean [repo] [--branch B] [--yes]` | Clear branch cache |
| `repofix model download` | Install local LLM deps + download weights |
| `repofix model status` | Local model status |
| `repofix model remove [--yes]` | Delete local weights (~2 GB) |

Run `repofix --help` and `repofix <command> --help` for the full Typer-generated help.

---

## Configuration

- **File:** `~/.repofix/config.toml`
- **Environment variables:** `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, optional `OPENAI_BASE_URL` for OpenAI-compatible endpoints.

Provider preference (e.g. try Gemini then OpenAI vs a single primary) is configurable via `repofix config set-default` (`--ai-provider`, `--ai-fallback` / `--no-ai-fallback`).

### Bring your own LLM

RepoFix does not lock you to one cloud model or one vendor.

**Per-provider model ids** — Set whichever checkpoint your org uses (as long as the provider’s API accepts that id):

```bash
repofix config set-default \
  --gemini-model gemini-2.0-flash-lite \
  --openai-model gpt-4o-mini \
  --anthropic-model claude-3-5-haiku-20241022
```

**OpenAI-compatible endpoints** — The OpenAI path uses the chat-completions JSON shape. You can aim it at a **local or self-hosted** server (Ollama’s OpenAI shim, LM Studio, vLLM, LiteLLM, many corporate gateways) by setting a base URL. Env var wins over config:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:11434/v1"   # example: Ollama
export OPENAI_API_KEY="ollama"                        # required field; use dummy if the server ignores it
repofix config set-default --openai-model llama3.2 --ai-provider openai
```

Or persist in config:

```bash
repofix config set-default --openai-base-url http://127.0.0.1:11434/v1 --openai-model your-model-name
```

Pin the provider so traffic doesn’t bounce to Gemini/Anthropic when you intend to use only that gateway: `--ai-provider openai` (and disable cross-provider fallback with `--no-ai-fallback` if you want a single stack).

**On-device default** — The bundled local weights are fixed to **Qwen2.5-Coder-3B** (GGUF) for a consistent offline path; custom GGUF paths are not wired in config today—use **cloud or OpenAI-compatible** for a fully custom model choice there.


---

## Supported stacks (high level)

| Ecosystem | Notes |
|-----------|--------|
| **Node.js** | Next.js, React, Express, Vue, Angular, NestJS, etc. |
| **Python** | FastAPI, Flask, Django, Streamlit, etc. |
| **Go** | `go run` / module workflows |
| **Rust** | Cargo |
| **Java / Kotlin** | Maven, Gradle |
| **PHP** | Composer, Laravel |
| **Ruby** | Rails, Sinatra |
| **Docker** | Dockerfile, docker-compose |

Exact behaviour depends on project layout and detection heuristics. If something mis-detects, **`--install`** / **`--command`** overrides are the escape hatch.

---

## How it works (short)

1. Resolve Git URL or local path → clone or validate.
2. Detect language, framework, and entry commands.
3. Resolve environment (e.g. `.env.example` hints, missing vars).
4. Run install → build → run with live log streaming.
5. On failure: classify → rule-based fix → retry up to limit.
6. If still stuck: local and/or cloud AI suggests next steps (JSON-structured, safety-filtered).
7. Record outcome in fix memory; register long-lived processes for `ps` / `logs` / lifecycle commands.

---

## Security & privacy

- RepoFix **executes shell commands** in your checked-out repos. Use **`assist` mode** or **`--no-fix`** when you don’t want automatic remediation.
- **Cloud AI** sends error context and project snippets to the provider you configure — use **local-only** mode if that’s unacceptable for your org.
- Review **`repofix fixing/safety.py`** and the allow/block behaviour before running on untrusted codebases.
- **`sudo` global npm installs** are never silent; they require explicit user choice.

---

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

---

## Contributing

We welcome issues and PRs. A few norms:

1. **Open an issue first** for large features (detection rules, new stack support, AI behaviour) so we align on design.
2. **Small, focused PRs** — one logical change per branch; match existing style (`ruff`, type hints where used).
3. **Tests** — Add or extend tests under `tests/` for detection, fixing, safety, or CLI behaviour you change.
4. **No drive-by refactors** — Keep diffs tight; unrelated formatting churn makes review harder.
5. **Document user-visible flags** — If you add CLI options, update this README and Typer help strings.

**License:** MIT — see [LICENSE](LICENSE).

---

## Support

- **Bug reports & feature requests:** [GitHub Issues](https://github.com/YOUR_ORG/repofix/issues) *(replace with your repo URL)*  
- **Questions & show-and-tell:** [GitHub Discussions](https://github.com/YOUR_ORG/repofix/discussions) *(optional)*

When reporting bugs, include: OS, Python version, RepoFix version, the **repo URL or stack**, full **`--mode debug`** output (redact secrets), and whether local LLM / cloud AI was enabled.

---

## Roadmap & status

Project status is **alpha** (`Development Status :: 3 - Alpha` on PyPI). Expect breaking CLI or config changes until **v1.0**. Priorities include broader stack coverage, sharper safety defaults, and more regression tests for real-world READMEs.

---

<div align="center">

**RepoFix — paste the URL. Run the code.**

MIT License · Built for developers who’d rather ship than configure.

</div>
