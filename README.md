# Shellmind

An agentic terminal that turns intent into safe, verified action.
Loop: **perceive → plan → act → verify → recover.**

## Run (100% local, via Ollama)

```bash
ollama pull qwen2.5-coder:7b     # ~4.7 GB, fits an RTX 4060 8 GB with room to spare
python shellmind.py
```

Python 3.8+, no pip packages. Ollama must be running (`ollama serve`, or the desktop app).

Env vars: `SHELLMIND_MODEL` (default `qwen2.5-coder:7b`), `OLLAMA_HOST`, `SHELLMIND_SHELL`.

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
