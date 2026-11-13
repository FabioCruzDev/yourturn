"""Teste do bb_pr.py sem rede nem git real: `/usr/bin/python3 tools/test_bb_pr.py`."""
import io
import json
import sys
import tempfile
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bb_pr  # noqa: E402

tmp = Path(tempfile.mkdtemp())
(tmp / "globex").mkdir()
(tmp / "orgs.tsv").write_text(f"acme\t~/.claude-acme\t{tmp}/T\nglobex\t~/.claude-globex\t{tmp}/Z\n")
(tmp / "globex" / "bitbucket.json").write_text('{"email": "eu@globex.com", "token": "tk"}')
(tmp / "Z" / "app").mkdir(parents=True)
bb_pr.ORG_DATA = tmp

# credencial pela pasta; fora das organizações com Bitbucket, recusa
assert bb_pr.creds(tmp / "Z" / "app") == {"email": "eu@globex.com", "token": "tk"}
try:
    bb_pr.creds(tmp / "T"); raise AssertionError("aceitou organização sem token")
except SystemExit as e:
    assert "acme" in str(e)

reqs, pushed = [], {"zy-5-login"}
GIT = {("remote", "get-url", "origin"): "git@bitbucket.org:globex/web-app.git", ("rev-parse", "--abbrev-ref", "HEAD"): "zy-5-login"}
bb_pr.git = lambda *a: GIT.get(a, "") if a[0] != "ls-remote" else ("abc refs/heads/" + a[-1] if a[-1] in pushed else "")


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def urlopen(req, timeout=0):
    reqs.append(req)
    if req.get_method() == "POST":
        return Resp(json.dumps({"id": 7, "links": {"html": {"href": "https://bitbucket.org/globex/web-app/pull-requests/7"}}}).encode())
    return Resp(json.dumps({"mainbranch": {"name": "develop"}}).encode())


bb_pr.urllib.request.urlopen = urlopen
assert bb_pr.repo() == "globex/web-app"
out = io.StringIO()
with redirect_stdout(out):
    bb_pr.create(bb_pr.argparse.Namespace(title="Login", body="Corrige o login", body_file=None, source=None, dest=None, draft=False),
                 {"email": "eu@globex.com", "token": "tk"}, "globex/web-app")
assert out.getvalue().strip() == "PR #7 aberto (zy-5-login → develop): https://bitbucket.org/globex/web-app/pull-requests/7", out.getvalue()
post = reqs[-1]
assert post.full_url == "https://api.bitbucket.org/2.0/repositories/globex/web-app/pullrequests" and post.get_method() == "POST"
assert json.loads(post.data) == {"title": "Login", "description": "Corrige o login", "draft": False, "close_source_branch": False,
                                 "source": {"branch": {"name": "zy-5-login"}}, "destination": {"branch": {"name": "develop"}}}
assert post.get_header("Authorization").startswith("Basic ")
# branch que não foi enviada ao origin: recusa antes de chamar a API
GIT[("rev-parse", "--abbrev-ref", "HEAD")] = "zy-6-local"
n = len(reqs)
try:
    bb_pr.create(bb_pr.argparse.Namespace(title="x", body=None, body_file=None, source=None, dest="main", draft=False), {}, "globex/web-app")
    raise AssertionError("abriu PR de branch que não está no origin")
except SystemExit as e:
    assert "git push -u origin zy-6-local" in str(e) and len(reqs) == n
# 403: diz qual escopo falta
def forbidden(req, timeout=0):
    raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"error": {"message": "Access denied"}}'))
bb_pr.urllib.request.urlopen = forbidden
try:
    bb_pr.call("POST", "/x", {"email": "e", "token": "t"}, {}); raise AssertionError("não acusou o 403")
except SystemExit as e:
    assert "403: Access denied" in str(e) and "falta escopo no token" in str(e)
# --org vence a pasta
assert bb_pr.creds(tmp / "T", org="globex")["email"] == "eu@globex.com"
# api: {repo} vira o repositório, /2.0 opcional, corpo => POST, JSON formatado ou texto puro (logs)
seen = []
def api_open(req, timeout=0):
    seen.append((req.get_method(), req.full_url, req.data))
    return Resp(b"linha 1 do log\n" if req.full_url.endswith("/log") else b'{"values": [{"id": 1}]}')
bb_pr.urllib.request.urlopen = api_open
c = {"email": "e", "token": "t"}
out = io.StringIO()
with redirect_stdout(out):
    bb_pr.api(bb_pr.argparse.Namespace(path="/2.0/repositories/{repo}/pullrequests/12/comments", method=None, data=None, data_file=None), c, lambda: "globex/web-app")
    bb_pr.api(bb_pr.argparse.Namespace(path="repositories/{repo}/pullrequests/12/comments", method=None, data='{"content": {"raw": "ok"}}', data_file=None), c, lambda: "globex/web-app")
    bb_pr.api(bb_pr.argparse.Namespace(path="/repositories/{repo}/pipelines/{u}/steps/{s}/log", method=None, data=None, data_file=None), c, lambda: "globex/web-app")
    bb_pr.api(bb_pr.argparse.Namespace(path="/user", method=None, data=None, data_file=None), c, lambda: (_ for _ in ()).throw(AssertionError("leu o repo sem precisar")))
assert seen[0][:2] == ("GET", "https://api.bitbucket.org/2.0/repositories/globex/web-app/pullrequests/12/comments"), seen[0]
assert seen[1][0] == "POST" and json.loads(seen[1][2]) == {"content": {"raw": "ok"}}
assert seen[2][1].endswith("/pipelines/{u}/steps/{s}/log") and seen[3][1] == "https://api.bitbucket.org/2.0/user"
assert '"id": 1' in out.getvalue() and "linha 1 do log" in out.getvalue(), out.getvalue()
print("ok")
