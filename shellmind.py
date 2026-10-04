#!/usr/bin/env python3
"""
Shellmind - an agentic terminal: perceive -> plan -> act -> verify -> recover.

Say what you want in plain English (any language). Shellmind detects your
environment, plans one or more commands, flags dangerous ones, dry-runs where
possible, asks before acting, reads errors and proposes fixes, predicts your
next command and lets you save workflows as macros.

Zero third-party dependencies. Runs fully local through Ollama (no API key, no cloud).
"""
import atexit
import getpass
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

try:
    import readline  # Linux/macOS built in; on Windows: pip install pyreadline3
except ImportError:
    readline = None

# Good fit for an 8 GB GPU (RTX 4060): ~4.7 GB at Q4, leaves room for the KV cache.
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("SHELLMIND_MODEL", "qwen2.5-coder:7b")
HOME = Path.home() / ".shellmind"
HISTORY = HOME / "history.jsonl"
MACROS = HOME / "macros.json"
MAX_FIX_ATTEMPTS = 2
RUN_TIMEOUT = int(os.environ.get("SHELLMIND_TIMEOUT", "600"))  # seconds; Ctrl+C works any time

# ---------------------------------------------------------------- UI helpers
if os.name == "nt":
    os.system("")  # enables ANSI escape codes on Windows 10+ consoles


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def dim(t): return c(t, "2")
def bold(t): return c(t, "1")
def red(t): return c(t, "31")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def cyan(t): return c(t, "36")


# ------------------------------------------------------------------- context
def is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def available_shells():
    shells = []
    if platform.system() == "Windows":
        if shutil.which("pwsh") or shutil.which("powershell"):
            shells.append("powershell")
        shells.append("cmd")
        if shutil.which("wsl"):
            shells.append("wsl")
    else:
        shells.append("bash" if shutil.which("bash") else "sh")
        if shutil.which("pwsh"):
            shells.append("powershell")
    return shells


def project_type(cwd):
    markers = {
        "package.json": "Node.js", "pyproject.toml": "Python", "requirements.txt": "Python",
        "go.mod": "Go", "Cargo.toml": "Rust", "CMakeLists.txt": "C/C++ (CMake)",
        "pom.xml": "Java (Maven)", "build.gradle": "Java/Kotlin (Gradle)",
        "pubspec.yaml": "Flutter/Dart", "Dockerfile": "Docker",
    }
    found = [v for k, v in markers.items() if (Path(cwd) / k).exists()]
    if (Path(cwd) / ".git").exists():
        found.append("git repo")
    return sorted(set(found)) or ["unknown"]


def common_dirs():
    """Real Desktop/Documents/Downloads paths (Windows often redirects them into OneDrive)."""
    home, out = Path.home(), {}
    for name in ("Desktop", "Documents", "Downloads"):
        for base in (home, home / "OneDrive"):
            if (base / name).is_dir():
                out[name] = str(base / name)
                break
    return out


def build_context(state):
    try:
        listing = sorted(os.listdir(state["cwd"]))[:40]
    except OSError:
        listing = []
    return {
        "os": f"{platform.system()} {platform.release()}",
        "inside_wsl": is_wsl(),
        "username": getpass.getuser(),
        "home": str(Path.home()),
        "common_dirs": common_dirs(),
        "recent_activity": state.get("turns", [])[-4:],
        "available_shells": available_shells(),
        "default_shell": state["shell"],
        "cwd": state["cwd"],
        "project_type": project_type(state["cwd"]),
        "dir_listing": listing,
    }


