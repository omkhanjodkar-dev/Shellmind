#!/usr/bin/env python3
"""
Shellmind bootstrap: make sure everything is present, then run the app.
Idempotent - every step is skipped when it is already satisfied.

  python bootstrap.py [run|setup|ollama|model|deps|doctor|clean] [--model NAME] [--yes]

Steps: 1) Ollama installed + server running  2) model pulled
       3) .venv + pip requirements           4) start shellmind.py
Standard library only, so it works before anything else is installed.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
DEFAULT_MODEL = "qwen2.5-coder:7b"


def say(msg):
    print(f"[shellmind] {msg}", flush=True)


def die(msg):
    print(f"[shellmind] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def confirm(question, yes):
    if yes or not sys.stdin.isatty():
        return True
    return input(f"[shellmind] {question} [Y/n] ").strip().lower() != "n"


# ------------------------------------------------------------------- Ollama
def host_url():
    h = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    return h if h.startswith("http") else "http://" + h


def is_local_host():
    host = host_url().split("//", 1)[1].split("/")[0].rsplit(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")


def server_up():
    try:
        with urllib.request.urlopen(host_url() + "/api/version", timeout=2):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def installed_models():
    with urllib.request.urlopen(host_url() + "/api/tags", timeout=5) as r:
        names = {m["name"] for m in json.load(r).get("models", [])}
    return {n if ":" in n else n + ":latest" for n in names}


def find_ollama():
    exe = shutil.which("ollama")
    if exe:
        return exe
    candidates = []
    if os.name == "nt":
        for var, sub in (("LOCALAPPDATA", "Programs/Ollama"), ("ProgramFiles", "Ollama")):
            if os.environ.get(var):
                candidates.append(Path(os.environ[var]) / sub / "ollama.exe")
    elif sys.platform == "darwin":
        candidates += [Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama"),
                       Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    else:
        candidates.append(Path("/usr/local/bin/ollama"))
    return next((str(c) for c in candidates if c.exists()), None)


def download(url, dest):
    def hook(blocks, size, total):
        if total > 0:
            print(f"\r[shellmind] downloading... {min(100, blocks * size * 100 // total)}%",
                  end="", flush=True)
    urllib.request.urlretrieve(url, dest, hook)
    print()


def install_ollama(yes):
    if not confirm("Ollama is not installed. Install it now?", yes):
        die("Ollama is required. Get it from https://ollama.com/download and re-run.")
    system = platform.system()
    if system == "Windows":
        if shutil.which("winget"):
            say("installing Ollama with winget...")
            subprocess.run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"])
        if not find_ollama():
            say("downloading the Ollama installer...")
            dest = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
            download("https://ollama.com/download/OllamaSetup.exe", dest)
            say("running the installer...")
            subprocess.run([str(dest), "/SILENT"])
    elif system == "Darwin":
        if not shutil.which("brew"):
            die("Install Ollama from https://ollama.com/download (or install Homebrew) and re-run.")
        say("installing Ollama with Homebrew...")
        subprocess.run(["brew", "install", "ollama"], check=True)
    else:
        say("running the official Ollama installer (asks for sudo)...")
        if subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True).returncode:
            die("the Ollama installer failed. See https://ollama.com/download/linux")
    if not find_ollama():
        die("Ollama installed but not found yet. Open a NEW terminal and re-run.")


def start_server(exe):
    say("starting the Ollama server...")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.CREATE_NO_WINDOW)
    else:
        kw["start_new_session"] = True
    subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **kw)
    for _ in range(60):
        if server_up():
            return
        time.sleep(0.5)
    die("the Ollama server did not start. Try `ollama serve` in another terminal.")


def ensure_ollama(yes):
    if server_up():
        say(f"Ollama server reachable at {host_url()}")
        return
    if not is_local_host():
        die(f"Ollama at {host_url()} is not reachable.")
    exe = find_ollama()
    if not exe:
        install_ollama(yes)
        exe = find_ollama()
    os.environ["PATH"] = str(Path(exe).parent) + os.pathsep + os.environ.get("PATH", "")
    start_server(exe)
    say("Ollama is ready.")


def api_pull(model):
    req = urllib.request.Request(host_url() + "/api/pull", method="POST",
                                 data=json.dumps({"model": model}).encode(),
                                 headers={"content-type": "application/json"})
    last = ""
    with urllib.request.urlopen(req) as r:
        for raw in r:
            ev = json.loads(raw)
            if "error" in ev:
                die(ev["error"])
            if ev.get("total"):
                line = f"  {ev.get('status', '')}: {int(100 * ev.get('completed', 0) / ev['total'])}%"
            else:
                line = f"  {ev.get('status', '')}"
            if line != last:
                print("\r" + line.ljust(64), end="", flush=True)
                last = line
    print()


def ensure_model(model):
    want = model if ":" in model else model + ":latest"
    if want in installed_models():
        say(f"model {model} is already pulled.")
        return
    say(f"pulling {model} (one-time download, several GB)...")
    api_pull(model)
    if want not in installed_models():
        die(f"could not pull {model}. Check the name at https://ollama.com/library")
    say(f"model {model} is ready.")


# --------------------------------------------------------------- Python deps
def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def req_digest():
    req = ROOT / "requirements.txt"
    return hashlib.sha256(req.read_bytes()).hexdigest() if req.exists() else ""


def deps_ok():
    stamp = VENV / ".requirements.sha256"
    return venv_python().exists() and stamp.exists() and stamp.read_text().strip() == req_digest()


def ensure_deps():
    py = venv_python()
    if not py.exists():
        say("creating virtual environment (.venv)...")
        if subprocess.run([sys.executable, "-m", "venv", str(VENV)]).returncode:
            shutil.rmtree(VENV, ignore_errors=True)   # never leave a half-built venv behind
            die("could not create a venv. On Debian/Ubuntu: sudo apt install python3-venv")
    if not deps_ok():
        req = ROOT / "requirements.txt"
        if req.exists():
            say("installing Python requirements...")
            r = subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check",
                                "-q", "-r", str(req)])
            if r.returncode:
                die("pip install failed.")
        (VENV / ".requirements.sha256").write_text(req_digest())
    else:
        say("Python requirements already installed.")
    return py


# ----------------------------------------------------------------- commands
def run_app(model):
    cmd = [str(venv_python()), str(ROOT / "shellmind.py")]
    env = dict(os.environ, SHELLMIND_MODEL=model)
    if os.name != "nt":
        os.execve(cmd[0], cmd, env)
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl+C belongs to Shellmind, not us
    sys.exit(subprocess.Popen(cmd, env=env).wait())


def doctor(model):
    up = server_up()
    have = False
    if up:
        try:
            have = (model if ":" in model else model + ":latest") in installed_models()
        except (urllib.error.URLError, OSError, ValueError):
            pass
    rows = [
        ("Python >= 3.8", sys.version.split()[0], sys.version_info >= (3, 8)),
        ("Ollama installed", find_ollama() or "not found", bool(find_ollama())),
        ("Ollama server", host_url(), up),
        (f"Model {model}", "pulled" if have else "not pulled", have),
        ("Virtual env (.venv)", str(VENV), venv_python().exists()),
        ("pip requirements", "up to date" if deps_ok() else "needs install", deps_ok()),
    ]
    for name, detail, ok in rows:
        print(f"  [{'ok' if ok else '--'}] {name:<22} {detail}")
    print("\n  Everything missing is installed automatically by: make   (or python bootstrap.py)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="run",
                    choices=["run", "setup", "ollama", "model", "deps", "doctor", "clean"])
    ap.add_argument("--model", default=os.environ.get("SHELLMIND_MODEL", DEFAULT_MODEL))
    ap.add_argument("--yes", action="store_true", help="do not ask before installing")
    a = ap.parse_args()
    if sys.version_info < (3, 8):
        die("Python 3.8 or newer is required.")

    if a.action == "doctor":
        return doctor(a.model)
    if a.action == "clean":
        shutil.rmtree(VENV, ignore_errors=True)
        for d in ROOT.rglob("__pycache__"):
            shutil.rmtree(d, ignore_errors=True)
        return say("removed .venv and caches.")
    if a.action in ("run", "setup", "ollama", "model"):
        ensure_ollama(a.yes)
    if a.action in ("run", "setup", "model"):
        ensure_model(a.model)
    if a.action in ("run", "setup", "deps"):
        ensure_deps()
    if a.action == "run":
        run_app(a.model)
    elif a.action == "setup":
        say("setup complete. Start Shellmind with: make")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
