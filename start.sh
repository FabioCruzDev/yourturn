#!/bin/bash
# Sobe o quadro dentro do tmux "kanban", se ainda não estiver no ar. Pode rodar quantas vezes quiser.
# Configuração local (não versionada): local.env, no formato KEY=valor (ver local.env.example).
set -u
cd "$(dirname "$0")"
[ -f local.env ] && { set -a; . ./local.env; set +a; }
PORT="${KANBAN_PORT:-8765}"
SESSION="${KANBAN_TMUX:-kanban}"
PY="${KANBAN_PYTHON:-$(command -v /usr/bin/python3 || command -v python3)}"

window() {  # window <nome> <comando>: (re)cria a janela no tmux
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION" -n "$1" "$2"
  elif tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -qx "$1"; then
    tmux respawn-window -k -t "$SESSION:$1" "$2"
  else
    tmux new-window -d -t "$SESSION:" -n "$1" "$2"
  fi
}

# Voz do Jarvis (experimental, só com KANBAN_VOICE=1 e o Piper instalado em tts/piper-venv)
if [ "${KANBAN_VOICE:-}" = 1 ] && [ -x tts/piper-venv/bin/python ] && ! curl -s -o /dev/null -m 2 "http://127.0.0.1:${KANBAN_TTS_PORT:-8766}/health"; then
  window voz "cd $(pwd)/tts && MEM=800M SWAP=0 ./limited.sh piper-venv/bin/python piper_server.py"
fi

if curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT/"; then
  echo "Quadro já está no ar: http://localhost:$PORT"
  exit 0
fi
# A janela do tmux não herda este shell (o servidor do tmux já pode estar rodando): passa as KANBAN_* explicitamente.
ENVS=$(env | grep -E '^(KANBAN_|LINEAR_API_KEY=)' | while IFS= read -r kv; do printf "%q " "$kv"; done)
window quadro "cd $(printf %q "$(pwd)") && exec env $ENVS $(printf %q "$PY") server.py"
echo "Quadro subindo em http://localhost:$PORT  (tmux attach -t $SESSION)"