# ----------------------------------------------------------------- LLM layer
SYSTEM = """You are Shellmind, the planning brain of an agentic terminal.
Turn the user's request (any language) into a minimal, correct sequence of shell commands.

Rules:
- Use ONLY shells listed in available_shells. Prefer default_shell; switch shell per step only when the task needs it (e.g. use "wsl" for a Linux-only tool on Windows).
- Commands must be valid for that exact shell's syntax (PowerShell != cmd != bash).
- Prefer safe, non-destructive approaches. If a destructive step is unavoidable, include a "dry_run" command that previews its effect without changing anything (e.g. `rsync -n`, `git clean -n`, `ls`/`Get-ChildItem` on the same glob, `-WhatIf` in PowerShell).
- Use the real paths in context (home, username, common_dirs). NEVER output placeholders such as YourUsername, <username> or <path>. For paths under the user's home, use common_dirs, ~, $HOME or $env:USERPROFILE.
- If the user is unsure where something is, do not guess a path: emit a search command (e.g. `Get-ChildItem -Path $HOME -Directory -Recurse -Filter name -ErrorAction SilentlyContinue`, `find ~ -type d -name name`).
- Never invent files that are not in dir_listing unless the user named them.
- Every step runs in a FRESH process: `cd`/`Set-Location` in one step does not carry to the next. Prefer explicit paths inside a single command (e.g. `Get-ChildItem -Path 'C:\\lighter' -Recurse`, `ls -R /some/dir`) instead of a separate cd step. Use a standalone cd step only when the user explicitly asks to change directory.
- recent_activity lists the user's previous requests, the commands that ran and their output. Resolve references like "it", "there", "that folder" from it, and honor hints such as "it must be in SY".
- When asked where something is, search from cwd first (the user may have just navigated there). Do not jump to a guessed folder like Documents; the app offers to widen the search if nothing is found.
- PowerShell: -Filter accepts ONE pattern only. For several extensions use -Include together with -Recurse, e.g. Get-ChildItem -Path . -Recurse -Include *.asm,*.s -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
- Keep steps few. Each step gets a one-sentence "explanation".
- If the request is ambiguous or impossible, return no steps and put your question in "summary".

Respond with ONLY JSON matching this shape:
{"summary": "one line of what will happen",
 "steps": [{"shell": "bash|sh|powershell|cmd|wsl", "command": "...", "explanation": "...", "dry_run": "optional command or empty string"}]}

Example (default_shell = powershell):
Request: kill whatever is on port 8080
{"summary": "Find the process listening on port 8080 and stop it.",
 "steps": [{"shell": "powershell", "command": "Stop-Process -Id (Get-NetTCPConnection -LocalPort 8080 -State Listen).OwningProcess -Force", "explanation": "Looks up the PID that owns port 8080 and force-stops it.", "dry_run": "Get-NetTCPConnection -LocalPort 8080 -State Listen | Select LocalPort,OwningProcess"}]}"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "shell": {"type": "string", "enum": ["bash", "sh", "powershell", "cmd", "wsl"]},
                "command": {"type": "string"},
                "explanation": {"type": "string"},
                "dry_run": {"type": "string"}},
            "required": ["shell", "command", "explanation", "dry_run"]}}},
    "required": ["summary", "steps"]}


def llm(system, messages):
    """Chat with a local Ollama model; the JSON schema forces well-formed plans."""
    payload = {
        "model": MODEL, "stream": False, "format": PLAN_SCHEMA, "keep_alive": "10m",
        "options": {"temperature": 0.1, "num_ctx": 8192},
        "messages": [{"role": "system", "content": system}] + messages,
    }
    if "qwen3" in MODEL:
        payload["think"] = False  # skip slow reasoning traces for a snappy shell
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=json.dumps(payload).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 404:
            raise RuntimeError(f"model '{MODEL}' not found. Run: ollama pull {MODEL}")
        raise RuntimeError(f"Ollama error {e.code}: {detail}")
    except urllib.error.URLError:
        raise RuntimeError(f"cannot reach Ollama at {OLLAMA_URL}. Is it running? (ollama serve)")


PLACEHOLDER_USER = re.compile(r"<?\[?your[_\- ]?user(name)?\]?>?|<user(name)?>|\[user(name)?\]|username_here", re.I)
LEFTOVER = re.compile(r"<[A-Za-z_ -]{2,30}>")


def fix_placeholders(cmd):
    return PLACEHOLDER_USER.sub(getpass.getuser(), cmd)


def parse_plan(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model returned no JSON")
    plan = json.loads(text[start:end + 1])
    steps = []
    for s in plan.get("steps", []):
        if isinstance(s, dict) and s.get("command"):
            steps.append({"shell": s.get("shell", "bash"),
                          "command": fix_placeholders(s["command"].strip()),
                          "explanation": s.get("explanation", ""),
                          "dry_run": fix_placeholders((s.get("dry_run") or "").strip())})
    return {"summary": plan.get("summary", ""), "steps": steps}


def make_plan(request, ctx):
    msg = f"Context:\n{json.dumps(ctx, indent=2)}\n\nRequest: {request}"
    return parse_plan(llm(SYSTEM, [{"role": "user", "content": msg}]))


def propose_fix(step, rc, output, ctx, reason="failed"):
    head = f"Context:\n{json.dumps(ctx, indent=2)}\n\n"
    cmd_info = f"shell: {step['shell']}\ncommand: {step['command']}\n"
    if reason == "no_results":
        msg = (head + "This search ran fine but found NOTHING.\n" + cmd_info +
               "Widen the search: parent folders, the home directory, common_dirs, other drives, "
               "alternative names or extensions. Do NOT repeat the same location. "
               "Say in 'summary' what you are widening to.")
    else:
        msg = (head + "This command FAILED.\n" + cmd_info + f"exit code: {rc}\n"
               f"output (tail):\n{output[-3000:]}\n\n"
               "Diagnose the cause in 'summary' (one or two sentences) and give a corrected "
               "step list. If it cannot be fixed by a command, return no steps.")
    return parse_plan(llm(SYSTEM, [{"role": "user", "content": msg}]))


# -------------------------------------------------------------- safety layer
RULES = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive)\b", "HIGH", "recursive force delete"),
    (r"\brm\s+-[a-z]*r", "MED", "recursive delete"),
    (r"remove-item\b.*-recurse", "HIGH", "recursive delete (PowerShell)"),
    (r"\b(del|erase)\b.*\s/[sq]\b", "HIGH", "bulk delete (cmd)"),
    (r"\brmdir\b.*\s/s\b|\brd\b.*\s/s\b", "HIGH", "recursive directory removal (cmd)"),
    (r"git\s+push\b.*(--force|-f\b)", "HIGH", "force push rewrites remote history"),
    (r"git\s+reset\s+--hard", "HIGH", "discards uncommitted changes"),
    (r"git\s+clean\b.*-[a-z]*f", "HIGH", "deletes untracked files"),
    (r"git\s+(checkout|restore)\s+(--\s+)?\.", "MED", "discards working-tree changes"),
    (r"\bdd\b.*\bof=", "HIGH", "raw disk write"),
    (r"\bmkfs(\.\w+)?\b|\bformat\s+[a-z]:", "HIGH", "formats a disk"),
    (r">\s*/dev/(sd|nvme|disk)", "HIGH", "writes directly to a block device"),
    (r"\bchmod\s+-r\b.*\b777\b|\bchmod\b.*\b777\b", "MED", "world-writable permissions"),
    (r"\bchown\s+-r\b", "MED", "recursive ownership change"),
    (r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "HIGH", "pipes remote script into a shell"),
    (r"\b(shutdown|reboot|halt|poweroff)\b|stop-computer|restart-computer", "MED", "powers off or reboots"),
    (r"\bkill(all)?\s+(-9|-kill)\b|\btaskkill\b.*/f|stop-process\b.*-force", "MED", "force-kills processes"),
    (r"drop\s+(table|database)|truncate\s+table", "HIGH", "destroys database data"),
    (r"\bsudo\b|\brunas\b", "MED", "elevated privileges"),
    (r":\(\)\s*\{.*\};\s*:", "HIGH", "fork bomb"),
    (r"(^|\s)(/|~|c:\\|\$home)\s*$", "MED", "operates on a root/home path"),
]


def assess(command):
    low = command.lower()
    flags = [(lvl, why) for pat, lvl, why in RULES if re.search(pat, low)]
    level = "HIGH" if any(l == "HIGH" for l, _ in flags) else ("MED" if flags else "LOW")
    return level, [why for _, why in flags]


# ------------------------------------------------------------------ execution
def argv_for(shell, cmd):
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-Command", cmd]
    if shell == "cmd":
        return ["cmd", "/c", cmd]
    if shell == "wsl":
        return ["wsl", "-e", "bash", "-lc", cmd]
    return [shutil.which("bash") or "sh", "-c", cmd]


def kill_tree(p):
    """Stop the command and everything it spawned."""
    if p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        p.kill()


TTY_TOOLS = {"ssh", "vim", "vi", "nvim", "nano", "emacs", "less", "more", "top", "htop", "man",
             "ipython", "irb", "mysql", "psql", "sqlite3", "sftp", "ftp", "telnet", "gdb", "lldb"}
NEEDS_INPUT = re.compile(r"EOFError|EOF when reading|not a tty|input device is not|stdin is not", re.I)


def needs_terminal(cmd):
    """Programs that need the real keyboard/screen (editors, REPLs, ssh...)."""
    toks = cmd.split()
    if not toks:
        return False
    exe = re.sub(r"\.exe$", "", os.path.basename(toks[0].strip("\"'")).lower())
    if exe in TTY_TOOLS:
        return True
    return exe in ("python", "python3", "py", "node") and (len(toks) == 1 or "-i" in toks[1:])


def run_terminal(shell, cmd, cwd):
    """Hand the real terminal to the command (keyboard input, colors, TUIs all work)."""
    try:
        p = subprocess.Popen(argv_for(shell, cmd), cwd=cwd)   # inherits stdin/stdout/stderr
    except FileNotFoundError as e:
        return 127, f"shell not found: {e}"
    try:
        while p.poll() is None:
            time.sleep(0.05)
        return p.returncode, ""
    except KeyboardInterrupt:
        # Ctrl+C already reached the child (same console); give it a moment, then force it.
        deadline = time.time() + 3
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else:
                p.terminate()
                try:
                    p.wait(2)
                except subprocess.TimeoutExpired:
                    p.kill()
        return 130, ""


def run(shell, cmd, cwd, timeout=None, stream=False, terminal=False):
    """Run a command, optionally streaming output live. Ctrl+C stops ONLY the command
    (returns 130); Shellmind itself keeps running."""
    if terminal:
        return run_terminal(shell, cmd, cwd)
    timeout = timeout or RUN_TIMEOUT
    flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    try:
        p = subprocess.Popen(argv_for(shell, cmd), cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1, **flags)
    except FileNotFoundError as e:
        return 127, f"shell not found: {e}"
    chunks = []

    def pump():
        for line in p.stdout:
            chunks.append(line)
            if stream:
                print(dim("  " + line.rstrip("\n")), flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    try:
        # poll with sleep(): unlike p.wait(), it is interruptible by Ctrl+C on Windows too
        while p.poll() is None:
            if time.time() > deadline:
                kill_tree(p)
                reader.join(2)
                return 124, "".join(chunks) + f"\ntimed out after {timeout}s"
            time.sleep(0.1)
        reader.join(5)
        return p.returncode, "".join(chunks)
    except KeyboardInterrupt:
        kill_tree(p)
        reader.join(2)
        return 130, "".join(chunks)


CD_RE = re.compile(
    r"""^\s*(?:cd|chdir|sl|set-location|pushd|push-location)\s+(?:/d\s+)?"""
    r"""(?:-(?:literalpath|path)\s+)?(?P<p>"[^"]+"|'[^']+'|[^\s;&|]+)\s*$""", re.I)


