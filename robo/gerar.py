"""Animador Automático — gera um vídeo de história a partir de um tema.
Etapas (cada uma é um passo do workflow, pra o app mostrar o progresso):
  python robo/gerar.py historia   -> saida/historia.json (roteiro, personagens, cenas, legenda)
  python robo/gerar.py imagens    -> saida/cena_XX.png (Cloudflare FLUX, personagem consistente)
  python robo/gerar.py voz        -> saida/voz.mp3 + saida/tempos.json (Google TTS)
  python robo/gerar.py montar     -> saida/video.mp4 + saida/capa.jpg
Entradas por variável de ambiente: TEMA, ESTILO, DURACAO, ARTE, VOZ, FINAL, ROTEIRO (opcional).
Segredos: GEMINI_API_KEY, CF_ACCOUNT_ID, CF_API_TOKEN."""
import base64, json, os, re, subprocess, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / 'saida'
SAIDA.mkdir(exist_ok=True)
GEM = 'https://generativelanguage.googleapis.com/v1beta/models'


def env(k, padrao=''):
    return (os.environ.get(k) or padrao).strip()


def log(*a):
    print(*a, flush=True)


def http(url, corpo=None, cab=None, metodo=None, timeout=240):
    dados = json.dumps(corpo).encode() if isinstance(corpo, (dict, list)) else corpo
    req = urllib.request.Request(url, data=dados, method=metodo or ('POST' if dados else 'GET'), headers=cab or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get('Content-Type', '')


_MODELOS = None


def modelos(tipo):
    """Descobre os modelos disponíveis pra esta chave (o Google troca os nomes com frequência)."""
    global _MODELOS
    if _MODELOS is None:
        try:
            bruto, _ = http(f'{GEM}?pageSize=300', cab={'x-goog-api-key': env('GEMINI_API_KEY')})
            _MODELOS = [m['name'].split('/')[-1] for m in json.loads(bruto).get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
        except Exception as e:  # noqa: BLE001
            log('   não consegui listar os modelos:', e); _MODELOS = []
    def ver(n):
        return [int(x) for x in re.findall(r'\d+', n.split('-')[1])] if len(n.split('-')) > 1 else [0]
    if tipo == 'tts':
        cands = [m for m in _MODELOS if 'tts' in m]
        cands.sort(key=lambda n: ('flash' not in n, [-x for x in ver(n)]))
        return cands + ['gemini-2.5-flash-preview-tts', 'gemini-2.5-pro-preview-tts']
    cands = [m for m in _MODELOS if m.startswith('gemini-') and 'flash' in m and not any(k in m for k in ('tts', 'image', 'audio', 'live', 'embedding', 'thinking', 'exp'))]
    cands.sort(key=lambda n: ('lite' in n, 'preview' in n, [-x for x in ver(n)]))
    return cands[:4] + ['gemini-3.8-flash', 'gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-2.5-flash']


def gemini_texto(prompt):
    corpo = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 1.0, 'responseMimeType': 'application/json'}}
    ultimo = None
    for modelo in dict.fromkeys(modelos('texto')):
        for t in range(3):
            try:
                bruto, _ = http(f'{GEM}/{modelo}:generateContent', corpo, {'Content-Type': 'application/json', 'x-goog-api-key': env('GEMINI_API_KEY')})
                d = json.loads(bruto)
                return json.loads(''.join(p.get('text', '') for p in d['candidates'][0]['content']['parts']))
            except urllib.error.HTTPError as e:
                ultimo = f'{modelo}: HTTP {e.code} {e.read().decode(errors="replace")[:200]}'
                log('  ', ultimo)
                if e.code in (429, 500, 503):
                    time.sleep(20 * (t + 1)); continue
                break
            except Exception as e:  # noqa: BLE001
                ultimo = f'{modelo}: {e}'; log('  ', ultimo); time.sleep(5)
    raise RuntimeError('Não consegui falar com a IA do Google. ' + str(ultimo))


