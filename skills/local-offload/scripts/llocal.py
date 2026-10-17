#!/usr/bin/env python3
"""
llocal — delegate work to local models (Ollama / llama.cpp) instead of paid API tokens.

  llocal doctor                 detect RAM, backends and local models
  llocal bench [--quick]        benchmark every local model that fits in memory
  llocal pick --task code       best local model for a task type ("none" if none is good enough)
  llocal sheet                  compact cheat sheet so the calling agent can choose a model itself
  llocal run --task code -f a.py -f b.py "prompt"     run on the best local model
  cat x.log | llocal run --task logs "prompt" -       ('-' = also read stdin)
  llocal feedback ok|bad        after verifying the last run's output: real-world score per model
  llocal stats                  tokens moved off the paid API and estimated savings
  llocal stop                   stop the llama-server that llocal started

Exit codes (stable, agents rely on them):
  0 ok | 1 bad input | 2 backend down/failed | 3 no local model good enough -> do it yourself
  4 --json requested but output was not valid JSON

Only the Python standard library is used. Config and results live in ~/.llocal/
(override with LLOCAL_HOME).
"""
import argparse
import fnmatch
import glob
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser(os.environ.get("LLOCAL_HOME", "~/.llocal"))
CONFIG_PATH = os.path.join(HOME, "config.json")
BENCH_PATH = os.path.join(HOME, "bench.json")
USAGE_PATH = os.path.join(HOME, "usage.jsonl")
FEEDBACK_PATH = os.path.join(HOME, "feedback.jsonl")
SERVER_STATE = os.path.join(HOME, "server.json")
SERVER_LOG = os.path.join(HOME, "llama-server.log")

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    # Extra llama-server (or any OpenAI-compatible) endpoints already running, e.g. ["http://localhost:8080"]
    "servers": [],
    "llama_server_bin": "llama-server",
    "llama_port": 8089,
    "llama_extra_args": ["-ngl", "99", "--jinja", "-fa", "auto"],
    "gguf_dirs": [
        "~/models",
        "~/.cache/llama.cpp",
        "~/Library/Caches/llama.cpp",
        "~/.lmstudio/models",
        "~/.cache/lm-studio/models",
    ],
    "ctx": 16384,
    # Largest model file llocal will use. With an NVIDIA GPU: vram_fraction * VRAM (models that
    # spill to system RAM get very slow). Otherwise (Apple unified memory, CPU): mem_fraction * RAM;
    # macOS lets the GPU use ~2/3-3/4 of unified memory and the KV cache needs room too.
    # Set max_model_gb to a number to override both.
    "mem_fraction": 0.62,
    "vram_fraction": 0.9,
    "max_model_gb": None,
    # Minimum benchmark score (0-1) in a category to accept a local model for that task.
    "min_score": 0.6,
    "exclude": ["embed", "embedding", "nomic", "bge", "mxbai", "whisper", "mmproj", "asr"],
    # Prices used ONLY to estimate savings in `llocal stats` (USD per million tokens).
    # Set them to the model that would otherwise do the work (default: Claude Sonnet 5.5).
    "price_in_per_mtok": 2.0,
    "price_out_per_mtok": 10.0,
    "request_timeout": 600,
    # bench stops early on models slower than this (tok/s): too slow to save anyone time
    "min_tps": 5,
}

# Task types accepted by `run`/`pick`, mapped to benchmark categories.
TASK_TO_CATEGORY = {
    "code": "code", "tests": "code", "refactor": "code", "boilerplate": "code",
    "json": "json", "extract": "json", "classify": "json",
    "summary": "summary", "docs": "summary", "explain": "summary", "translate": "summary", "logs": "summary",
    "reason": "reason",
    "general": None,  # average of all categories
}

SYSTEM_PROMPTS = {
    "code": "You are a precise senior software engineer. Output only what is asked. When writing code, "
            "return complete, runnable code in a single fenced block, no explanations unless asked.",
    "json": "You extract and structure information. Reply with valid JSON only, no prose, no code fences.",
    "summary": "You summarize and explain technical material faithfully and concisely. Never invent facts "
               "that are not in the input. Use bullet points when useful.",
    "reason": "Think carefully, then give a short, exact answer.",
    None: "You are a helpful, precise assistant. Be concise and do not invent facts.",
}


# --------------------------------------------------------------------------- utils

class Fail(Exception):
    """Expected failure: printed as one line plus a hint, exits with `code`, no traceback."""
    def __init__(self, code, msg, hint=""):
        super().__init__(msg)
        self.code, self.hint = code, hint


def _self_cmd():
    """How to invoke this script in hints: `llocal` if that is on PATH and is this file,
    else `python3 /path/to/llocal.py` (the usual case after `npx skills add`)."""
    me = os.path.realpath(sys.argv[0] or __file__)
    on_path = shutil.which("llocal")
    if on_path and os.path.realpath(on_path) == me:
        return "llocal"
    return "python3 " + me.replace(os.path.expanduser("~"), "~", 1)


LLOCAL = _self_cmd()
NO_MODEL_HINT = "do the task yourself; do not retry llocal for this task type"
BACKEND_HINT = f"check `{LLOCAL} doctor`; meanwhile do the task yourself"


def ensure_home():
    os.makedirs(HOME, exist_ok=True)


def load_config():
    ensure_home()
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except (ValueError, OSError) as e:
            raise Fail(1, f"cannot read {CONFIG_PATH}: {e}", "fix or delete that file")
    else:
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return cfg


def eprint(*a):
    print(*a, file=sys.stderr)


def http_json(url, payload=None, timeout=30, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def alive(url, timeout=2):
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # it answered
    except Exception:
        return False


def total_ram_bytes():
    try:
        if platform.system() == "Darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 16 * 1024 ** 3


def total_vram_bytes():
    """Largest NVIDIA GPU's memory, or 0 if there is none (nvidia-smi missing or failing)."""
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.total",
                                       "--format=csv,noheader,nounits"], text=True, timeout=5)
        return max(int(x) for x in out.split()) * 1024 ** 2
    except Exception:
        return 0


def gpu_room(cfg):
    """(bytes of VRAM llocal could use right now, description of other GPU users) or (None, "")
    without NVIDIA. Counts memory held by Ollama models as usable, since llocal unloads them."""
    if not shutil.which("nvidia-smi"):
        return None, ""
    try:
        free = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.free",
                                        "--format=csv,noheader,nounits"], text=True, timeout=5)
        apps = subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                        "--format=csv,noheader,nounits"], text=True, timeout=5)
    except Exception:
        return None, ""
    room = max(int(x) for x in free.split()) * 1024 ** 2
    try:
        ps = http_json(cfg["ollama_url"].rstrip("/") + "/api/ps", timeout=2)
        room += sum(int(m.get("size_vram", 0)) for m in ps.get("models", []))
    except Exception:
        pass
    hogs = []
    for line in apps.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 3 and parts[2].isdigit() and int(parts[2]) >= 1024 and "ollama" not in parts[1]:
            name = parts[1] if "no data" not in parts[1].lower() else "process of another user"
            hogs.append(f"{name} pid {parts[0]} {int(parts[2]) / 1024:.1f} GB")
    return room, ", ".join(hogs)


def wait_for_room(cfg, need, timeout=30):
    """gpu_room(), but if `need` does not fit yet, wait up to `timeout` s: a model that was just
    unloaded (by llocal or by Ollama's own scheduler) frees its VRAM a few seconds later."""
    deadline = time.time() + timeout
    while True:
        room, hogs = gpu_room(cfg)
        if room is None or need <= room or time.time() >= deadline:
            return room, hogs
        time.sleep(1)


def needs_room(model):
    """Rough VRAM need: weights + ~15% for KV cache and buffers. Remote servers need none."""
    return 0 if model["backend"] == "server" else model["size"] * 1.15


def memory_budget(cfg):
    """(max model file size in bytes, human-readable explanation)."""
    if cfg.get("max_model_gb"):
        return cfg["max_model_gb"] * 1024 ** 3, f"max_model_gb={cfg['max_model_gb']}"
    vram = total_vram_bytes()
    if vram:
        return cfg["vram_fraction"] * vram, f"NVIDIA VRAM {gb(vram)} x vram_fraction={cfg['vram_fraction']}"
    ram = total_ram_bytes()
    return cfg["mem_fraction"] * ram, f"RAM {gb(ram)} x mem_fraction={cfg['mem_fraction']}"


def gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def strip_thinking(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if text.lstrip().startswith("<think>"):  # cut off by max_tokens while still thinking
        return ""
    # Some chat templates swallow the opening tag, leaving "reasoning...</think>answer".
    # Only treat it as reasoning when it looks like it (multi-line or long), so an answer
    # that merely mentions "</think>" is kept intact.
    m = re.match(r"(.*?)</think>", text, flags=re.S)
    if m and ("\n" in m.group(1).strip() or len(m.group(1)) > 200):
        text = text[m.end():]
    return text.strip()


def est_tokens(text):
    return max(1, len(text) * 2 // 7)  # ~3.5 chars/token, same estimate as the input limit


# Agent-side tokens per run that a local run can't avoid: the stderr line, verifying, feedback.
RUN_OVERHEAD_TOKENS = 150


def net_saving(row, cfg):
    """Estimated USD the calling agent saved with one run (negative = offloading cost more).
    Avoided: reading the files/stdin and writing the answer itself. Spent: writing the prompt
    words (output price) and reading the answer back unless it went to --out."""
    p_in, p_out = cfg["price_in_per_mtok"] / 1e6, cfg["price_out_per_mtok"] / 1e6
    if "in_ctx" not in row:  # rows from before llocal logged the split: old gross estimate
        return row.get("in", 0) * p_in + row.get("out", 0) * p_out
    out = row.get("out", 0)
    avoided = row["in_ctx"] * p_in + out * p_out
    spent = (row.get("in_prompt", 0) * p_out + (0 if row.get("to_file") else out * p_in)
             + RUN_OVERHEAD_TOKENS * p_in)
    return avoided - spent


# --------------------------------------------------------------------------- discovery

def discover(cfg, verbose=False):
    """Return list of model dicts: {id, backend, name, size, path/url}."""
    models = []
    excl = [e.lower() for e in cfg["exclude"]]

    # Ollama
    try:
        tags = http_json(cfg["ollama_url"].rstrip("/") + "/api/tags", timeout=3)
        for m in tags.get("models", []):
            name = m["name"]
            if any(e in name.lower() for e in excl):
                continue
            if m.get("remote_host") or name.endswith(":cloud") or name.endswith("-cloud"):
                continue  # Ollama cloud model: would send your files off this machine
            caps = m.get("capabilities")
            if caps is not None and "completion" not in caps:
                continue  # embedding, or a decision model (e.g. nimble): not a chat model
            models.append({"id": "ollama:" + name, "backend": "ollama", "name": name,
                           "size": int(m.get("size", 0))})
    except Exception as e:
        if verbose:
            eprint(f"  Ollama not reachable at {cfg['ollama_url']} ({e.__class__.__name__})")

    # GGUF files for llama.cpp
    seen = set()
    for d in cfg["gguf_dirs"]:
        for p in glob.glob(os.path.join(os.path.expanduser(d), "**", "*.gguf"), recursive=True):
            base = os.path.basename(p).lower()
            if any(e in base for e in excl):
                continue
            m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", base)
            if m and m.group(1) != "00001":
                continue  # only first shard of split models
            real = os.path.realpath(p)
            if real in seen:
                continue
            seen.add(real)
            size = os.path.getsize(real)
            if m:  # sum shards
                size = sum(os.path.getsize(s) for s in glob.glob(real.replace(m.group(0), "-*-of-" + m.group(2) + ".gguf")))
            models.append({"id": "gguf:" + real, "backend": "gguf", "name": os.path.basename(p),
                           "size": size, "path": real})

    # Already-running OpenAI-compatible servers
    for url in cfg["servers"]:
        url = url.rstrip("/")
        try:
            data = http_json(url + "/v1/models", timeout=3)
            names = [x.get("id") for x in data.get("data", [])] or ["default"]
            for n in names:
                models.append({"id": f"server:{url}#{n}", "backend": "server", "name": n,
                               "size": 0, "url": url})
        except Exception as e:
            if verbose:
                eprint(f"  server {url} not reachable ({e.__class__.__name__})")
    return models


def nick(mid):
    """Shortest unambiguous-enough name for agents: what `run --model` also accepts."""
    name = short(mid)
    name = name.removeprefix("ollama:")
    return re.sub(r"^hf\.co/[^/]+/", "", name)


def short(mid):
    return "gguf:" + os.path.basename(mid[5:]) if mid.startswith("gguf:") else mid


def fits(model, budget):
    return model["size"] == 0 or model["size"] <= budget


# --------------------------------------------------------------------------- llama-server management

def read_state():
    try:
        with open(SERVER_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def is_our_server(st):
    """True only if the PID in server.json is still the llama-server llocal started.
    After a reboot that PID may belong to an unrelated process, which must never be killed."""
    pid = st.get("pid")
    if not pid or not pid_alive(pid):
        return False
    try:
        cmd = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True, timeout=5)
    except Exception:
        return False
    return "llama-server" in cmd and str(st.get("path", "")) in cmd


def stop_server(quiet=False):
    st = read_state()
    pid = st.get("pid")
    if is_our_server(st):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        if not quiet:
            eprint(f"stopped llama-server (pid {pid})")
    elif not quiet:
        eprint("no llama-server started by llocal is running" +
               (f" (pid {pid} in state file is not ours; left alone)" if pid and pid_alive(pid) else ""))
    if os.path.exists(SERVER_STATE):
        os.remove(SERVER_STATE)


def ensure_gguf_server(path, cfg):
    """Start (or reuse) llama-server for this GGUF. Returns base URL."""
    port = cfg["llama_port"]
    url = f"http://127.0.0.1:{port}"
    st = read_state()
    if st.get("path") == path and is_our_server(st) and alive(url + "/health"):
        return url
    stop_server(quiet=True)
    unload_ollama_all(cfg)  # 16 GB: never keep two models in memory
    binary = shutil.which(cfg["llama_server_bin"]) or cfg["llama_server_bin"]
    if not shutil.which(binary) and not os.path.exists(binary):
        raise RuntimeError("llama-server not found. Install llama.cpp (`brew install llama.cpp`) "
                           f"or set llama_server_bin in {CONFIG_PATH}")
    cmd = [binary, "-m", path, "--port", str(port), "--host", "127.0.0.1",
           "-c", str(cfg["ctx"])] + list(cfg["llama_extra_args"])
    with open(SERVER_LOG, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    with open(SERVER_STATE, "w") as f:
        json.dump({"pid": proc.pid, "path": path, "port": port}, f)
    deadline = time.time() + 300
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited (code {proc.returncode}); see {SERVER_LOG}")
        try:
            h = http_json(url + "/health", timeout=2)
            if h.get("status") == "ok":
                return url
        except urllib.error.HTTPError:
            pass  # 503 while loading
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"llama-server did not become ready; see {SERVER_LOG}")


def unload_ollama(cfg, name):
    try:
        http_json(cfg["ollama_url"].rstrip("/") + "/api/generate",
                  {"model": name, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


def unload_ollama_all(cfg, keep=None):
    try:
        ps = http_json(cfg["ollama_url"].rstrip("/") + "/api/ps", timeout=2)
        for m in ps.get("models", []):
            if m["name"] != keep:
                unload_ollama(cfg, m["name"])
    except Exception:
        pass


# --------------------------------------------------------------------------- chat

def chat(model, messages, cfg, max_tokens=2048, temperature=0.2):
    """Returns dict(text, prompt_tokens, completion_tokens, tps, seconds).
    Any backend problem (down, timeout, HTTP error, malformed reply) raises RuntimeError."""
    try:
        return _chat(model, messages, cfg, max_tokens, temperature)
    except RuntimeError:
        raise
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{model['backend']} HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as e:
        raise RuntimeError(f"{model['backend']} unreachable or timed out "
                           f"(request_timeout={cfg['request_timeout']}s): {getattr(e, 'reason', e)}")
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise RuntimeError(f"{model['backend']} returned an unexpected reply ({e.__class__.__name__}: {e})")


def _chat(model, messages, cfg, max_tokens, temperature):
    t0 = time.time()
    if model["backend"] == "ollama":
        stop_server(quiet=True)  # free memory held by llama-server
        unload_ollama_all(cfg, keep=model["name"])  # never two models in (V)RAM at once
        base = cfg["ollama_url"].rstrip("/")
        payload = {"model": model["name"], "messages": messages, "stream": False, "think": False,
                   "keep_alive": "10m",
                   "options": {"num_ctx": cfg["ctx"], "num_predict": max_tokens, "temperature": temperature}}
        try:
            r = http_json(base + "/api/chat", payload, timeout=cfg["request_timeout"])
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            if "think" in body.lower():  # model without thinking support
                payload.pop("think")
                r = http_json(base + "/api/chat", payload, timeout=cfg["request_timeout"])
            else:
                if "out of memory" in body.lower():
                    raise RuntimeError(f"{model['name']} does not fit in free GPU memory right now "
                                       "(CUDA out of memory); close other GPU programs or use a smaller model")
                raise RuntimeError(f"Ollama error {e.code}: {body[:300]}")
        text = strip_thinking(r.get("message", {}).get("content", ""))
        ct = r.get("eval_count", est_tokens(text))
        dur = r.get("eval_duration", 0) / 1e9
        return {"text": text, "prompt_tokens": r.get("prompt_eval_count", 0), "completion_tokens": ct,
                "tps": ct / dur if dur else ct / max(time.time() - t0, 1e-6), "seconds": time.time() - t0}

    # llama-server / OpenAI-compatible
    if model["backend"] == "gguf":
        url = ensure_gguf_server(model["path"], cfg)
        mname = model["name"]
    else:
        url, mname = model["url"], model["name"]
    payload = {"model": mname, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "stream": False,
               "chat_template_kwargs": {"enable_thinking": False}}
    try:
        r = http_json(url + "/v1/chat/completions", payload, timeout=cfg["request_timeout"])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"server error {e.code}: {e.read().decode(errors='ignore')[:300]}")
    msg = r["choices"][0]["message"]
    text = strip_thinking(msg.get("content") or "")
    usage = r.get("usage", {})
    ct = usage.get("completion_tokens", est_tokens(text))
    tps = (r.get("timings") or {}).get("predicted_per_second") or ct / max(time.time() - t0, 1e-6)
    return {"text": text, "prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": ct,
            "tps": tps, "seconds": time.time() - t0}


# --------------------------------------------------------------------------- benchmark suite

def extract_code(text):
    # any fence tag: ```python, ```Python, ```python3, ```py, or none
    blocks = re.findall(r"```[\w+.-]*[ \t]*\n(.*?)```", text, flags=re.S)
    if blocks:
        return max(blocks, key=len)
    m = re.search(r"```[\w+.-]*[ \t]*\n(.*)", text, flags=re.S)  # fence never closed (cut off)
    return m.group(1) if m else text


def run_python_tests(code, tests, timeout=10):
    # A random token printed after the asserts: model code cannot fake a pass by printing it.
    token = "ALL_OK_" + secrets.token_hex(8)
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "t.py")
        with open(f, "w") as fh:
            fh.write(code + "\n\n" + tests + f"\nprint({token!r})\n")
        try:
            out = subprocess.run([sys.executable, "-I", f], capture_output=True, text=True,
                                 timeout=timeout, cwd=d)
            return out.returncode == 0 and out.stdout.rstrip().endswith(token)
        except subprocess.TimeoutExpired:
            return False


def check_code(tests):
    return lambda text: 1.0 if run_python_tests(extract_code(text), tests) else 0.0


def parse_json_loose(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except Exception:
            continue
    return None


def check_answer(expected):
    def f(text):
        m = re.findall(r"ANSWER:\s*\**\s*([^\n*]+)", text, flags=re.I)
        if not m:
            return 0.0
        got = m[-1].strip().strip(".").lower()
        # "32", "32 boxes", "Friday." all count; "320" or "friday or saturday" do not
        return 1.0 if re.fullmatch(re.escape(str(expected).lower()) + r"(\s+[a-z]+)?", got) else 0.0
    return f


def check_keywords(groups, max_words):
    """groups: list of lists — each inner list is alternatives, one must appear."""
    def f(text):
        low = text.lower()
        hits = sum(1 for g in groups if any(k in low for k in g))
        score = hits / len(groups)
        if len(text.split()) > max_words:
            score *= 0.5
        return score
    return f


def check_order_json(text):
    d = parse_json_loose(text)
    if not isinstance(d, dict):
        return 0.0
    pts = 0
    pts += str(d.get("customer", "")).lower().startswith("marta")
    pts += str(d.get("email", "")).lower() == "marta.ruiz@example.com"
    items = d.get("items") or []
    try:
        q = {str(i.get("product", "")).lower(): int(i.get("quantity")) for i in items}
        pts += any("usb" in k and v == 3 for k, v in q.items())
        pts += any("monitor" in k and v == 1 for k, v in q.items())
        pts += len(items) == 2
    except Exception:
        pass
    pts += str(d.get("urgent")).lower() == "true"
    return pts / 6


def check_tool_json(text):
    d = parse_json_loose(text)
    if not isinstance(d, dict):
        return 0.0
    pts = (d.get("tool") == "grep") + (isinstance(d.get("args"), dict) and
                                         "timeout" in str(d["args"].get("pattern", "")).lower()) + \
          (isinstance(d.get("args"), dict) and str(d["args"].get("path", "")).rstrip("/") in ("src", "./src"))
    return pts / 3


def check_classify(text):
    d = parse_json_loose(text)
    if not isinstance(d, list) or len(d) != 4:
        return 0.0
    want = ["bug", "feature", "question", "bug"]
    got = [str(x.get("label", "")).lower() if isinstance(x, dict) else str(x).lower() for x in d]
    return sum(a == b for a, b in zip(got, want)) / 4


LOG_TEXT = """2026-10-02 14:03:11 INFO  payments-api  request id=8812 POST /charge 201 112ms
2026-10-02 14:03:12 INFO  payments-api  request id=8813 POST /charge 201 98ms
2026-10-02 14:05:40 WARN  payments-api  db pool usage 18/20
2026-10-02 14:06:02 WARN  payments-api  db pool usage 20/20
2026-10-02 14:06:03 ERROR payments-api  request id=8901 POST /charge 500 timeout acquiring connection from pool after 5000ms
2026-10-02 14:06:03 ERROR payments-api  request id=8902 POST /charge 500 timeout acquiring connection from pool after 5000ms
2026-10-02 14:06:04 INFO  auth-api      request id=8903 GET /me 200 12ms
2026-10-02 14:06:05 ERROR payments-api  request id=8904 POST /refund 500 timeout acquiring connection from pool after 5000ms
2026-10-02 14:06:09 WARN  payments-api  slow query 4870ms: SELECT * FROM ledger WHERE account_id=? (missing index on ledger.account_id)
2026-10-02 14:07:30 INFO  deploy        payments-api v2.14.0 rolled out (previous v2.13.2)
2026-10-02 14:09:00 ERROR payments-api  request id=8950 POST /charge 500 timeout acquiring connection from pool after 5000ms
2026-10-02 14:12:44 INFO  payments-api  db pool usage 4/20 after rollback to v2.13.2"""

DOC_TEXT = """The export service accepts jobs through POST /exports with a JSON body containing
`dataset` and `format` (csv or parquet). Jobs are queued in Redis and processed by workers;
each worker handles one job at a time. Exports larger than 2 GB are split into parts of 500 MB.
Results are stored in the S3 bucket `acme-exports` for 7 days and then deleted automatically.
A webhook is sent to the URL configured per tenant when the export is ready. Retries: failed
jobs are retried 3 times with exponential backoff. Authentication uses tenant API keys
passed in the X-Api-Key header."""

def _long_services_doc():
    """~8k-token inventory with one relevant entry in the middle (tests real long-context use:
    a model that truncates or skims the input cannot answer)."""
    teams = ["platform", "search", "growth", "data", "mobile", "infra", "ml", "security"]
    lines = []
    for i in range(1, 321):
        if i == 173:
            lines.append("svc-173 | owner: billing-ops | port: 7173 | retries: 9 | "
                         "purpose: renders monthly invoices and emails them to customers via SMTP")
            continue
        lines.append(f"svc-{i:03d} | owner: {teams[i % 8]} | port: {7000 + i} | retries: {i % 4} | "
                     f"purpose: internal {['cache', 'queue', 'proxy', 'cron', 'index'][i % 5]} worker #{i}")
    return "\n".join(lines)


def check_needle(text):
    d = parse_json_loose(text)
    if not isinstance(d, dict):
        return 0.0
    return (("173" in str(d.get("service", ""))) + (str(d.get("port")) == "7173") +
            (str(d.get("retries")) == "9")) / 3


def check_tool_plan(text):
    d = parse_json_loose(text)
    if not isinstance(d, list) or not d:
        return 0.0
    allowed = {"git_log": {"path", "n"}, "read_file": {"path"}, "grep": {"pattern", "path"},
               "run_tests": {"target"}}
    calls = [c for c in d if isinstance(c, dict)]
    valid = calls and all(c.get("tool") in allowed and isinstance(c.get("args"), dict)
                          and set(c["args"]) <= allowed[c["tool"]] for c in calls)
    ran = any(c.get("tool") == "run_tests" and "test_api" in str(c.get("args", {}).get("target", ""))
              for c in calls)
    history = any(c.get("tool") == "git_log" for c in calls)
    return (bool(valid) + ran + history + (len(d) == 3)) / 4


def check_strict_bullets(text):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    bullets = [l for l in lines if re.match(r"^([-*•]|\d+[.)])\s+", l)]
    pts = (len(bullets) == 3) + (len(lines) == len(bullets))
    pts += bool(bullets) and all(len(re.sub(r"^([-*•]|\d+[.)])\s+", "", b).split()) <= 12 for b in bullets)
    return pts / 3


GET_PATH = '''def get_path(d, path):
    """Return the value at a dotted path, e.g. get_path({"a": {"b": 1}}, "a.b") == 1."""
    cur = d
    for key in path.split("."):
        cur = cur[key]
    return cur
'''

BUGGY_BSEARCH = '''def bsearch(xs, target):
    """Return index of target in sorted list xs, or -1."""
    lo, hi = 0, len(xs)
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] == target:
            return mid
        elif xs[mid] < target:
            lo = mid
        else:
            hi = mid - 1
    return -1
'''

SUITE = [
    # ---- code
    {"cat": "code", "id": "palindrome", "max_tokens": 600,
     "prompt": "Write a Python function `is_palindrome(s: str) -> bool` that ignores case and any "
               "non-alphanumeric characters. Return only the code.",
     "check": check_code("assert is_palindrome('A man, a plan, a canal: Panama')\n"
                         "assert not is_palindrome('race a car')\nassert is_palindrome('')\n"
                         "assert is_palindrome('No lemon, no melon')")},
    {"cat": "code", "id": "merge_intervals", "max_tokens": 800,
     "prompt": "Write a Python function `merge_intervals(intervals: list[list[int]]) -> list[list[int]]` "
               "that merges overlapping (or touching) closed intervals and returns them sorted by start. "
               "Input may be unsorted. Return only the code.",
     "check": check_code("assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]\n"
                         "assert merge_intervals([[1,4],[4,5]])==[[1,5]]\nassert merge_intervals([])==[]\n"
                         "assert merge_intervals([[5,7],[1,2],[2,3]])==[[1,3],[5,7]]")},
    {"cat": "code", "id": "parse_duration", "max_tokens": 800,
     "prompt": "Write a Python function `parse_duration(s: str) -> int` that converts strings like "
               "'1h30m', '45s', '2h5s', '1d2h' into total seconds (units: d, h, m, s; each unit at most once, "
               "in that order). Raise ValueError for invalid input such as '', 'abc' or '5x'. Return only the code.",
     "check": check_code("assert parse_duration('1h30m')==5400\nassert parse_duration('45s')==45\n"
                         "assert parse_duration('2h5s')==7205\nassert parse_duration('1d2h')==93600\n"
                         "for bad in ['', 'abc', '5x']:\n    try:\n        parse_duration(bad)\n"
                         "        raise SystemExit('no error for '+bad)\n    except ValueError:\n        pass")},
    {"cat": "code", "id": "fix_bsearch", "max_tokens": 700,
     "prompt": "This function has bugs (it can loop forever and miss elements). Fix it, keeping the same "
               "signature. Return only the corrected code.\n\n```python\n" + BUGGY_BSEARCH + "```",
     "check": check_code("xs=[1,3,5,7,9,11]\nfor i,x in enumerate(xs): assert bsearch(xs,x)==i\n"
                         "for t in [0,2,4,12]: assert bsearch(xs,t)==-1\nassert bsearch([],1)==-1\n"
                         "assert bsearch([2],2)==0")},
    # ---- json / extraction / tool choice
    {"cat": "json", "id": "order_extract", "max_tokens": 400,
     "prompt": "Extract this order into JSON with keys: customer (string), email (string), "
               "items (list of {product, quantity}), urgent (boolean).\n\n"
               "---\nHola, soy Marta Ruiz (marta.ruiz@example.com). Quiero pedir 3 cables USB-C y "
               "un monitor de 27 pulgadas. Por favor, lo necesito antes del viernes, es urgente. Gracias.\n---",
     "check": check_order_json},
    {"cat": "json", "id": "tool_choice", "max_tokens": 200,
     "prompt": "Available tools: read_file{path}, grep{pattern, path}, run_tests{target}.\n"
               "User request: 'find where we set the request timeout anywhere under the src directory'.\n"
               'Reply with JSON only: {"tool": <name>, "args": {...}}',
     "check": check_tool_json},
    {"cat": "json", "id": "classify", "max_tokens": 300,
     "prompt": "Classify each issue title as bug, feature or question. Reply with a JSON list of "
               'objects {"id": n, "label": ...} in the same order.\n'
               "1. App crashes when uploading a PNG larger than 10MB\n2. Add dark mode to settings page\n"
               "3. How do I rotate my API key?\n4. Login button does nothing on Safari 17",
     "check": check_classify},
    # ---- summary
    {"cat": "summary", "id": "log_rootcause", "max_tokens": 400,
     "prompt": "Here is a production log. In at most 80 words: what failed, the most likely root cause, "
               "and what fixed it.\n\n" + LOG_TEXT,
     "check": check_keywords([["payments"], ["pool"], ["v2.14", "deploy", "release"],
                              ["rollback", "rolled back", "revert"]], 110)},
    {"cat": "summary", "id": "doc_facts", "max_tokens": 400,
     "prompt": "Summarize the following doc as at most 6 short bullets for a new engineer.\n\n" + DOC_TEXT,
     "check": check_keywords([["csv"], ["parquet"], ["500 mb", "500mb"], ["7 days", "seven days"],
                              ["3 times", "three times", "3 retries", "3 attempts", "retried 3", "retries: 3"],
                              ["x-api-key"]], 140)},
    # ---- reasoning
    {"cat": "reason", "id": "boxes", "max_tokens": 600,
     "prompt": "A warehouse has 3 shelves with 17 boxes each. 8 boxes are removed from each shelf, then 5 new "
               "boxes are added in total. How many boxes are there now? End with a line 'ANSWER: <number>'.",
     "check": check_answer("32")},
    {"cat": "reason", "id": "weekday", "max_tokens": 600,
     "prompt": "Today is Wednesday. What day of the week will it be 100 days from today? "
               "End with a line 'ANSWER: <day>'.",
     "check": check_answer("friday")},
    {"cat": "reason", "id": "ordering", "max_tokens": 600,
     "prompt": "Alice is older than Bob. Carol is younger than Bob. Dan is older than Alice. "
               "Who is the second youngest? End with a line 'ANSWER: <name>'.",
     "check": check_answer("bob")},
    {"cat": "reason", "id": "code_trace", "max_tokens": 600,
     "prompt": "What does this print?\n```python\nx = [1, 2, 3, 4, 5]\nprint(sum(v*v for v in x if v % 2) - len(x))\n```\n"
               "End with a line 'ANSWER: <number>'.",
     "check": check_answer("30")},
    # ---- harder, discriminating tasks (suite v2)
    {"cat": "code", "id": "code_edit", "max_tokens": 900,
     "prompt": "Modify this function: add an optional `default` parameter. If any key along the path is "
               "missing (or an intermediate value is not a dict), return `default` when it was given, "
               "otherwise raise KeyError. `default=None` passed explicitly must return None, so use a "
               "module-level sentinel object (e.g. `_MISSING = object()`) as the parameter's default. "
               "Return only the full updated code.\n\n```python\n" + GET_PATH + "```",
     "check": check_code("d={'a':{'b':{'c':3}},'x':5}\nassert get_path(d,'a.b.c')==3\n"
                         "assert get_path(d,'a.z',default=7)==7\nassert get_path(d,'x.y',default=1)==1\n"
                         "assert get_path(d,'a.z',default=None) is None\n"
                         "try:\n    get_path(d,'a.z')\n    raise SystemExit('no KeyError')\nexcept KeyError:\n    pass")},
    {"cat": "json", "id": "tool_plan", "max_tokens": 400,
     "prompt": "Tools: git_log{path, n}, read_file{path}, grep{pattern, path}, run_tests{target}.\n"
               "User: 'tests/test_api.py started failing after yesterday's changes, find out why'.\n"
               'Reply with JSON only: a list of exactly 3 tool calls, in the order you would make them, '
               'each {"tool": <name>, "args": {...}} using only the listed argument names.',
     "check": check_tool_plan},
    {"cat": "summary", "id": "long_needle", "max_tokens": 200,
     "prompt": "Service inventory:\n\n" + _long_services_doc() + "\n\nWhich service sends invoices to "
               'customers? Reply with JSON only: {"service": ..., "port": <int>, "retries": <int>}',
     "check": check_needle},
    {"cat": "summary", "id": "strict_format", "max_tokens": 200,
     "prompt": "Summarize this doc as exactly 3 bullet points, each at most 12 words. Output only the "
               "3 bullets, nothing before or after.\n\n" + DOC_TEXT,
     "check": check_strict_bullets},
]
# Bump when SUITE changes: older results are re-run by a plain `llocal bench`.
SUITE_VERSION = 3
QUICK_IDS = {"palindrome", "merge_intervals", "code_edit", "order_extract", "tool_plan", "log_rootcause",
             "long_needle", "strict_format", "boxes", "weekday"}
CATEGORIES = ["code", "json", "summary", "reason"]


def bench_model(model, cfg, quick=False):
    tasks = [t for t in SUITE if not quick or t["id"] in QUICK_IDS]
    per_cat = {c: [] for c in CATEGORIES}
    tps_list, details, errors = [], [], 0
    # warm-up (model load time should not count against speed)
    try:
        chat(model, [{"role": "user", "content": "Say OK."}], cfg, max_tokens=8)
    except Exception as e:
        return {"error": str(e)[:300]}
    for t in tasks:
        if len(tps_list) >= 2 and max(tps_list) < cfg["min_tps"]:
            return {"error": f"too slow: {max(tps_list):.1f} tok/s < min_tps={cfg['min_tps']} "
                             "(running on CPU?); not used", "suite": SUITE_VERSION}
        msgs = [{"role": "system", "content": SYSTEM_PROMPTS[t["cat"]]},
                {"role": "user", "content": t["prompt"]}]
        try:
            r = chat(model, msgs, cfg, max_tokens=t["max_tokens"], temperature=0.0)
            score = float(t["check"](r["text"]))
            if r["completion_tokens"] >= 20:
                tps_list.append(r["tps"])
            d = {"id": t["id"], "score": round(score, 2), "s": round(r["seconds"], 1)}
            if score < 1:
                d["answer"] = r["text"][:600]  # to see *why* it lost points (`llocal report` / bench.json)
            details.append(d)
        except Exception as e:
            errors += 1
            score = 0.0
            details.append({"id": t["id"], "score": 0, "error": str(e)[:200]})
        per_cat[t["cat"]].append(score)
        eprint(f"    {t['id']:<16} {score:.2f}")
    scores = {c: round(sum(v) / len(v), 3) for c, v in per_cat.items() if v}
    tps_list.sort()
    tps = tps_list[len(tps_list) // 2] if tps_list else 0.0
    return {"scores": scores, "overall": round(sum(scores.values()) / len(scores), 3),
            "tps": round(tps, 1), "errors": errors, "details": details, "quick": quick,
            "suite": SUITE_VERSION,
            "date": time.strftime("%Y-%m-%d %H:%M")}


# --------------------------------------------------------------------------- routing

def load_bench():
    try:
        with open(BENCH_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # skip a corrupt/truncated line instead of failing
    except OSError:
        pass
    return rows


def field_stats():
    """{(model_id, category): [ok, total]} from `llocal feedback`; category None = all tasks."""
    st = {}
    for r in read_jsonl(FEEDBACK_PATH):
        for key in ((r.get("model"), TASK_TO_CATEGORY.get(r.get("task"))), (r.get("model"), None)):
            ok_n = st.setdefault(key, [0, 0])
            ok_n[0] += r.get("verdict") == "ok"
            ok_n[1] += 1
    return st


# Real-world feedback counts like this many benchmark samples' worth of prior: a few bad
# verdicts lower a model's score, a long track record dominates it.
PRIOR_WEIGHT = 3


def blended(score, ok_n):
    ok, n = ok_n or (0, 0)
    return (PRIOR_WEIGHT * score + ok) / (PRIOR_WEIGHT + n)


def check_task(task):
    if task not in TASK_TO_CATEGORY:
        raise Fail(1, f"unknown task '{task}'", "use one of: " + ", ".join(TASK_TO_CATEGORY))


def rank(task, cfg, models=None):
    """Return list of (rank_value, cat_score, model_id, result) best first, filtered by min_score."""
    check_task(task)
    cat = TASK_TO_CATEGORY[task]
    bench = load_bench()
    available = {m["id"] for m in (models if models is not None else discover(cfg))}
    field = field_stats()
    out = []
    for mid, res in bench.items():
        if "scores" not in res or mid not in available:
            continue
        s = blended(res["overall"] if cat is None else res["scores"].get(cat, 0), field.get((mid, cat)))
        if s < cfg["min_score"]:
            continue
        speed = min(res.get("tps", 0) / 40.0, 1.0)  # 40 tok/s counts as "fast enough"
        out.append((round(s + 0.15 * speed, 4), s, mid, res))
    # Ties (common: a category has only a few tests) go to the better all-round model, then the
    # faster one; otherwise a fast but weak model wins a category on a lucky 2/2.
    out.sort(key=lambda x: (x[0], x[3].get("overall", 0), x[3].get("tps", 0)), reverse=True)
    return out


def no_model_failure(task, cfg):
    """Explain *why* no model was picked, so an agent knows whether to give up or report it."""
    bench = {k: v for k, v in load_bench().items() if "scores" in v}
    if not bench:
        return Fail(3, "no benchmark results yet, so no local model is trusted",
                    f"do the task yourself; a human can run `{LLOCAL} bench --quick` once to enable llocal")
    cat = TASK_TO_CATEGORY[task]
    good = [k for k, v in bench.items()
            if (v["overall"] if cat is None else v["scores"].get(cat, 0)) >= cfg["min_score"]]
    if good:
        return Fail(2, f"benchmarked models for '{task}' are not reachable now ({', '.join(map(short, good[:3]))})",
                    f"is Ollama / llama-server running? see `{LLOCAL} doctor`; meanwhile " + NO_MODEL_HINT)
    return Fail(3, f"no local model scores >= min_score={cfg['min_score']} for '{task}'", NO_MODEL_HINT)


def find_model(mid, cfg):
    models = discover(cfg)
    for m in models:
        if mid in (m["id"], m["name"], short(m["id"]), "ollama:" + m["name"], nick(m["id"])):
            return m
    # forgiving: a unique partial name ("gpt-oss" or "gpt-oss-derestricted:20b") is enough
    hits = [m for m in models if mid.lower() in m["id"].lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise Fail(1, f"model '{mid}' is ambiguous: " + ", ".join(nick(m["id"]) for m in hits[:6]),
                   "pass one of those names")
    raise Fail(1, f"model '{mid}' not found", f"run `{LLOCAL} sheet` to list usable local models")


# --------------------------------------------------------------------------- commands

def cmd_doctor(args, cfg):
    budget, why = memory_budget(cfg)
    print(f"max model file size: {gb(budget)}  ({why})")
    room, hogs = gpu_room(cfg)
    if room is not None:
        print(f"VRAM usable right now: {gb(room)}" + (f"   other GPU users: {hogs}" if hogs else ""))
    print(f"Ollama ({cfg['ollama_url']}): {'OK' if alive(cfg['ollama_url']) else 'not running'}")
    lb = shutil.which(cfg["llama_server_bin"])
    print(f"llama-server binary: {lb or 'not found (optional; install llama.cpp to use .gguf files)'}")
    print(f"state dir: {HOME}")
    models = discover(cfg, verbose=True)
    bench = load_bench()
    if not models:
        print("\nNo local models found. Example: `ollama pull qwen2.5-coder:7b`, or put .gguf files in ~/models")
        return
    print(f"\n{'model':<60} {'size':>8}  fits  benchmarked")
    for m in sorted(models, key=lambda x: x["size"]):
        print(f"{short(m['id'])[:60]:<60} {gb(m['size']) if m['size'] else '?':>8}  "
              f"{'yes ' if fits(m, budget) else 'NO  '}  {'yes' if m['id'] in bench else '-'}")
    if not any("scores" in v for v in bench.values()):
        print(f"\nNext: `{LLOCAL} bench --quick` (loads each model once; large models on a GPU take a while)")


def cmd_bench(args, cfg):
    budget, why = memory_budget(cfg)
    models = discover(cfg, verbose=True)
    if args.model:
        models = [m for m in models if args.model in m["id"]]
    bench = load_bench()
    todo = []
    for m in models:
        if not fits(m, budget) and not args.force:
            eprint(f"skip {short(m['id'])} ({gb(m['size'])} > {gb(budget)}: {why}; --force to try)")
            continue
        prev = bench.get(m["id"], {})
        if "scores" in prev and prev.get("suite", 1) == SUITE_VERSION and not args.redo:
            eprint(f"skip {short(m['id'])} (already benchmarked; --redo to repeat)")
            continue
        if str(prev.get("error", "")).startswith("too slow") and prev.get("suite") == SUITE_VERSION \
                and not args.redo:
            eprint(f"skip {short(m['id'])} ({prev['error'][:40]}...; --redo to retry)")
            continue
        todo.append(m)
    if not todo:
        eprint("nothing to benchmark")
    for i, m in enumerate(todo, 1):
        eprint(f"[{i}/{len(todo)}] {short(m['id'])}  ({gb(m['size']) if m['size'] else '?'})  loading...")
        room, hogs = wait_for_room(cfg, needs_room(m))
        if room is not None and needs_room(m) > room and not args.force:
            eprint(f"    skipped: GPU busy, {gb(room)} VRAM free, needs ~{gb(needs_room(m))}"
                   + (f" (in use by: {hogs})" if hogs else "") + "; free it and re-run, or --force")
            continue
        res = bench_model(m, cfg, quick=args.quick)
        res["size"] = m["size"]
        bench[m["id"]] = res
        with open(BENCH_PATH, "w") as f:
            json.dump(bench, f, indent=2)
        if m["backend"] == "ollama":
            unload_ollama(cfg, m["name"])
        if "error" in res:
            eprint(f"    ERROR: {res['error']}")
    stop_server(quiet=True)
    cmd_report(args, cfg)


def cmd_report(args, cfg):
    bench = load_bench()
    rows = [(k, v) for k, v in bench.items() if "scores" in v]
    if not rows:
        print(f"no benchmark results yet; run `{LLOCAL} bench --quick`")
        return
    rows.sort(key=lambda kv: kv[1]["overall"], reverse=True)
    print(f"\n{'model':<48} {'code':>5} {'json':>5} {'summ':>5} {'reas':>5} {'all':>5} {'tok/s':>6}")
    for k, v in rows:
        s = v["scores"]
        print(f"{short(k)[:48]:<48} " + " ".join(f"{s.get(c, 0):>5.2f}" for c in CATEGORIES) +
              f" {v['overall']:>5.2f} {v['tps']:>6.1f}" + ("  (quick)" if v.get("quick") else ""))
    print("\nBest per task (min_score=%.2f):" % cfg["min_score"])
    models = discover(cfg)
    for task in ["code", "json", "summary", "reason", "general"]:
        r = rank(task, cfg, models)
        print(f"  {task:<8} -> {short(r[0][2]) if r else 'none (the agent does it itself)'}")


def cmd_sheet(args, cfg):
    """Compact cheat sheet for the calling agent: what each usable local model is good at,
    so a stronger model can route by itself (`run --model X`) instead of trusting `rank`."""
    bench = {k: v for k, v in load_bench().items() if "scores" in v}
    if not bench:
        raise no_model_failure("general", cfg)
    models = {m["id"]: m for m in discover(cfg)}
    room, hogs = gpu_room(cfg)
    print(f"# local models (scores 0-1 on llocal's suite; min_score={cfg['min_score']}; "
          f"call: {LLOCAL} run --model <model> ...)")
    if room is not None:
        print(f"# VRAM usable now: {gb(room)}" + (f" (busy: {hogs})" if hogs else ""))
    field = field_stats()
    print("model | GB | tok/s | code json summ reas | real ok/n | now | weak spots")
    for mid, v in sorted(bench.items(), key=lambda kv: -kv[1]["overall"]):
        if mid not in models:
            continue
        m = models[mid]
        now = "ok" if room is None or needs_room(m) <= room else "no-vram"
        weak = [f"{d['id']}={d['score']:g}" for d in v.get("details", []) if d.get("score", 0) < 1]
        sc = v["scores"]
        old = " (old suite: re-bench)" if v.get("suite", 1) != SUITE_VERSION else ""
        print(f"{nick(mid)}{old} | {m['size'] / 1024 ** 3:.0f} | {v.get('tps', 0):.0f} | "
              + " ".join(f"{sc.get(c, 0):.2f}" for c in CATEGORIES)
              + " | {}/{}".format(*field.get((mid, None), [0, 0])) + f" | {now} | "
              + (", ".join(weak) or "-"))
    routes = []
    for task in ["code", "json", "summary", "reason"]:
        r = rank(task, cfg, list(models.values()))
        routes.append(f"{task}->{nick(r[0][2]) if r else 'none'}")
    print("# default routes: " + ", ".join(routes))
    print("# weak spots = benchmark tests below 1 (code: palindrome merge_intervals code_edit; json: "
          "order_extract tool_plan; summ: log_rootcause long_needle strict_format; reas: boxes weekday). "
          "Models can't run tools.")


def cmd_pick(args, cfg):
    r = rank(args.task, cfg)
    if not r:
        print("none")
        raise no_model_failure(args.task, cfg)
    print(r[0][2] if not args.verbose else json.dumps(
        [{"model": x[2], "score": x[1], "tps": x[3].get("tps")} for x in r], indent=2))


def read_files(paths, max_chars):
    parts, total = [], 0
    for p in paths:
        try:
            with open(p, errors="replace") as f:
                content = f.read()
        except OSError as e:
            raise Fail(1, f"cannot read -f {p}: {e.strerror}", "pass existing regular files")
        total += len(content)
        if total > max_chars:
            raise Fail(1, f"input too large for the local context (~{total * 2 // 7} tokens, limit "
                          f"~{max_chars * 2 // 7})", "split it into smaller calls, or do it yourself")
        parts.append(f"=== FILE: {p} ===\n{content}\n=== END FILE: {p} ===")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- explore tools

LIST_CAP, GREP_CAP, READ_CAP, LINE_CAP = 200, 50, 200, 400
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


class ToolError(Exception):
    """A tool call the model got wrong: the message goes back to the model as the result."""


def explore_root(path):
    """(root_dir, single_file_rel_or_None) for `--explore PATH`."""
    p = os.path.realpath(os.path.expanduser(path))
    if os.path.isdir(p):
        return p, None
    if os.path.isfile(p):
        return os.path.dirname(p), os.path.basename(p)
    raise Fail(1, f"--explore {path}: no such file or directory", "pass an existing directory or one file")


def safe_path(root, rel):
    """Absolute real path of `rel` under `root`; symlinks are resolved before the check."""
    if not isinstance(rel, str) or not rel.strip():
        raise ToolError("path is required")
    root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        raise ToolError(f"{rel}: outside the explored root")
    if SKIP_DIRS & set(os.path.relpath(p, root).split(os.sep)):
        raise ToolError(f"{rel}: not searchable")
    if not os.path.exists(p):
        raise ToolError(f"{rel}: no such file")
    return p


def _is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(4096)
    except OSError:
        return True


def _glob_match(rel, pat):
    return fnmatch.fnmatch(rel, pat) or (pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]))


def _walk(root, pat, log):
    """Root-relative text files matching `pat`; only `log["only"]` for a single-file root."""
    pat = pat or "**/*"
    if not isinstance(pat, str):
        raise ToolError("glob must be a string")
    if log.get("only"):
        cands = [log["only"]]
    else:
        cands = []
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(d, name), root)
                if _glob_match(rel, pat):
                    cands.append(rel)
    for rel in cands:
        try:
            p = safe_path(root, rel)
        except ToolError:
            continue
        if os.path.isfile(p) and not _is_binary(p):
            yield rel, p


def _lines(path):
    with open(path, errors="replace") as f:
        for n, line in enumerate(f, 1):
            yield n, line.rstrip("\n")


def _clip(line):
    return line if len(line) <= LINE_CAP else f"{line[:LINE_CAP]} ...(+{len(line) - LINE_CAP} chars)"


def tool_list_files(root, log, glob="**/*"):
    files = list(_walk(root, glob, log))
    if not files:
        return "no files match"
    out = [f"{rel} ({os.path.getsize(p)} B)" for rel, p in files[:LIST_CAP]]
    if len(files) > LIST_CAP:
        out.append(f"({len(files)} files, showing {LIST_CAP}; narrow the glob)")
    return "\n".join(out)


def tool_grep(root, log, pattern, glob="**/*"):
    rx = re.compile(pattern)
    hits, total = [], 0
    for rel, p in _walk(root, glob, log):
        for n, line in _lines(p):
            if rx.search(line):
                total += 1
                if len(hits) < GREP_CAP:
                    hits.append(f"{rel}:{n}: {_clip(line)}")
    return "\n".join(hits + [f"({total} matches, showing {len(hits)})"])


def tool_read_lines(root, log, path, start=1, end=None):
    if log.get("only") and os.path.normpath(path) != log["only"]:
        raise ToolError(f"only {log['only']} can be read")
    p = safe_path(root, path)
    if not os.path.isfile(p) or _is_binary(p):
        raise ToolError(f"{path}: not a text file")
    start = max(1, int(start))
    end = int(end) if end is not None else start + READ_CAP - 1
    last = min(end, start + READ_CAP - 1)
    out, total = [], 0
    for n, line in _lines(p):
        total = n
        if start <= n <= last:
            out.append(f"{n}: {_clip(line)}")
    if not out:
        raise ToolError(f"{path} has {total} lines; start={start} is past the end")
    shown = int(out[-1].split(":", 1)[0])
    if shown < min(end, total):
        out.append(f"(showing {start}-{shown} of {total} lines; call again with start={shown + 1})")
    return "\n".join(out)


def tool_count(root, log, pattern, glob="**/*"):
    rx = re.compile(pattern)
    per, total = [], 0
    for rel, p in _walk(root, glob, log):
        n = sum(1 for _, line in _lines(p) if rx.search(line))
        if n:
            per.append(f"{rel}: {n}")
            total += n
    log.setdefault("counts", {})[pattern] = total
    return "\n".join(per[:LIST_CAP] + [f"total: {total} matching lines"])


TOOLS = {"list_files": tool_list_files, "grep": tool_grep, "read_lines": tool_read_lines,
         "count": tool_count}


def _spec(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_GLOB = {"type": "string", "description": "file glob relative to the root, e.g. **/*.py (default: all)"}
TOOL_SPECS = [
    _spec("list_files", "List text files under the root with their sizes.", {"glob": _GLOB}, []),
    _spec("grep", f"Search lines with a Python regex. Returns path:line: text (max {GREP_CAP}) "
                  "and the total number of matches.",
          {"pattern": {"type": "string", "description": "Python regular expression"}, "glob": _GLOB},
          ["pattern"]),
    _spec("read_lines", f"Read numbered lines of one file (max {READ_CAP} per call).",
          {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
          ["path", "start", "end"]),
    _spec("count", "Count the lines matching a Python regex, per file and in total. Use it for any "
                   "'how many' question.",
          {"pattern": {"type": "string", "description": "Python regular expression"}, "glob": _GLOB},
          ["pattern"]),
]


def run_tool(root, name, args, log):
    """Run one tool call; any mistake by the model comes back as `error: ...` text."""
    try:
        fn = TOOLS.get(name)
        if fn is None:
            raise ToolError(f"unknown tool {name!r}; use one of {', '.join(TOOLS)}")
        if not isinstance(args, dict):
            raise ToolError("arguments must be a JSON object")
        out = fn(root, log, **args)
    except ToolError as e:
        out = f"error: {e}"
    except re.error as e:
        out = f"error: invalid regex: {e}"
    except (TypeError, ValueError) as e:
        out = f"error: bad arguments: {e}"
    except OSError as e:
        out = f"error: {e.strerror or e}"
    log["read_chars"] = log.get("read_chars", 0) + len(out)
    return out


def cmd_run(args, cfg):
    check_task(args.task)
    if args.timeout:
        cfg["request_timeout"] = args.timeout
    # stdin is read only when asked (`-` in the prompt words): inside agents stdin is often an
    # open non-tty pipe and reading it blindly would hang forever.
    words = list(args.prompt or [])
    use_stdin = "-" in words
    words = [w for w in words if w != "-"]
    prompt = " ".join(words)
    piped = ""
    if use_stdin and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            prompt = (prompt + "\n\n" + piped) if prompt else piped
        else:
            piped = ""
    if not prompt.strip():
        raise Fail(1, "empty prompt", "give the instruction as an argument; add '-' to also read stdin")
    budget_tokens = cfg["ctx"] - args.max_tokens - 512
    if budget_tokens < 256:
        raise Fail(1, f"--max-tokens {args.max_tokens} leaves no room for input in ctx={cfg['ctx']}",
                   "lower --max-tokens or raise ctx in config")
    # 3.5 chars/token is optimistic for dense code; Ollama silently drops the start of a prompt
    # that overflows num_ctx, so stay on the safe side.
    max_input_chars = int(budget_tokens * 3.5)
    files = list(args.files)
    for pat in args.globs:
        hits = sorted(p for p in glob.glob(os.path.expanduser(pat), recursive=True) if os.path.isfile(p))
        if not hits:
            raise Fail(1, f"--glob {pat!r} matched no files", "check the pattern; quote it so the shell doesn't expand it")
        files += [p for p in hits if p not in files]
    ctx_text = read_files(files, max_input_chars) if files else ""
    user = (ctx_text + "\n\n" + prompt) if ctx_text else prompt
    if len(user) > max_input_chars:
        raise Fail(1, f"input too large for the local context (~{len(user) * 2 // 7} tokens, limit "
                      f"~{budget_tokens})", "split it into smaller calls, or do it yourself")

    if args.model:
        model = find_model(args.model, cfg)
    else:
        models = discover(cfg)
        r = rank(args.task, cfg, models)
        if not r:
            raise no_model_failure(args.task, cfg)
        by_id = {m["id"]: m for m in models}
        room, hogs = gpu_room(cfg)
        fitting = [x for x in r if room is None or needs_room(by_id[x[2]]) <= room]
        if not fitting:  # maybe memory is still being released: wait for the smallest candidate
            room, hogs = wait_for_room(cfg, min(needs_room(by_id[x[2]]) for x in r))
            fitting = [x for x in r if room is None or needs_room(by_id[x[2]]) <= room]
        if not fitting:
            raise Fail(2, f"GPU busy: only {gb(room)} VRAM free, the best model for '{args.task}' "
                          f"needs ~{gb(needs_room(by_id[r[0][2]]))}" + (f" (in use by: {hogs})" if hogs else ""),
                       "do the task yourself now; tell the user the GPU is busy")
        model = by_id[fitting[0][2]]

    cat = TASK_TO_CATEGORY[args.task]
    msgs = [{"role": "system", "content": SYSTEM_PROMPTS.get(cat, SYSTEM_PROMPTS[None])},
            {"role": "user", "content": user}]
    eprint(f"[llocal] {short(model['id'])} working... (first call loads the model; can take a minute)")
    res = chat(model, msgs, cfg, max_tokens=args.max_tokens, temperature=args.temperature)
    out = res["text"]
    if not out.strip():
        raise Fail(2, f"{short(model['id'])} returned an empty answer (maybe cut off while thinking)",
                   "retry once with a larger --max-tokens, or do it yourself")
    if args.json:
        d = parse_json_loose(out)
        if d is None:
            raise Fail(4, "model did not return valid JSON", "retry once, or do it yourself")
        out = json.dumps(d, indent=2, ensure_ascii=False)
    if args.out:
        try:
            with open(args.out, "w") as f:
                f.write(out + "\n")
        except OSError as e:
            raise Fail(1, f"cannot write --out {args.out}: {e.strerror}")
        print(f"wrote {args.out} ({len(out)} chars)")
    else:
        print(out)
    words_text = " ".join(words)
    row = {"ts": time.time(), "task": args.task, "model": model["id"],
           "in": res["prompt_tokens"] or est_tokens(user), "out": res["completion_tokens"],
           "in_ctx": est_tokens(ctx_text + piped) if (ctx_text or piped) else 0,
           "in_prompt": est_tokens(words_text) if words_text else 0,
           "to_file": bool(args.out), "s": round(res["seconds"], 1)}
    with open(USAGE_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")
    eprint(f"[llocal] {short(model['id'])}  in={res['prompt_tokens']} out={res['completion_tokens']} "
           f"{res['tps']:.0f} tok/s  {res['seconds']:.1f}s  -> after checking it: `{LLOCAL} feedback ok|bad`")
    if net_saving(row, cfg) < 0:
        eprint("[llocal] note: this run likely cost more API tokens than doing it yourself (small input, "
               "answer read back); offload inputs of ~1k+ tokens passed by -f/--glob/stdin")


def cmd_feedback(args, cfg):
    runs = read_jsonl(USAGE_PATH)
    if args.model:
        model = find_model(args.model, cfg)["id"]
        task = args.task or next((r["task"] for r in reversed(runs) if r.get("model") == model), "general")
    elif runs:
        model, task = runs[-1]["model"], args.task or runs[-1]["task"]
    else:
        raise Fail(1, "no local run to give feedback on", "run `llocal run ...` first, or pass --model")
    check_task(task)
    ensure_home()
    with open(FEEDBACK_PATH, "a") as f:
        f.write(json.dumps({"ts": time.time(), "model": model, "task": task, "verdict": args.verdict,
                            "note": args.note or ""}) + "\n")
    ok, n = field_stats().get((model, TASK_TO_CATEGORY[task]), [0, 0])
    print(f"recorded {args.verdict} for {short(model)} on '{task}' ({ok}/{n} ok in that category)")


def cmd_stats(args, cfg):
    if not os.path.exists(USAGE_PATH):
        print("no local runs yet")
        return
    rows = read_jsonl(USAGE_PATH)
    # A `feedback bad` applies to the latest run of that model before it: the agent redid that work.
    bad = set()
    for fb in read_jsonl(FEEDBACK_PATH):
        if fb.get("verdict") != "bad":
            continue
        prior = [i for i, r in enumerate(rows) if r.get("model") == fb.get("model") and r.get("ts", 0) <= fb.get("ts", 0)]
        if prior:
            bad.add(prior[-1])
    tin = sum(r.get("in", 0) for r in rows)
    tout = sum(r.get("out", 0) for r in rows)
    usd = sum(net_saving(r, cfg) for i, r in enumerate(rows) if i not in bad)
    losing = sum(1 for i, r in enumerate(rows) if i not in bad and net_saving(r, cfg) < 0)
    by_task = {}
    for r in rows:
        by_task[r.get("task", "?")] = by_task.get(r.get("task", "?"), 0) + 1
    print(f"local runs: {len(rows)}   tokens in: {tin:,}   tokens out: {tout:,}")
    print(f"estimated net API cost saved: ${usd:.2f}  (at ${cfg['price_in_per_mtok']}/M in, "
          f"${cfg['price_out_per_mtok']}/M out — edit in {CONFIG_PATH})")
    print(f"not counted: {len(bad)} run(s) rated bad; runs that cost more than they saved: {losing}")
    print("by task: " + ", ".join(f"{k}={v}" for k, v in sorted(by_task.items())))


def cmd_stop(args, cfg):
    stop_server()
    unload_ollama_all(cfg)


def main():
    p = argparse.ArgumentParser(prog="llocal", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="show memory budget, backends and models")
    b = sub.add_parser("bench", help="benchmark local models")
    b.add_argument("--quick", action="store_true", help="7 tasks instead of 13")
    b.add_argument("--redo", action="store_true", help="re-run models already benchmarked")
    b.add_argument("--force", action="store_true", help="also try models larger than the memory budget")
    b.add_argument("--model", help="only models whose id contains this text")
    sub.add_parser("report", help="show benchmark table")
    sub.add_parser("sheet", help="compact model cheat sheet for agents that route themselves")
    k = sub.add_parser("pick", help="print the best local model for a task (or 'none', exit 3)")
    k.add_argument("--task", default="general", help="|".join(TASK_TO_CATEGORY))
    k.add_argument("-v", "--verbose", action="store_true")
    r = sub.add_parser("run", help="run a prompt on the best local model")
    r.add_argument("prompt", nargs="*")
    r.add_argument("--task", default="general", help="|".join(TASK_TO_CATEGORY))
    r.add_argument("-f", "--file", dest="files", action="append", default=[],
                   help="file given to the model as context (repeatable: -f a.py -f b.py)")
    r.add_argument("-g", "--glob", dest="globs", action="append", default=[],
                   help="quoted glob of context files, ** recursive (repeatable: -g 'src/**/*.py')")
    r.add_argument("--model", help="force a model id/name")
    r.add_argument("--max-tokens", type=int, default=2048)
    r.add_argument("--temperature", type=float, default=0.2)
    r.add_argument("--timeout", type=int, help="seconds to wait for the model (default: request_timeout)")
    r.add_argument("--out", help="write the answer to this file instead of stdout")
    r.add_argument("--json", action="store_true", help="validate and pretty-print JSON output")
    fb = sub.add_parser("feedback", help="record whether the last run's output was right (after verifying)")
    fb.add_argument("verdict", choices=["ok", "bad"])
    fb.add_argument("--model", help="model to rate (default: the one used by the last run)")
    fb.add_argument("--task", help="task type to rate (default: the last run's)")
    fb.add_argument("--note", help="short reason, e.g. 'tests failed: off-by-one'")
    sub.add_parser("stats", help="tokens offloaded and estimated savings")
    sub.add_parser("stop", help="stop llama-server / unload Ollama models")
    args = p.parse_args()
    try:
        cfg = load_config()
        {"doctor": cmd_doctor, "bench": cmd_bench, "report": cmd_report, "sheet": cmd_sheet, "pick": cmd_pick, "run": cmd_run,
         "feedback": cmd_feedback, "stats": cmd_stats, "stop": cmd_stop}[args.cmd](args, cfg)
    except Fail as e:
        eprint(f"llocal: error: {e}" + (f"\nllocal: next: {e.hint}" if e.hint else "") + f"  [exit {e.code}]")
        sys.exit(e.code)
    except RuntimeError as e:
        eprint(f"llocal: error: {e}\nllocal: next: {BACKEND_HINT}  [exit 2]")
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