SKIP_DIRS = {"node_modules", "appdata", "$recycle.bin", "windows", "program files",
             "program files (x86)", "__pycache__", "venv", "site-packages"}


def find_dir_candidates(target, state, max_depth=4, budget=4.0):
    """The path was wrong: look for a directory with that name near where users keep things."""
    parts = [x.lower() for x in Path(target).parts[-3:]]
    name = parts[-1] if parts else ""
    roots = list(common_dirs().values()) + [state["cwd"], str(Path.home())]
    deadline, hits, seen_roots = time.time() + budget, [], set()
    for root in roots:
        if root in seen_roots or not os.path.isdir(root):
            continue
        seen_roots.add(root)
        base = root.rstrip("\\/").count(os.sep)
        for dp, dns, _ in os.walk(root):
            if time.time() > deadline:
                break
            depth = dp.count(os.sep) - base
            dns[:] = [d for d in dns if depth < max_depth and not d.startswith(".")
                      and d.lower() not in SKIP_DIRS]
            if os.path.basename(dp).lower() == name and dp not in hits:
                hits.append(dp)

    def score(path):
        have = [x.lower() for x in Path(path).parts]
        return -sum(1 for a, b in zip(reversed(parts), reversed(have)) if a == b)
    return sorted(hits, key=lambda h: (score(h), len(h)))[:5]


