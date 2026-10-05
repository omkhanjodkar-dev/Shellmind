# Shellmind

An agentic terminal that turns intent into safe, verified action.
Loop: **perceive → plan → act → verify → recover.**

## Quick start (one command)

```bash
make            # installs/starts Ollama, pulls the model, installs deps, runs Shellmind
```

Every step is skipped if it is already done, so `make` is also how you launch it day to day.

| Command | What it does |
|---|---|
| `make` / `make run` | set up whatever is missing, then run |
| `make setup` | set up only, don't start the app |
| `make doctor` | show what is installed and what is missing |
| `make clean` | delete the `.venv` |
| `make MODEL=qwen3:8b` | use a different model (pulled automatically) |
| `make YES=1` | never ask before installing |

No `make` on Windows? Either `winget install ezwinports.make` (or `choco install make`), or skip it:
`run.bat` or `python bootstrap.py` do exactly the same thing.

What gets installed: Ollama (winget or the official installer on Windows, Homebrew on macOS, the
official script on Linux, which asks for sudo), the model, and a local `.venv` for `requirements.txt`
(only `pyreadline3` on Windows, for Tab completion). If `OLLAMA_HOST` points at a remote server that
is reachable, nothing is installed locally.

Env vars: `SHELLMIND_MODEL` (default `qwen2.5-coder:7b`), `OLLAMA_HOST`, `SHELLMIND_SHELL`, `SHELLMIND_TIMEOUT`.

Other models that fit 8 GB VRAM:

| Model | Notes |
|---|---|
| `qwen2.5-coder:7b` (default) | Best command/code accuracy for the size, fast |
| `qwen3:8b` | Stronger general reasoning, better for messy multi-step or non-English requests (thinking is turned off automatically) |
| `llama3.1:8b` | Solid all-rounder, weaker on PowerShell/cmd syntax |

Plans are forced into a JSON schema using Ollama's structured outputs, so small models
can't return malformed output. Safety rules still run locally on every command.

## Try

```
> find the 5 largest files in this folder and compress them
> kill whatever is running on port 8080
> undo my last commit but keep the changes
> :save cleanup      # store the last plan as a macro
> :run cleanup
```

## How each idea maps to code

| Feature | Where |
|---|---|
| Context (OS, WSL, shells, cwd, project type) | `build_context`, `available_shells`, `project_type` |
| Multi-step planning, per-step explanation, shell switching | `SYSTEM` prompt, `make_plan`, `execute_steps` |
| Safety: risk rules, dry-run preview, confirm / type `yes` for HIGH | `RULES`, `assess`, `confirm` |
| Self-correction (reads error, proposes fix, re-confirms, max 2 tries) | `propose_fix`, `execute_steps` |
| Learns you: next-command prediction + macros | `predict_next`, `~/.shellmind/history.jsonl`, `macros.json` |

## Design notes

- Typed commands get your real terminal (`input()`, `ssh`, `vim`, REPLs work). AI-planned steps are captured so errors can be diagnosed; if one turns out to need the keyboard, Shellmind offers to re-run it interactively.
- Tab completes paths (spaces handled with quotes) and `:commands`. Linux/macOS work out of the box; on Windows run `pip install pyreadline3`.
- Ctrl+C stops only the running command; `:quit` or Ctrl+D exits.
- Typed commands (`ls`, `git status`, `Get-ChildItem -Recurse`) run directly with no AI round trip; risky ones still ask first. Sentences go to the planner, and `?` forces the planner.
- Safety is enforced **locally** by regex rules; the model cannot bypass it. Dry-run
  commands suggested by the model are only auto-run if they pass the same check.
- Nothing runs without confirmation. HIGH-risk steps need a typed `yes`.
- `cd` is handled in-process so the working directory persists between steps.

## Ideas to extend

- Replace the bigram predictor with a small n-gram/embedding model per project.
- Stream output live instead of capturing it.
- Add parameterised macros (`:run deploy env=staging`).
- Add a sandbox/undo layer (git stash or filesystem snapshot before risky steps).
