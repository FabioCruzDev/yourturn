#!/usr/bin/env python3
"""Quadro Kanban local: issues do Linear x sessões do Claude Code rodando em tmux.

Uso: python3 server.py  ->  http://localhost:8765
"""
import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import signal
import struct
import termios
import threading
import shlex
import subprocess
import time
import unicodedata
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("KANBAN_PORT", "8765"))
TMUX = os.environ.get("KANBAN_TMUX", "kanban")
REPOS_ROOT = Path(os.environ.get("KANBAN_REPOS_ROOT", "~/Projects")).expanduser()
TMUX_SIZE = os.environ.get("KANBAN_TMUX_SIZE", "120x45")
# Hosts extras aceitos (ex.: o nome .ts.net do `tailscale serve`); o servidor continua ouvindo só em 127.0.0.1
EXTRA_HOSTS = {h.strip() for h in os.environ.get("KANBAN_HOSTS", "").split(",") if h.strip()}
REMOTE_CONTROL = os.environ.get("KANBAN_REMOTE_CONTROL") == "1"
PROJECTS = Path("~/.claude/projects").expanduser()  # base; cada organização tem o seu (ver orgs())
ORGS_FILE = Path("~/.config/claude-orgs/orgs.tsv").expanduser()  # pasta -> organização (tools/claude_orgs.py)
DEFAULT_ORG = os.environ.get("KANBAN_ORG", "")  # organização ativa até alguém escolher outra (vazio = a 1ª do orgs.tsv)
VOICE = os.environ.get("KANBAN_VOICE") == "1"  # Jarvis (conversa por voz): experimental, desligado por padrão
LANGS = ("pt-BR", "en-US")
# Variáveis que identificam conta/credencial: nunca passam de quem subiu o quadro para o processo de uma organização
ORG_VARS = ("CLAUDE_CONFIG_DIR", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GH_TOKEN", "GITHUB_TOKEN", "LINEAR_API_KEY")
ORG_COLORS = ["#4f8cff", "#2ec27e", "#f5a524", "#e5484d", "#a855f7", "#14b8a6"]
KEY_FILE = Path("~/.config/kanban-claude/linear_key").expanduser()
# Voz do Jarvis: servidor local tts/tts_server.py (XTTS-v2 na GPU); fora do ar, o quadro usa a voz do navegador
TTS_URL = f"http://127.0.0.1:{os.environ.get('KANBAN_TTS_PORT', '8766')}"
LAUNCHED = HERE / "launched.json"  # session_id -> {issue, window, cwd}
TASKS = HERE / "tasks.json"  # tarefas livres, fora do Linear
BOARD = HERE / "board.json"  # por card: raia escolhida à mão e lembrete
SETTINGS = HERE / "settings.json"  # configurações do quadro (valem em todos os aparelhos)
DEFAULT_SETTINGS = {"remind_minutes": 5, "hibernate_hours": 2}
HOOK_DIR = Path("~/.cache/kanban-claude/state").expanduser()  # gravado pelo hook.py (hooks do Claude Code)
UPLOADS = Path("~/.cache/kanban-claude/uploads").expanduser()
PUSH_SUBS = HERE / "push_subs.json"  # aparelhos com notificação ativa (inscrições Web Push)
# App instalável (PWA): manifesto, service worker e ícones, servidos da raiz para o escopo ser o quadro inteiro
PWA = {"/manifest.webmanifest": ("pwa/manifest.webmanifest", "application/manifest+json"),
       "/sw.js": ("pwa/sw.js", "text/javascript"),
       "/icon-192.png": ("pwa/icon-192.png", "image/png"), "/icon-512.png": ("pwa/icon-512.png", "image/png")}
MAX_UPLOAD = 50 * 1024 * 1024
RECENT_DAYS = 7
# xterm.js 5.3.0 + addon-fit 0.8.0 copiados para vendor/ (sem depender de CDN)
VENDOR = {"/vendor/xterm.js": "text/javascript", "/vendor/xterm-addon-fit.js": "text/javascript",
          "/vendor/xterm-addon-web-links.js": "text/javascript", "/vendor/xterm.css": "text/css"}
ALLOWED_KEYS = {"Enter", "Escape", "Up", "Down", "Tab", "1", "2", "3"}

LINEAR_QUERY = """query {
  issues(filter: {assignee: {isMe: {eq: true}}}, first: 100, orderBy: updatedAt) {
    nodes { identifier title url branchName priority priorityLabel createdAt updatedAt
            state { name type } team { key name } parent { identifier } }
  }
}"""
_linear_cache = {}  # organização -> (ts, issues, erro)


def linear_key_file(org=None):
    return ORGS_FILE.parent / org / "linear_key" if org else KEY_FILE


def linear_key(org=None):
    if not org and os.environ.get("LINEAR_API_KEY"):  # só antes das organizações
        return os.environ["LINEAR_API_KEY"].strip()
    try:
        return linear_key_file(org).read_text().strip() or None
    except OSError:
        return None


def tracker_issues(max_age=60):
    """(issues de todas as organizações, do Linear e/ou do Jira, cada uma com "org"; erro). Cache de 60 s por fonte para o
    polling do navegador não estourar o rate limit; o Jarvis aceita até 10 min (o polling já renova) para não esperar a
    rede no meio da conversa. Organização sem Linear nem Jira simplesmente não traz issues."""
    srcs = []
    for org, name in [(o["id"], o["name"]) for o in org_list()] or [(None, "")]:
        if linear_key(org):
            srcs.append((org, name, "Linear", lambda k=linear_key(org): linear_fetch(k)))
        if org and jira_conf(org):
            srcs.append((org, name, "Jira", lambda c=jira_conf(org): jira_fetch(c)))
    if not srcs and orgs():
        return [], None  # organizações só com tarefas do quadro: escolha válida, não é erro
    if not srcs:
        where = ORGS_FILE.parent / (active_org() if orgs() else "")
        return None, tr("Nenhuma organização com Linear ou Jira. Linear: crie a chave em Linear → Settings → Security & access → "
                        "Personal API keys e salve em {key}; Jira: {jira} (ou pelo formulário de organização)",
                        key=linear_key_file(active_org() if orgs() else None), jira=where / "jira.json")
    issues, errs = [], []
    for org, name, kind, fetch in srcs:
        ts, got, err = _linear_cache.get((org, kind), (0.0, None, None))
        if time.time() - ts >= max_age or (got is None and not err):
            got, err = fetch()
            _linear_cache[(org, kind)] = (time.time(), got, err)
        if err:
            errs.append(f"{name} ({kind}): {err}" if name else err)
        issues += [{**i, "org": org, "source": kind.lower()} for i in got or []]
    return (issues if issues or not errs else None), "; ".join(errs) or None


# ---------- Jira (Cloud: e-mail + API token; Data Center: token pessoal) ----------

JIRA_JQL = "assignee = currentUser() ORDER BY updated DESC"  # como no Linear: as suas, as mais recentes primeiro
JIRA_PRIORITY = {"highest": 1, "blocker": 1, "critical": 1, "high": 2, "major": 2, "medium": 3, "low": 4, "minor": 4,
                 "lowest": 4, "trivial": 4}
JIRA_STATE = {"new": "unstarted", "indeterminate": "started", "done": "completed"}  # statusCategory.key


def jira_conf(org):
    """~/.config/claude-orgs/<org>/jira.json: {"url", "email" (só no Cloud), "token", "jql" (opcional)}."""
    try:
        c = json.loads((ORGS_FILE.parent / org / "jira.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    return c if c.get("url") and c.get("token") else None


def jira_time(t):
    """2026-10-30T12:34:56.789-0300 -> com dois-pontos no fuso (o Date.parse do navegador exige)."""
    return re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", t or "")


def jira_issue(base, x):
    """Issue do Jira no mesmo formato das do Linear, para o quadro tratar as duas iguais."""
    f = x.get("fields") or {}
    prio = JIRA_PRIORITY.get(((f.get("priority") or {}).get("name") or "").lower(), 0)
    status = f.get("status") or {}
    return {"identifier": x["key"], "title": f.get("summary") or "", "url": f"{base}/browse/{x['key']}",
            "branchName": f"{x['key'].lower()}-{slug(f.get('summary') or '')}", "priority": prio,
            "priorityLabel": PRIORITY_LABELS[prio], "createdAt": jira_time(f.get("created")), "updatedAt": jira_time(f.get("updated")),
            "state": {"name": status.get("name", ""), "type": JIRA_STATE.get((status.get("statusCategory") or {}).get("key"), "unstarted")},
            "team": {"name": (f.get("project") or {}).get("name", "")}}


def jira_fetch(c):
    base = c["url"].rstrip("/")
    body = {"jql": c.get("jql") or JIRA_JQL, "maxResults": 100,
            "fields": ["summary", "status", "priority", "created", "updated", "project", "issuetype"]}
    if c.get("email"):  # Cloud
        auth, path = "Basic " + base64.b64encode(f"{c['email']}:{c['token']}".encode()).decode(), "/rest/api/3/search/jql"
    else:  # Data Center / Server
        auth, path = f"Bearer {c['token']}", "/rest/api/2/search"
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}" + (tr(" (confira e-mail e token)") if e.code in (401, 403) else "")
    except Exception as e:  # rede, timeout: mostra na tela e mantém o resto do quadro
        return None, str(e)
    # Subtarefas ficam fora do quadro, como as sub-issues do Linear
    return [jira_issue(base, x) for x in data.get("issues") or [] if not ((x.get("fields") or {}).get("issuetype") or {}).get("subtask")], None


def linear_fetch(key):
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": LINEAR_QUERY}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.load(r)
        if body.get("errors"):
            issues, err = None, body["errors"][0].get("message", tr("erro do Linear"))
        else:
            # Sub-issues ficam fora do quadro: o card da issue mãe já representa o trabalho
            issues, err = [i for i in body["data"]["issues"]["nodes"] if not i.get("parent")], None
    except Exception as e:  # rede, 401, timeout: mostra na tela e mantém o resto do quadro
        issues, err = None, f"Linear: {e}"
    return issues, err


# ---------- sessões locais (~/.claude/projects/*/*.jsonl) ----------

def tail_records(path, size=65536):
    """Lê só o fim do jsonl: alguns passam de 100 MB e o quadro consulta a cada poucos segundos."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        n = f.tell()
        f.seek(max(0, n - size))
        lines = f.read().split(b"\n")
    if n > size:
        lines = lines[1:]  # primeira linha veio cortada
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def text_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def last_prompt(recs):
    """Último prompt que você digitou (pula resultado de ferramenta, mensagens internas e comandos)."""
    for r in reversed(recs):
        if r.get("type") == "user" and not r.get("isMeta") and not r.get("isSidechain"):
            text = text_of(r.get("message")).strip()
            if text and not text.startswith("<"):
                return text[:500]
    return ""


_prompt_cache = {}  # path -> (mtime, prompt)


def prompt_of(path, mtime, recs):
    """Procura no fim já lido; se o último prompt ficou mais para trás (saídas longas de ferramenta),
    lê até 2 MB do fim, uma vez por mudança do arquivo."""
    hit = _prompt_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    prompt = last_prompt(recs) or last_prompt(tail_records(path, 2_000_000))
    _prompt_cache[path] = (mtime, prompt)
    return prompt


def summarize(recs, now, mtime):
    """Estado da sessão a partir do fim do transcript.

    ponytail: heurística pelo jsonl; não enxerga o prompt de permissão em si
    (usa "tool_use parado há 30 s"). Para precisão, ler a tela via tmux capture-pane.
    """
    convo = [r for r in recs if r.get("sessionId") and not r.get("isSidechain")]
    if not convo:
        return None
    # O Claude Code grava no fim registros de controle sem pasta (custo, último prompt, link de PR): a pasta e a branch
    # vêm do último registro que as tem
    where = next((r for r in reversed(convo) if r.get("cwd")), convo[-1])
    s = {"id": convo[-1]["sessionId"], "cwd": where.get("cwd", ""), "branch": where.get("gitBranch", ""),
         "title": "", "last": "", "updated": mtime}
    for r in recs:
        if r.get("type") == "agent-name":
            s["title"] = r.get("agentName") or s["title"]
        elif r.get("type") == "ai-title" and not s["title"]:
            s["title"] = r.get("aiTitle", "")
    turn = [r for r in convo if r.get("type") in ("user", "assistant")
            or (r.get("type") == "system" and r.get("subtype") == "turn_duration")]
    for r in reversed(turn):
        if r.get("type") == "assistant" and text_of(r.get("message")).strip():
            s["last"] = text_of(r["message"]).strip()[-400:]
            break
    age = now - mtime
    last = turn[-1] if turn else {}
    if last.get("type") == "system":
        s["status"] = "waiting"  # turno terminou: a vez é sua
    elif last.get("type") == "assistant" and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in last.get("message", {}).get("content") or []) and age > 30:
        s["status"] = "waiting"  # ferramenta pedida e nada andou: provavelmente pedindo permissão
    else:
        s["status"] = "working"
    if age > 1800:
        s["status"] = "idle"
    return s


def load_launched():
    try:
        return json.loads(LAUNCHED.read_text())
    except (OSError, ValueError):
        return {}


def tmux(*args, check=True):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def tmux_windows():
    r = tmux("list-windows", "-t", TMUX, "-F", "#{window_name}", check=False)
    return set(r.stdout.split()) if r.returncode == 0 else set()


MENU = re.compile(r"Esc to cancel|Do you want to")
SPINNER = re.compile(r"…\s*\(\d")  # "· Reticulating… (18m 32s · ↓ 25.0k tokens)"


def screen_status(text):
    """Estado pela tela do Claude: o spinner logo acima da caixa de input só existe com o turno rodando."""
    if MENU.search(text):
        return "waiting"  # permissão ou pergunta com opções
    lines = text.splitlines()
    borders = [i for i, line in enumerate(lines) if line.strip().startswith("──")]
    if len(borders) < 2:
        return None  # tela fora do padrão (ainda iniciando, scroll...): fica com a heurística do jsonl
    above = [line for line in lines[:borders[-2]] if line.strip()]
    return "working" if above and SPINNER.search(above[-1]) else "waiting"


def spinner_visible(text):
    """Spinner do turno em qualquer das últimas linhas acima da caixa de input (pode haver dica/todo no meio)."""
    lines = text.splitlines()
    borders = [i for i, line in enumerate(lines) if line.strip().startswith("──")]
    above = [line for line in lines[:borders[-2] if len(borders) >= 2 else len(lines)] if line.strip()]
    return any(SPINNER.search(line) or "esc to interrupt" in line for line in above[-8:])


def hook_state(sid):
    try:
        return json.loads((HOOK_DIR / f"{sid}.json").read_text())
    except (OSError, ValueError):
        return None


def live_status(sid, window, now):
    """Trabalhando ou esperando você. Os hooks dizem quando o turno acaba (Stop) e quando ele
    pede algo (permissão, pergunta); a tela cobre o resto (Esc interrompe sem hook)."""
    r = tmux("capture-pane", "-p", "-t", f"{TMUX}:{window}", check=False)
    return decide_status(r.stdout if r.returncode == 0 else "", hook_state(sid), now)


def decide_status(text, h, now):
    if MENU.search(text):
        return "waiting"  # permissão ou pergunta com opções na tela
    spinning = spinner_visible(text)
    if h and h.get("event") in ("Stop", "StopFailure") and not (spinning and now - h["ts"] > 3):
        return "waiting"  # turno terminou
    if spinning:
        return "working"
    if h:
        return "working" if h.get("state") == "working" and now - h["ts"] < 20 else "waiting"
    return screen_status(text) if text else None


def window_status(window):
    r = tmux("capture-pane", "-p", "-t", f"{TMUX}:{window}", check=False)
    return screen_status(r.stdout) if r.returncode == 0 else None


# ---------- organizações: cada uma com o próprio CLAUDE_CONFIG_DIR (conta, skills, histórico) ----------
def orgs():
    """[(organização, pasta de config, pasta de projetos)] do orgs.tsv; vazio antes do corte (tudo no ~/.claude)."""
    try:
        lines = ORGS_FILE.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        f = line.split("\t")
        if len(f) == 3 and f[0] and not f[0].startswith("#"):
            out.append((f[0], Path(f[1]).expanduser(), str(Path(f[2]).expanduser())))
    return out


def org_config(cwd=None):
    """Pasta de config da organização dona de `cwd` (a pasta mais específica vence); fora delas, o ~/.claude se ele
    tiver login (None), senão a da organização ativa. None sem organizações."""
    best, blen = None, 0
    for _, d, p in orgs():
        if cwd and (cwd == p or cwd.startswith(p + "/")) and len(p) > blen:
            best, blen = d, len(p)
    if best or (cwd and base_logged()):
        return best  # pasta fora das organizações com o ~/.claude logado: fica no Claude padrão
    return next((d for o, d, _ in orgs() if o == active_org()), None)


def base_logged():
    """O ~/.claude (base) tem login? Depois do corte do claude_orgs.py ele fica sem login, e aí uma pasta fora das
    organizações usa a organização ativa; numa instalação comum ele é a conta do usuário e vale para o resto."""
    return (PROJECTS.parent / ".credentials.json").exists()


def active_org():
    """Organização cuja conta o Jarvis e os resumos usam (escolhida no seletor do quadro)."""
    return load_settings().get("active_org") or DEFAULT_ORG or next((o for o, _, _ in orgs()), "")


def org_meta(org):
    """Nome, cor e org do GitHub da organização (~/.config/claude-orgs/<org>/org.json, opcional)."""
    try:
        return json.loads((ORGS_FILE.parent / org / "org.json").read_text())
    except (OSError, ValueError, TypeError):
        return {}


def org_of(path):
    """Organização dona de uma pasta (a mais específica vence); None fora delas."""
    best, blen = None, 0
    for o, _, p in orgs():
        if path and (path == p or path.startswith(p + "/")) and len(p) > blen:
            best, blen = o, len(p)
    return best


def org_list():
    ids = dict.fromkeys((o, d) for o, d, _ in orgs())
    out = []
    for k, (o, d) in enumerate(ids):
        m = org_meta(o)
        out.append({"id": o, "name": m.get("name") or o.capitalize(), "color": m.get("color") or ORG_COLORS[k % len(ORG_COLORS)],
                    "github_org": m.get("github_org", ""),
                    "login": (d / ".credentials.json").exists(),  # ponytail: arquivo de login do Claude no Linux; token vencido não aparece aqui
                    "git_host": m.get("git_host", "github"),
                    # GitHub só importa para quem guarda o código lá; no Bitbucket o acesso é pela chave SSH da organização
                    "gh_login": gh_logged(d) if m.get("git_host", "github") == "github" else None,
                    "ssh_key": (Path.home() / ".ssh" / f"id_ed25519_{o}").exists() if m.get("git_host") == "bitbucket" else None,
                    "paths": [p for o2, _, p in orgs() if o2 == o]})
    return out


def org_name(org):
    """Nome da organização para avisos; vazio com uma só (não precisa dizer de onde veio)."""
    ol = org_list()
    return next((o["name"] for o in ol if o["id"] == org), "") if len(ol) > 1 else ""


def org_of_config(config):
    return next((o for o, d, _ in orgs() if str(d) == str(config)), None)


def gh_logged(config):
    """Login do GitHub da organização: None se ela não tem gh próprio (GH_CONFIG_DIR), senão se há conta no hosts.yml."""
    gh = org_vars(config).get("GH_CONFIG_DIR")
    if not gh:
        return None
    try:
        return "github.com" in (Path(gh) / "hosts.yml").read_text()
    except OSError:
        return False


def org_vars(config):
    """CLAUDE_CONFIG_DIR + as variáveis da organização (GH_CONFIG_DIR, GIT_CONFIG_GLOBAL…, do "env" do settings dela)."""
    if not config:
        return {}
    try:
        env = json.loads((Path(config) / "settings.json").read_text()).get("env") or {}
    except (OSError, ValueError):
        env = {}
    if Path(config) == PROJECTS.parent:  # organização no ~/.claude de sempre: CLAUDE_CONFIG_DIR explícito faria o
        return {k: str(v) for k, v in env.items()}  # Claude ler ~/.claude/.claude.json (perde confiança, MCP, onboarding)
    return {**{k: str(v) for k, v in env.items()}, "CLAUDE_CONFIG_DIR": str(config)}


def claude_env(config=None):
    """Ambiente de um processo da organização (claude, gh, git) montado do zero: tira as credenciais herdadas de quem
    subiu o quadro e põe só as da organização."""
    env = {k: v for k, v in os.environ.items() if k not in ORG_VARS}
    return {**env, **org_vars(config)}


def claude_cmd(args, config):
    """Linha de comando para o tmux: o servidor do tmux não passa pelo .bashrc, então a organização vai explícita."""
    unset = [x for v in ORG_VARS for x in ("-u", v)]
    return shlex.join(["env", *unset, *(f"{k}={v}" for k, v in org_vars(config).items())] + args)


def session_files(pattern="*/*.jsonl"):
    dirs = dict.fromkeys([PROJECTS] + [d / "projects" for _, d, _ in orgs()])
    return [p for d in dirs if d.is_dir() for p in d.glob(pattern)]


def sessions():
    now, cutoff = time.time(), time.time() - RECENT_DAYS * 86400
    launched, windows = load_launched(), tmux_windows()
    out = {}
    for p in session_files():
        mtime = p.stat().st_mtime
        if mtime < cutoff:
            continue
        recs = tail_records(p)
        s = summarize(recs, now, mtime)
        if not s:
            continue
        s["prompt"] = prompt_of(p, mtime, recs)
        s["config"] = str(p.parents[2]) if p.parents[1] != PROJECTS else None  # o --resume precisa da mesma conta
        info = launched.get(s["id"], {})
        s["issue"] = info.get("issue", "")
        s["window"] = info.get("window") if info.get("window") in windows else None
        if info.get("closed") and not s["window"]:
            continue  # você encerrou pelo quadro: some do card
        if s["window"]:
            s["status"] = live_status(s["id"], s["window"], now) or ("waiting" if s["status"] == "idle" else s["status"])
        out[s["id"]] = s
    # Lançada agora há pouco e o jsonl ainda não existe
    for sid, info in launched.items():
        if sid not in out and info.get("window") in windows:
            out[sid] = {"id": sid, "cwd": info["cwd"], "branch": "", "title": info.get("title", ""), "last": "", "prompt": "",
                        "updated": info.get("ts", now), "status": live_status(sid, info["window"], now) or "working",
                        "issue": info["issue"], "window": info["window"]}
    for s in out.values():
        s["org"] = org_of_config(s.get("config") or org_config(s["cwd"]))
    return sorted(out.values(), key=lambda s: -s["updated"])


def repos():
    roots = dict.fromkeys([REPOS_ROOT] + [Path(p) for _, _, p in orgs()])  # repositórios de todas as organizações
    return sorted(str(p) for r in roots if r.is_dir() for p in r.iterdir() if (p / ".git").exists())


def launch_dirs():
    """Onde dá para iniciar sessão: os repositórios + a pasta de cada organização (para começar antes de clonar)."""
    return repos() + sorted({p for _, _, p in orgs() if Path(p).is_dir()} - set(repos()))


# ---------- tarefas livres ----------

PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
TASK_STATES = {"unstarted": "A fazer", "started": "Em andamento", "completed": "Concluída"}


def load_tasks():
    try:
        return json.loads(TASKS.read_text())
    except (OSError, ValueError):
        return []


def save_tasks(tasks):
    TASKS.write_text(json.dumps(tasks, indent=1, ensure_ascii=False))


def slug(text):
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")[:40]


def task_as_issue(t):
    """Mesmo formato das issues do Linear, para o quadro tratar as duas iguais."""
    return {"identifier": t["id"], "title": t["title"], "description": t.get("description", ""), "url": "",
            "branchName": f"task/{t['id'].lower()}-{slug(t['title'])}", "priority": t["priority"],
            "priorityLabel": PRIORITY_LABELS[t["priority"]], "createdAt": t["createdAt"], "updatedAt": t["updatedAt"],
            "state": {"name": tr(TASK_STATES[t["state"]]), "type": t["state"]}, "team": {"name": tr("Tarefa")}, "local": True,
            "org": t.get("org", DEFAULT_ORG)}  # tarefas sem organização (de antes das organizações) = KANBAN_ORG


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def task_create(body):
    title = str(body.get("title", "")).strip()
    if not title:
        raise ValueError(tr("Dê um título para a tarefa"))
    tasks = load_tasks()
    n = max((int(t["id"].split("-")[1]) for t in tasks), default=0) + 1
    t = {"id": f"T-{n}", "title": title[:200], "description": str(body.get("description", ""))[:4000],
         "priority": int(body.get("priority", 0)) if int(body.get("priority", 0)) in PRIORITY_LABELS else 0,
         "state": "unstarted", "createdAt": now_iso(), "updatedAt": now_iso(), "org": active_org()}
    save_tasks(tasks + [t])
    return task_as_issue(t)


def task_update(body):
    tasks = load_tasks()
    t = next((t for t in tasks if t["id"] == body.get("id")), None)
    if not t:
        raise ValueError(tr("Tarefa não encontrada"))
    if body.get("delete"):
        tasks.remove(t)
    elif body.get("state") in TASK_STATES:
        t["state"], t["updatedAt"] = body["state"], now_iso()
    elif "title" in body:  # edição do conteúdo
        title = str(body["title"]).strip()
        if not title:
            raise ValueError(tr("Dê um título para a tarefa"))
        prio = int(body.get("priority", t["priority"]))
        t.update(title=title[:200], description=str(body.get("description", ""))[:4000],
                 priority=prio if prio in PRIORITY_LABELS else t["priority"], updatedAt=now_iso())
    save_tasks(tasks)
    return {"ok": True}


# ---------- ações ----------

def ensure_tmux():
    if tmux("has-session", "-t", TMUX, check=False).returncode != 0:
        tmux("new-session", "-d", "-s", TMUX, "-n", "home")
    # Sem cliente anexado o tmux usa 80x24, apertado para o Claude e para a tela nas janelas do quadro
    tmux("set-option", "-t", TMUX, "default-size", TMUX_SIZE, check=False)
    # Repassa hyperlinks (OSC 8) do Claude até o terminal do navegador, para links longos continuarem clicáveis
    if "hyperlinks" not in tmux("show-options", "-sv", "terminal-features", check=False).stdout:
        tmux("set-option", "-sa", "terminal-features", "xterm-256color:hyperlinks", check=False)


def save_launch(sid, issue, window, cwd, title):
    data = load_launched()
    data[sid] = {"issue": issue, "window": window, "cwd": cwd, "title": title, "ts": time.time()}
    LAUNCHED.write_text(json.dumps(data, indent=1))


def make_worktree(repo, branch, issue):
    """Worktree irmão do repo (padrão <repo>-<issue>), para sessões paralelas não pisarem uma na outra."""
    path = Path(repo).parent / f"{Path(repo).name}-{issue.lower()}"
    if path.exists():
        return str(path)
    # A branch já aberta em outra worktree do repo (criada a partir de outra pasta): o git recusa abrir de novo, então
    # a sessão vai para lá
    wl = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    for block in wl.split("\n\n"):
        lines = dict(l.split(" ", 1) for l in block.splitlines() if " " in l)
        if lines.get("branch") == f"refs/heads/{branch}" and Path(lines.get("worktree", "")).is_dir():
            return lines["worktree"]
    has_branch = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                                capture_output=True).returncode == 0
    env = claude_env(org_config(repo))  # credenciais e identidade de git da organização do repositório
    subprocess.run(["git", "-C", repo, "fetch", "--quiet", "origin"], capture_output=True, env=env)
    # branch nova sai da principal do origin; repositório sem origin (só local) sai do HEAD
    base = "origin/HEAD" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
                                           capture_output=True).returncode == 0 else "HEAD"
    cmd = ["git", "-C", repo, "worktree", "add", str(path)] + ([branch] if has_branch else ["-b", branch, base])
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip() or tr("git worktree add falhou"))
    return str(path)


def launch(body):
    issue, title, repo, prompt = body["issue"], body.get("title", ""), body["repo"], body["prompt"]
    if repo not in launch_dirs():
        raise ValueError(tr("Repositório fora da lista permitida"))
    # A organização da sessão é a da pasta: repositório de outra organização abriria o Claude (conta, skills, memórias)
    # errado para o card, então nem a tela nem o Jarvis conseguem
    if orgs() and body.get("org") and org_of(repo) != body["org"]:
        names = {o["id"]: o["name"] for o in org_list()}
        raise ValueError(tr("Esse repositório é da {repo_org}; o card é da {card_org}. Escolha um repositório dela.",
                            repo_org=names.get(org_of(repo), tr("nenhuma organização")), card_org=names.get(body["org"], body["org"])))
    git = (Path(repo) / ".git").exists()
    cwd = make_worktree(repo, body["branch"], issue) if git and body.get("worktree") and body.get("branch") else repo
    sid = str(uuid.uuid4())
    window = f"{issue}-{sid[:4]}"
    cmd = ["claude", "--session-id", sid, "-n", f"{issue} {title}"[:80]]
    if REMOTE_CONTROL:
        cmd.append(f"--remote-control={issue}")
    cmd.append(prompt)
    ensure_tmux()
    tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", window, "-c", cwd, claude_cmd(cmd, org_config(cwd)))
    save_launch(sid, issue, window, cwd, f"{issue} {title}")
    return {"id": sid, "window": window, "cwd": cwd}


def start_cwd(sid):
    """Pasta onde a sessão nasceu (1º registro com pasta). O `--resume` tem de voltar para ela: se a sessão já foi
    retomada em outro lugar, o fim do histórico aponta para a pasta errada (e o Claude pede confiança de novo)."""
    path = next(iter(session_files(f"*/{sid}.jsonl")), None)
    try:
        with open(path or "", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                cwd = json.loads(line).get("cwd") if '"cwd"' in line else None
                if cwd and Path(cwd).is_dir():
                    return cwd
    except (OSError, ValueError):
        pass
    return ""


def open_terminal(body):
    """Terminal avulso numa pasta das organizações: shell (bash) ou o Claude sem card, com a conta e as ferramentas da
    organização da pasta (CLAUDE_CONFIG_DIR, GH_CONFIG_DIR… explícitos, como nas sessões)."""
    path, kind = body.get("path"), body.get("kind") or "shell"
    if path not in launch_dirs():
        raise ValueError(tr("Pasta fora da lista"))
    if kind not in ("shell", "claude"):
        raise ValueError(tr("Escolha shell ou Claude"))
    sid, name = str(uuid.uuid4()), Path(path).name
    window = f"{'term' if kind == 'shell' else 'claude'}-{re.sub(r'[^A-Za-z0-9_-]', '', name)[:20]}-{sid[:4]}"
    cmd = ["bash", "-l"] if kind == "shell" else ["claude", "--session-id", sid, "-n", name]
    ensure_tmux()
    tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", window, "-c", path, claude_cmd(cmd, org_config(path)))
    save_launch(sid, "", window, path, f"{'Terminal' if kind == 'shell' else 'Claude'} · {name}")
    return {"id": sid, "window": window}


# ---------- hibernação: sessão parada há horas fecha sozinha (a conversa fica salva; volta pelo card) ----------

HIBERNATED = HERE / "hibernated.json"  # {sid: quando hibernou}


def hibernated():
    try:
        return json.loads(HIBERNATED.read_text())
    except (OSError, ValueError):
        return {}


def hibernate_idle(now=None):
    """Fecha as sessões do Claude abertas no quadro que não trabalham há mais de `hibernate_hours` (configuração; 0
    desliga). Só sessão com conversa gravada (terminal avulso e login não) e nunca uma trabalhando. Devolve as fechadas."""
    hours, now = float(load_settings().get("hibernate_hours", 2) or 0), now or time.time()
    if hours <= 0 or load_settings().get("eco"):
        return []
    log, done = hibernated(), []
    for x in sessions():
        if x["window"] and "config" in x and x["status"] != "working" and now - x["updated"] > hours * 3600:
            tmux("kill-window", "-t", f"{TMUX}:{x['window']}", check=False)
            log[x["id"]] = now
            done.append(x["id"])
    if done:
        HIBERNATED.write_text(json.dumps(log))
        print(f"hibernadas (paradas há mais de {hours:g} h): {', '.join(d[:8] for d in done)}", flush=True)
    return done


def watch_hibernate(every=300):
    while True:
        try:
            hibernate_idle()
        except Exception as e:  # noqa: BLE001  (a vigia não pode morrer)
            print(f"hibernação: {e}", flush=True)
        time.sleep(every)


def resume(body):
    s = next((s for s in sessions() if s["id"] == body["id"]), None)
    if not s:
        raise ValueError(tr("Sessão não encontrada"))
    if s["window"]:
        return {"window": s["window"]}  # já está aberta no quadro: dois `claude --resume` da mesma sessão se atropelam
    window = f"{body.get('issue') or 'sess'}-{s['id'][:4]}"
    ensure_tmux()
    cwd = start_cwd(s["id"]) or s["cwd"] or str(Path.home())
    tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", window, "-c", cwd,
         claude_cmd(["claude", "--resume", s["id"]], s.get("config") or org_config(cwd)))
    save_launch(s["id"], body.get("issue", ""), window, cwd, s["title"])
    log = hibernated()
    if log.pop(s["id"], None):  # voltou da hibernação
        HIBERNATED.write_text(json.dumps(log))
    return {"window": window}


def org_create(body):
    """Organização nova pelo formulário do quadro: o mesmo `add` do tools/claude_orgs.py (pasta de config do Claude sem
    login, GitHub e git próprios, Linear e/ou Jira opcionais). Identificador que já existe: só atualiza Linear/Jira."""
    import importlib
    import sys
    sys.path.insert(0, str(HERE / "tools"))
    co = importlib.import_module("claude_orgs")
    co.tr = tr  # mensagens de erro do formulário no idioma da tela
    co.ORGS = co.load_orgs()  # alguém pode ter mexido no orgs.tsv desde o último import
    paths = body.get("paths") or []
    paths = paths.splitlines() if isinstance(paths, str) else [str(p) for p in paths]
    org, tools = str(body.get("id", "")).strip().lower(), ("linear_key", "jira_url", "jira_email", "jira_token", "jira_jql", "git_host", "bitbucket_email",
                                                         "bitbucket_token", "slack_token")
    if org in co.ORGS:  # já existe: o formulário só atualiza o Linear/Jira dela
        changed = co.set_tools(org, **{k: str(body.get(k) or "") for k in tools})
        if not changed:
            raise ValueError(tr("A organização {org} já existe e nada mudou (Linear, Jira e onde fica o código são o que dá para atualizar)", org=org))
        _linear_cache.clear()
        return {"orgs": org_list(), "updated": changed}
    co.add(org, [p for p in paths if p.strip()], use_default_claude=bool(body.get("use_default_claude")),
           **{k: str(body.get(k) or "") for k in ("name", "color", "github_org", "email") + tools})
    return {"orgs": org_list()}


# ---------- assistente de configuração (1ª execução, sem nenhuma organização) ----------

def gh_user(env=None):
    """Conta do GitHub logada no `gh` (vazio se não houver gh ou login)."""
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=15, env=env)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def setup_info(body):
    """O que o assistente precisa saber da máquina: se o Claude de sempre tem login, quem está no gh, a pasta padrão."""
    return {"claude_login": base_logged(), "gh_user": gh_user(claude_env()), "repos_root": str(REPOS_ROOT).replace(str(Path.home()), "~", 1),
            "home": str(Path.home())}


def setup_scan(body):
    """Quantos repositórios git há logo abaixo da pasta (o que o quadro vai oferecer para iniciar sessões)."""
    raw = str(body.get("path", "")).strip()
    p = Path(raw).expanduser()
    if not raw or not p.is_dir():
        return {"exists": False, "repos": 0}
    try:
        n = sum(1 for x in p.iterdir() if (x / ".git").exists())
    except OSError:
        n = 0
    return {"exists": True, "repos": n, "is_repo": (p / ".git").exists()}


def setup_test(body):
    """Testa uma credencial antes de salvar: {"kind": "linear"|"jira"|"slack"|"github", ...} → {"ok", "detail"}."""
    kind = body.get("kind")
    if kind == "linear":
        issues, err = linear_fetch(str(body.get("key", "")).strip())
        return {"ok": False, "detail": err} if err else {"ok": True, "detail": tr("{n} issues atribuídas a você", n=len(issues))}
    if kind == "jira":
        c = {k: str(body.get(k, "")).strip() for k in ("url", "email", "token", "jql")}
        if not c["url"] or not c["token"]:
            return {"ok": False, "detail": tr("Informe o endereço e o token do Jira")}
        issues, err = jira_fetch(c)
        return {"ok": False, "detail": err} if err else {"ok": True, "detail": tr("{n} issues atribuídas a você", n=len(issues))}
    if kind == "slack":
        tok = str(body.get("token", "")).strip()
        if not tok.startswith("xoxp-"):
            return {"ok": False, "detail": tr("Use o User OAuth Token do app do Slack (começa com xoxp-)")}
        try:
            d = slack_call("auth.test", tok)
        except Exception as e:  # noqa: BLE001  (token inválido, sem rede)
            return {"ok": False, "detail": str(e)}
        return {"ok": True, "detail": tr("conectado como {user} em {team}", user=d.get("user", ""), team=d.get("team", ""))}
    if kind == "github":
        u = gh_user(claude_env())
        return {"ok": bool(u), "detail": tr("logado como {user}", user=u) if u else tr("o gh não está logado")}
    raise ValueError(tr("Teste desconhecido"))


def org_gh_login(body):
    """Terminal com o `gh auth login` da organização (GH_CONFIG_DIR dela); fica aberto no fim para você ver o resultado."""
    if body.get("default"):  # assistente de configuração: o gh de sempre (~/.config/gh), antes de existir organização
        o, config = {"id": "default", "name": "GitHub", "paths": []}, None
    else:
        o = next((o for o in org_list() if o["id"] == body.get("org")), None)
        if not o:
            raise ValueError(tr("Organização desconhecida"))
        config = next(d for oid, d, _ in orgs() if oid == o["id"])
        if not org_vars(config).get("GH_CONFIG_DIR") and config != PROJECTS.parent:
            raise ValueError(tr("A organização {name} não tem GitHub próprio (GH_CONFIG_DIR)", name=o["name"]))
    sid = str(uuid.uuid4())
    window = f"github-{o['id']}-{sid[:4]}"
    script = f'gh auth login -h github.com -p https -w; echo; gh auth status; read -rp {shlex.quote(tr("Enter para fechar "))}'
    ensure_tmux()
    tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", window, "-c", next((p for p in o["paths"] if Path(p).is_dir()), str(Path.home())),
         claude_cmd(["bash", "-c", script], config))
    save_launch(sid, "", window, str(Path.home()), f"GitHub {o['name']}")
    return {"id": sid, "window": window}


def org_bitbucket(body):
    """Terminal que cria a chave SSH da organização (se faltar), mostra a parte pública para colar no Bitbucket e testa.
    Os repositórios da organização já usam essa chave (core.sshCommand no gitconfig dela, via includeIf)."""
    o = next((o for o in org_list() if o["id"] == body.get("org")), None)
    if not o or o["git_host"] != "bitbucket":
        raise ValueError(tr("Organização desconhecida ou que não usa Bitbucket"))
    key = Path.home() / ".ssh" / f"id_ed25519_{o['id']}"
    script = (f'k={shlex.quote(str(key))}; mkdir -p "$(dirname "$k")" && chmod 700 "$(dirname "$k")"; '
              f'[ -f "$k" ] || ssh-keygen -t ed25519 -N "" -q -C {shlex.quote("kanban-" + o["id"])} -f "$k" '
              f'|| {{ read -rp {shlex.quote(tr("Não consegui criar a chave. Enter para fechar "))}; exit 1; }}; '
              f'echo; echo {shlex.quote(tr("Cole esta chave em Bitbucket → Personal settings → SSH keys → Add key:"))}; echo; cat "$k.pub"; echo; '
              f'read -rp {shlex.quote(tr("Depois de adicionar, tecle Enter para testar a conexão… "))}; '
              'ssh -i "$k" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -T git@bitbucket.org; '
              f'echo; read -rp {shlex.quote(tr("Enter para fechar "))}')
    sid = str(uuid.uuid4())
    window = f"bitbucket-{o['id']}-{sid[:4]}"
    ensure_tmux()
    tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", window, "-c", str(Path.home()), claude_cmd(["bash", "-c", script], None))
    save_launch(sid, "", window, str(Path.home()), f"Bitbucket {o['name']}")
    return {"id": sid, "window": window}


def org_login(body):
    """Abre uma sessão do Claude na organização: sem login, o próprio Claude pede o login ali; depois vira sessão comum."""
    o = next((o for o in org_list() if o["id"] == body.get("org")), None)
    if not o:
        raise ValueError(tr("Organização desconhecida"))
    config = next(d for oid, d, _ in orgs() if oid == o["id"])
    cwd = next((p for p in o["paths"] if Path(p).is_dir()), str(Path.home()))
    sid = str(uuid.uuid4())
    window = f"login-{o['id']}-{sid[:4]}"
    ensure_tmux()
    tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", window, "-c", cwd,
         claude_cmd(["claude", "--session-id", sid, "-n", f"Login {o['name']}"], config))
    save_launch(sid, "", window, cwd, f"Login {o['name']}")
    return {"id": sid, "window": window}


# ---------- modo economia ----------

ECO = HERE / "eco.json"  # sessões que o modo economia fechou, para retomar ao desligar
VOICE_CMD = f"cd {HERE / 'tts'} && MEM=800M SWAP=0 ./limited.sh piper-venv/bin/python piper_server.py"  # = start.sh


def voice_window(on):
    """A voz do Jarvis (Piper, ~300 MB) roda na janela `voz` do tmux; o modo economia a desliga."""
    if not VOICE:
        return
    if not on:
        tmux("kill-window", "-t", f"{TMUX}:voz", check=False)
    elif "voz" not in tmux_windows() and (HERE / "tts" / "piper-venv" / "bin" / "python").exists():
        ensure_tmux()
        tmux("new-window", "-d", "-t", f"{TMUX}:", "-n", "voz", VOICE_CMD, check=False)


DOCKER = os.environ.get("KANBAN_DOCKER", "docker")  # trocável nos testes (para não parar containers reais)
SCRATCH = re.compile(r"/tmp/claude-\d+/")  # pastas de rascunho das sessões do Claude (testes, servidores de teste)


def wsl_held_mb():
    """Memória que o WSL segura (usada + cache): é o que o VmmemWSL mostra no Windows."""
    m = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
    return (m["MemTotal"] - m["MemFree"]) // 1024, (m.get("Cached", 0) + m.get("Buffers", 0)) // 1024


def proc_tree():
    """{pid: (ppid, comm)} de todos os processos visíveis."""
    out = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                stat = (d / "stat").read_text()
                out[int(d.name)] = (int(stat[stat.rindex(")") + 2:].split()[1]), (d / "comm").read_text().strip())
            except (OSError, ValueError, IndexError):
                pass
    return out


def leftovers():
    """Processos de teste ESQUECIDOS: órfãos (não descendem de nenhum `claude` vivo nem do quadro) rodando de dentro de uma
    pasta de rascunho do Claude (servidores e scripts de teste) ou o Chrome de testes do playwright. Comandos de uma
    sessão ainda aberta (no quadro ou fora dele) ficam de fora mesmo rodando no rascunho."""
    tree = proc_tree()
    roots = {p for p, (_, comm) in tree.items() if comm == "claude"} | {os.getpid()}

    def protected(p):
        for _ in range(64):  # sobe até o init
            if p in roots:
                return True
            if p <= 1 or p not in tree:
                return False
            p = tree[p][0]
        return True  # árvore estranha: na dúvida, não mata

    out = []
    for pid in tree:
        d = Path("/proc") / str(pid)
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            cwd = os.readlink(d / "cwd")
            rss = next(int(l.split()[1]) for l in (d / "status").read_text().splitlines() if l.startswith("VmRSS:"))
        except (OSError, StopIteration, ValueError):
            continue  # processo de outro usuário, de kernel ou que acabou de sair
        if cmd and (SCRATCH.match(cwd + "/") or "chrome-for-testing" in cmd) and not protected(pid):
            out.append({"pid": pid, "cmd": cmd[:120], "mb": rss // 1024})
    return out


def containers():
    """Containers Docker rodando, com a memória de cada um."""
    try:
        r = subprocess.run([DOCKER, "stats", "--no-stream", "--format", "{{.ID}}\t{{.Name}}\t{{.MemUsage}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [{"id": f[0], "name": f[1], "mem": f[2].split(" /")[0]} for f in (l.split("\t") for l in r.stdout.splitlines()) if len(f) == 3]


DROP_CACHE = ["sudo", "-n", "/usr/sbin/sysctl", "-w", "vm.drop_caches=3"]  # exige a regra em /etc/sudoers.d/kanban-eco
COMPACT = ["sudo", "-n", "/usr/sbin/sysctl", "-w", "vm.compact_memory=1"]


def can_drop_cache():
    """A regra kanban-eco existe? `sudo -l <cmd>` só diz se o comando é PERMITIDO (com senha, tudo é, para quem é
    administrador); o que importa é ele aparecer como NOPASSWD na listagem das regras."""
    r = subprocess.run(["sudo", "-n", "-k", "-l"], capture_output=True, text=True)
    return r.returncode == 0 and any("NOPASSWD" in l and "vm.drop_caches\\=3" in l for l in r.stdout.splitlines())


def eco_plan():
    """Prévia do que o modo economia vai fechar, para a confirmação na tela."""
    live = [x for x in sessions() if x["window"]]
    held, cache = wsl_held_mb()
    return {"sessions": len(live), "busy": sum(x["status"] == "working" for x in live), "leftovers": leftovers(),
            "containers": containers(), "held_mb": held, "cache_mb": cache, "drop_cache": can_drop_cache()}


def eco(body):
    """Modo economia: fecha as sessões abertas no quadro (o Claude grava a conversa enquanto roda; o `--resume` volta de
    onde estava), a voz e o cérebro do Jarvis. Ao desligar, retoma as mesmas sessões, cada uma na conta da organização
    dela, e religa a voz e o Jarvis."""
    on, s = bool(body.get("on")), load_settings()
    if on == bool(s.get("eco")):  # clique repetido (dois aparelhos): não sobrescreve a lista guardada
        return {"eco": on, "sessions": [], "failed": []}
    done, failed = [], []
    if on:
        live = [x for x in sessions() if x["window"]]
        ECO.write_text(json.dumps([{"id": x["id"], "issue": x.get("issue") or "", "title": x.get("title") or ""} for x in live]))
        s["eco"] = True
        SETTINGS.write_text(json.dumps(s, indent=1))
        held_before = wsl_held_mb()[0]
        for x in live:
            tmux("kill-window", "-t", f"{TMUX}:{x['window']}", check=False)
            done.append(x["id"])
        BRAIN.stop()
        voice_window(False)
        for x in leftovers():  # testes esquecidos: TERM, e KILL em quem não sair
            try:
                os.kill(x["pid"], signal.SIGTERM)
            except OSError:
                pass
        time.sleep(1)
        for x in leftovers():
            try:
                os.kill(x["pid"], signal.SIGKILL)
            except OSError:
                pass
        stopped = containers()
        if stopped:
            subprocess.run([DOCKER, "stop", *[c["id"] for c in stopped]], capture_output=True, timeout=120)
        saved = json.loads(ECO.read_text())
        ECO.write_text(json.dumps({"sessions": saved, "containers": [c["name"] for c in stopped]}))
        cache = subprocess.run(DROP_CACHE, capture_output=True).returncode == 0 if can_drop_cache() else None
        if cache:
            subprocess.run(COMPACT, capture_output=True)
        time.sleep(1)
        return {"eco": True, "sessions": done, "failed": [], "containers": [c["name"] for c in stopped],
                "held_before": held_before, "held_after": wsl_held_mb()[0], "drop_cache": cache}
    else:
        try:
            saved = json.loads(ECO.read_text())
        except (OSError, ValueError):
            saved = []
        if isinstance(saved, dict):  # {"sessions": [...], "containers": [...]}; a lista pura é do formato antigo
            if saved.get("containers"):
                subprocess.run([DOCKER, "start", *saved["containers"]], capture_output=True, timeout=120)
            saved = saved.get("sessions") or []
        for x in saved:
            try:
                resume({"id": x["id"], "issue": x["issue"]})
                done.append(x["id"])
            except (ValueError, subprocess.CalledProcessError):  # sem conversa gravada (ex.: terminal de login)
                failed.append(x.get("title") or x["id"])
        ECO.unlink(missing_ok=True)
        s["eco"] = False
        SETTINGS.write_text(json.dumps(s, indent=1))
        voice_window(True)
        if VOICE:
            threading.Thread(target=BRAIN.warm, daemon=True).start()
    return {"eco": on, "sessions": done, "failed": failed}


def done_card_ids():
    """Cards concluídos ou cancelados (Linear, Jira e tarefas): os worktrees deles são candidatos à limpeza."""
    issues, _ = tracker_issues(max_age=600)
    tasks = [task_as_issue(t) for t in load_tasks()]
    return {i["identifier"] for i in (issues or []) + tasks if (i.get("state") or {}).get("type") in ("completed", "canceled")}


def res_action(name):
    """Ações da página Recursos (as travas ficam no resources.py)."""
    def run(body):
        import resources
        return getattr(resources, name)(body)
    return run


def link(body):
    data = load_launched()
    data.setdefault(body["id"], {"window": None, "cwd": "", "ts": time.time()})["issue"] = body["issue"]
    LAUNCHED.write_text(json.dumps(data, indent=1))
    return {"ok": True}


def window_of(sid):
    w = load_launched().get(sid, {}).get("window")
    if not w or w not in tmux_windows():
        raise ValueError(tr("Essa sessão não está numa janela do quadro"))
    return f"{TMUX}:{w}"


def kill(body):
    """Encerra a sessão: fecha a janela do tmux (o claude recebe SIGHUP e sai) e tira ela do card."""
    tmux("kill-window", "-t", window_of(body["id"]))
    data = load_launched()
    data[body["id"]]["closed"] = True
    LAUNCHED.write_text(json.dumps(data, indent=1))
    return {"ok": True}


# ---------- resumo geral ----------

SUMMARY_PROMPT = """Você resume o andamento do trabalho de um desenvolvedor em cada card do quadro dele.
Para cada card abaixo você recebe o título e o trecho final da conversa da sessão do Claude Code (pedidos dele e respostas do Claude).
Use a conversa toda para entender o estado atual; o que vale é o final.
Para CADA card, escreva 1 ou 2 frases curtas (até ~180 caracteres) sobre o estado do TRABALHO em si:
o que já está pronto, o que está em construção ou bloqueando, e o que falta.
Exemplos de tom: "Código pronto e revisado, PRs abertos aguardando review."
"Código em construção; bloqueio no bot ao enviar anexo."
Não fale de raia, de status do quadro nem de "aguardando você". Português, sem markdown.
Responda SOMENTE com JSON: [{"id": "<id do card>", "status": "<texto>"}], na mesma ordem dos cards.

"""


def summary_prompt():
    """O prompt fica em português; com a tela em inglês, pede os textos em inglês."""
    if load_settings().get("lang") == "en-US":
        return SUMMARY_PROMPT + "IMPORTANTE: escreva cada \"status\" em inglês (en-US), não em português.\n\n"
    return SUMMARY_PROMPT


FENCE = re.compile(r"```([\w+.-]*)[^\n]*\n(.*?)```", re.S)


def code_blocks(sid):
    """Blocos de código da última resposta do Claude (tudo o que ele escreveu desde o seu último pedido), lidos do
    jsonl: o markdown original, sem as quebras de linha que o terminal põe. Para copiar no celular."""
    if not re.fullmatch(r"[0-9a-f-]{36}", sid or ""):
        raise ValueError(tr("Sessão inválida"))
    path = next(iter(session_files(f"*/{sid}.jsonl")), None)
    if not path:
        return {"blocks": []}
    texts = []
    for r in reversed(tail_records(path, 2_000_000)):
        if r.get("isSidechain") or r.get("isMeta"):
            continue
        if r.get("type") == "user":
            c = (r.get("message") or {}).get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue  # resultado de ferramenta no meio da resposta
            if text_of(r.get("message")).strip():
                break  # seu último pedido: a resposta começa depois dele
        elif r.get("type") == "assistant":
            texts.append(text_of(r.get("message")))
    text = "\n".join(reversed(texts))
    blocks = [{"lang": m.group(1).lower(), "code": m.group(2).rstrip("\n")} for m in FENCE.finditer(text)]
    return {"blocks": [b for b in blocks if b["code"].strip()][-8:]}


def history(sid, limit=30, max_chars=7000):
    """Últimas mensagens da conversa (seus pedidos e as respostas do Claude, sem saída de ferramenta),
    do fim do jsonl, para o resumo ter contexto além da última troca."""
    if not re.fullmatch(r"[0-9a-f-]{36}", sid or ""):
        return []
    path = next(iter(session_files(f"*/{sid}.jsonl")), None)
    if not path:
        return []
    with open(path, "rb") as f:  # só no clique do Resumo; saídas de ferramenta ocupam quase todo o arquivo
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 8_000_000))
        raw = f.read().split(b"\n")[1:]
    msgs = []
    for line in raw:
        # Filtro barato antes do json.loads: resultado de ferramenta e chamada de ferramenta não entram
        if b'"tool_result"' in line or (b'"type":"assistant"' in line and b'"type":"text"' not in line):
            continue
        if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("isSidechain") or r.get("isMeta") or r.get("type") not in ("user", "assistant"):
            continue
        text = text_of(r.get("message")).strip()
        if text and not text.startswith("<"):
            msgs.append(("pedido" if r["type"] == "user" else "claude", " ".join(text.split())[:600]))
    out, size = [], 0
    for role, text in reversed(msgs[-limit:]):  # do mais recente para trás até o limite de tamanho
        size += len(text)
        if size > max_chars:
            break
        out.append(f"{role}: {text}")
    return out[::-1]


# ---------- PRs do card (via gh) ----------

# PR citado pelo nome curto ("rpa#294" = <KANBAN_GH_ORG>/<KANBAN_PR_PREFIX>rpa#294), só nos repositórios listados em
# KANBAN_PR_SHORT (vírgulas) e só na organização cujo github_org é KANBAN_GH_ORG. Sem isso, só links completos.
GH_ORG = os.environ.get("KANBAN_GH_ORG", "")
PR_PREFIX = os.environ.get("KANBAN_PR_PREFIX", "")
_SHORT = [re.escape(x.strip()) for x in os.environ.get("KANBAN_PR_SHORT", "").split(",") if x.strip()]
PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_SHORT = re.compile(rf"\b(?:{re.escape(PR_PREFIX)})?({'|'.join(_SHORT)})\s?#(\d+)", re.I) if GH_ORG and _SHORT else None
BB_PR_URL = re.compile(r"bitbucket\.org/([\w.-]+/[\w.-]+)/pull-requests/(\d+)")
_repo_cache, _pr_cache = {}, {}  # cwd -> ("github" | "bitbucket" | "", "dono/repo");  (host, repo, n) -> (ts, info)
_me = {}  # GH_CONFIG_DIR -> seu login no GitHub naquela organização (lido do gh uma vez)


def gh_me(env=None):
    k = (env or {}).get("GH_CONFIG_DIR", "")
    if k not in _me:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=30, env=env)
        _me[k] = r.stdout.strip()
    return _me[k]
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def repo_of(cwd):
    """(serviço, "dono/repo") do origin do checkout; ("", "") se não for GitHub nem Bitbucket."""
    if cwd not in _repo_cache:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True)
        m = re.search(r"(github\.com|bitbucket\.org)[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", r.stdout.strip())
        _repo_cache[cwd] = (m.group(1).split(".")[0], m.group(2)) if m else ("", "")
    return _repo_cache[cwd]


def gh_json(*args, env=None):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, env=env)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def card_prs(card_id, sessions, texts, env=None, short=True, bb=None):
    """PRs de um card, como (serviço, "dono/repo", número): PRs citados na conversa (link do GitHub ou do Bitbucket, ou
    repo#N) + o PR da branch da sessão, só quando a branch é deste card (tem o ID no nome): sessão rodando num checkout
    que está na branch de outra tarefa traria o PR errado. `bb` = credencial do Bitbucket da organização."""
    found = set()
    for t in texts:
        found |= {("github", repo, int(n)) for repo, n in PR_URL.findall(t)}
        found |= {("bitbucket", repo, int(n)) for repo, n in BB_PR_URL.findall(t)}
        if short and PR_SHORT:  # nome curto ("rpa#294") só onde foi configurado; no resto, só links completos
            found |= {("github", f"{GH_ORG}/{PR_PREFIX}{name.lower()}", int(n)) for name, n in PR_SHORT.findall(t)}
    for s in sessions:
        (host, repo), branch = repo_of(s.get("cwd") or "."), s.get("branch") or ""
        if not (repo and branch and card_id and card_id.lower() in branch.lower()):
            continue
        if host == "github":
            for pr in gh_json("pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", "number", env=env) or []:
                found.add(("github", repo, pr["number"]))
        elif host == "bitbucket" and bb:
            q = 'source.branch.name="%s" AND (state="OPEN" OR state="MERGED" OR state="DECLINED")' % branch.replace('"', '\\"')
            for pr in (bb_get(f"/repositories/{repo}/pullrequests?q={quote(q)}&fields=values.id", bb) or {}).get("values") or []:
                found.add(("bitbucket", repo, pr["id"]))
    return sorted(found, key=lambda x: -x[2])[:6]


# ---------- PRs do Bitbucket Cloud (API 2.0; a chave SSH só serve para o git, a API pede token de API da Atlassian) ----------

BB_API = "https://api.bitbucket.org/2.0"
_bb_me = {}  # e-mail -> account_id


def bb_conf(org):
    """~/.config/claude-orgs/<org>/bitbucket.json: {"email", "token"} (token de API da Atlassian com escopos de leitura
    de repositório e pull request do Bitbucket)."""
    try:
        c = json.loads((ORGS_FILE.parent / org / "bitbucket.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    return c if c.get("email") and c.get("token") else None


def bb_get(path, bb):
    req = urllib.request.Request(BB_API + path, headers={
        "Authorization": "Basic " + base64.b64encode(f"{bb['email']}:{bb['token']}".encode()).decode(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001  (sem rede/sem permissão: o resumo sai sem PRs, como no GitHub)
        return None


def bb_me(bb):
    """Seu account_id no Bitbucket; vazio se o token não puder ler o usuário (aí os PRs não são filtrados por autor)."""
    if bb["email"] not in _bb_me:
        _bb_me[bb["email"]] = (bb_get("/user", bb) or {}).get("account_id", "")
    return _bb_me[bb["email"]]


def bb_badges(d, statuses):
    """Os mesmos selos dos PRs do GitHub, a partir do PR e dos builds do Bitbucket."""
    if d.get("state") == "MERGED":
        return [(tr("mergeado"), "ok")]
    if d.get("state") in ("DECLINED", "SUPERSEDED"):
        return [(tr("fechado"), "muted")]
    b = [(tr("rascunho"), "muted")] if d.get("draft") else []
    builds = [x.get("state") for x in statuses]
    failed = [x for x in builds if x in ("FAILED", "STOPPED")]
    if failed:
        b.append((tr("teste quebrado ({n})", n=len(failed)), "bad"))
    elif "INPROGRESS" in builds:
        b.append((tr("testes rodando"), "warn"))
    elif builds:
        b.append((tr("testes ok"), "ok"))
    people = d.get("participants") or []
    if any(p.get("state") == "changes_requested" for p in people):
        b.append((tr("pedido de mudança"), "bad"))
    elif any(p.get("approved") for p in people):
        b.append((tr("aprovado"), "ok"))
    else:
        b.append((tr("sem review"), "warn"))
    n = d.get("comment_count") or 0
    if n:
        b.append((tr("{n} comentários", n=n) if n > 1 else tr("1 comentário"), "muted"))
    return b


def bb_pr_info(repo, n, bb):
    hit = _pr_cache.get(("bitbucket", repo, n))
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    d = bb_get(f"/repositories/{repo}/pullrequests/{n}", bb)
    info = None
    if d:
        statuses = (bb_get(f"/repositories/{repo}/pullrequests/{n}/statuses?pagelen=50", bb) or {}).get("values") or []
        info = {"host": "bitbucket", "repo": repo.split("/")[-1], "number": n, "url": ((d.get("links") or {}).get("html") or {}).get("href", ""),
                "title": d.get("title", ""), "open": d.get("state") == "OPEN", "author": (d.get("author") or {}).get("account_id", ""),
                "badges": [{"text": t, "tone": tone} for t, tone in bb_badges(d, statuses)]}
    _pr_cache[("bitbucket", repo, n)] = (time.time(), info)
    return info


def pr_badges(d):
    """Status no estilo do GitHub: estado, review, testes, comentários e se dá para mergear."""
    b = []
    if d["state"] == "MERGED":
        return [(tr("mergeado"), "ok")]
    if d["state"] == "CLOSED":
        return [(tr("fechado"), "muted")]
    if d.get("isDraft"):
        b.append((tr("rascunho"), "muted"))
    checks = d.get("statusCheckRollup") or []
    failed = [c for c in checks if (c.get("conclusion") or c.get("state")) in FAILED]
    pending = [c for c in checks if c.get("status") not in (None, "COMPLETED") or c.get("state") == "PENDING"]
    if failed:
        b.append((tr("teste quebrado ({n})", n=len(failed)), "bad"))
    elif pending:
        b.append((tr("testes rodando"), "warn"))
    elif checks:
        b.append((tr("testes ok"), "ok"))
    review = d.get("reviewDecision")
    states = {r.get("state") for r in d.get("reviews") or []}
    if review == "CHANGES_REQUESTED" or "CHANGES_REQUESTED" in states:
        b.append((tr("pedido de mudança"), "bad"))
    elif review == "APPROVED":
        b.append((tr("aprovado"), "ok"))
    else:
        b.append((tr("sem review"), "warn"))
    n_comments = len(d.get("comments") or []) + sum(1 for r in d.get("reviews") or [] if (r.get("body") or "").strip())
    if n_comments:
        b.append((tr("{n} comentários", n=n_comments) if n_comments > 1 else tr("1 comentário"), "muted"))
    if d.get("mergeable") == "CONFLICTING":
        b.append((tr("conflito"), "bad"))
    elif d.get("mergeStateStatus") in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        b.append((tr("pode mergear"), "ok"))
    return b


def pr_info(repo, n, env=None):
    hit = _pr_cache.get((repo, n))
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    d = gh_json("pr", "view", str(n), "--repo", repo, "--json",
                "number,title,url,state,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,comments,reviews,author", env=env)
    info = None if not d else {"host": "github", "repo": repo.split("/")[-1], "number": n, "url": d["url"], "title": d["title"],
                               "open": d["state"] == "OPEN", "author": (d.get("author") or {}).get("login", ""),
                               "badges": [{"text": t, "tone": tone} for t, tone in pr_badges(d)]}
    _pr_cache[(repo, n)] = (time.time(), info)
    return info


def summary(body):
    """Uma ou duas frases por card sobre o estado do trabalho, via `claude -p` (Haiku), só no clique,
    mais os PRs do card com status do GitHub (gh), buscados em paralelo.
    O claude roda sem ferramentas, sem hooks, sem MCP e sem gravar sessão (não aparece no quadro)."""
    items = body.get("items") or []
    if not items:
        return {"items": []}
    cut = lambda v, n: str(v or "")[:n].replace("\n", " ")
    by_id = {x["id"]: x for x in sessions()}
    lines, texts = [], {}
    for it in items[:30]:
        lines.append(f"- id: {cut(it.get('id'), 20)} | título: {cut(it.get('title'), 120)}")
        texts[it.get("id")] = []
        for s in (it.get("sessions") or [])[:3]:
            hist = history(s.get("id", ""))
            texts[it.get("id")] += hist
            if hist:
                lines.append("    conversa (mais recente por último):")
                lines += [f"      {h}" for h in hist]
            else:
                lines.append(f"    pedido: {cut(s.get('prompt'), 400)}")
                lines.append(f"    resposta: {cut(s.get('last'), 400)}")

    def prs_of(it):
        sess = [by_id[s["id"]] for s in it.get("sessions") or [] if s.get("id") in by_id]
        org = it.get("org") or active_org()
        env = claude_env(next((d for o, d, _ in orgs() if o == org), None))  # gh logado na conta da organização do card
        short = bool(GH_ORG) and org_meta(org).get("github_org", GH_ORG if not orgs() else "") == GH_ORG
        bb = bb_conf(org) if orgs() else None
        try:
            refs = card_prs(it.get("id"), sess, texts[it.get("id")], env, short, bb)
            infos = [pr_info(r, n, env) if host == "github" else bb_pr_info(r, n, bb) if bb else None for host, r, n in refs]
            # Só PRs seus (citados na conversa aparecem PRs de outras pessoas) e sem os fechados sem merge
            me = lambda p: gh_me(env) if p["host"] == "github" else bb_me(bb)
            infos = [p for p in infos if p and (p["author"] == me(p) or not me(p))
                     and p["badges"] != [{"text": tr("fechado"), "tone": "muted"}]]
            return it.get("id"), sorted(infos, key=lambda p: not p["open"])[:4]  # abertos primeiro
        except (subprocess.SubprocessError, OSError, ValueError):
            return it.get("id"), []  # sem gh/sem rede: o resumo sai sem PRs

    with ThreadPoolExecutor(max_workers=8) as pool:
        prs_future = [pool.submit(prs_of, it) for it in items[:30]]
        r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence",
                            "--settings", json.dumps({"disableAllHooks": True}), "--tools", "", "--strict-mcp-config"],
                           input=summary_prompt() + "\n".join(lines), capture_output=True, text=True, timeout=120, cwd=HERE,
                           env=claude_env(org_config()))
        prs = dict(f.result() for f in prs_future)
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        raise ValueError((r.stderr or out).strip()[-300:] or tr("o claude não respondeu"))
    try:
        data = json.loads(out[out.index("["):out.rindex("]") + 1])
        return {"items": [{"id": str(d.get("id", "")), "status": str(d.get("status", "")), "prs": prs.get(str(d.get("id")), [])}
                          for d in data]}
    except (ValueError, AttributeError):
        return {"text": out}  # veio fora do formato: mostra como texto


# ---------- comandos por voz ----------

VOICE_PROMPT = """Você é o assistente de voz do quadro Kanban de um desenvolvedor: conversa com ele por voz
(a fala chega transcrita, pode ter erros de reconhecimento) e transforma pedidos em ações no quadro. Os cards são issues do Linear (ex.: ABC-123) ou tarefas livres (T-12); "tarefa 12" = T-12,
"abc um dois três" = ABC-123. Cada card pode ter sessões do Claude Code abertas (ids abaixo).

Ações possíveis (use só estas, com estes campos):
- {"type": "create_task", "title": "...", "description": "...", "priority": 0-4}  (1 urgente, 2 alta, 3 média, 4 baixa, 0 sem)
- {"type": "task_state", "card": "T-n", "state": "completed" | "unstarted" | "started"}  (só tarefas T-)
- {"type": "move", "card": "...", "lane": "todo" | "claude" | "waiting" | "review" | "done"}
- {"type": "archive", "card": "...", "archived": true | false}
- {"type": "remind", "card": "...", "on": true | false}
- {"type": "send", "card": "...", "session": "<id opcional>", "text": "..."}  (manda mensagem/prompt para a sessão do card;
  "text" é o que ele quer dizer à sessão, fiel à fala, só sem o comando em si; responder "sim"/"pode seguir" também é send)
- {"type": "key", "card": "...", "session": "<id opcional>", "key": "Escape" | "Enter" | "1" | "2" | "3"}
  (Escape = interromper/parar; "1"/"2"/"3" = escolher a opção do menu na tela)
- {"type": "launch", "card": "...", "prompt": "<opcional: instrução extra>"}  (iniciar/abrir sessão nova para o card)
- {"type": "open", "card": "...", "session": "<id opcional>"}  (abrir/mostrar o terminal da sessão)
- {"type": "kill", "card": "...", "session": "<id opcional>"}  (encerrar a sessão)
- {"type": "summary"}  (resumo geral do quadro)
- {"type": "org", "org": "<id>"}  (trocar de organização: filtra o quadro e passa a usar a conta dela; ids na lista
  "Organizações" do contexto)

Raias: todo=A fazer, claude=Claude trabalhando, waiting=Aguardando você, review=Em revisão, done=Concluído.
Mover um card arquivado para uma raia já o desarquiva.
Perguntas sobre o quadro ("o que a tarefa quinze está fazendo?", "quem está me esperando?") você responde com os
dados abaixo (raia, status e última resposta de cada sessão), sem ações. Se o pedido não estiver claro ou o card não
existir, devolva actions vazio e pergunte em "reply". Use a conversa anterior para entender referências ("e a dois?").
"reply" será LIDO EM VOZ ALTA: 1 a 3 frases curtas, tom cordial e direto (estilo JARVIS), sem markdown, sem listas, sem URLs.
Fale de forma natural e variada, como numa conversa entre colegas: chame-o de "senhor" só de vez em quando (no máximo
uma vez a cada três ou quatro respostas, nunca em toda frase), não comece as respostas sempre do mesmo jeito e não use
fórmulas repetidas como "Pois não". Nunca use o nome dele.
Chame os cards de "a T-15", "a ABC-123". Só kill (encerrar uma sessão) é crítico: aí o reply PERGUNTA antes ("Encerro
a sessão da T-15?") e MESMO ASSIM a ação kill vai em AÇÕES (o quadro guarda e só executa depois do sim). Todo o resto
roda na hora, sem pedir confirmação: o reply conta o que
foi feito, curto ("Mandei a T-15 rodar os testes, te aviso quando terminar.").
Responda SOMENTE com JSON: {"actions": [...], "reply": "<o que você vai falar>"}

Cards:
"""

VOICE_ACTIONS = {"create_task", "task_state", "move", "archive", "remind", "send", "key", "launch", "open", "kill", "summary", "org"}
VOICE_NEEDS_SESSION = {"send", "key", "open", "kill"}


def voice_actions(data, cards):
    """Só passa ação conhecida, sobre card que existe, e sessão aberta no tmux daquele card.
    cards: {id: [sids com janela, mais recente primeiro]}."""
    out = []
    for a in (data.get("actions") if isinstance(data, dict) else None) or []:
        if not isinstance(a, dict) or a.get("type") not in VOICE_ACTIONS:
            continue
        t, card = a["type"], str(a.get("card", "")).upper()
        if t == "create_task":
            title = str(a.get("title", "")).strip()
            prio = a.get("priority") if a.get("priority") in PRIORITY_LABELS else 0
            if title:
                out.append({"type": t, "title": title[:200], "description": str(a.get("description", ""))[:4000], "priority": prio})
            continue
        if t == "summary":
            out.append({"type": t})
            continue
        if t == "org":
            org = str(a.get("org", "")).lower()
            if org in {o["id"] for o in org_list()}:
                out.append({"type": t, "org": org})
            continue
        if card not in cards:
            continue
        x = {"type": t, "card": card}
        if t == "task_state":
            if not card.startswith("T-") or a.get("state") not in TASK_STATES:
                continue
            x["state"] = a["state"]
        elif t == "move":
            if a.get("lane") not in LANES:
                continue
            x["lane"] = a["lane"]
        elif t in ("archive", "remind"):
            x["on" if t == "remind" else "archived"] = bool(a.get("on" if t == "remind" else "archived", True))
        elif t == "launch":
            x["prompt"] = str(a.get("prompt") or "")[:4000]
        if t in VOICE_NEEDS_SESSION:
            sids = cards[card]
            sid = a.get("session") if a.get("session") in sids else (sids[0] if sids else None)
            if not sid:
                continue  # card sem sessão aberta no quadro
            x["session"] = sid
        if t == "send":
            x["text"] = str(a.get("text", "")).strip()[:4000]
            if not x["text"]:
                continue
        if t == "key":
            if a.get("key") not in ALLOWED_KEYS:
                continue
            x["key"] = a["key"]
        out.append(x)
    return out[:10]


def voice(body):
    """Um turno da conversa por voz: `claude -p` (Haiku, sem ferramentas) com o histórico devolve a resposta falada e ações
    validadas; quem executa é o quadro, pelas mesmas rotas dos botões. Os cards vêm do navegador (ele sabe a raia), mas só valem os que o servidor conhece."""
    text = str(body.get("text", "")).strip()[:2000]
    if not text:
        raise ValueError("Não ouvi nada")
    cards, lines = voice_context(body)
    cut = lambda v, n: str(v or "")[:n].replace("\n", " ")
    talk = [f"{'Usuário' if h.get('role') == 'user' else 'Você'}: {cut(h.get('text'), 300)}" for h in (body.get("history") or [])[-10:]]
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence",
                        "--settings", json.dumps({"disableAllHooks": True}), "--tools", "", "--strict-mcp-config"],
                       input=VOICE_PROMPT + "\n".join(lines) + ("\n\nConversa até aqui:\n" + "\n".join(talk) if talk else "")
                       + f"\n\nUsuário agora: {text}",
                       capture_output=True, text=True, timeout=60, cwd=HERE, env=claude_env(org_config()))
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        raise ValueError((r.stderr or out).strip()[-300:] or tr("o claude não respondeu"))
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        return {"actions": [], "reply": out[:300]}
    return {"actions": voice_actions(data, cards), "reply": str(data.get("reply", ""))[:300]}


# ---------- Jarvis rápido: um Claude sempre ligado, respondendo em fluxo ----------
# Subir o `claude -p` a cada fala custava ~2,4 s só de partida. Aqui um processo do Haiku fica aberto (stream-json),
# já aquecido, e guarda a conversa sozinho; a resposta vem em pedaços e a fala sai frase por frase para o navegador,
# enquanto o resto ainda está sendo gerado. Formato: primeiro a fala, depois a linha "AÇÕES: [...]".
VOICE_SYSTEM = VOICE_PROMPT.split("Responda SOMENTE com JSON")[0].replace("com os\ndados abaixo", "com os\ndados do quadro") + """
Cada mensagem traz o estado do quadro (ou avisa que não mudou) e a fala do usuário.
Formato da resposta: PRIMEIRO a fala, em texto puro (1 a 3 frases curtas, sem markdown); DEPOIS, numa linha própria,
"AÇÕES: " seguido do JSON da lista de ações ("AÇÕES: []" se não houver). Nada depois disso.
"""
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def take_sentences(spoken, start):
    """Frases completas de spoken[start:] (terminadas em pontuação + espaço) e até onde foi. O pedaço depois da última
    pontuação ainda pode estar chegando (inclusive o começo do marcador "AÇÕES:"), então fica para depois."""
    ends = [m.end() for m in re.finditer(r"[.!?…]+\s+", spoken[start:])]
    if not ends:
        return [], start
    chunk = spoken[start:start + ends[-1]]
    return [f.strip() for f in SENTENCE_END.split(chunk) if f.strip()], start + ends[-1]


class JarvisBrain:
    MAX_TURNS, IDLE, TIMEOUT = 20, 20 * 60, 40

    def __init__(self):
        self.proc, self.turns, self.last, self.ctx_hash = None, 0, 0.0, None
        self.lock = threading.Lock()

    def _spawn(self):
        self.stop()
        self.proc = subprocess.Popen(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--model", "haiku", "--no-session-persistence",
             "--settings", json.dumps({"disableAllHooks": True}), "--tools", "", "--strict-mcp-config", "--system-prompt", VOICE_SYSTEM],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=HERE,
            env=claude_env(org_config()))
        self.turns, self.ctx_hash, self.last = 0, None, time.time()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def warm(self):
        with self.lock:
            if not (self.proc and self.proc.poll() is None):
                self._spawn()

    def ask(self, context, text, on_delta):
        """Manda a fala (com o quadro, se mudou) e chama on_delta(texto) conforme a resposta chega. Devolve o texto todo."""
        with self.lock:
            if not (self.proc and self.proc.poll() is None) or self.turns >= self.MAX_TURNS or time.time() - self.last > self.IDLE:
                self._spawn()  # conversa longa ou parada há muito: recomeça limpa (contexto leve = resposta rápida)
            h = hash(context)
            head = f"Quadro agora:\n{context}" if h != self.ctx_hash else "Quadro: sem mudanças desde a última mensagem."
            self.ctx_hash = h
            msg = {"type": "user", "message": {"role": "user", "content": f"{head}\n\nUsuário agora: {text}"}}
            try:
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                full, deadline = "", time.time() + self.TIMEOUT
                while True:
                    if not select.select([self.proc.stdout], [], [], max(0.1, deadline - time.time()))[0]:
                        raise TimeoutError("o Jarvis demorou demais")
                    line = self.proc.stdout.readline()
                    if not line:
                        raise RuntimeError("o processo do Jarvis caiu")
                    ev = json.loads(line)
                    if ev.get("type") == "stream_event":
                        d = ev.get("event", {}).get("delta", {})
                        if d.get("type") == "text_delta":
                            full += d["text"]
                            on_delta(full)
                    elif ev.get("type") == "result":
                        self.turns += 1
                        self.last = time.time()
                        return full or str(ev.get("result", ""))
            except Exception:
                self.stop()  # na próxima fala sobe outro; esta cai no caminho antigo
                raise


BRAIN = JarvisBrain()


def voice_stream(body, emit):
    """Um turno do Jarvis em fluxo: emit({"say": frase}) assim que cada frase da fala fica pronta e, no fim,
    emit({"done": True, "actions": [...validadas...], "reply": fala}). Se o cérebro falhar, usa o voice() antigo."""
    text = str(body.get("text", "")).strip()[:2000]
    if not text:
        raise ValueError("Não ouvi nada")
    cards, lines = voice_context(body)
    sent = [0]  # até onde a fala já virou frase

    def on_delta(full):
        frases, sent[0] = take_sentences(full.split("AÇÕES:")[0], sent[0])
        for f in frases:
            emit({"say": f})

    try:
        full = BRAIN.ask("\n".join(lines) or "(nenhum card)", text, on_delta)
    except Exception as e:  # noqa: BLE001  (cérebro fora: caminho antigo, mais lento mas funciona)
        print(f"Jarvis rápido falhou ({e}); usando claude -p", flush=True)
        r = voice(body)
        if r.get("reply"):
            emit({"say": r["reply"]})
        emit({"done": True, **r})
        return
    spoken, _, acts = full.partition("AÇÕES:")
    for f in [x.strip() for x in SENTENCE_END.split(spoken[sent[0]:]) if x.strip()]:
        emit({"say": f})  # o que sobrou (a última frase, sem espaço depois dela)
    try:
        data = {"actions": json.loads(acts[acts.index("["):acts.rindex("]") + 1])} if "[" in acts else {"actions": []}
    except ValueError:
        data = {"actions": []}
    actions = voice_actions(data, cards)
    print(f"jarvis: {len(data['actions'])} ação(ões) do modelo, {len(actions)} válida(s) {[a['type'] for a in actions]} "
          f"| cards com sessão: {[k for k, v in cards.items() if v]} | fim do texto: {full[-140:]!r}", flush=True)
    emit({"done": True, "actions": actions, "reply": " ".join(spoken.split())[:600]})


def voice_context(body):
    """Cards que o navegador mandou (ele sabe a raia), filtrados pelos que o servidor conhece, com as sessões abertas de
    cada um: ({card: [sids]}, linhas de texto para o modelo)."""
    issues, _ = tracker_issues(max_age=600)
    known = {i["identifier"] for i in issues or []} | {t["id"] for t in load_tasks()}
    live = {s["id"]: s for s in sessions() if s["window"]}
    cut = lambda v, n: str(v or "")[:n].replace("\n", " ")
    cards, lines = {}, []
    for c in (body.get("cards") or [])[:150]:
        cid = str(c.get("id", ""))
        if cid not in known:
            continue
        sess = [live[s["id"]] for s in c.get("sessions") or [] if s.get("id") in live]
        cards[cid] = [s["id"] for s in sess]
        lines.append(f"- {cid} | raia: {cut(c.get('lane'), 20)} | org: {cut(c.get('org'), 20)} | {cut(c.get('title'), 120)}")
        for s in sess:  # o que a sessão está fazendo, para responder perguntas (jsonl inteiro deixaria o turno lento)
            lines.append(f"    sessão {s['id']} ({s['status']}): {cut(s['title'], 80)}")
            lines.append(f"      pedido: {cut(s['prompt'], 300)}")
            lines.append(f"      última resposta do Claude: {cut(s['last'], 300)}")
    if org_list():
        lines.insert(0, "Organizações: " + ", ".join(o["id"] + (" (ativa)" if o["id"] == active_org() else "") for o in org_list()))
    return cards, lines



_tts_cache = (0.0, False)


def tts_ready():
    """A voz local está carregada? Guardado por 10 s: o /api/state roda a cada 3 s."""
    global _tts_cache
    if not VOICE:
        return False
    if time.time() - _tts_cache[0] < 10:
        return _tts_cache[1]
    try:
        with urllib.request.urlopen(TTS_URL + "/health", timeout=1) as r:
            ok = bool(json.loads(r.read()).get("ready"))
    except (OSError, ValueError):
        ok = False
    _tts_cache = (time.time(), ok)
    return ok


def local_tts_stream(text):
    """Abre o stream de áudio (PCM 24 kHz) da voz local; com erro, o quadro cai na voz do navegador."""
    text = str(text or "").strip()[:600]
    if not text:
        raise ValueError("nada para falar")
    req = urllib.request.Request(TTS_URL + "/stream", method="POST", data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        raise ValueError(f"voz local {e.code}: {e.read()[:200].decode(errors='replace')}")
    except OSError as e:
        raise ValueError(f"voz local fora do ar: {e}")


# ---------- notificações (Web Push) ----------

_push_lock = threading.Lock()


def load_subs():
    try:
        return json.loads(PUSH_SUBS.read_text())
    except (OSError, ValueError):
        return []


def push_subscribe(body):
    import push
    sub = body.get("subscription") or {}
    if not push.allowed(sub.get("endpoint")) or not (sub.get("keys") or {}).get("p256dh") or not sub["keys"].get("auth"):
        raise ValueError(tr("Inscrição inválida"))
    with _push_lock:
        subs = [x for x in load_subs() if x["endpoint"] != sub["endpoint"]]
        subs.append({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
                     "ua": str(body.get("ua", ""))[:120], "ts": time.time()})
        PUSH_SUBS.write_text(json.dumps(subs, indent=1))
    return {"ok": True, "devices": len(subs)}


def push_unsubscribe(body):
    with _push_lock:
        subs = [x for x in load_subs() if x["endpoint"] != body.get("endpoint")]
        PUSH_SUBS.write_text(json.dumps(subs, indent=1))
    return {"ok": True, "devices": len(subs)}


def push_all(message):
    """Manda para todos os aparelhos; inscrição expirada (404/410) sai da lista. Roda fora da thread do HTTP."""
    subs = load_subs()
    if not subs:
        return
    import push
    for sub in subs:
        try:
            status = push.send(sub, message)
        except Exception as e:  # noqa: BLE001  (sem rede, serviço fora: tenta na próxima)
            print(f"push falhou: {e}", flush=True)
            continue
        if status in (404, 410):
            push_unsubscribe({"endpoint": sub["endpoint"]})
        elif status >= 300:
            print(f"push recusado ({status}) por {sub['endpoint'][:40]}", flush=True)


def push_test(body):
    if not load_subs():
        raise ValueError(tr("Nenhum aparelho com notificação ativa"))
    threading.Thread(target=push_all, args=({"title": "YourTurn", "body": tr("Notificações funcionando."), "tag": "teste", "url": "/"},),
                     daemon=True).start()
    return {"ok": True, "devices": len(load_subs())}


def new_waits(prev, current):
    """Sessões que acabaram de passar a esperar você. prev/current: {sid: status}. Na 1ª rodada (prev None) não avisa
    nada (senão o reinício do servidor dispararia uma notificação por sessão já parada)."""
    if prev is None:
        return []
    return [sid for sid, st in current.items() if st == "waiting" and prev.get(sid) != "waiting"]


def watch_sessions(every=10):
    """Vigia as sessões abertas no tmux mesmo sem nenhum navegador aberto e avisa quando uma passa a aguardar você.
    Cards arquivados não avisam (como no quadro)."""
    prev = None
    while True:
        try:
            if load_subs():
                board = load_board()
                live = [s for s in sessions() if s["window"] and not board.get(s.get("issue") or "", {}).get("archived")]
                current = {s["id"]: s["status"] for s in live}
                by_id = {s["id"]: s for s in live}
                for sid in new_waits(prev, current):
                    s = by_id[sid]
                    last = " ".join((s.get("last") or "").replace("`", "").split())  # sem as crases do markdown
                    threading.Thread(target=push_all, daemon=True, args=({
                        "title": tr("Aguardando você") + " · " + " · ".join(x for x in (org_name(s.get("org")), s.get("issue") or s.get("window")) if x),
                        "body": (s.get("title") or "") + (f"\n{last[-140:]}" if last else ""),
                        "tag": sid, "url": f"/?abrir={sid}"},)).start()
                prev = current
            else:
                prev = None  # sem aparelho inscrito: nem vigia (e não avisa o acumulado quando alguém se inscrever)
        except Exception as e:  # noqa: BLE001  (a vigia não pode morrer)
            print(f"vigia das sessões: {e}", flush=True)
        time.sleep(every)


# ---------- Slack: DMs e menções a você, um token de usuário por organização ----------

SLACK_API = os.environ.get("KANBAN_SLACK_API", "https://slack.com/api")  # trocável nos testes
SLACK_SEEN = HERE / "slack_seen.json"  # até onde você já viu, por organização
SLACK_HIDDEN = HERE / "slack_hidden.json"  # mensagens que você tirou da lista: {org: ["canal|ts", …]}
_slack = {}  # organização -> {"me", "items", "error", "notified"}


def slack_via_mcp(org):
    """Workspace que não deixa instalar app próprio: lê e responde pelo conector claude.ai Slack (MCP) da organização.
    slack.json {"via": "mcp", "every": segundos}. Cada consulta é um `claude -p` (Haiku): ~30 s e consome limite."""
    try:
        c = json.loads((ORGS_FILE.parent / org / "slack.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    return c if c.get("via") == "mcp" else None


SLACK_MCP_SEARCH = "mcp__claude_ai_Slack__slack_search_public_and_private"
SLACK_MCP_SEND = "mcp__claude_ai_Slack__slack_send_message"
SLACK_MCP_POLL = """Busque no Slack, sem mandar nada, usando só {tool} (use ToolSearch para carregá-la):
1) mensagens diretas e de grupo para mim: channel_types "im,mpim", after "{since}", sort "timestamp", sort_dir "desc", limit 50, include_bots false;
2) menções a mim em canais: use a sintaxe de busca do Slack para mensagens que me mencionam, after "{since}", include_bots false, limit 50.
Responda APENAS com um JSON (sem texto antes ou depois, sem markdown) no formato
{{"me": "<meu user id>", "items": [{{"ts": "...", "channel_id": "...", "channel_type": "im|mpim|channel", "channel_name": "...", "user_id": "...", "user_name": "...", "is_bot": false, "thread_ts": "... ou vazio", "permalink": "...", "text": "..."}}]}}
sem as mensagens enviadas por mim. Se algum campo não vier na ferramenta, deixe vazio."""


def slack_mcp(org, prompt, tools, timeout=300):
    """Um `claude -p` (Haiku) da organização, só com as ferramentas pedidas; devolve o texto final."""
    config = next((d for o, d, _ in orgs() if o == org), None)
    cwd = next((p for o, _, p in orgs() if o == org and Path(p).is_dir()), str(Path.home()))
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence", "--output-format", "json",
                        "--settings", json.dumps({"disableAllHooks": True}), "--allowedTools", "ToolSearch", *tools],
                       input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=claude_env(config))
    try:
        out = json.loads(r.stdout).get("result", "")
    except ValueError:
        raise ValueError((r.stderr or r.stdout).strip()[-200:] or tr("o claude não respondeu"))
    return out


def slack_poll_mcp(org, st):
    """Mesma lista do app próprio, lida pelo conector: o Claude devolve JSON e o quadro filtra (bots e você fora)."""
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
    out = slack_mcp(org, SLACK_MCP_POLL.format(tool=SLACK_MCP_SEARCH, since=since), [SLACK_MCP_SEARCH])
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(tr("o conector do Slack não devolveu a lista"))
    d = json.loads(m.group(0))
    st["me"] = d.get("me") or st["me"]
    items = []
    for i in sorted(d.get("items") or [], key=lambda i: -float(i.get("ts") or 0)):
        kind = i.get("channel_type") or ""
        if not i.get("ts") or not i.get("channel_id") or i.get("is_bot") or (st["me"] and i.get("user_id") == st["me"]):
            continue
        items.append({"org": org, "ts": str(i["ts"]), "channel": i["channel_id"], "dm": kind in ("im", "mpim"),
                      "where": "DM" if kind == "im" else ("grupo" if kind == "mpim" else "#" + (i.get("channel_name") or "").lstrip("#")),
                      "thread_ts": str(i.get("thread_ts") or ""), "from": i.get("user_name") or i.get("user_id") or "",
                      "text": str(i.get("text") or "").replace(f"<@{st['me']}>", tr("@você"))[:1500], "link": i.get("permalink") or ""})
    return items[:50]


def slack_conf(org):
    """~/.config/claude-orgs/<org>/slack.json: {"token": "xoxp-…"} (app do Slack seu, escopos de leitura de DM, busca,
    usuários e chat:write para responder em seu nome)."""
    try:
        c = json.loads((ORGS_FILE.parent / org / "slack.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    return c if str(c.get("token", "")).startswith("xoxp-") else None


def slack_call(method, token, params=None, post=None):
    url = f"{SLACK_API}/{method}" + (f"?{urlencode(params)}" if params else "")
    req = urllib.request.Request(url, data=json.dumps(post).encode() if post is not None else None, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if not d.get("ok"):
        raise ValueError(d.get("error") or tr("erro do Slack"))
    return d


def slack_thread(m):
    return (parse_qs(urlparse(m.get("permalink", "")).query).get("thread_ts") or [""])[0]


def slack_conv_key(channel, dm, thread_ts, ts):
    """Conversa de uma mensagem: a thread, se houver; senão a DM inteira; menção solta em canal = a thread que a
    resposta abre nela."""
    return f"{channel}|{thread_ts}" if thread_ts else (channel if dm else f"{channel}|{ts}")


def slack_poll(org):
    """Busca as mensagens dos últimos 2 dias e fica com DMs/grupos de DM e menções a você (fora as suas). Devolve as
    novas desde a última busca (na 1ª, nenhuma: não avisa o acumulado).
    Duas buscas direcionadas por rodada (`is:dm` e `<@você>`): uma busca geral por data trazia só ruído de canal (994
    mensagens em 2 dias num workspace real, nenhuma DM entre as 100 mais novas). Limite do Slack: 20 buscas/min."""
    c, mcp = slack_conf(org), slack_via_mcp(org)
    if not (c or mcp):
        _slack.pop(org, None)
        return []
    st = _slack.setdefault(org, {"me": "", "items": [], "error": None, "notified": None, "users": {}, "last": 0.0})
    if mcp:  # pelo conector: no ritmo configurado (cada consulta custa ~30 s e limite do Claude)
        if time.time() - st["last"] < int(mcp.get("every") or 600):
            return []
        st["last"] = time.time()
        try:
            st["items"], st["error"] = slack_poll_mcp(org, st), None
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)[:200]
            return []
        return slack_fresh(st)
    try:
        if not st["me"]:
            st["me"] = slack_call("auth.test", c["token"])["user_id"]
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
        matches, seen = [], set()
        # DMs, menções a você e as SUAS mensagens (para saber em que conversa você respondeu por último)
        for q in (f"is:dm after:{since}", f"<@{st['me']}> after:{since}", f"from:<@{st['me']}> after:{since}"):
            d = slack_call("search.messages", c["token"], {"query": q, "sort": "timestamp", "sort_dir": "desc", "count": 100})
            for m in (d.get("messages") or {}).get("matches") or []:
                k = ((m.get("channel") or {}).get("id"), m.get("ts"))
                if k not in seen:
                    seen.add(k)
                    matches.append(m)
        items, mine = [], {}  # mine: conversa -> sua mensagem mais nova nela
        for m in sorted(matches, key=lambda m: -float(m.get("ts") or 0)):
            ch, text = m.get("channel") or {}, m.get("text") or ""
            dm = bool(ch.get("is_im") or ch.get("is_mpim"))
            if m.get("user") == st["me"]:
                k = slack_conv_key(ch.get("id", ""), dm, slack_thread(m), m["ts"])
                mine[k] = max(mine.get(k, 0.0), float(m["ts"]))
                continue
            if not (dm or f"<@{st['me']}>" in text):
                continue
            # Apps (Linear, Linear Asks…) só se distinguem pelo cadastro do remetente: is_bot no users.info (1 vez por usuário)
            u = m.get("user") or ""
            if u and u not in st["users"]:
                info = slack_call("users.info", c["token"], {"user": u}).get("user") or {}
                st["users"][u] = (bool(info.get("is_bot")), info.get("real_name") or info.get("name") or u)
            if not u or st["users"][u][0] or m.get("bot_id") or m.get("subtype") == "bot_message":
                continue
            items.append({"org": org, "ts": m["ts"], "channel": ch.get("id", ""), "dm": dm,
                          "where": "DM" if ch.get("is_im") else ("grupo" if ch.get("is_mpim") else "#" + ch.get("name", "")),
                          "thread_ts": slack_thread(m),
                          "from": st["users"][u][1], "text": text.replace(f"<@{st['me']}>", tr("@você"))[:1500],
                          "link": m.get("permalink", "")})
        # ✓ respondido: a última mensagem da conversa (DM ou thread) é sua
        last = {}
        for i in items:
            k = slack_conv_key(i["channel"], i["dm"], i["thread_ts"], i["ts"])
            last[k] = max(last.get(k, 0.0), float(i["ts"]))
        for i in items:
            k = slack_conv_key(i["channel"], i["dm"], i["thread_ts"], i["ts"])
            i["replied"] = mine.get(k, 0.0) > last[k]
        st["items"], st["error"] = items[:50], None
    except Exception as e:  # noqa: BLE001  (token revogado, sem rede: mostra na tela e tenta de novo depois)
        st["error"] = str(e)[:200]
        return []
    return slack_fresh(st)


def slack_fresh(st):
    """As mensagens novas desde a última rodada (na 1ª, nenhuma: não avisa o acumulado)."""
    newest = max((float(i["ts"]) for i in st["items"]), default=0.0)
    fresh = [] if st["notified"] is None else [i for i in st["items"] if float(i["ts"]) > st["notified"]]
    st["notified"] = max(newest, st["notified"] or 0.0)
    return fresh


def slack_seen():
    try:
        return json.loads(SLACK_SEEN.read_text())
    except (OSError, ValueError):
        return {}


def slack_hidden():
    try:
        return json.loads(SLACK_HIDDEN.read_text())
    except (OSError, ValueError):
        return {}


def slack_state():
    """Por organização com Slack: mensagens (mais novas primeiro, sem as que você tirou da lista), quantas você ainda
    não viu e erro."""
    seen, hidden = slack_seen(), slack_hidden()
    out = {}
    for org, st in _slack.items():
        gone = set(hidden.get(org) or [])
        items = [i for i in st["items"] if f"{i['channel']}|{i['ts']}" not in gone]
        out[org] = {"items": items, "error": st["error"],  # já respondida não conta como não vista
                    "unread": sum(float(i["ts"]) > float(seen.get(org, 0)) and not i.get("replied") for i in items)}
    return out


def slack_item(org, channel, ts):
    return next((i for i in (_slack.get(org) or {}).get("items", []) if i["channel"] == channel and i["ts"] == ts), None)


def slack_hide(body):
    """Tira a mensagem da lista (fica guardado; uma mensagem nova da mesma pessoa aparece normalmente)."""
    org, channel, ts = body.get("org"), body.get("channel"), body.get("ts")
    if not slack_item(org, channel, ts):
        raise ValueError(tr("Mensagem não encontrada na lista do Slack"))
    hidden = slack_hidden()
    hidden[org] = ((hidden.get(org) or []) + [f"{channel}|{ts}"])[-500:]
    SLACK_HIDDEN.write_text(json.dumps(hidden))
    return {"ok": True}


def slack_history(q):
    """Conversa recente com o remetente de uma mensagem da lista: a DM (ou grupo) inteira recente, ou a thread. Menção
    em canal fora de thread: as mensagens do canal até ela + as respostas na thread dela (escopos channels:history e
    groups:history; sem eles, só a menção, com aviso)."""
    org, channel, ts = q.get("org"), q.get("channel"), q.get("ts")
    item, c = slack_item(org, channel, ts), slack_conf(org)
    if not item:
        raise ValueError(tr("Mensagem não encontrada na lista do Slack"))
    if not c:
        raise ValueError(tr("A conversa completa só existe com o app do Slack (não pelo conector)"))
    st = _slack[org]
    if item["thread_ts"]:
        msgs = slack_call("conversations.replies", c["token"], {"channel": channel, "ts": item["thread_ts"], "limit": 50})["messages"]
        where = "thread"
    elif item["dm"]:
        msgs = slack_call("conversations.history", c["token"], {"channel": channel, "limit": 30})["messages"]
        where = "conversa"
    else:
        try:
            msgs = slack_call("conversations.history", c["token"], {"channel": channel, "latest": ts, "inclusive": "true", "limit": 12})["messages"]
            if any(m.get("reply_count") for m in msgs if m.get("ts") == ts):
                msgs += slack_call("conversations.replies", c["token"], {"channel": channel, "ts": ts, "limit": 50})["messages"][1:]
        except ValueError as e:
            if "missing_scope" not in str(e):
                raise
            return {"where": "menção", "messages": [{"from": item["from"], "me": False, "ts": item["ts"], "text": item["text"]}],
                    "note": tr("Falta a permissão de ler canais no app do Slack (channels:history e groups:history).")}
        where = "canal"
    out = []
    for m in sorted(msgs, key=lambda m: float(m["ts"])):
        u = m.get("user") or ""
        if u and u not in st["users"]:
            info = slack_call("users.info", c["token"], {"user": u}).get("user") or {}
            st["users"][u] = (bool(info.get("is_bot")), info.get("real_name") or info.get("name") or u)
        out.append({"from": tr("Você") if u == st["me"] else (st["users"].get(u) or (False, m.get("username") or "?"))[1],
                    "me": u == st["me"], "ts": m["ts"], "text": (m.get("text") or "").replace(f"<@{st['me']}>", tr("@você"))[:4000]})
    return {"where": where, "messages": out}


def slack_read(body):
    """Marca como vistas as mensagens das organizações pedidas (as que estão na tela)."""
    seen = slack_seen()
    for org in body.get("orgs") or []:
        if org in _slack and _slack[org]["items"]:
            seen[org] = max(float(i["ts"]) for i in _slack[org]["items"])
    SLACK_SEEN.write_text(json.dumps(seen))
    return {"ok": True}


def slack_reply(body):
    """Responde em seu nome, na conversa (ou thread) de uma mensagem que está na lista: a rota não manda para qualquer
    canal."""
    org, channel, text = body.get("org"), body.get("channel"), str(body.get("text") or "").strip()
    item = slack_item(org, channel, body.get("ts"))
    if not item:
        raise ValueError(tr("Mensagem não encontrada na lista do Slack"))
    if not text:
        raise ValueError(tr("Escreva a resposta"))
    thread = item["thread_ts"] or ("" if item["dm"] else item["ts"])  # canal: responde na thread da menção
    if slack_via_mcp(org):
        out = slack_mcp(org, f"""Use {SLACK_MCP_SEND} (carregue com ToolSearch) UMA vez com channel_id "{channel}"
{f'e thread_ts "{thread}"' if thread else "sem thread_ts"} e message EXATAMENTE igual ao texto entre <msg> e </msg>, sem mudar
nem acrescentar nada (nem assinatura):
<msg>{text[:4000]}</msg>
Depois responda só OK, ou ERRO: e o motivo.""", [SLACK_MCP_SEND], timeout=180)
        if not out.strip().upper().startswith("OK"):
            raise ValueError(tr("Slack (conector): {error}", error=out.strip()[:200] or tr("sem resposta")))
        return {"ok": True}
    slack_call("chat.postMessage", slack_conf(org)["token"], post={"channel": channel, "text": text[:4000],
                                                                  **({"thread_ts": thread} if thread else {})})
    return {"ok": True}


def slack_push(org_name, i):
    if load_subs():
        threading.Thread(target=push_all, daemon=True, args=({
            "title": f"{org_name} · Slack · {i['from']}", "body": f"{tr(i['where'])}: {i['text'][:160]}",
            "tag": f"slack-{i['ts']}", "url": "/?slack=1"},)).start()


def slack_poll_bg(org, org_name):
    st = _slack.setdefault(org, {"me": "", "items": [], "error": None, "notified": None, "users": {}, "last": 0.0})
    st["busy"] = True
    try:
        for i in slack_poll(org):
            slack_push(org_name, i)
    finally:
        st["busy"] = False


def watch_slack(every=45):
    """Busca DMs e menções de cada organização com Slack (fora do modo economia) e avisa as novas no celular."""
    while True:
        try:
            if not load_settings().get("eco"):
                names = {o["id"]: o["name"] for o in org_list()}
                for org in names:
                    if slack_via_mcp(org):
                        if not (_slack.get(org) or {}).get("busy"):
                            threading.Thread(target=slack_poll_bg, args=(org, names[org]), daemon=True).start()
                        continue
                    for i in slack_poll(org):
                        if load_subs():
                            threading.Thread(target=push_all, daemon=True, args=({
                                "title": f"{names[org]} · Slack · {i['from']}", "body": f"{tr(i['where'])}: {i['text'][:160]}",
                                "tag": f"slack-{i['ts']}", "url": "/?slack=1"},)).start()
        except Exception as e:  # noqa: BLE001  (a vigia não pode morrer)
            print(f"vigia do Slack: {e}", flush=True)
        time.sleep(every)


# ---------- configurações ----------

# ---------- idioma (Configurações → Idioma): o texto-fonte é pt-BR; i18n_en.json = {"texto pt": "texto en"} ----------
I18N_FILE = HERE / "i18n_en.json"
_i18n = (0.0, {})


def i18n_en():
    global _i18n
    try:
        m = I18N_FILE.stat().st_mtime
        if m != _i18n[0]:
            _i18n = (m, json.loads(I18N_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return _i18n[1]


def tr(msg, **kw):
    """Mensagem para a tela no idioma das Configurações; `kw` preenche os {campos} depois de traduzir."""
    if load_settings().get("lang") == "en-US":
        msg = i18n_en().get(msg, msg)
    return msg.format(**kw) if kw else msg


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def settings_update(body):
    s = load_settings()
    if "remind_minutes" in body:
        m = round(float(body["remind_minutes"]) * 2) / 2  # de meio em meio minuto
        if not 0.5 <= m <= 240:
            raise ValueError(tr("O lembrete precisa ficar entre 30 segundos (0,5) e 240 minutos"))
        s["remind_minutes"] = m
    if "hibernate_hours" in body:
        h = round(float(body["hibernate_hours"]) * 2) / 2
        if not 0 <= h <= 72:
            raise ValueError(tr("A hibernação vai de 0 (desligada) a 72 horas"))
        s["hibernate_hours"] = h
    if "lang" in body:
        if body["lang"] not in LANGS:
            raise ValueError(tr("Idioma desconhecido"))
        s["lang"] = body["lang"]
    if "active_org" in body:
        if body["active_org"] not in {o["id"] for o in org_list()}:
            raise ValueError(tr("Organização desconhecida"))
        if body["active_org"] != s.get("active_org"):
            BRAIN.stop()  # o Jarvis sobe de novo com a conta da organização nova na próxima fala
        s["active_org"] = body["active_org"]
    SETTINGS.write_text(json.dumps(s, indent=1))
    return s


# ---------- preferências por card ----------

LANES = {"todo", "claude", "waiting", "review", "done"}


def load_board():
    try:
        return json.loads(BOARD.read_text())
    except (OSError, ValueError):
        return {}


def card_update(body):
    """Raia escolhida à mão (vale até a raia automática do card mudar) e lembrete de 5 min."""
    cid, board = str(body.get("id", "")), load_board()
    if not cid:
        raise ValueError(tr("Card sem id"))
    card = board.setdefault(cid, {})
    if "lane" in body:
        if body["lane"] not in LANES or body.get("auto") not in LANES:
            raise ValueError(tr("Raia inválida"))
        card["lane"], card["auto"], card["archived"] = body["lane"], body["auto"], False
        if cid.startswith("T-"):  # tarefa livre: soltar em Concluído conclui de verdade
            task_update({"id": cid, "state": "completed" if body["lane"] == "done" else "unstarted"})
    if "remind" in body:
        card["remind"] = bool(body["remind"])
    if "archived" in body:
        card["archived"] = bool(body["archived"])  # fica fora das raias até você arrastar de volta
    if "org" in body:  # organização trocada à mão; vazio = volta à de origem (a do Linear/Jira ou da tarefa)
        if body["org"] and body["org"] not in {o["id"] for o in org_list()}:
            raise ValueError(tr("Organização desconhecida"))
        if body["org"]:
            card["org"] = body["org"]
        else:
            card.pop("org", None)
    BOARD.write_text(json.dumps(board, indent=1))
    return {"ok": True}


def send(body):
    target = window_of(body["id"])
    if body.get("bang"):
        # "! comando" no prompt da sessão, SEM Enter: o "!" digitado põe o Claude no modo bash e o comando entra por
        # bracketed paste (as quebras de linha não viram Enter). Você confere e confirma.
        tmux("send-keys", "-t", target, "-l", "!")
        time.sleep(0.15)
        tmux("set-buffer", "-b", "kanban-bang", str(body["bang"])[:20000])
        tmux("paste-buffer", "-p", "-d", "-b", "kanban-bang", "-t", target)
    elif body.get("text"):
        tmux("send-keys", "-t", target, "-l", body["text"])
        time.sleep(0.15)  # o TUI do claude engole o Enter se vier colado no texto
        tmux("send-keys", "-t", target, "Enter")
    elif body.get("key") in ALLOWED_KEYS:
        tmux("send-keys", "-t", target, body["key"])
    else:
        raise ValueError(tr("Nada para enviar"))
    return {"ok": True}


def safe_name(name):
    """Nome de arquivo sem pasta nem espaço: o caminho vai colado no prompt do Claude."""
    return re.sub(r"[^\w.-]+", "_", Path(name).name).strip("._") or "arquivo"


def upload(sid, name, data):
    """Salva o arquivo e cola o caminho no prompt da sessão (sem Enter), como arrastar no terminal.
    Colar com -p usa bracketed paste: o Claude reconhece caminho de imagem e anexa como [Image #N]."""
    target = window_of(sid)  # só sessões do quadro; também barra sid com ../
    folder = UPLOADS / sid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}-{safe_name(name)}"
    path.write_bytes(data)
    tmux("set-buffer", "-b", "kanban-upload", f"{path} ")
    tmux("paste-buffer", "-p", "-d", "-b", "kanban-upload", "-t", target)
    return {"path": str(path)}


def screen(sid):
    return tmux("capture-pane", "-p", "-t", window_of(sid), "-S", "-60").stdout.rstrip()


# ---------- terminal embutido (pty com tmux attach, via SSE + POST) ----------

_terms = {}  # tid -> {"pid", "fd", "view"}


def set_winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 5), max(cols, 20), 0, 0))