def try_cd(cmd, state):
    """Every step runs in a fresh process, so directory changes must be applied
    in-process. Returns None (not a cd), True (moved) or False (failed)."""
    m = CD_RE.match(cmd)
    if not m:
        return None
    raw = m.group("p").strip("\"'")
    target = Path(os.path.expanduser(raw))
    if not target.is_absolute():
        target = Path(state["cwd"]) / target
    if not target.is_dir():
        print(red(f"  no such directory: {target}"))
        print(dim("  searching nearby..."))
        cands = find_dir_candidates(target, state)
        if not cands:
            return False
        for n, cnd in enumerate(cands, 1):
            print(f"  {bold(str(n))}) {cnd}")
        pick = input("  go to which? [number, Enter to cancel] ").strip()
        if not (pick.isdigit() and 1 <= int(pick) <= len(cands)):
            return False
        target = Path(cands[int(pick) - 1])
    state["cwd"] = str(target.resolve())
    print(green(f"  cwd -> {state['cwd']}"))
    return True


# --------------------------------------------------------- history & learning
def log_history(cmd, shell, cwd):
    HOME.mkdir(exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"cmd": cmd, "shell": shell, "cwd": cwd, "ts": time.time()}) + "\n")


def load_history():
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def predict_next(last_cmd, project):
    """Bigram model over past successful commands; prefers same-project history."""
    hist = load_history()
    table = defaultdict(Counter)
    for a, b in zip(hist, hist[1:]):
        w = 2 if project and project in a.get("cwd", "") else 1
        table[a["cmd"]][(b["cmd"], b["shell"])] += w
    if last_cmd in table:
        (cmd, shell), n = table[last_cmd].most_common(1)[0]
        return {"cmd": cmd, "shell": shell, "count": n}
    return None


