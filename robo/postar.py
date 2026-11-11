"""Animador Automático — publica uma prévia como Reels no Instagram do cliente, testa as chaves e renova o token.
python robo/postar.py postar <id>     -> publica previas/<id>/video.mp4 com a legenda de previas/<id>/dados.json
python robo/postar.py testar          -> confere as chaves e grava pub/teste.json
python robo/postar.py renovar         -> renova o token do Instagram (60 dias) e salva no segredo IG_TOKEN
Segredos: IG_TOKEN (login do Instagram "IGAA…" ou token do Facebook "EAA…"), GH_PAT, GEMINI_API_KEY, CF_ACCOUNT_ID, CF_API_TOKEN."""
import base64, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PUB = RAIZ / 'pub'  # checkout do branch "previas"
REPO = os.environ.get('GITHUB_REPOSITORY', '')
V = 'v21.0'


def env(k):
    return (os.environ.get(k) or '').strip()


def log(*a):
    print(*a, flush=True)


def req(url, dados=None, metodo=None, cab=None):
    corpo = urllib.parse.urlencode(dados).encode() if isinstance(dados, dict) else dados
    r = urllib.request.Request(url, data=corpo, method=metodo or ('POST' if corpo else 'GET'), headers=cab or {})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            t = resp.read().decode()
            return json.loads(t) if t else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors='replace')
        try:
            msg = json.loads(msg).get('error', {}).get('message', msg)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f'HTTP {e.code}: {str(msg)[:300]}')


def instagram():
    """Devolve (base_url, ig_user_id, username, token) para os dois tipos de token."""
    tok = env('IG_TOKEN')
    if not tok:
        raise RuntimeError('Falta o token do Instagram (passo 5 da ativação).')
    if tok.startswith('EAA'):
        base = f'https://graph.facebook.com/{V}'
        for p in req(f'{base}/me/accounts?fields=instagram_business_account{{id,username}}&access_token={tok}').get('data', []):
            ig = p.get('instagram_business_account')
            if ig:
                return base, ig['id'], ig.get('username', ''), tok
        raise RuntimeError('Nenhum Instagram profissional ligado às suas páginas.')
    base = f'https://graph.instagram.com/{V}'
    me = req(f'{base}/me?fields=user_id,username&access_token={tok}')
    return base, me.get('user_id') or me.get('id'), me.get('username', ''), tok


def postar(pid):
    dados = json.loads((PUB / pid / 'dados.json').read_text(encoding='utf-8'))
    base, ig, user, tok = instagram()
    url = f'https://raw.githubusercontent.com/{REPO}/previas/{pid}/video.mp4'
    log('Publicando no @' + user)
    cid = req(f'{base}/{ig}/media', {'media_type': 'REELS', 'video_url': url, 'caption': dados.get('legenda', ''), 'share_to_feed': 'true', 'access_token': tok})['id']
    for _ in range(60):
        st = req(f'{base}/{cid}?fields=status_code,status&access_token={tok}')
        if st.get('status_code') == 'FINISHED':
            break
        if st.get('status_code') == 'ERROR':
            raise RuntimeError('O Instagram não aceitou o vídeo: ' + str(st.get('status')))
        time.sleep(10)
    else:
        raise RuntimeError('O Instagram demorou demais pra processar o vídeo. Tente postar de novo.')
    mid = req(f'{base}/{ig}/media_publish', {'creation_id': cid, 'access_token': tok})['id']
    link = req(f'{base}/{mid}?fields=permalink&access_token={tok}').get('permalink', '')
    dados.update({'status': 'postado', 'permalink': link, 'postado_em': time.strftime('%Y-%m-%dT%H:%M:%S')})
    dados.pop('erro_post', None)
    (PUB / pid / 'dados.json').write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding='utf-8')
    indice = PUB / 'index.json'
    lista = json.loads(indice.read_text(encoding='utf-8')) if indice.exists() else []
    for x in lista:
        if x['id'] == pid:
            x.update({'status': 'postado', 'permalink': link})
    indice.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding='utf-8')
    log('Postado:', link)


def _segredo(nome, valor):
    from nacl import encoding, public
    cab = {'Authorization': f'Bearer {env("GH_PAT")}', 'Accept': 'application/vnd.github+json'}
    pk = req(f'https://api.github.com/repos/{REPO}/actions/secrets/public-key', cab=cab)
    caixa = public.SealedBox(public.PublicKey(pk['key'].encode(), encoding.Base64Encoder()))
    cifrado = base64.b64encode(caixa.encrypt(valor.encode())).decode()
    req(f'https://api.github.com/repos/{REPO}/actions/secrets/{nome}', json.dumps({'encrypted_value': cifrado, 'key_id': pk['key_id']}).encode(), 'PUT', {**cab, 'Content-Type': 'application/json'})


def renovar():
    tok = env('IG_TOKEN')
    if not tok or tok.startswith('EAA'):
        log('Sem token de login do Instagram pra renovar.'); return
    novo = req(f'https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token={tok}')
    if novo.get('access_token') and env('GH_PAT'):
        _segredo('IG_TOKEN', novo['access_token'])
        log('Token do Instagram renovado por mais', round(novo.get('expires_in', 0) / 86400), 'dias')


def testar():
    res = {'quando': time.strftime('%Y-%m-%dT%H:%M:%S')}
    try:
        req('https://generativelanguage.googleapis.com/v1beta/models?pageSize=1', cab={'x-goog-api-key': env('GEMINI_API_KEY')})
        res['google'] = 'ok'
    except Exception as e:  # noqa: BLE001
        res['google'] = f'erro: {e}'
    try:
        conta, tok = env('CF_ACCOUNT_ID'), env('CF_API_TOKEN')
        if not conta or not tok:
            raise RuntimeError('faltando')
        req(f'https://api.cloudflare.com/client/v4/accounts/{conta}/ai/models/search?per_page=1', cab={'Authorization': f'Bearer {tok}'})
        res['cloudflare'] = 'ok'
    except Exception as e:  # noqa: BLE001
        res['cloudflare'] = 'erro: ' + str(e).replace(env('CF_ACCOUNT_ID') or '-', '<conta>')
    try:
        if env('IG_TOKEN'):
            _, _, user, _ = instagram()
            res['instagram'] = 'ok'; res['usuario'] = user
        else:
            res['instagram'] = 'faltando'
    except Exception as e:  # noqa: BLE001
        res['instagram'] = f'erro: {e}'
    PUB.mkdir(exist_ok=True)
    (PUB / 'teste.json').write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
    log(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    cmd = sys.argv[1]
    try:
        {'postar': lambda: postar(sys.argv[2]), 'renovar': renovar, 'testar': testar}[cmd]()
    except Exception as e:  # noqa: BLE001
        log('ERRO:', e)
        if cmd == 'postar':
            p = PUB / sys.argv[2] / 'dados.json'
            if p.exists():
                d = json.loads(p.read_text(encoding='utf-8')); d['erro_post'] = str(e)
                p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        sys.exit(1)