def term_open(sid, cols, rows):
    """Anexa um cliente tmux à janela da sessão. Cada terminal usa uma sessão agrupada
    própria, senão dois terminais abertos no quadro brigariam pela janela atual."""
    window = window_of(sid).split(":", 1)[1]
    ensure_tmux()  # garante a passagem de hyperlinks antes do cliente anexar
    tid = uuid.uuid4().hex[:8]
    view = f"{TMUX}-view-{tid}"
    pid, fd = pty.fork()
    if pid == 0:  # filho
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}  # permite rodar o servidor de dentro do tmux
        env["TERM"] = "xterm-256color"
        os.execvpe("tmux", ["tmux", "new-session", "-t", TMUX, "-s", view, ";",
                            "set-option", "destroy-unattached", "on", ";",
                            "set-option", "status", "off", ";",
                            "select-window", "-t", f"{view}:{window}"], env)
    set_winsize(fd, cols, rows)
    _terms[tid] = {"pid": pid, "fd": fd, "view": view}
    return tid


def term_close(tid):
    t = _terms.pop(tid, None)
    if not t:
        return
    for fn in (lambda: os.kill(t["pid"], signal.SIGTERM), lambda: os.waitpid(t["pid"], 0), lambda: os.close(t["fd"])):
        try:
            fn()
        except OSError:
            pass
    tmux("kill-session", "-t", t["view"], check=False)  # a sessão agrupada some; as janelas ficam


