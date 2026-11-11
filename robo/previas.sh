#!/usr/bin/env bash
# abre (ou cria) o branch "previas" em ./pub  |  uso: robo/previas.sh abrir | robo/previas.sh salvar "mensagem"
set -e
git config user.name "Animador Automático"; git config user.email "robo@users.noreply.github.com"
if [ "$1" = "abrir" ]; then
  if git fetch -q origin previas 2>/dev/null; then git worktree add -q pub FETCH_HEAD && (cd pub && git checkout -q -B previas);
  else git worktree add -q --orphan -b previas pub; fi
  exit 0
fi
cd pub
git config user.name "Animador Automático"; git config user.email "robo@users.noreply.github.com"
git add -A
git commit -qm "${2:-atualização}" || { echo "nada novo"; exit 0; }
for i in 1 2 3 4 5; do
  git push -q origin HEAD:previas && exit 0
  git pull -q --rebase -X theirs origin previas || true; sleep $((i*3))
done
exit 1