ESTILOS = {
    'drama': 'dramática, com tensão crescente e uma virada emocional',
    'medo': 'assustadora, de suspense e terror leve (sem violência explícita), com clima de mistério',
    'risada': 'engraçada, com situações constrangedoras e humor do dia a dia brasileiro',
    'emocao': 'emocionante, tocante, que faz chorar no final',
    'suspense': 'de suspense, com mistério e uma revelação no final',
    'romance': 'de romance, leve e envolvente',
    'motiva': 'motivacional, de superação, com uma lição no final',
    'curio': 'de curiosidade, contando um fato surpreendente em forma de história',
    'decide': 'interativa, que termina num dilema com duas opções pra o público votar nos comentários',
    'familia': 'de treta de família, situação polêmica do dia a dia que divide opiniões',
}
ARTES = {
    'Cartoon 2D': '2D cartoon illustration, bold clean outlines, vibrant flat colors, expressive faces',
    'Anime': 'anime illustration, cel shading, expressive eyes, cinematic lighting',
    'Graphic novel': '2D graphic novel comic style, inked outlines, dramatic shading, rich colors',
    '3D fofinho': 'cute 3D animated movie style render, soft lighting, big expressive eyes',
}
FINAIS = {
    'Pergunta pra comentar': 'Termine com uma pergunta direta pro público responder nos comentários.',
    'Continua na parte 2': 'Termine num gancho forte, dizendo que a parte 2 sai em breve e pedindo pra seguir o perfil.',
    'Final fechado': 'Termine com um desfecho completo e satisfatório.',
}
CENAS_POR_DURACAO = {'30s': 5, '45s': 7, '60s': 9, 'Série (parte 1)': 7}


def historia():
    tema, estilo = env('TEMA'), env('ESTILO', 'drama')
    n = CENAS_POR_DURACAO.get(env('DURACAO', '45s'), 7)
    roteiro = env('ROTEIRO')
    prompt = f"""Você é roteirista de vídeos virais curtos (Reels/TikTok) de histórias animadas em português do Brasil.
Escreva uma história {ESTILOS.get(estilo, ESTILOS['drama'])} sobre o tema: "{tema}".
{('Use este roteiro como base: ' + roteiro) if roteiro else ''}
Regras:
- Exatamente {n} cenas. Cada cena tem UMA fala de narração, curta (8 a 22 palavras), linguagem simples e natural do Brasil.
- A cena 1 é o GANCHO: em uma frase, o conflito e o que está em jogo. Nada de "olá" ou apresentação.
- Cada cena traz uma escalada ou virada. {FINAIS.get(env('FINAL'), FINAIS['Pergunta pra comentar'])}
- Personagens fictícios. Nada de pessoas reais, marcas reais, violência gráfica ou conteúdo adulto.
- Para cada cena, "imagem": descrição VISUAL em inglês da cena (quem aparece, expressão, ação, lugar, enquadramento), sem texto escrito na imagem.
- "personagem": descrição visual fixa, em inglês, do protagonista (idade, cabelo, roupa, traços), pra manter igual em todas as cenas.
- "legenda": legenda do post em português com 2 a 3 frases curtas, um emoji, uma chamada pra comentar e 4 hashtags.
Responda só em JSON: {{"titulo": str, "personagem": str, "cenas": [{{"fala": str, "imagem": str, "destaque": str}}], "legenda": str}}
("destaque" é a palavra mais forte da fala)."""
    h = gemini_texto(prompt)
    h['cenas'] = h['cenas'][:n]
    (SAIDA / 'historia.json').write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding='utf-8')
    log('História:', h['titulo'], '|', len(h['cenas']), 'cenas')