# WebSocket mínimo (RFC 6455): um por terminal. SSE + POST travava com vários terminais abertos,
# porque o navegador só abre 6 conexões HTTP por host e cada SSE prendia uma.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MOUSE_MODE = re.compile(rb"\x1b\[\?([\d;]+)h")
MOUSE_TRACKING = {b"1000", b"1002", b"1003"}


def strip_mouse_tracking(data):
    """Tira do fluxo o pedido de rastrear mouse que o Claude faz: com ele, arrastar no terminal do
    navegador vira clique para o Claude e não seleciona texto. A roda do mouse é repassada pela página."""
    def keep(m):
        modes = [x for x in m.group(1).split(b";") if x not in MOUSE_TRACKING]
        return b"\x1b[?" + b";".join(modes) + b"h" if modes else b""
    return MOUSE_MODE.sub(keep, data)


def ws_send(sock, payload, opcode=2):
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else
                                     bytes([126]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([127]) + struct.pack(">Q", n))
    sock.sendall(head + payload)


def ws_frames(buf):
    """Frames completos de `buf` -> ([(opcode, payload)], resto).

    ponytail: ignora fragmentação (FIN=0); o navegador manda mensagens pequenas inteiras.
    """
    out = []
    while len(buf) >= 2:
        op, masked, n, i = buf[0] & 0x0F, buf[1] & 0x80, buf[1] & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                break
            n, i = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                break
            n, i = struct.unpack(">Q", buf[2:10])[0], 10
        mask = buf[i:i + 4] if masked else b""
        i += 4 if masked else 0
        if len(buf) < i + n:
            break
        data = buf[i:i + n]
        if masked:
            data = bytes(b ^ mask[k % 4] for k, b in enumerate(data))
        out.append((op, data))
        buf = buf[i + n:]
    return out, buf


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        # Contra DNS rebinding: este servidor digita em terminais, só aceita localhost.
        return self.headers.get("Host", "").split(":")[0] in {"localhost", "127.0.0.1"} | EXTRA_HOSTS

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # navegador recarregou/fechou a aba no meio do polling

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "host"})
        if self.path in ("/", "/index.html", "/recursos"):
            data = (HERE / ("recursos.html" if self.path == "/recursos" else "index.html")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/i18n.js":  # o mesmo dicionário do servidor + as funções de tradução da tela
            data = ("window.I18N_EN = " + json.dumps(i18n_en(), ensure_ascii=False) + ";\n").encode() + (HERE / "i18n.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path in PWA:
            path, ctype = PWA[self.path]
            data = (HERE / path).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")  # o navegador confere o service worker novo a cada abertura
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/api/push/key":
            try:
                import push
            except ImportError:  # notificações precisam do pacote cryptography (python3-cryptography)
                return self._json(501, {"error": tr("Notificações precisam do pacote Python cryptography")})
            return self._json(200, {"key": push.public_key(), "devices": len(load_subs())})
        if self.path in VENDOR:
            data = (HERE / self.path.lstrip("/")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", VENDOR[self.path])
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/api/state":
            issues, err = tracker_issues()
            tasks = [task_as_issue(t) for t in load_tasks()]
            issues, board = issues or [], load_board()
            # Organização trocada à mão no card (board.json "org") vale para o quadro inteiro: filtro, sessão, Jarvis
            cards_out = [{**i, "org": board.get(i["identifier"], {}).get("org") or i.get("org"), "source_org": i.get("org")}
                         for i in issues + tasks]
            return self._json(200, {"issues": cards_out, "linear_count": len(issues), "orgs": org_list(), "active_org": active_org(), "cards": load_board(), "settings": load_settings(), "tts": tts_ready(), "voice": VOICE,
                                    "linear_error": err, "sessions": sessions(),
                                    "repos": launch_dirs(), "tmux": TMUX,
                                    "repo_info": {p: {"org": org_of(p), "git": (Path(p) / ".git").exists()} for p in launch_dirs()},
                                    "slack": slack_state(), "hibernated": hibernated()})
        if self.path.startswith("/api/slack/history?"):
            try:
                return self._json(200, slack_history({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}))
            except (ValueError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001  (Slack fora do ar, permissão faltando)
                return self._json(502, {"error": f"Slack: {e}"})
        if self.path == "/api/resources":
            import resources
            return self._json(200, resources.snapshot())
        if self.path == "/api/resources/cleanup":
            import resources
            return self._json(200, resources.cleanup(None))
        if self.path == "/api/eco/plan":
            return self._json(200, eco_plan())
        if self.path.startswith("/api/blocks?"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                return self._json(200, code_blocks(q.get("id", "")))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if self.path.startswith("/api/term?"):
            return self._term_ws()
        self._json(404, {"error": "not found"})

    def _term_ws(self):
        # WebSocket não passa por CORS: sem checar a Origin, qualquer site aberto no navegador digitaria nos terminais
        allowed = {"localhost", "127.0.0.1"} | EXTRA_HOSTS
        if urlparse(self.headers.get("Origin", "")).hostname not in allowed or \
                self.headers.get("Upgrade", "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            return self._json(403, {"error": "forbidden"})
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            tid = term_open(q.get("id", ""), int(q.get("cols", 100)), int(q.get("rows", 30)))
        except (ValueError, subprocess.CalledProcessError) as e:
            return self._json(400, {"error": str(e)})
        accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock, fd, buf = self.connection, _terms[tid]["fd"], b""
        try:
            while True:
                ready, _, _ = select.select([fd, sock], [], [], 30)
                if not ready:
                    ws_send(sock, b"", 9)  # ping: descobre aba fechada mesmo sem saída
                    continue
                if fd in ready:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        ws_send(sock, b"", 8)  # o tmux saiu (janela encerrada)
                        return
                    ws_send(sock, strip_mouse_tracking(data))
                if sock in ready:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    frames, buf = ws_frames(buf + chunk)
                    for op, payload in frames:
                        if op == 8:
                            ws_send(sock, b"", 8)
                            return
                        if op == 1:
                            msg = json.loads(payload)
                            if msg.get("t") == "in":
                                os.write(fd, msg["d"].encode())
                            elif msg.get("t") == "resize":
                                set_winsize(fd, int(msg["c"]), int(msg["r"]))
        except (OSError, ValueError, KeyError):
            pass
        finally:
            term_close(tid)

    def _upload(self):
        # application/octet-stream também força o preflight CORS, como o JSON das outras rotas
        size = int(self.headers.get("Content-Length", 0))
        if self.headers.get("Content-Type") != "application/octet-stream":
            return self._json(403, {"error": "forbidden"})
        if size > MAX_UPLOAD:
            return self._json(413, {"error": tr("Arquivo maior que {n} MB", n=MAX_UPLOAD // 2**20)})
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            self._json(200, upload(q.get("id", ""), q.get("name", ""), self.rfile.read(size)))
        except subprocess.CalledProcessError as e:
            self._json(400, {"error": (e.stderr or str(e)).strip()})
        except (ValueError, OSError) as e:
            self._json(400, {"error": str(e)})

    def _voice(self):
        if self.headers.get("Content-Type") != "application/json":
            return self._json(403, {"error": "forbidden"})
        if load_settings().get("eco"):
            return self._json(409, {"error": "Modo economia ligado: o Jarvis está desligado"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self._json(400, {"error": tr("corpo inválido")})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        try:
            voice_stream(body, emit)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                emit({"error": str(e)[:300]})
            except OSError:
                pass

    def _tts(self):
        if self.headers.get("Content-Type") != "application/json":
            return self._json(403, {"error": "forbidden"})
        try:
            up = local_tts_stream(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))).get("text"))
        except ValueError as e:
            return self._json(502, {"error": str(e)})
        with up:  # repassa os pedaços conforme chegam, sem esperar a frase inteira
            self.send_response(200)
            self.send_header("Content-Type", up.headers.get("Content-Type", "audio/L16; rate=24000"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while chunk := up.read1(8192):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # o navegador cortou (você falou por cima)

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        if self.path.startswith("/api/upload?"):
            return self._upload()
        if self.path in ("/api/tts", "/api/voice") and not VOICE:
            return self._json(404, {"error": "voice disabled (KANBAN_VOICE=1)"})
        if self.path == "/api/tts":
            return self._tts()
        if self.path == "/api/voice":
            return self._voice()
        # Content-Type JSON força preflight CORS (que não respondemos): outro site não consegue postar aqui.
        if self.headers.get("Content-Type") != "application/json":
            return self._json(403, {"error": "forbidden"})
        actions = {"/api/launch": launch, "/api/resume": resume, "/api/send": send, "/api/link": link, "/api/kill": kill, "/api/card": card_update, "/api/settings": settings_update, "/api/summary": summary,
                   "/api/push/subscribe": push_subscribe, "/api/push/unsubscribe": push_unsubscribe, "/api/push/test": push_test,
                   "/api/task/create": task_create, "/api/task/update": task_update, "/api/org/create": org_create,
                   "/api/setup/info": setup_info, "/api/setup/scan": setup_scan, "/api/setup/test": setup_test,
                   "/api/org/login": org_login, "/api/org/gh-login": org_gh_login, "/api/org/bitbucket": org_bitbucket,
                   "/api/eco": eco, "/api/slack/read": slack_read, "/api/slack/reply": slack_reply,
                   "/api/slack/hide": slack_hide, "/api/terminal": open_terminal, "/api/resources/kill": res_action("kill"),
                   "/api/resources/docker": res_action("docker_action"), "/api/resources/dropcache": res_action("drop_cache"),
                   "/api/resources/cleanup": res_action("cleanup")}
        if self.path not in actions:
            return self._json(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._json(200, actions[self.path](body))
        except subprocess.CalledProcessError as e:
            self._json(400, {"error": (e.stderr or str(e)).strip()})
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001  (sem isto o navegador só vê "Failed to fetch")
            import traceback
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    print(f"Quadro em http://localhost:{PORT}  (sessões no tmux '{TMUX}': tmux attach -t {TMUX})")
    threading.Thread(target=watch_sessions, daemon=True).start()
    import resources  # página /recursos: coleta em segundo plano (mais espaçada no modo economia)
    resources.start(sessions=lambda: {s["id"]: s for s in sessions()}, leftovers=lambda: [x["pid"] for x in leftovers()],
                    eco=lambda: bool(load_settings().get("eco")), can_drop_cache=can_drop_cache, docker=DOCKER,
                    drop_cache=lambda: subprocess.run(DROP_CACHE, capture_output=True).returncode == 0
                    and subprocess.run(COMPACT, capture_output=True).returncode >= 0, board_pid=os.getpid(),
                    repos=repos, live_cwds=lambda: [x["cwd"] for x in sessions() if x["window"] and x["cwd"]],
                    done_ids=done_card_ids, tr=tr)
    threading.Thread(target=watch_hibernate, daemon=True).start()
    threading.Thread(target=watch_slack, args=(int(os.environ.get("KANBAN_SLACK_EVERY", "45")),), daemon=True).start()
    if VOICE and not load_settings().get("eco"):
        threading.Thread(target=BRAIN.warm, daemon=True).start()  # o Jarvis já sobe aquecido (fora do modo economia)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
