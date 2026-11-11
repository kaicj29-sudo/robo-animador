"""Copia a prévia gerada (saida/) para pub/<id>/ e atualiza pub/index.json. Em caso de erro, grava o motivo."""
import json, os, shutil, sys, time
from pathlib import Path
RAIZ = Path(__file__).resolve().parent.parent
SAIDA, PUB = RAIZ / 'saida', RAIZ / 'pub'
pid = os.environ['ID']
pasta = PUB / pid
pasta.mkdir(parents=True, exist_ok=True)
ok = (SAIDA / 'video.mp4').exists()
h = json.loads((SAIDA / 'historia.json').read_text(encoding='utf-8')) if (SAIDA / 'historia.json').exists() else {}
dados = {'id': pid, 'tema': os.environ.get('TEMA', ''), 'estilo': os.environ.get('ESTILO', ''), 'titulo': h.get('titulo', ''),
         'legenda': h.get('legenda', ''), 'falas': [c['fala'] for c in h.get('cenas', [])], 'criado': time.strftime('%Y-%m-%dT%H:%M:%S'),
         'status': 'previa' if ok else 'erro'}
if ok:
    for a in ('video.mp4', 'capa.jpg'):
        shutil.copy(SAIDA / a, pasta / a)
else:
    dados['erro'] = (SAIDA / 'erro.txt').read_text(encoding='utf-8') if (SAIDA / 'erro.txt').exists() else 'Falha inesperada. Tente de novo.'
(pasta / 'dados.json').write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding='utf-8')
idx = PUB / 'index.json'
lista = [x for x in (json.loads(idx.read_text(encoding='utf-8')) if idx.exists() else []) if x['id'] != pid]
if ok:
    lista.insert(0, {k: dados[k] for k in ('id', 'titulo', 'estilo', 'criado', 'status')})
idx.write_text(json.dumps(lista[:200], ensure_ascii=False, indent=1), encoding='utf-8')
print('salvo', pid, dados['status'])