def _multipart(campos, arquivos):
    fr = uuid.uuid4().hex
    corpo = b''
    for k, v in campos.items():
        corpo += f'--{fr}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, dados in arquivos.items():
        corpo += (f'--{fr}\r\nContent-Disposition: form-data; name="{k}"; filename="{k}.png"\r\nContent-Type: image/png\r\n\r\n').encode() + dados + b'\r\n'
    return corpo + f'--{fr}--\r\n'.encode(), f'multipart/form-data; boundary={fr}'


def _ref(caminho):
    from PIL import Image
    import io
    im = Image.open(caminho).convert('RGB'); im.thumbnail((508, 508))
    b = io.BytesIO(); im.save(b, 'PNG'); return b.getvalue()


def cf_imagem(prompt, w, h, refs=(), seed=None):
    conta, token = env('CF_ACCOUNT_ID'), env('CF_API_TOKEN')
    campos = {'prompt': prompt, 'width': w, 'height': h}
    if seed is not None:
        campos['seed'] = seed
    corpo, tipo = _multipart(campos, {f'input_image_{i}': _ref(r) for i, r in enumerate(refs)})
    url = f'https://api.cloudflare.com/client/v4/accounts/{conta}/ai/run/@cf/black-forest-labs/flux-2-klein-4b'
    for t in range(3):
        try:
            bruto, ct = http(url, corpo, {'Authorization': f'Bearer {token}', 'Content-Type': tipo}, timeout=180)
            if 'json' in ct:
                img = (json.loads(bruto).get('result') or {}).get('image')
                if not img:
                    raise RuntimeError('resposta sem imagem')
                return base64.b64decode(img)
            return bruto
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')[:300]
            log(f'   imagem: HTTP {e.code} {msg.replace(conta, "<conta>")}')
            if 'daily free allocation' in msg or '4006' in msg:
                raise RuntimeError('A cota grátis de imagens de hoje acabou. Ela renova às 21h (horário de Brasília).')
            if e.code in (401, 403):
                raise RuntimeError('O token da Cloudflare foi recusado. Refaça o passo 2 da ativação.')
            if t < 2:
                time.sleep(15 * (t + 1)); continue
            raise RuntimeError(f'Cloudflare respondeu HTTP {e.code}')


def imagens():
    h = json.loads((SAIDA / 'historia.json').read_text(encoding='utf-8'))
    arte = ARTES.get(env('ARTE', 'Cartoon 2D'), ARTES['Cartoon 2D'])
    seed = sum(map(ord, h['titulo'])) % 100000
    ref = SAIDA / 'personagem.png'
    ref.write_bytes(cf_imagem(f"{arte}. Character reference, single character, full body, neutral pose, plain light background. {h['personagem']}", 768, 768, seed=seed))
    log('personagem ok')
    for i, c in enumerate(h['cenas']):
        p = f"{arte}. Vertical 9:16 frame. Main character (exactly as in the reference image, same face, same hair, same clothes and colors in every scene): {h['personagem']}. Scene: {c['imagem']}. No text, no letters, no watermark."
        (SAIDA / f'cena_{i:02d}.png').write_bytes(cf_imagem(p, 720, 1280, refs=[ref], seed=seed + i + 1))
        log(f'cena {i + 1}/{len(h["cenas"])} ok')


VOZES = {'Masculina': 'Puck', 'Feminina': 'Kore', 'Grave de suspense': 'Charon', 'Jovem animada': 'Leda'}
TOM = {'drama': 'dramático e envolvente', 'medo': 'baixo, tenso e misterioso', 'risada': 'divertido e animado', 'emocao': 'emocionado e suave',
       'suspense': 'tenso e misterioso', 'romance': 'suave e apaixonado', 'motiva': 'inspirador e forte', 'curio': 'curioso e animado',
       'decide': 'envolvente, de quem conta uma fofoca', 'familia': 'indignado e engraçado'}