def load_macros():
    return json.loads(MACROS.read_text(encoding="utf-8")) if MACROS.exists() else {}


def save_macros(m):
    HOME.mkdir(exist_ok=True)
    MACROS.write_text(json.dumps(m, indent=2), encoding="utf-8")


# ------------------------------------------------------ command-or-sentence?
BUILTINS = {
    "ls", "dir", "cd", "pwd", "cat", "type", "echo", "cp", "copy", "mv", "move", "rm", "del",
    "mkdir", "md", "rmdir", "rd", "cls", "clear", "gci", "gc", "sl", "ni", "ri", "man", "history",
    "export", "source", "alias", "touch", "exit", "set", "where", "which", "tree", "ren", "rename",
    "start", "ps", "kill", "sort", "find", "findstr", "head", "tail", "grep", "chmod", "sudo",
}
STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "from", "with", "and", "or", "my",
    "me", "this", "that", "these", "those", "is", "are", "all", "any", "it", "its", "into",
    "which", "what", "whatever", "where", "how", "please", "i", "you", "then", "also", "so",
    "can", "should", "will", "do", "does", "not", "but", "if", "than", "using", "named",
}


def looks_like_command(line):
    """True when the line is probably something you'd type into a shell, not a request."""
    parts = line.split(None, 1)
    if not parts:
        return False
    first = parts[0].strip("\"'")
    known = (first.lower() in BUILTINS
             or shutil.which(first) is not None
             or re.fullmatch(r"[A-Za-z]+-[A-Za-z]+", first) is not None      # PowerShell cmdlet
             or first.startswith((".\\", "./", "$", "&", "~", "/"))
             or re.match(r"^[A-Za-z]:[\\/]", first) is not None)
    if not known:
        return False
    rest = re.sub(r"""(["']).*?\1""", "", parts[1]) if len(parts) > 1 else ""
    words = [w for w in rest.split() if re.fullmatch(r"[A-Za-z']+", w)]
    if any(w.lower() in STOPWORDS for w in words):
        return False
    return len(words) < 4


