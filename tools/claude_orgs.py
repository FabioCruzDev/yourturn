#!/usr/bin/env python3
"""Separa o ~/.claude em uma base genérica e uma pasta por organização (CLAUDE_CONFIG_DIR próprio).

  python3 claude_orgs.py              prévia: mostra o que vai para onde, não mexe em nada
  python3 claude_orgs.py apply        backup completo + corte (recusa com alguma sessão do Claude aberta)
  python3 claude_orgs.py verify       confere links, logins e se a base ficou limpa
  python3 claude_orgs.py sync         refaz links e settings das organizações a partir da base
  python3 claude_orgs.py undo <tgz>   volta ao estado do backup (o atual vai para ~/claude-orgs-undo-<data>/)
  python3 claude_orgs.py add <org> <pasta>...   cria uma organização (o quadro faz o mesmo pelo formulário)
  python3 claude_orgs.py env <org> K=V [K=]     define (ou apaga, com K=) variáveis que as sessões da organização recebem

Depois do corte:
  ~/.claude            base genérica de todas as organizações, sem login
  ~/.claude-<org>      tudo da organização (login, skills dela, histórico, memórias, MCP); o genérico entra por
                       link simbólico para a base, o CLAUDE.md importa o da base e o settings.json é a base mesclada
  ~/.config/claude-orgs/orgs.tsv   pasta -> organização (lido pelo `claude` do shell e pelo quadro)
  ~/.config/claude-orgs/<org>/     org.json (nome, cor, org do GitHub), linear_key, jira.json, bitbucket.json, slack.json, gh/ (GH_CONFIG_DIR), gitconfig
                                   (e-mail dos commits, via includeIf "gitdir:<pasta>/" no ~/.gitconfig)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HOME = Path.home()
# Mensagens para a tela: o quadro troca por tradução (server.tr) ao importar; na linha de comando, pt-BR.
tr = lambda msg, **kw: msg.format(**kw) if kw else msg  # noqa: E731
BASE = HOME / ".claude"
BASE_JSON = HOME / ".claude.json"
ORG_DATA = HOME / ".config" / "claude-orgs"
ORGS_FILE = ORG_DATA / "orgs.tsv"
BASHRC = HOME / ".bashrc"
MARK_BEGIN, MARK_END = "# >>> claude-orgs >>>", "# <<< claude-orgs <<<"

# Só para o `apply` (corte inicial), antes de existir o orgs.tsv: tudo o que não for genérico vai para a organização
# padrão (CLAUDE_ORGS_DEFAULT, "main" se vazio), com projetos em CLAUDE_ORGS_PATHS (vírgulas; padrão ~/Projects).
# Depois do corte, a lista vem do orgs.tsv e as organizações novas entram pelo quadro ou pelo `add`.
DEFAULT_ORG = os.environ.get("CLAUDE_ORGS_DEFAULT") or "main"
SEED = {DEFAULT_ORG: {"dir": HOME / f".claude-{DEFAULT_ORG}",
                      "paths": [x.strip() for x in os.environ.get("CLAUDE_ORGS_PATHS", "~/Projects").split(",") if x.strip()]}}

def load_orgs():
    try:
        lines = ORGS_FILE.read_text().splitlines()
    except OSError:
        return {}  # sem orgs.tsv: nenhuma organização (o `apply` parte do SEED, ver seed())
    out = {}
    for line in lines:
        f = line.split("\t")
        if len(f) == 3 and f[0] and not f[0].startswith("#"):
            out.setdefault(f[0], {"dir": Path(f[1]).expanduser(), "paths": []})["paths"].append(f[2])
    return out


ORGS = load_orgs()
# Genérico: fica na base e entra em cada organização por link (o resto de skills/ vai para a organização padrão).
# Skills genéricas compartilhadas: uma por linha em ~/.config/claude-orgs/shared_skills.
try:
    SHARED_SKILLS = [x.strip() for x in (ORG_DATA / "shared_skills").read_text().splitlines() if x.strip() and not x.startswith("#")]
except OSError:
    SHARED_SKILLS = []
LINKED = ["commands", "hooks", "plugins"] + [f"skills/{x}" for x in SHARED_SKILLS]
# Genérico que fica só na base (cada organização tem o seu, gerado a partir deste)
BASE_ONLY = ["CLAUDE.md", "settings.json", "settings.local.json", ".claude"]
# Chaves do settings.json que são da organização e saem da base
ORG_SETTINGS = {DEFAULT_ORG: ["permissions", "autoMode"]}
# Variáveis que a organização passa às sessões dela (settings.json "env")
ORG_ENV = {}


def h(p):
    return str(p).replace(str(HOME), "~", 1)


def size(p):
    if p.is_symlink() or p.is_file():
        return p.lstat().st_size
    return sum(f.lstat().st_size for f in p.rglob("*") if not f.is_symlink() and f.is_file())


def human(n):
    for u in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def encode(path):
    """Nome da pasta de projeto que o Claude Code usa para um diretório (/ e . viram -)."""
    return str(Path(path).expanduser()).replace("/", "-").replace(".", "-")


def org_of_path(path):
    best, blen = DEFAULT_ORG, 0
    for org, cfg in ORGS.items():
        for p in cfg["paths"]:
            p = str(Path(p).expanduser())
            if (path == p or path.startswith(p + "/")) and len(p) > blen:
                best, blen = org, len(p)
    return best


def org_of_project(name):
    best, blen = DEFAULT_ORG, 0
    for org, cfg in ORGS.items():
        for p in cfg["paths"]:
            e = encode(p)
            if (name == e or name.startswith(e + "-")) and len(e) > blen:
                best, blen = org, len(e)
    return best


def seed():
    """Antes do corte (sem orgs.tsv), o preview/apply partem da organização padrão do SEED."""
    if not ORGS:
        ORGS.update({k: {**v, "paths": list(v["paths"])} for k, v in SEED.items()})


def plan():
    """Lista de (origem, destino) dos movimentos + resumo do resto."""
    moves = []
    generic = set(LINKED) | set(BASE_ONLY)
    for p in sorted(BASE.iterdir()):
        rel = p.name
        if rel in generic:
            continue
        if rel == "projects":
            for proj in sorted(p.iterdir()):
                moves.append((proj, ORGS[org_of_project(proj.name)]["dir"] / "projects" / proj.name))
        elif rel == "history.jsonl":
            continue  # dividido linha a linha pelo campo "project"
        elif rel in ("skills", "scripts"):
            for c in sorted(p.iterdir()):
                if f"{rel}/{c.name}" not in generic:
                    moves.append((c, ORGS[DEFAULT_ORG]["dir"] / rel / c.name))
        else:
            moves.append((p, ORGS[DEFAULT_ORG]["dir"] / rel))
    if BASE_JSON.exists():
        moves.append((BASE_JSON, ORGS[DEFAULT_ORG]["dir"] / ".claude.json"))
    return moves


def history_split():
    out = {org: [] for org in ORGS}
    f = BASE / "history.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                out[org_of_path(json.loads(line).get("project", ""))].append(line)
            except json.JSONDecodeError:
                out[DEFAULT_ORG].append(line)
    return out


def preview():
    seed()
    moves = plan()
    print(f"BASE {h(BASE)} fica com (genérico):")
    for g in BASE_ONLY + LINKED:
        if (BASE / g).exists():
            print(f"  {g:32} {human(size(BASE / g)):>6}" + ("   → link em cada organização" if g in LINKED else ""))
    s = json.loads((BASE / "settings.json").read_text())
    for org, keys in ORG_SETTINGS.items():
        print(f"  settings.json perde {', '.join(k for k in keys if k in s)} (vão só para {org})")
    print("  sem login (.credentials.json e ~/.claude.json vão para a organização)")
    hist = history_split()
    for org, cfg in ORGS.items():
        d = cfg["dir"]
        mine = [(a, b) for a, b in moves if str(b).startswith(str(d) + "/") or b == d]
        print(f"\n{org.upper()} {h(d)}  (pastas: {', '.join(cfg['paths'])})")
        projs = [a for a, b in mine if a.parent.name == "projects"]
        rest = [(a, b) for a, b in mine if a.parent.name != "projects"]
        for a, b in rest:
            extra = f"  (link → {h(os.readlink(a))})" if a.is_symlink() else ""
            print(f"  {h(a):48} {human(size(a)):>6}{extra}")
        if projs:
            print(f"  projects/: {len(projs)} pastas, {human(sum(size(a) for a in projs))}")
            for a in projs:
                print(f"     {a.name[:70]:70} {human(size(a)):>6}")
        print(f"  history.jsonl: {len(hist[org])} linhas")
        print(f"  links → base: {', '.join(LINKED)}")
        print(f"  CLAUDE.md = @~/.claude/CLAUDE.md (+ o que for só da {org})")
        print(f"  settings.json = base + {', '.join(ORG_SETTINGS.get(org, [])) or 'nada próprio'}")
    print(f"\nCaminhos ~/.claude/… dentro do que vai para a {DEFAULT_ORG} (skills, scripts, memórias, permissões) são "
          f"reescritos para {h(ORGS[DEFAULT_ORG]['dir'])}/…")
    for org, env in ORG_ENV.items():
        print(f"settings.json da {org} ganha env: " + ", ".join(f"{k}={h(v)}" for k, v in env.items()))
    print(f"\n{h(ORGS_FILE)} e bloco '{MARK_BEGIN}' no {h(BASHRC)}: `claude` escolhe a organização pela pasta;")
    print("atalhos " + ", ".join(f"claude-{o}" for o in ORGS) + " forçam uma organização")
    running = claude_processes()
    print(f"\nSessões do Claude abertas agora: {len(running)}" + (" — feche todas antes do apply" if running else ""))
    for r in running:
        print(f"  {r}")


def claude_processes():
    r = subprocess.run(["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True)
    return [l.strip()[:110] for l in r.stdout.splitlines() if l.split()[1:2] == ["claude"]]


# ---------- corte ----------

def merge(base, org):
    """Organização mesclada com a base: dicionários por chave, listas sem repetir, valores simples da base vencem."""
    if isinstance(base, dict) and isinstance(org, dict):
        out = dict(org)
        for k, v in base.items():
            out[k] = merge(v, org[k]) if k in org else v
        return out
    if isinstance(base, list) and isinstance(org, list):
        seen, out = set(), []
        for x in org + base:
            key = json.dumps(x, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(x)
        return out
    return base


def write_json(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def link(org_dir, rel):
    dst, src = org_dir / rel, BASE / rel
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        raise ValueError(tr("{p} existe e não é link para a base: resolva à mão antes do sync", p=h(dst)))
    dst.symlink_to(src)


def sync():
    """Links do genérico, CLAUDE.md que importa a base e settings = base mesclada com o que a organização tem."""
    for org, cfg in ORGS.items():
        d = cfg["dir"]
        d.mkdir(mode=0o700, exist_ok=True)
        for rel in LINKED:
            link(d, rel)
        md = d / "CLAUDE.md"
        if not md.exists():
            md.write_text("@~/.claude/CLAUDE.md\n")
        write_bitbucket_md(org)
        for name in ("settings.json", "settings.local.json"):
            b = BASE / name
            if b.exists():
                cur = d / name
                write_json(cur, merge(json.loads(b.read_text()), json.loads(cur.read_text()) if cur.exists() else {}))
        write_git(org)
    write_orgs_file()
    write_bashrc()
    print("sync ok")


BB_BEGIN, BB_END = "<!-- bitbucket: gerado pelo claude_orgs.py -->", "<!-- /bitbucket -->"


def write_bitbucket_md(org):
    """Organização com código no Bitbucket: o CLAUDE.md dela diz como abrir PR (não há `gh` para o Bitbucket)."""
    md = ORGS[org]["dir"] / "CLAUDE.md"
    t = md.read_text() if md.exists() else "@~/.claude/CLAUDE.md\n"
    if BB_BEGIN in t:
        t = t[:t.index(BB_BEGIN)].rstrip("\n") + "\n" + t[t.index(BB_END) + len(BB_END):].lstrip("\n")
    if meta(org).get("git_host") == "bitbucket":
        tool = h(Path(__file__).resolve().parent / "bb_pr.py")
        t = t.rstrip("\n") + f"""

