#!/usr/bin/python3
"""Pull requests no Bitbucket Cloud pela API (o `gh` do GitHub não tem equivalente para o Bitbucket).

  bb_pr.py create --title "..." [--body "..." | --body-file arq] [--source branch] [--dest branch] [--draft]
  bb_pr.py list [--state OPEN|MERGED|DECLINED]      PRs do repositório (padrão: abertos)
  bb_pr.py view <número>                            estado, builds, aprovações e link
  bb_pr.py api [-X MÉTODO] <caminho> [--data JSON | --data-file arq]
                                                    qualquer chamada da API 2.0, como o `gh api` ({repo} = este
                                                    repositório; com corpo o método padrão é POST); o que dá para
                                                    fazer é o que os escopos do token permitem
  --org <org> (antes do subcomando): usa a credencial dessa organização em vez da dona da pasta

Exemplos de `api`:
  comentários do PR 12:      bb_pr.py api "/repositories/{repo}/pullrequests/12/comments?pagelen=100"
  responder no PR 12:        bb_pr.py api /repositories/{repo}/pullrequests/12/comments --data '{"content": {"raw": "..."}}'
  pipelines recentes:        bb_pr.py api "/repositories/{repo}/pipelines?sort=-created_on&pagelen=10"
  passos de uma pipeline:    bb_pr.py api "/repositories/{repo}/pipelines/{uuid}/steps"
  log de um passo:           bb_pr.py api "/repositories/{repo}/pipelines/{uuid}/steps/{step_uuid}/log"

Roda dentro do checkout: o repositório vem do `origin` (SSH ou HTTPS) e a credencial da organização dona da pasta
(~/.config/claude-orgs/orgs.tsv -> ~/.config/claude-orgs/<org>/bitbucket.json: e-mail + token de API da Atlassian com
escopos de leitura de repositório e leitura/escrita de pull request). A source padrão é a branch atual, que precisa
estar no origin (git push antes); a dest padrão é a branch principal do repositório.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

API = "https://api.bitbucket.org/2.0"
ORG_DATA = Path.home() / ".config" / "claude-orgs"


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def repo():
    m = re.search(r"bitbucket\.org[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", git("remote", "get-url", "origin"))
    if not m:
        sys.exit("O origin deste checkout não é do Bitbucket.")
    return m.group(1)


def creds(cwd=None, org=None):
    """Credencial da organização dona da pasta (a pasta mais específica do orgs.tsv vence), ou da `org` pedida."""
    cwd, best, blen = str(Path(cwd or Path.cwd()).resolve()), None, 0
    try:
        lines = (ORG_DATA / "orgs.tsv").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        f = line.split("\t")
        if len(f) == 3 and f[0] and not f[0].startswith("#"):
            p = str(Path(f[2]).expanduser())
            if (cwd == p or cwd.startswith(p + "/")) and len(p) > blen:
                best, blen = f[0], len(p)
    best = org or best
    try:
        c = json.loads((ORG_DATA / (best or "-") / "bitbucket.json").read_text())
    except (OSError, ValueError):
        c = {}
    if not (c.get("email") and c.get("token")):
        sys.exit(f"Sem token do Bitbucket para a organização {best or '(nenhuma)'}: cadastre no formulário de organização do quadro.")
    return c


def call(method, path, c, body=None, raw=False):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
        "Authorization": "Basic " + base64.b64encode(f"{c['email']}:{c['token']}".encode()).decode(),
        "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        return data if raw else (json.loads(data) if data.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            msg = (json.loads(e.read()).get("error") or {}).get("message", "")
        except ValueError:
            msg = ""
        hint = {401: " (e-mail ou token inválidos)", 403: " (falta escopo no token ou permissão no repositório)"}.get(e.code, "")
        sys.exit(f"Bitbucket respondeu {e.code}: {msg or e.reason}{hint}")


def create(a, c, r):
    source = a.source or git("rev-parse", "--abbrev-ref", "HEAD")
    if not git("ls-remote", "--heads", "origin", source):
        sys.exit(f"A branch {source} não está no origin: rode `git push -u origin {source}` antes.")
    dest = a.dest or ((call("GET", f"/repositories/{r}", c).get("mainbranch") or {}).get("name") or "main")
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    pr = call("POST", f"/repositories/{r}/pullrequests", c, {
        "title": a.title, "description": body, "draft": a.draft, "close_source_branch": False,
        "source": {"branch": {"name": source}}, "destination": {"branch": {"name": dest}}})
    print(f"PR #{pr['id']} aberto ({source} → {dest}): {pr['links']['html']['href']}")


def list_(a, c, r):
    q = quote(f'state="{a.state}"')
    for pr in call("GET", f"/repositories/{r}/pullrequests?q={q}&pagelen=50", c).get("values") or []:
        print(f"#{pr['id']}  {pr['source']['branch']['name']} → {pr['destination']['branch']['name']}  {pr['title']}")


def view(a, c, r):
    pr = call("GET", f"/repositories/{r}/pullrequests/{a.number}", c)
    builds = [s.get("state") for s in call("GET", f"/repositories/{r}/pullrequests/{a.number}/statuses", c).get("values") or []]
    ok = [p["user"]["display_name"] for p in pr.get("participants") or [] if p.get("approved")]
    print(f"#{pr['id']} {pr['title']}\n  {pr['state']}{' (rascunho)' if pr.get('draft') else ''} · "
          f"{pr['source']['branch']['name']} → {pr['destination']['branch']['name']}\n"
          f"  builds: {', '.join(builds) or 'nenhum'} · aprovado por: {', '.join(ok) or 'ninguém'} · "
          f"{pr.get('comment_count', 0)} comentários\n  {pr['links']['html']['href']}")


def api(a, c, r):
    path = "/" + re.sub(r"^/?(2\.0/)?", "", a.path)
    if "{repo}" in path:
        path = path.replace("{repo}", r())
    body = json.loads(Path(a.data_file).read_text()) if a.data_file else json.loads(a.data) if a.data else None
    out = call(a.method or ("POST" if body is not None else "GET"), path, c, body, raw=True)
    try:
        print(json.dumps(json.loads(out), indent=2, ensure_ascii=False))
    except ValueError:  # logs de pipeline e afins vêm como texto
        sys.stdout.write(out.decode(errors="replace"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create")
    p.add_argument("--title", required=True)
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--source")
    p.add_argument("--dest")
    p.add_argument("--draft", action="store_true")
    sub.add_parser("list").add_argument("--state", default="OPEN", choices=["OPEN", "MERGED", "DECLINED"])
    sub.add_parser("view").add_argument("number", type=int)
    p = sub.add_parser("api")
    p.add_argument("path")
    p.add_argument("-X", "--method")
    p.add_argument("--data")
    p.add_argument("--data-file")
    a = ap.parse_args(argv)
    if a.cmd == "api":  # pode rodar fora de um checkout (ex.: /user); o repositório só é lido se o caminho pedir
        return api(a, creds(org=a.org), repo)
    {"create": create, "list": list_, "view": view}[a.cmd](a, creds(org=a.org), repo())


if __name__ == "__main__":
    main()