SEARCHY = re.compile(r"-(filter|include|recurse)\b|\bfind\b.*-name|\bgrep\b|select-string"
                     r"|\bfindstr\b|\blocate\b|\bwhere\b", re.I)


def remember_turn(state, request):
    ran = state.get("ran", [])[-3:]
    state.setdefault("turns", []).append({"user_request": request, "commands_run": ran})
    state["turns"] = state["turns"][-5:]
    state["ran"] = []


# ------------------------------------------------------------------ the agent
def confirm(level):
    if level == "HIGH":
        return input(red("  HIGH RISK - type 'yes' to run, anything else to cancel: ")).strip().lower() == "yes"
    ans = input(f"  Run? [{green('y')}/n/e(dit)/s(kip)] ").strip().lower()
    return ans if ans in ("e", "s") else ans in ("y", "yes")


def execute_steps(steps, state, ctx_fn, depth=0):
    """Act + verify + recover. Returns True when every step succeeded."""
    for i, step in enumerate(steps, 1):
        shell, cmd = step["shell"], step["command"]
        if shell not in available_shells() + ["sh"]:
            print(yellow(f"  shell '{shell}' unavailable, using {state['shell']}"))
            shell = step["shell"] = state["shell"]
        level, why = assess(cmd)
        badge = {"LOW": green("safe"), "MED": yellow("caution"), "HIGH": red("DANGEROUS")}[level]
        quiet = step.get("direct") and level == "LOW"
        if not quiet:
            print(f"\n{bold(f'[{i}/{len(steps)}]')} {step['explanation']}")
            print(f"  {cyan(shell + ' $')} {bold(cmd)}   [{badge}]")
        for w in why:
            print(red(f"    ! {w}"))
        if LEFTOVER.search(cmd):
            print(yellow("    ! command looks like it contains a placeholder - consider (e)dit"))

        if step.get("dry_run") and level != "LOW":
            dl, _ = assess(step["dry_run"])
            if dl == "LOW":
                print(dim(f"  dry run: {step['dry_run']}"))
                _, out = run(shell, step["dry_run"], state["cwd"])
                print(dim("  " + (out.strip()[:1500] or "(no output)").replace("\n", "\n  ")))

        decision = True if quiet else confirm(level)
        if decision == "e":
            cmd = step["command"] = input("  edit> ").strip() or cmd
            level, _ = assess(cmd)
            decision = confirm(level)
        if decision == "s":
            continue
        if not decision:
            print(dim("  cancelled."))
            return False
        moved = try_cd(cmd, state)
        if moved is False:
            print(red("  aborting remaining steps: running them in the wrong directory is unsafe."))
            return False
        if moved:
            log_history(cmd, shell, state["cwd"])
            continue

        terminal = bool(step.get("direct")) or needs_terminal(cmd)
        if not terminal:
            print(dim("  (Ctrl+C stops this command)"))
        rc, out = run(shell, cmd, state["cwd"], stream=True, terminal=terminal)
        state.setdefault("ran", []).append(
            {"cmd": cmd, "exit": rc, "output_head": out.strip()[:200] or (
                "(ran in terminal, output not captured)" if terminal else "(no output)")})
        if rc == 130:
            print(yellow("  stopped. Shellmind is still running."))
            return False
        if terminal and rc != 0:
            print(red(f"  exit {rc}"))
            return False
        if rc == 0 and not out.strip() and not terminal and SEARCHY.search(cmd):
            print(yellow("  no matches."))
            if depth < MAX_FIX_ATTEMPTS and input("  widen the search? [Y/n] ").strip().lower() != "n":
                try:
                    wider = propose_fix(step, rc, "", ctx_fn(), reason="no_results")
                except Exception as e:
                    print(red(f"  could not widen: {e}"))
                    return False
                print(yellow(f"  {wider['summary']}"))
                if wider["steps"]:
                    return execute_steps(wider["steps"], state, ctx_fn, depth + 1)
            continue
        if rc == 0:
            print(green("  ok"))
            log_history(cmd, shell, state["cwd"])
            state["last_cmd"] = cmd
            continue

        print(red(f"  failed (exit {rc})"))
        if NEEDS_INPUT.search(out):   # captured run can't type: offer the real terminal
            if input("  this command wants keyboard input. Run it interactively? [Y/n] ").strip().lower() != "n":
                rc2, _ = run(shell, cmd, state["cwd"], terminal=True)
                if rc2 == 0:
                    log_history(cmd, shell, state["cwd"])
                    state["last_cmd"] = cmd
                    continue
            return False
        if depth >= MAX_FIX_ATTEMPTS:
            print(yellow("  giving up after repeated fixes."))
            return False
        print(dim("  diagnosing..."))
        try:
            fix = propose_fix(step, rc, out, ctx_fn())
        except Exception as e:  # network/parse problems should not crash the loop
            print(red(f"  could not diagnose: {e}"))
            return False
        print(yellow(f"  diagnosis: {fix['summary']}"))
        if not fix["steps"]:
            return False
        return execute_steps(fix["steps"], state, ctx_fn, depth + 1)
    return True