def tts(texto, voz):
    corpo = {'contents': [{'parts': [{'text': texto}]}],
             'generationConfig': {'responseModalities': ['AUDIO'], 'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voz}}}}}
    ultimo = None
    for modelo in dict.fromkeys(modelos('tts')):
        for t in range(4):
            try:
                bruto, _ = http(f'{GEM}/{modelo}:generateContent', corpo, {'Content-Type': 'application/json', 'x-goog-api-key': env('GEMINI_API_KEY')})
                for p in json.loads(bruto)['candidates'][0]['content']['parts']:
                    if 'inlineData' in p:
                        return base64.b64decode(p['inlineData']['data'])
                ultimo = 'resposta sem áudio'
            except urllib.error.HTTPError as e:
                ultimo = f'{modelo}: HTTP {e.code} {e.read().decode(errors="replace")[:200]}'
                log('  ', ultimo)
                if e.code in (429, 500, 503):
                    time.sleep(40); continue
            break
    raise RuntimeError('Não consegui gravar a narração. ' + str(ultimo))


def silencios(arq):
    r = subprocess.run(['ffmpeg', '-v', 'info', '-i', str(arq), '-af', 'silencedetect=n=-35dB:d=0.25', '-f', 'null', '-'], capture_output=True, text=True).stderr
    ini = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', r)]
    fim = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', r)]
    return list(zip(ini, fim))


def duracao(arq):
    return float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(arq)], capture_output=True, text=True).stdout.strip())


def voz():
    h = json.loads((SAIDA / 'historia.json').read_text(encoding='utf-8'))
    falas = [c['fala'] for c in h['cenas']]
    instr = f"Narre como contador de histórias brasileiro de Reels, tom {TOM.get(env('ESTILO'), 'envolvente')}, ritmo dinâmico, pausa curta entre os parágrafos, sotaque brasileiro natural:"
    (SAIDA / 'voz.pcm').write_bytes(tts(instr + '\n\n' + '\n\n'.join(falas), VOZES.get(env('VOZ'), 'Puck')))
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 's16le', '-ar', '24000', '-ac', '1', '-i', str(SAIDA / 'voz.pcm'),
                    '-af', 'silenceremove=start_periods=1:start_threshold=-45dB,loudnorm=I=-15:TP=-1.5:LRA=11', '-ar', '44100', str(SAIDA / 'voz.mp3')], check=True)
    total = duracao(SAIDA / 'voz.mp3')
    sil = [s for s in silencios(SAIDA / 'voz.mp3') if s[0] > 0.3 and s[1] < total - 0.3]
    pesos = [len(f) + 8 for f in falas]
    acum, cortes = 0, []
    for p in pesos[:-1]:
        acum += p
        alvo = total * acum / sum(pesos)
        cand = min(sil, key=lambda s: abs((s[0] + s[1]) / 2 - alvo)) if sil else None
        meio = (cand[0] + cand[1]) / 2 if cand else alvo
        cortes.append(meio if abs(meio - alvo) < 2.5 else alvo)
    marcas = [0.0] + sorted(cortes) + [total + 0.6]
    tempos = [{'ini': round(marcas[i], 2), 'fim': round(marcas[i + 1], 2), 'fala': falas[i], 'destaque': h['cenas'][i].get('destaque', '')} for i in range(len(falas))]
    (SAIDA / 'tempos.json').write_text(json.dumps(tempos, ensure_ascii=False, indent=1), encoding='utf-8')
    log(f'narração {total:.1f}s')


def _fonte():
    f = RAIZ / 'robo' / 'Montserrat-ExtraBold.ttf'
    if not f.exists():
        try:
            bruto, _ = http('https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-ExtraBold.ttf', timeout=60)
            f.write_bytes(bruto)
        except Exception:  # noqa: BLE001
            return 'DejaVu Sans'
    return 'Montserrat ExtraBold'


def _t(s):
    h, r = divmod(max(0, s), 3600); m, s2 = divmod(r, 60)
    return f'{int(h)}:{int(m):02d}:{s2:05.2f}'


