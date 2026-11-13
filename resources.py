"""Recursos da máquina do quadro: amostra o /proc a cada 5 s (30 s no modo economia), guarda 30 min de histórico,
agrupa o consumo pelo que faz sentido (sessões do Claude, Jarvis, quadro, testes esquecidos, seus outros processos,
sistema), no WSL pergunta ao Windows quanto o WSL ocupa (VmmemWSL) e executa as ações de gerenciamento com travas.

O servidor do quadro chama `start(...)` com as funções dele (sessões, testes esquecidos, modo economia, cache) e expõe
`snapshot()` e as ações em /api/resources*. Página: recursos.html.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

HZ = os.sysconf("SC_CLK_TCK")
PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 2**20
UID = os.getuid()
SESSION_RE = re.compile(r"--(?:resume|session-id)[ =]([0-9a-f]{8}-[0-9a-f-]{27})")
# Nunca encerrados pela página, mesmo sendo seus: derrubariam o quadro, o tmux (todas as sessões) ou o WSL
PROTECTED_COMM = {"tmux: server", "tmux", "systemd", "(sd-pam)", "init", "dbus-daemon", "sshd", "login", "Relay(9)"}

history = deque(maxlen=360)  # 30 min a cada 5 s
_lock = threading.Lock()
_last = {"sample": None}
_prev = {"cpu": None, "ticks": {}, "t": 0.0}
_cache = {"sessions": ({}, 0.0), "leftovers": ([], 0.0), "docker": ([], 0.0), "windows": (None, 0.0)}
hooks = {}


def tr(msg, **kw):
    """Texto para a tela no idioma do quadro (server.py passa `tr` em hooks; sem ele, pt-BR)."""
    f = hooks.get("tr")
    return f(msg, **kw) if f else (msg.format(**kw) if kw else msg)


# ---------- coleta ----------

def cpu_times():
    out = []
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("cpu"):
            f = line.split()
            vals = [int(x) for x in f[1:9]]
            out.append((sum(vals), vals[3] + vals[4]))  # total, ocioso (idle + iowait)
    return out


def cpu_pct(prev, cur):
    """[total, núcleo 0, núcleo 1, …] em %."""
    return [round(100 * (1 - (i1 - i0) / (t1 - t0)), 1) if t1 > t0 else 0.0 for (t0, i0), (t1, i1) in zip(prev, cur)]


def meminfo():
    m = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in Path("/proc/meminfo").read_text().splitlines()}
    total, avail = m["MemTotal"], m["MemAvailable"]
    return {"total": total, "used": total - avail, "free": m["MemFree"], "held": total - m["MemFree"],
            "cache": m.get("Cached", 0) + m.get("Buffers", 0) + m.get("SReclaimable", 0),
            "swap_total": m.get("SwapTotal", 0), "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}


def processes():
    """{pid: {pid, ppid, comm, cmd, rss (MB), ticks, uid}} de todos os processos visíveis."""
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            st = (d / "stat").read_text()
            rp = st.rindex(")")
            f = st[rp + 2:].split()
            out[int(d.name)] = {"pid": int(d.name), "ppid": int(f[1]), "comm": st[st.index("(") + 1:rp],
                                "cmd": (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()[:240],
                                "rss": round(int(f[21]) * PAGE_MB, 1), "ticks": int(f[11]) + int(f[12]), "uid": d.stat().st_uid}
        except (OSError, ValueError, IndexError):
            continue  # processo que acabou de sair
    return out


def _is_wsl():
    try:
        return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


WSL = _is_wsl()  # no WSL a página também mostra o lado do Windows (VmmemWSL, C:, arquivo .vhdx)


def disks():
    out = []
    for label, path in (("Linux (/)", "/"),) + ((("Windows (C:)", "/mnt/c"),) if WSL else ()) + extra_disks():
        try:
            u = shutil.disk_usage(path)
            out.append({"label": label, "total": u.total // 2**20, "used": (u.total - u.free) // 2**20})
        except OSError:
            pass
    return out


def extra_disks():
    """Discos extras a mostrar: KANBAN_DISKS="Rótulo=/caminho,Outro=/mnt/dados"."""
    out = []
    for x in os.environ.get("KANBAN_DISKS", "").split(","):
        label, _, path = x.partition("=")
        if label.strip() and path.strip():
            out.append((label.strip(), os.path.expanduser(path.strip())))
    return tuple(out)


def windows():
    """Memória do lado do Windows: VmmemWSL (o que o Gerenciador de Tarefas mostra) e o total/livre do Windows."""
    if not WSL:
        return None
    ps = ("$p = Get-Process -Name 'vmmem*' -ErrorAction SilentlyContinue | Select-Object WorkingSet64; "
          "$o = Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, FreePhysicalMemory; "
          "[pscustomobject]@{vmmem=$p; os=$o} | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout)
        vm = d.get("vmmem") or {}
        vm = vm[0] if isinstance(vm, list) else vm
        return {"vmmem": (vm.get("WorkingSet64") or 0) // 2**20, "total": d["os"]["TotalVisibleMemorySize"] // 1024,
                "free": d["os"]["FreePhysicalMemory"] // 1024}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None  # fora do WSL ou PowerShell indisponível: a página só não mostra o lado do Windows


def docker():
    """Containers (rodando e parados), com memória e CPU dos que estão rodando."""
    exe = hooks.get("docker", "docker")
    try:
        ps = subprocess.run([exe, "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"],
                            capture_output=True, text=True, timeout=20).stdout
        st = subprocess.run([exe, "stats", "--no-stream", "--format", "{{.ID}}\t{{.MemUsage}}\t{{.CPUPerc}}"],
                            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    stats = {f[0][:12]: (f[1].split(" /")[0], f[2]) for f in (l.split("\t") for l in st.splitlines()) if len(f) == 3}
    out = []
    for f in (l.split("\t") for l in ps.splitlines()):
        if len(f) == 5:
            mem, cpu = stats.get(f[0][:12], ("", ""))
            out.append({"id": f[0][:12], "name": f[1], "state": f[2], "status": f[3], "image": f[4], "mem": mem, "cpu": cpu})
    return out


def cached(key, every, fn):
    """Valor guardado de `fn`, renovado a cada `every` s (sessões, testes, Docker e Windows são caros para 5 s)."""
    val, ts = _cache[key]
    if time.time() - ts >= every:
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001  (uma fonte com erro não derruba a coleta)
            print(f"recursos ({key}): {e}", flush=True)
        _cache[key] = (val, time.time())
    return val


# ---------- agrupamento ----------

def group(ps, sessions, leftover_pids, board_pid):
    """Consumo por grupo, na ordem em que cada processo é atribuído (um processo conta num grupo só):
    sessões do Claude (com a árvore de comandos delas), Jarvis, testes esquecidos, o quadro, seus outros, sistema."""
    kids = {}
    for p in ps.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])

    def tree(pid):
        seen, stack = [], [pid]
        while stack:
            x = stack.pop()
            if x in ps and x not in seen:
                seen.append(x)
                stack += kids.get(x, [])
        return seen

    claimed, groups = set(), []

    def add(kind, key, label, pids, **extra):
        pids = [x for x in dict.fromkeys(pids) if x not in claimed]
        if not pids:
            return
        claimed.update(pids)
        items = sorted((ps[x] for x in pids), key=lambda p: -p["rss"])
        groups.append({"kind": kind, "key": key, "label": label, "rss": round(sum(p["rss"] for p in items), 1),
                       "cpu": round(sum(p.get("cpu", 0.0) for p in items), 1), "count": len(items),
                       "procs": [{k: p[k] for k in ("pid", "comm", "cmd", "rss")} | {"cpu": p.get("cpu", 0.0),
                                  "can_kill": can_kill(p, ps)} for p in items[:25]], **extra})

    for p in sorted(ps.values(), key=lambda p: -p["rss"]):  # sessões do Claude (interativas)
        if p["comm"] == "claude" and p["uid"] == UID and " -p" not in f" {p['cmd']} ":
            m = SESSION_RE.search(p["cmd"])
            s = sessions.get(m.group(1), {}) if m else {}
            label = (f"{s.get('issue')} · " if s.get("issue") else "") + (s.get("title") or "")
            add("session", m.group(1) if m else str(p["pid"]), label.strip(" ·") or (tr("Claude fora do quadro") if not s else tr("Sessão do Claude")),
                tree(p["pid"]), session=m.group(1) if m and s else None, org=s.get("org"), window=s.get("window"),
                status=s.get("status"), in_board=bool(s.get("window")))
    add("jarvis", "jarvis", tr("Jarvis (voz e cérebro)"),
        # a voz é o Python que roda o piper_server.py (o tmux também tem esse texto no comando da janela `voz`)
        [x for p in ps.values() if p["uid"] == UID and ((p["comm"].startswith("python") and "piper_server.py" in p["cmd"])
                                                       or (p["comm"] == "claude" and " -p" in f" {p['cmd']} "))
         for x in tree(p["pid"])])
    add("leftover", "leftovers", tr("Testes esquecidos"), [x for pid in leftover_pids for x in tree(pid)])
    add("board", "board", tr("Quadro (servidor e terminais do navegador)"), tree(board_pid))
    add("mine", "mine", tr("Seus outros processos"), [p["pid"] for p in ps.values() if p["uid"] == UID])
    add("system", "system", tr("Sistema e serviços (root: Docker, Postgres, RabbitMQ, Redis…)"), list(ps))
    return sorted(groups, key=lambda g: -g["rss"])


def protected(ps):
    """Processos que a página nunca encerra: o quadro e os ancestrais dele, os ancestrais de cada Claude (shell, tmux)
    e os do sistema que seguram o WSL."""
    out = set()

    def up(pid):
        while pid > 1 and pid in ps and pid not in out:
            out.add(pid)
            pid = ps[pid]["ppid"]
    up(hooks.get("board_pid", os.getpid()))
    for p in ps.values():
        if p["comm"] == "claude":
            up(p["ppid"])
        if p["comm"] in PROTECTED_COMM or p["pid"] == 1:
            out.add(p["pid"])
    return out


def can_kill(p, ps):
    return p["uid"] == UID and p["pid"] not in _last.get("protected", set()) and p["comm"] != "claude"


# ---------- amostragem ----------

def sample():
    now, cpu = time.time(), cpu_times()
    ps = processes()
    dt = now - _prev["t"] if _prev["t"] else 0
    for p in ps.values():  # CPU de cada processo desde a amostra anterior (100% = um núcleo inteiro)
        t0 = _prev["ticks"].get(p["pid"])
        p["cpu"] = round(100 * (p["ticks"] - t0) / HZ / dt, 1) if dt and t0 is not None and p["ticks"] >= t0 else 0.0
    pct = cpu_pct(_prev["cpu"], cpu) if _prev["cpu"] else [0.0] * len(cpu)
    _prev.update(cpu=cpu, ticks={p["pid"]: p["ticks"] for p in ps.values()}, t=now)
    _last["protected"] = protected(ps)
    sessions = cached("sessions", 15, hooks.get("sessions", lambda: {}))
    leftover_pids = cached("leftovers", 15, hooks.get("leftovers", lambda: []))
    mem, win = meminfo(), cached("windows", 30, windows)
    snap = {"t": now, "cpu": {"total": pct[0], "cores": pct[1:], "load": list(os.getloadavg()), "n": len(pct) - 1},
            "mem": mem, "windows": win, "disks": disks(), "docker": cached("docker", 10, docker),
            "groups": group(ps, sessions or {}, leftover_pids or [], hooks.get("board_pid", os.getpid())),
            "procs": len(ps), "wsl": WSL, "eco": bool(hooks.get("eco", lambda: False)()), "drop_cache": hooks.get("can_drop_cache", lambda: False)()}
    with _lock:
        _last["sample"] = snap
        history.append({"t": round(now), "cpu": pct[0], "used": mem["used"], "cache": mem["cache"],
                        "vmmem": (win or {}).get("vmmem")})
    return snap


def loop():
    while True:
        try:
            sample()
        except Exception as e:  # noqa: BLE001  (a coleta não pode morrer)
            print(f"recursos: {e}", flush=True)
        time.sleep(30 if hooks.get("eco", lambda: False)() else 5)


def start(**kw):
    """kw: sessions() -> {sid: sessão}, leftovers() -> [pid], eco() -> bool, can_drop_cache() -> bool,
    drop_cache() -> bool, docker (binário), board_pid, tr(msg, **kw) -> texto no idioma da tela."""
    hooks.update(kw)
    threading.Thread(target=loop, daemon=True).start()


def snapshot():
    with _lock:
        return {**(_last["sample"] or {}), "history": list(history)}


# ---------- ações (a página confirma antes; aqui ficam as travas) ----------

def kill(body):
    """Encerra um processo seu que a página mostrou como encerrável (TERM; KILL se não sair em 3 s). Sessão do Claude
    se encerra pelo card (/api/kill), que fecha a janela do quadro direito."""
    pid = int(body.get("pid") or 0)
    ps = processes()
    p = ps.get(pid)
    if not p:
        raise ValueError(tr("Esse processo já terminou"))
    if p["uid"] != UID or pid in protected(ps) or p["comm"] == "claude":
        raise ValueError(tr("Esse processo é protegido (sessão do Claude, quadro, tmux ou sistema)"))
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        if not Path(f"/proc/{pid}").exists():
            return {"ok": True, "how": "TERM"}
    os.kill(pid, signal.SIGKILL)
    return {"ok": True, "how": "KILL"}


def docker_action(body):
    cid, action = str(body.get("id") or ""), body.get("action")
    if action not in ("start", "stop", "restart"):
        raise ValueError(tr("Ação de container inválida"))
    if cid not in {c["id"] for c in docker()}:
        raise ValueError(tr("Container não encontrado"))
    r = subprocess.run([hooks.get("docker", "docker"), action, cid], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout).strip()[-200:] or tr("docker {action} falhou", action=action))
    _cache["docker"] = ([], 0.0)  # a próxima amostra relê
    return {"ok": True}


def drop_cache(_body):
    """Libera o cache de disco do Linux para o WSL devolver RAM ao Windows (regra de sudo kanban-eco)."""
    if not hooks.get("can_drop_cache", lambda: False)():
        raise ValueError(tr("Falta a regra de sudo: sudo bash ~/Projects/Pessoal/kanban-claude/tools/install-eco-sudo.sh"))
    before = meminfo()["held"]
    if not hooks["drop_cache"]():
        raise ValueError(tr("O sudo recusou liberar o cache"))
    time.sleep(1)
    return {"ok": True, "before": before, "after": meminfo()["held"]}


# ---------- limpeza de disco (análise sob demanda, em segundo plano: o du de worktrees grandes demora) ----------

_clean = {"data": None, "ts": 0.0, "busy": False}
CACHES = [("cache-uv", "Cache do uv (pacotes Python baixados)", "~/.cache/uv"),
          ("cache-pip", "Cache do pip", "~/.cache/pip"),
          ("cache-npm", "Cache do npm", "~/.npm/_cacache")]


def du_mb(path):
    try:
        r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=300)
        return int(r.stdout.split()[0]) // 1024
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def git(*args, timeout=60):
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def card_in(text, done):
    """Card concluído cujo id aparece no nome da branch/pasta (T-1 não casa com t-15)."""
    t = text.lower()
    return next((c for c in done if re.search(rf"(^|[^a-z0-9]){re.escape(c.lower())}($|[^0-9])", t)), None)


def worktrees():
    """Worktrees das organizações (não o checkout principal): tamanho, idade do último commit, se tem alteração não
    salva, se alguma sessão aberta está nele e se é de um card já concluído."""
    out, done = [], hooks.get("done_ids", lambda: set())()
    live = hooks.get("live_cwds", lambda: [])()
    for repo in hooks.get("repos", lambda: [])():
        if not Path(repo, ".git").is_dir():
            continue  # worktree também aparece em repos(); quem lista é o checkout principal
        for block in git("-C", repo, "worktree", "list", "--porcelain").split("\n\n")[1:]:  # o 1º é o próprio repositório
            f = dict((l.split(" ", 1) + [""])[:2] for l in block.splitlines())
            path, branch = f.get("worktree", ""), f.get("branch", "").replace("refs/heads/", "")
            if not path or not Path(path).is_dir():
                continue
            last = git("-C", path, "log", "-1", "--format=%ct")
            dirty = bool(git("-C", path, "status", "--porcelain", timeout=120))
            in_use = any(c == path or c.startswith(path + "/") for c in live)
            card = card_in(f"{branch} {Path(path).name}", done)
            age = round((time.time() - int(last)) / 86400) if last.isdigit() else None
            why = [x for x in (tr("card {card} concluído", card=card) if card else "",
                                tr("sem commit há {n} dias", n=age) if (age or 0) > 30 else "") if x]
            out.append({"kind": "worktree", "id": path, "label": f"{Path(repo).name} · {Path(path).name}", "mb": du_mb(path),
                        "detail": branch or tr("(sem branch)"), "age": age, "suggest": bool(why), "why": why,
                        "block": tr("tem alteração não salva") if dirty else (tr("sessão aberta nele") if in_use else ""), "repo": repo})
    return out


def live_session_ids():
    return {m.group(1) for p in processes().values() if p["comm"] == "claude" for m in [SESSION_RE.search(p["cmd"])] if m}


def scratch_dirs():
    """Pastas de rascunho (/tmp/claude-<uid>/<projeto>/<sessão>) de sessões que já não estão rodando, paradas há 1 dia."""
    base, live, out = Path(f"/tmp/claude-{UID}"), live_session_ids(), []
    for proj in (base.iterdir() if base.is_dir() else []):
        for d in (proj.iterdir() if proj.is_dir() else []):
            try:
                if d.is_dir() and d.name not in live and time.time() - d.stat().st_mtime > 86400:
                    out.append(d)
            except OSError:
                pass
    return out


def docker_df():
    try:
        r = subprocess.run([hooks.get("docker", "docker"), "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"],
                           capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {f[0]: (f[1], f[2]) for f in (l.split("\t") for l in r.splitlines()) if len(f) == 3}


def vhdx():
    """O arquivo do disco do Linux no C: (tamanho e se é esparso; não esparso não encolhe quando você apaga no Linux)."""
    if not WSL:
        return None
    ps = (r"Get-ChildItem -Path $env:LOCALAPPDATA\Packages\*\LocalState\ext4.vhdx,$env:LOCALAPPDATA\wsl\*\ext4.vhdx "
          r"-ErrorAction SilentlyContinue | Select-Object FullName, Length, "
          r"@{n='Sparse';e={($_.Attributes -band [IO.FileAttributes]::SparseFile) -ne 0}} | ConvertTo-Json -Compress")
    try:
        d = json.loads(subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                      capture_output=True, text=True, timeout=30).stdout)
        d = d[0] if isinstance(d, list) else d
        return {"path": d["FullName"], "mb": d["Length"] // 2**20, "sparse": bool(d["Sparse"])}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None


def cleanup_scan():
    items = worktrees()
    for key, label, path in CACHES:
        p = Path(path).expanduser()
        if p.is_dir():
            items.append({"kind": "cache", "id": key, "label": tr(label), "detail": str(p).replace(str(Path.home()), "~"),
                          "mb": du_mb(p), "suggest": True, "why": [tr("baixa de novo quando precisar")], "block": ""})
    sd = scratch_dirs()
    if sd:
        items.append({"kind": "scratch", "id": "scratch", "label": tr("Rascunhos de 1 sessão do Claude já encerrada") if len(sd) == 1
                      else tr("Rascunhos de {n} sessões do Claude já encerradas", n=len(sd)),
                      "detail": f"/tmp/claude-{UID}", "mb": sum(du_mb(d) or 0 for d in sd), "suggest": True,
                      "why": [tr("sessão encerrada há mais de 1 dia")], "block": ""})
    df = docker_df()
    if "Images" in df:
        items.append({"kind": "docker-images", "id": "docker-images", "label": tr("Imagens Docker sem uso por nenhum container"),
                      "detail": tr("recuperável: {free} de {total}", free=df["Images"][1], total=df["Images"][0]), "mb": None, "suggest": True,
                      "why": [tr("baixa ou constrói de novo quando precisar")], "block": ""})
    if "Build Cache" in df:
        items.append({"kind": "docker-build", "id": "docker-build", "label": tr("Cache de build do Docker"),
                      "detail": tr("recuperável: {free}", free=df["Build Cache"][1]), "mb": None, "suggest": False, "why": [], "block": ""})
    return {"t": time.time(), "items": sorted(items, key=lambda i: -(i["mb"] or 0)), "vhdx": vhdx(), "disks": disks(),
            "distro": os.environ.get("WSL_DISTRO_NAME", "")}


def cleanup(body):
    """GET (sem corpo): a análise guardada. POST {"refresh": true}: analisa de novo em segundo plano.
    POST com kind/id: limpa um item da análise atual, com as travas."""
    if body is None or body.get("refresh") is not None:
        if (body or {}).get("refresh"):  # só quando você pede: medir 14 GB de cache e os worktrees pesa no disco
            if not _clean["busy"]:
                def run():
                    _clean["busy"] = True
                    try:
                        _clean["data"], _clean["ts"] = cleanup_scan(), time.time()
                    finally:
                        _clean["busy"] = False
                threading.Thread(target=run, daemon=True).start()
        return {**(_clean["data"] or {}), "busy": _clean["busy"]}
    kind, iid = body.get("kind"), body.get("id")
    item = next((i for i in (_clean["data"] or {}).get("items", []) if i["kind"] == kind and i["id"] == iid), None)
    if not item:
        raise ValueError(tr("Item fora da análise atual: analise de novo"))
    before = disks()[0]["used"]
    if kind == "worktree":
        if git("-C", item["id"], "status", "--porcelain", timeout=120):
            raise ValueError(tr("O worktree tem alteração não salva: não removo"))
        if any(c == item["id"] or c.startswith(item["id"] + "/") for c in hooks.get("live_cwds", lambda: [])()):
            raise ValueError(tr("Há uma sessão aberta nesse worktree"))
        r = subprocess.run(["git", "-C", item["repo"], "worktree", "remove", item["id"]], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise ValueError(r.stderr.strip()[-200:] or tr("git worktree remove falhou"))
    elif kind == "cache":
        shutil.rmtree(Path(dict((k, p) for k, _, p in CACHES)[iid]).expanduser(), ignore_errors=True)
    elif kind == "scratch":
        for d in scratch_dirs():  # recalcula: só o que continua de sessão encerrada
            shutil.rmtree(d, ignore_errors=True)
    elif kind in ("docker-images", "docker-build"):
        cmd = ["image", "prune", "-a", "-f"] if kind == "docker-images" else ["builder", "prune", "-f"]
        r = subprocess.run([hooks.get("docker", "docker"), *cmd], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise ValueError((r.stderr or r.stdout).strip()[-200:])
    else:
        raise ValueError(tr("Tipo de limpeza desconhecido"))
    _clean["data"]["items"] = [i for i in _clean["data"]["items"] if i is not item]
    return {"ok": True, "freed_mb": max(0, before - disks()[0]["used"])}