{BB_BEGIN}
## Pull requests (Bitbucket)

O código desta organização fica no Bitbucket: `gh` não serve. Use, de dentro do checkout:
- abrir: `git push -u origin <branch>` e depois `/usr/bin/python3 {tool} create --title "..." --body-file <arquivo>`
  (`--dest <branch>` se não for a principal; `--draft` para rascunho)
- listar: `/usr/bin/python3 {tool} list` · detalhes: `/usr/bin/python3 {tool} view <número>`
- qualquer outra coisa da API 2.0 (como o `gh api`; `{{repo}}` = este repositório): `/usr/bin/python3 {tool} api <caminho>`
  - comentários de review do PR N: `api "/repositories/{{repo}}/pullrequests/N/comments?pagelen=100"`
  - responder: `api /repositories/{{repo}}/pullrequests/N/comments --data '{{"content": {{"raw": "..."}}}}'`
  - CI: `api "/repositories/{{repo}}/pipelines?sort=-created_on&pagelen=10"`, depois `.../pipelines/<uuid>/steps` e
    `.../steps/<step_uuid>/log` (texto)
Título, descrição e comentários sem nenhuma atribuição ao Claude. Comentar no PR de outra pessoa ou rodar pipeline só
com o OK do usuário.
{BB_END}
"""
    md.write_text(t)


def org_env(org):
    try:
        return json.loads((ORGS[org]["dir"] / "settings.json").read_text()).get("env") or {}
    except (OSError, ValueError):
        return {}


GIT_HOSTS = ("github", "bitbucket")


def meta(org):
    try:
        return json.loads((ORG_DATA / org / "org.json").read_text())
    except (OSError, ValueError):
        return {}


def ssh_key(org):
    """Chave SSH da organização (Bitbucket): só os repositórios dela a usam (core.sshCommand via includeIf)."""
    return HOME / ".ssh" / f"id_ed25519_{org}"


def write_git(org):
    """gitconfig da organização, ligado por includeIf "gitdir:<pasta>/" no ~/.gitconfig (vale no terminal, VS Code e
    Claude): e-mail dos commits (se ela tiver) e o `gh` dela como credencial do git push (se ela tiver gh próprio)."""
    f = ORG_DATA / org / "gitconfig"
    email = subprocess.run(["git", "config", "-f", str(f), "user.email"], capture_output=True, text=True).stdout.strip() \
        if f.exists() else ""
    gh = org_env(org).get("GH_CONFIG_DIR", "")
    text = f"[user]\n\temail = {email}\n" if email else ""
    if meta(org).get("git_host") == "bitbucket":
        text += f"[core]\n\tsshCommand = ssh -i {ssh_key(org)} -o IdentitiesOnly=yes\n"
    if gh and Path(gh) != HOME / ".config" / "gh":  # o padrão já é o helper do ~/.gitconfig
        for host in ("https://github.com", "https://gist.github.com"):
            text += f'[credential "{host}"]\n\thelper =\n\thelper = !GH_CONFIG_DIR={gh} /usr/bin/gh auth git-credential\n'
    if not text:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    for p in ORGS[org]["paths"]:
        subprocess.run(["git", "config", "--global", f"includeIf.gitdir:{p}/.path", h(f)], check=True)


def rewrite_paths(root, new_dir, only_projects=False):
    """Troca ~/.claude/… por <organização>/… nos arquivos de texto que foram para a organização."""
    olds = [f"{HOME}/.claude/", "~/.claude/", "$HOME/.claude/", "${HOME}/.claude/"]
    if only_projects:
        olds = [o + "projects/" for o in olds]
    news = {o: o.replace(".claude/", new_dir.name + "/") for o in olds}
    n = 0
    files = [root] if root.is_file() else [Path(d) / n for d, _, ns in os.walk(root) for n in ns]  # não segue links
    for f in files:
        if root.name == "projects" and "memory" not in f.relative_to(root).parts:
            continue  # nos projetos só as memórias; transcrições ficam como foram gravadas
        if f.is_symlink() or not f.is_file() or f.suffix not in (".md", ".py", ".sh", ".json", ".txt", ".env", ""):
            continue
        try:
            t = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        t2 = t
        for o in olds:
            t2 = t2.replace(o, news[o])
        if t2 != t:
            f.write_text(t2)
            n += 1
    return n


def write_orgs_file():
    ORGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# organização<TAB>pasta de config do Claude<TAB>pasta de projetos (a mais específica vence)"]
    lines += [f"{org}\t{h(cfg['dir'])}\t{p}" for org, cfg in ORGS.items() for p in cfg["paths"]]
    ORGS_FILE.write_text("\n".join(lines) + "\n")


BASH_BLOCK = r'''{begin}
# `claude` usa a organização da pasta atual (~/.config/claude-orgs/orgs.tsv); fora delas, a base ~/.claude.
# CLAUDE_CONFIG_DIR definido à mão vence. Gerado por ~/Projects/Pessoal/kanban-claude/tools/claude_orgs.py
_claude_org_dir() {{
  local f=~/.config/claude-orgs/orgs.tsv best="" len=0 org dir path
  [ -r "$f" ] || return 0
  while IFS=$'\t' read -r org dir path; do
    [[ -z "$org" || "$org" == \#* ]] && continue
    path="${{path/#\~/$HOME}}"
    if [[ ( "$PWD" == "$path" || "$PWD" == "$path"/* ) && ${{#path}} -gt $len ]]; then best="${{dir/#\~/$HOME}}"; len=${{#path}}; fi
  done < "$f"
  printf '%s' "$best"
}}
claude() {{
  if [ -n "${{CLAUDE_CONFIG_DIR:-}}" ]; then command claude "$@"; return; fi
  local d; d=$(_claude_org_dir)
  if [ -n "$d" ]; then CLAUDE_CONFIG_DIR="$d" command claude "$@"; else command claude "$@"; fi
}}
# `gh` com a conta da organização da pasta (GH_CONFIG_DIR do settings.json dela); GH_CONFIG_DIR à mão vence
gh() {{
  if [ -n "${{GH_CONFIG_DIR:-}}" ]; then command gh "$@"; return; fi
  local d g=""; d=$(_claude_org_dir)
  [ -n "$d" ] && g=$(sed -n 's/.*"GH_CONFIG_DIR": *"\([^"]*\)".*/\1/p' "$d/settings.json" 2>/dev/null | head -1)
  if [ -n "$g" ]; then GH_CONFIG_DIR="$g" command gh "$@"; else command gh "$@"; fi
}}
{aliases}
{end}'''


def write_bashrc():
    aliases = "\n".join(f'claude-{o}() {{ CLAUDE_CONFIG_DIR={h(c["dir"]).replace("~", "$HOME", 1)} command claude "$@"; }}'
                        for o, c in ORGS.items())
    block = BASH_BLOCK.format(begin=MARK_BEGIN, end=MARK_END, aliases=aliases)
    t = BASHRC.read_text() if BASHRC.exists() else ""
    if MARK_BEGIN in t:
        t = t[:t.index(MARK_BEGIN)] + block + t[t.index(MARK_END) + len(MARK_END):]
    else:
        t = t.rstrip("\n") + "\n\n" + block + "\n"
    BASHRC.write_text(t)


def apply():
    seed()
    running = claude_processes()
    if running:
        raise SystemExit("Há sessões do Claude abertas (gravam em ~/.claude enquanto rodam). Feche todas:\n  "
                         + "\n  ".join(running))
    for cfg in ORGS.values():
        if cfg["dir"].exists():
            raise SystemExit(f"{h(cfg['dir'])} já existe: o corte já foi feito? (use sync, ou undo antes)")
    ts = time.strftime("%Y%m%d-%H%M%S")
    bk = HOME / f"claude-orgs-backup-{ts}.tgz"
    print(f"backup → {h(bk)} …", flush=True)
    with tarfile.open(bk, "w:gz") as tar:
        for p in (BASE, BASE_JSON, BASHRC, ORGS_FILE):
            if p.exists():
                tar.add(p, arcname=str(p.relative_to(HOME)))
    os.chmod(bk, 0o600)
    print(f"backup ok ({human(bk.stat().st_size)})")

    hist = history_split()
    for cfg in ORGS.values():
        cfg["dir"].mkdir(mode=0o700)
    for a, b in plan():
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
    for org, lines in hist.items():
        ORGS[org]["dir"].mkdir(mode=0o700, exist_ok=True)
        (ORGS[org]["dir"] / "history.jsonl").write_text("".join(l + "\n" for l in lines))
    (BASE / "history.jsonl").unlink(missing_ok=True)
    for sub in ("projects", "scripts"):
        if (BASE / sub).is_dir() and not any((BASE / sub).iterdir()):
            (BASE / sub).rmdir()

    # settings: a organização recebe o original inteiro; a base perde as chaves que são da organização
    s = json.loads((BASE / "settings.json").read_text())
    for org in ORG_SETTINGS:
        write_json(ORGS[org]["dir"] / "settings.json", s)
    for org, env in ORG_ENV.items():
        f = ORGS[org]["dir"] / "settings.json"
        d = json.loads(f.read_text()) if f.exists() else {}
        d["env"] = {**d.get("env", {}), **env}
        write_json(f, d)
    for keys in ORG_SETTINGS.values():
        for k in keys:
            s.pop(k, None)
    write_json(BASE / "settings.json", s)

    # caminhos: o que foi para a organização padrão aponta para ela; nas outras, só os de projects/
    d0 = ORGS[DEFAULT_ORG]["dir"]
    n = sum(rewrite_paths(d0 / sub, d0) for sub in ("skills", "scripts", "projects") if (d0 / sub).exists())
    # no settings só as permissões (os hooks apontam para a base e têm de ficar iguais aos dela, senão rodam 2x)
    f = d0 / "settings.json"
    st = json.loads(f.read_text())
    if "permissions" in st:
        st["permissions"] = json.loads(json.dumps(st["permissions"]).replace(f"{HOME}/.claude/", f"{d0}/"))
        write_json(f, st)
    for org, cfg in ORGS.items():
        if org != DEFAULT_ORG and (cfg["dir"] / "projects").exists():
            n += rewrite_paths(cfg["dir"] / "projects", cfg["dir"], only_projects=True)
    print(f"caminhos reescritos em {n} arquivos")
    sync()
    print(f"\nPronto. Abra um terminal novo (ou `source ~/.bashrc`) e rode: python3 {Path(__file__)} verify")
    print(f"Para desfazer: python3 {Path(__file__)} undo {bk}")


def verify():
    ok = True

    def check(cond, msg):
        nonlocal ok
        ok &= bool(cond)
        print(("  ok   " if cond else "  FALHA ") + msg)
    print("base")
    check(not (BASE / ".credentials.json").exists(), "sem login")
    leaks = [p.name for p in (BASE / "skills").iterdir() if f"skills/{p.name}" not in LINKED]
    check(not leaks, f"só skills genéricas ({', '.join(leaks) or 'nenhuma de organização'})")
    check(not (BASE / "projects").exists() or not any((BASE / "projects").iterdir()), "sem histórico de projetos")
    for org, cfg in ORGS.items():
        d = cfg["dir"]
        print(org)
        for rel in LINKED:
            check((d / rel).is_symlink() and (d / rel).exists(), f"link {rel}")
        check((d / "CLAUDE.md").read_text().startswith("@~/.claude/CLAUDE.md"), "CLAUDE.md importa a base")
        r = subprocess.run(["claude", "auth", "status"], env={**os.environ, "CLAUDE_CONFIG_DIR": str(d)},
                           capture_output=True, text=True, timeout=60)
        try:
            st = json.loads(r.stdout)
            print(f"  info  login: {st.get('loggedIn')} {st.get('email') or st.get('emailAddress') or ''}".rstrip())
        except json.JSONDecodeError:
            print(f"  info  auth status: {r.stdout.strip()[:120]}")
        for p in cfg["paths"]:
            p = Path(p).expanduser()
            if p.is_dir():
                out = subprocess.run(["bash", "-ic", 'printf "@@%s@@" "$(_claude_org_dir)"'], cwd=p,
                                     capture_output=True, text=True).stdout  # o .bashrc pode imprimir outras coisas
                got = out.split("@@")[1] if out.count("@@") >= 2 else ""
                check(got == str(d), f"`claude` em {h(p)} usa {h(d)}")
    print("tudo certo" if ok else "há falhas acima")
    return ok


def undo(tgz):
    tgz = Path(tgz).expanduser()
    if not tgz.is_file():
        raise SystemExit(f"backup não encontrado: {tgz}")
    if claude_processes():
        raise SystemExit("Feche as sessões do Claude antes de desfazer.")
    keep = HOME / f"claude-orgs-undo-{time.strftime('%Y%m%d-%H%M%S')}"
    keep.mkdir()
    for p in [BASE, BASE_JSON, ORGS_FILE] + [cfg["dir"] for cfg in ORGS.values()]:
        if p.exists() or p.is_symlink():
            shutil.move(str(p), str(keep / p.name))
    with tarfile.open(tgz) as tar:
        tar.extractall(HOME, filter="tar")
    print(f"restaurado de {h(tgz)}; o estado anterior ficou em {h(keep)} (apague quando conferir)")


# ---------- organização nova ----------

def set_env(org, env):
    """Variáveis que as sessões da organização recebem (settings.json "env" dela); valor vazio apaga."""
    f = ORGS[org]["dir"] / "settings.json"
    d = json.loads(f.read_text()) if f.exists() else {}
    d["env"] = {k: v for k, v in {**d.get("env", {}), **env}.items() if v}
    write_json(f, d)


def check_jira(url, token):
    if (url or token) and not re.fullmatch(r"https://[^\s/]+(/\S*)?", url.strip()):
        raise ValueError(tr("URL do Jira: https://suaempresa.atlassian.net"))
    if url.strip() and not token.strip():
        raise ValueError(tr("Informe o token do Jira"))


def set_tools(org, linear_key="", jira_url="", jira_email="", jira_token="", jira_jql="", git_host="", bitbucket_email="",
              bitbucket_token="", slack_token=""):
    """Linear, Jira e onde fica o código (GitHub/Bitbucket); só o que vier preenchido muda. Devolve o que mudou."""
    check_jira(jira_url, jira_token)
    if git_host and git_host not in GIT_HOSTS:
        raise ValueError(tr("O código fica no GitHub ou no Bitbucket"))
    data = ORG_DATA / org
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    changed = []
    if linear_key.strip():
        (data / "linear_key").write_text(linear_key.strip() + "\n")
        os.chmod(data / "linear_key", 0o600)
        changed.append("Linear")
    if jira_url.strip():
        f = data / "jira.json"
        write_json(f, {k: v.strip() for k, v in {"url": jira_url.rstrip("/"), "email": jira_email, "token": jira_token,
                                                  "jql": jira_jql}.items() if v.strip()})
        os.chmod(f, 0o600)
        changed.append("Jira")
    if bitbucket_token.strip():  # API do Bitbucket (PRs nos cards); a chave SSH só serve para o git
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", bitbucket_email.strip()):
            raise ValueError(tr("Informe o e-mail da conta Atlassian junto com o token do Bitbucket"))
        f = data / "bitbucket.json"
        write_json(f, {"email": bitbucket_email.strip(), "token": bitbucket_token.strip()})
        os.chmod(f, 0o600)
        changed.append("token do Bitbucket")
    if slack_token.strip():  # DMs e menções no quadro; o token de USUÁRIO (xoxp-) responde em seu nome
        if not slack_token.strip().startswith("xoxp-"):
            raise ValueError(tr("Use o User OAuth Token do app do Slack (começa com xoxp-)"))
        f = data / "slack.json"
        write_json(f, {"token": slack_token.strip()})
        os.chmod(f, 0o600)
        changed.append("Slack")
    if git_host and git_host != meta(org).get("git_host", "github"):
        write_json(data / "org.json", {**meta(org), "git_host": git_host})
        changed.append("Bitbucket" if git_host == "bitbucket" else "GitHub")
        if org in ORGS:
            write_git(org)
            write_bitbucket_md(org)
    return changed


def add(org, paths, name="", color="", github_org="", email="", linear_key="", jira_url="", jira_email="", jira_token="",
        jira_jql="", git_host="github", bitbucket_email="", bitbucket_token="", slack_token=""):
    """Cria a organização: pasta de config do Claude (sem login), GitHub e git próprios, Linear e/ou Jira opcionais."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", org or ""):
        raise ValueError(tr("Identificador: só letras minúsculas, números e hífen (ex.: acme)"))
    if org in ORGS:
        raise ValueError(tr("A organização {org} já existe", org=org))
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError(tr("Cor inválida (use #rrggbb)"))
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError(tr("E-mail inválido"))
    check_jira(jira_url, jira_token)
    git_host = git_host or "github"
    if git_host not in GIT_HOSTS:
        raise ValueError(tr("O código fica no GitHub ou no Bitbucket"))
    taken = {str(Path(p).expanduser()) for cfg in ORGS.values() for p in cfg["paths"]}
    clean = []
    for p in paths:
        p = Path(p.strip()).expanduser().resolve()
        if not str(p).startswith(str(HOME) + "/"):
            raise ValueError(tr("{p}: a pasta precisa ficar dentro de {home}", p=p, home=HOME))
        if str(p) in taken:
            raise ValueError(tr("{p} já é de outra organização", p=h(p)))
        p.mkdir(parents=True, exist_ok=True)
        clean.append(h(p))
    if not clean:
        raise ValueError(tr("Informe ao menos uma pasta de projetos"))
    data = ORG_DATA / org
    (data / "gh").mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    os.chmod(data / "gh", 0o700)
    if email:  # o write_git (no sync) completa com a credencial do gh e liga o includeIf das pastas
        (data / "gitconfig").write_text(f"[user]\n\temail = {email}\n")
    write_json(data / "org.json", {k: v for k, v in {"name": name.strip(), "color": color, "github_org": github_org.strip(),
                                                      "git_host": git_host if git_host != "github" else ""}.items() if v})
    set_tools(org, linear_key, jira_url, jira_email, jira_token, jira_jql, bitbucket_email=bitbucket_email,
              bitbucket_token=bitbucket_token, slack_token=slack_token)
    ORGS[org] = {"dir": HOME / f".claude-{org}", "paths": clean}
    ORGS[org]["dir"].mkdir(mode=0o700, exist_ok=True)
    set_env(org, {"GH_CONFIG_DIR": str(data / "gh")})
    sync()
    return ORGS[org]


def main(cmd):
    if cmd == "undo":
        undo(sys.argv[2])
    elif cmd == "add":
        print(add(sys.argv[2], sys.argv[3:]))
    elif cmd == "env":
        set_env(sys.argv[2], dict(a.split("=", 1) for a in sys.argv[3:]))
    elif cmd in ("preview", "apply", "sync", "verify"):
        globals()[cmd]()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "preview")
    except ValueError as e:  # recusa (pasta que não é link, organização repetida…): mensagem, sem traceback
        raise SystemExit(str(e))