def legendas(tempos, fonte, arq):
    linhas = ['[Script Info]', 'ScriptType: v4.00+', 'PlayResX: 1080', 'PlayResY: 1920', '',
              '[V4+ Styles]', 'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
              f'Style: L,{fonte},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,520,1', '',
              '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for t in tempos:
        palavras = t['fala'].split()
        blocos = [palavras[i:i + 3] for i in range(0, len(palavras), 3)]
        dur = max(0.5, (t['fim'] - 0.4) - t['ini'])
        pesos = [sum(len(w) + 1 for w in b) for b in blocos]
        dest = (t.get('destaque') or '').strip('.,!?…').lower()
        ac = t['ini']
        for b, p in zip(blocos, pesos):
            d = dur * p / sum(pesos)
            txt = ' '.join(('{\\c&H3FD2FF&}' + w + '{\\c&HFFFFFF&}') if dest and w.strip('.,!?…').lower() == dest else w for w in b)
            linhas.append(f'Dialogue: 0,{_t(ac)},{_t(ac + d)},L,,0,0,0,,{{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{txt}')
            ac += d
    arq.write_text('\n'.join(linhas) + '\n', encoding='utf-8')


def montar():
    tempos = json.loads((SAIDA / 'tempos.json').read_text(encoding='utf-8'))
    fonte = _fonte()
    partes = []
    for i, t in enumerate(tempos):
        d = t['fim'] - t['ini'] + (0.4 if i < len(tempos) - 1 else 0)
        n = max(2, int(d * 30))
        z = "'min(1+0.0009*on,1.15)'" if i % 2 == 0 else "'max(1.15-0.0009*on,1.0)'"
        saida = SAIDA / f'parte_{i:02d}.mp4'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-loop', '1', '-i', str(SAIDA / f'cena_{i:02d}.png'),
                        '-vf', f"scale=1296:2304,zoompan=z={z}:x='iw/2-(iw/zoom/2)+sin(on/45)*12':y='ih/2-(ih/zoom/2)':d={n}:s=1080x1920:fps=30,format=yuv420p",
                        '-frames:v', str(n), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', str(saida)], check=True)
        partes.append((saida, d))
    entradas, filtro, ultimo, desloc = [], [], '0:v', 0.0
    for i, (p, d) in enumerate(partes):
        entradas += ['-i', str(p)]
        if i == 0:
            desloc = d - 0.4; continue
        filtro.append(f'[{ultimo}][{i}:v]xfade=transition={"slideleft" if i % 2 else "fade"}:duration=0.4:offset={desloc:.2f}[v{i}]')
        ultimo = f'v{i}'; desloc += d - 0.4
    legendas(tempos, fonte, SAIDA / 'leg.ass')
    fontes = f":fontsdir={RAIZ / 'robo'}" if fonte.startswith('Montserrat') else ''
    vf = (';'.join(filtro) + f';[{ultimo}]' if filtro else '[0:v]') + f"ass={SAIDA / 'leg.ass'}{fontes}[vf]"
    subprocess.run(['ffmpeg', '-y', '-v', 'error', *entradas, '-i', str(SAIDA / 'voz.mp3'), '-filter_complex', vf,
                    '-map', '[vf]', '-map', f'{len(partes)}:a', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '160k', '-shortest', '-movflags', '+faststart', str(SAIDA / 'video.mp4')], check=True)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', '0.8', '-i', str(SAIDA / 'video.mp4'), '-frames:v', '1', '-q:v', '3', str(SAIDA / 'capa.jpg')], check=True)
    log(f'vídeo pronto: {duracao(SAIDA / "video.mp4"):.1f}s')


if __name__ == '__main__':
    etapa = sys.argv[1]
    try:
        {'historia': historia, 'imagens': imagens, 'voz': voz, 'montar': montar}[etapa]()
    except Exception as e:  # noqa: BLE001
        (SAIDA / 'erro.txt').write_text(f'{etapa}: {e}', encoding='utf-8')
        log('ERRO:', e)
        sys.exit(1)