def suggest_next(state):
    if not state.get("last_cmd"):
        return
    p = predict_next(state["last_cmd"], Path(state["cwd"]).name)
    if (p and p["count"] >= 2 and p["cmd"] != state["last_cmd"]
            and not CD_RE.match(p["cmd"])):
        state["pending_next"] = p
        print(dim(f"\n  next? you usually run: {p['cmd']}   (type !next to run it)"))


HELP = """Commands:
  <anything>        describe what you want in plain language
  <real command>    e.g. ls, git status, Get-ChildItem -Recurse: runs immediately
                    (risky ones still ask first). Prefix with ? to force the AI.
  !<command>        always treat the line as a raw command
  !next             run the predicted next command from your history
  :save <name>      save the last plan as a macro
  :run <name>       run a macro        :macros   list macros
  :shell <name>     switch default shell (powershell|cmd|wsl|bash)
  :ctx              show detected context   :help   this help   :quit  exit\n  Tab               completes paths and :commands (Windows: pip install pyreadline3)\n  typed commands get your real terminal, so input(), ssh, vim, REPLs all work"""


def path_completions(buf, state, cmds):
    """Candidates for the text before the cursor; each is a full replacement line."""
    if buf.startswith(":run "):
        return [":run " + n for n in load_macros() if n.startswith(buf[5:])]
    if buf.startswith(":") and " " not in buf:
        return [c for c in cmds if c.startswith(buf)]
    in_quote = buf.count('"') % 2 == 1 or buf.count("'") % 2 == 1
    cut = max(buf.rfind('"'), buf.rfind("'")) if in_quote else buf.rfind(" ")
    head, token = buf[:cut + 1], buf[cut + 1:]
    expanded = os.path.expanduser(token)
    sep_at = max(expanded.rfind("/"), expanded.rfind("\\"))
    partial = expanded[sep_at + 1:]
    orig_dir = token[:max(token.rfind("/"), token.rfind("\\")) + 1]   # keeps ~ as typed
    base = os.path.join(state["cwd"], expanded[:sep_at + 1])           # absolute paths win
    try:
        names = sorted(os.listdir(base), key=str.lower)
    except OSError:
        return []
    fold = (lambda x: x.lower()) if os.name == "nt" else (lambda x: x)
    out = []
    for n in names:
        if n.startswith(".") and not partial.startswith("."):
            continue
        if fold(n).startswith(fold(partial)):
            cand = orig_dir + n + (os.sep if os.path.isdir(os.path.join(base, n)) else "")
            quote = '"' if (" " in cand and not in_quote) else ""
            out.append(head + quote + cand)
    return out


def setup_completion(state):
    if readline is None:
        if os.name == "nt":
            print(dim("  tip: pip install pyreadline3  -> enables Tab completion and history on Windows"))
        return
    cmds = [":help", ":quit", ":save ", ":run ", ":macros", ":shell ", ":ctx"]
    cache = []

    def completer(text, idx):
        if idx == 0:
            buf = readline.get_line_buffer()[:readline.get_endidx()]
            cache[:] = path_completions(buf, state, cmds)
        return cache[idx] if idx < len(cache) else None

    try:
        readline.set_completer_delims("\n")   # we parse the line ourselves (paths can have spaces)
        readline.set_completer(completer)
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        hist = HOME / "readline_history"
        try:
            readline.read_history_file(str(hist))
        except OSError:
            pass
        atexit.register(lambda: readline.write_history_file(str(hist)))
    except Exception:
        pass   # completion is a nicety; never block startup


def safe_execute(steps, state, ctx_fn):
    try:
        return execute_steps(steps, state, ctx_fn)
    except KeyboardInterrupt:
        print(yellow("\n  cancelled."))
        return False


def main():
    HOME.mkdir(exist_ok=True)
    state = {"cwd": os.getcwd(), "last_cmd": None, "last_plan": None, "pending_next": None}
    state["shell"] = os.environ.get("SHELLMIND_SHELL") or available_shells()[0]
    ctx_fn = lambda: build_context(state)
    setup_completion(state)

    print(bold("Shellmind") + dim(f"  model={MODEL}  shell={state['shell']}  (:help for commands)"))
    while True:
        try:
            line = input(f"\n{green('shellmind')} {dim(state['cwd'])}\n> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print(dim("\n  (Ctrl+C clears the line. Use :quit or Ctrl+D to exit.)"))
            continue
        if not line:
            continue
        if line in (":quit", ":q", "exit"):
            break
        if line == ":help":
            print(HELP)
        elif line == ":ctx":
            print(json.dumps(ctx_fn(), indent=2))
        elif line.startswith(":shell "):
            name = line.split(None, 1)[1]
            if name in available_shells():
                state["shell"] = name
            else:
                print(red(f"available: {', '.join(available_shells())}"))
        elif line == ":macros":
            for k, v in load_macros().items():
                print(f"  {bold(k)}: " + " ; ".join(s["command"] for s in v))
        elif line.startswith(":save "):
            if state["last_plan"]:
                m = load_macros()
                m[line.split(None, 1)[1]] = state["last_plan"]
                save_macros(m)
                print(green("  saved."))
            else:
                print(red("  nothing to save yet."))
        elif line.startswith(":run "):
            steps = load_macros().get(line.split(None, 1)[1])
            if steps:
                safe_execute(steps, state, ctx_fn)
                suggest_next(state)
            else:
                print(red("  no such macro."))
        elif line == "!next":
            p = state.get("pending_next")
            if p:
                step = {"shell": p["shell"], "command": p["cmd"], "explanation": "predicted from your history"}
                state["last_plan"] = [step]
                safe_execute([step], state, ctx_fn)
                suggest_next(state)
            else:
                print(dim("  no prediction yet."))
        elif line.startswith("!"):
            step = {"shell": state["shell"], "command": line[1:].strip(), "explanation": "raw command", "direct": True}
            state["last_plan"] = [step]
            safe_execute([step], state, ctx_fn)
            suggest_next(state)
        elif looks_like_command(line):
            step = {"shell": state["shell"], "command": line, "explanation": "typed command",
                    "direct": True}
            state["last_plan"] = [step]
            state["ran"] = []
            safe_execute([step], state, ctx_fn)
            remember_turn(state, line)
            suggest_next(state)
        else:
            line = line.lstrip("?").strip()   # a leading ? forces the AI planner
            try:
                plan = make_plan(line, ctx_fn())
            except KeyboardInterrupt:
                print(yellow("\n  cancelled."))
                continue
            except Exception as e:
                print(red(f"  planning failed: {e}"))
                continue
            print(bold(plan["summary"]))
            if not plan["steps"]:
                continue
            state["last_plan"] = plan["steps"]
            state["ran"] = []
            ok = safe_execute(plan["steps"], state, ctx_fn)
            remember_turn(state, line)
            if ok:
                suggest_next(state)


if __name__ == "__main__":
    main()
