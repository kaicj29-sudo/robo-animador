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
# Idioma do robô: 'pt' (padrão) ou 'en' (versão em inglês). Vem do arquivo robo/idioma.txt que o app grava no repositório.
_arq_idioma = RAIZ / 'robo' / 'idioma.txt'
IDIOMA = ((os.environ.get('IDIOMA') or (_arq_idioma.read_text(encoding='utf-8') if _arq_idioma.exists() else 'pt')).strip().lower() or 'pt')[:2]
REGRA_EN = ('\n\nREGRA DE IDIOMA (obrigatória, vale mais que qualquer instrução acima): escreva TODOS os textos que vão para o público '
            '(falas, títulos, pergunta, destaques, etiquetas, legenda do post e hashtags) em inglês americano natural, para um público dos EUA. '
            'Use nomes, lugares, dinheiro (US$) e situações do dia a dia americano. Não mude os nomes das chaves do JSON nem os valores técnicos (cenas, peças, animações, cores).')
GEM = 'https://generativelanguage.googleapis.com/v1beta/models'


def env(k, padrao=''):
    return (os.environ.get(k) or padrao).strip()


# Opções extras chegam grudadas no campo FINAL (o workflow do cliente não muda): "Pergunta pra comentar||leg=anton.amarelo"
LEG = {'fonte': 'classica', 'cor': 'branco'}
_fin = os.environ.get('FINAL', '')
if '||' in _fin:
    os.environ['FINAL'], _extra = _fin.split('||', 1)
    for _kv in _extra.split(';'):
        _k, _, _v = _kv.partition('=')
        if _k.strip() == 'leg':
            _f, _, _c = _v.strip().lower().partition('.')
            LEG = {'fonte': _f or 'classica', 'cor': _c or 'branco'}


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
            bruto, _ = http(f'{GEM}?pageSize=300', cab={'x-goog-api-key': env('GEMINI_API_KEY')}, timeout=30)
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
    if IDIOMA == 'en':
        prompt += REGRA_EN
    corpo = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 1.0, 'responseMimeType': 'application/json'}}
    if not env('GEMINI_API_KEY'):
        raise RuntimeError('A chave da IA do Google não está no robô. Abra o app > Conta > Ligar o robô e cole a chave de novo.')
    ultimo, cota, inicio = None, 0, time.time()
    for modelo in list(dict.fromkeys(modelos('texto')))[:5]:
        for t in range(2):
            if time.time() - inicio > 420:   # no máximo 7 min nessa etapa
                break
            try:
                bruto, _ = http(f'{GEM}/{modelo}:generateContent', corpo, {'Content-Type': 'application/json', 'x-goog-api-key': env('GEMINI_API_KEY')}, timeout=90)
                d = json.loads(bruto)
                return json.loads(''.join(p.get('text', '') for p in d['candidates'][0]['content']['parts']))
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors='replace')
                ultimo = f'{modelo}: HTTP {e.code} {txt[:200]}'
                log('  ', ultimo)
                if e.code in (400, 401, 403) and re.search(r'API.?key|PERMISSION|invalid', txt, re.I):
                    raise RuntimeError('A chave da IA do Google foi recusada. Crie uma chave nova no Google AI Studio e cole no app (Conta > Ligar o robô).')
                if e.code == 429:
                    cota += 1
                    break   # cota desse modelo acabou: tenta o próximo sem esperar
                if e.code in (500, 503):
                    time.sleep(8 * (t + 1)); continue
                break
            except Exception as e:  # noqa: BLE001
                ultimo = f'{modelo}: {e}'; log('  ', ultimo); time.sleep(3)
        else:
            continue
        if time.time() - inicio > 420:
            break
    if cota:
        raise RuntimeError('A cota grátis da IA do Google acabou por hoje nessa chave. Tente de novo mais tarde (ela renova todo dia) ou crie outra chave no Google AI Studio.')
    raise RuntimeError('A IA do Google não respondeu agora (instabilidade do Google). Espere uns minutos e toque em Criar vídeo de novo.')


ESTILOS = {
    'drama': 'dramática, com tensão crescente e uma virada emocional',
    'medo': 'assustadora, de suspense e terror leve (sem violência explícita), com clima de mistério',
    'risada': 'engraçada, com situações constrangedoras e humor do dia a dia brasileiro',
    'emocao': 'emocionante, tocante, que faz chorar no final',
    'suspense': 'de suspense, com mistério e uma revelação no final',
    'romance': 'de romance, leve e envolvente',
    'motiva': 'motivacional, de superação, com uma lição no final',
    'curio': 'de curiosidade, contando um fato surpreendente em forma de história',
    'curio3d': 'de curiosidade em 3D (fatos reais)',
    'explica3d': 'explicativo em 3D (como as coisas funcionam)',
    'ese3d': 'E se...? em 3D (situação hipotética explicada com ciência)',
    'historia3d': 'história em 3D com personagens (conflito do dia a dia)',
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


CENAS_3D = {
    'terra': 'o planeta Terra girando no espaço (clima, continentes, rotação, população, natureza, geografia)',
    'lua': 'a Lua de perto com a Terra ao fundo (Lua, marés, astronautas, noite)',
    'sol': 'o Sol em chamas (calor, luz, energia, estrelas, tempo da luz)',
    'espaco': 'voo pelo espaço entre estrelas e nebulosas (universo, galáxias, distâncias, mistérios)',
    'planeta': 'um planeta com anéis tipo Saturno (outros planetas, sistema solar)',
    'oceano': 'ondas do oceano (mar, profundezas, água, animais marinhos, navios)',
    'atomo': 'um átomo com elétrons girando (ciência, química, física, matéria, energia)',
    'dna': 'uma hélice de DNA girando (corpo humano, genética, células, animais, vida)',
    'numero': 'o número ou palavra de impacto em 3D gigante (use quando a fala tiver um número, medida ou dado marcante)',
}


def historia_3d(tema, n):
    lista = '\n'.join(f'- "{k}": {v}' for k, v in CENAS_3D.items())
    prompt = f"""Você é roteirista de vídeos virais curtos (Reels/TikTok) de CURIOSIDADES em português do Brasil, narrados sobre animações 3D.
Tema: "{tema}".
Regras:
- Use só fatos reais, conhecidos e verificáveis. Se não tiver certeza de um número, não use número. Nada de boato, nada inventado.
- Exatamente {n} falas de narração, curtas (8 a 22 palavras), linguagem simples e animada do Brasil.
- A fala 1 é o GANCHO: uma pergunta ou afirmação surpreendente que prende em 2 segundos. Nada de "olá".
- Cada fala traz um fato novo que deixa a pessoa mais curiosa. {FINAIS.get(env('FINAL'), FINAIS['Pergunta pra comentar'])}
- Para cada fala, escolha "cena3d" desta lista (a que melhor combina com o que está sendo dito). Não repita a mesma cena em falas seguidas e use "numero" em pelo menos 2 falas:
{lista}
- "texto3d": quando a cena for "numero", o número ou palavra de impacto (até 2 palavras, ex.: "8 MINUTOS", "70%", "NUNCA"); nas outras cenas, "".
- "destaque": a palavra mais forte da fala.
- "legenda": legenda do post em português com 2 a 3 frases curtas, um emoji, uma chamada pra comentar e 4 hashtags.
Responda só em JSON: {{"titulo": str, "cenas": [{{"fala": str, "cena3d": str, "texto3d": str, "destaque": str}}], "legenda": str}}"""
    h = gemini_texto(prompt)
    h['cenas'] = h['cenas'][:n]
    for i, c in enumerate(h['cenas']):
        if c.get('cena3d') not in CENAS_3D:
            c['cena3d'] = 'numero' if c.get('texto3d') else ['terra', 'espaco', 'atomo', 'oceano'][i % 4]
        if c['cena3d'] == 'numero' and not c.get('texto3d'):
            c['texto3d'] = c.get('destaque', '')
    return h


KIT_3D = '''Kit de peças (coordenadas em metros: y pra cima, chão em y=0; a cena cabe em x de -4 a 4, y de 0 a 7, z de -2 a 2):
- {"tipo":"caixa","pos":[x,y,z],"tam":[largura,altura,profundidade],"cor":"#hex","rot":[gx,gy,gz]}  (paredes, cabine, máquina, móveis; pos é o CENTRO)
- {"tipo":"vidro", ...como caixa}  (parede transparente pra mostrar o que tem dentro: visão em corte)
- {"tipo":"cilindro","pos","tam":[diâmetro,altura],"cor"} · {"tipo":"esfera","pos","tam":[diâmetro],"cor"}
- {"tipo":"cano","pos":[x,y,z],"ate":[x,y,z],"raio":0.08,"cor"}  (cano, fio, cabo de aço, haste, entre dois pontos)
- {"tipo":"boneco","pos":[x,0,z],"cor":"#hex da camisa","escala":1}  (pessoa low-poly de 1,9 m; pos é o pé)
- {"tipo":"fogo","pos","escala"} · {"tipo":"agua","pos":[onde a água sai],"altura":2} (jato caindo) · {"tipo":"faisca","pos"}
- {"tipo":"seta","pos","rot":[0,0,graus],"cor","escala"}  (aponta pra cima com rot 0; 90 = esquerda, -90 = direita, 180 = baixo)
- {"tipo":"mola","pos","tam":[diâmetro,altura],"cor"} · {"tipo":"engrenagem","pos","tam":[diâmetro],"cor"}
- {"tipo":"texto","pos","texto":"até 3 palavras"}  (etiqueta branca que aponta a peça importante)
Qualquer peça aceita "anim": {"tipo":"subir|descer|mover|cair|andar","para":[x,y,z]} (vai de pos até para durante a fala), {"tipo":"vaievem","para":[x,y,z]}, {"tipo":"girar","eixo":"x|y|z","vel":1}, {"tipo":"pulsar"} ou {"tipo":"piscar"}.
Cena: {"fundo":"#hex topo","fundo2":"#hex baixo","chao":"#hex" (ou false sem chão),"camera":{"alvo":[x,y,z],"dist":9,"altura":25,"lado":20},"objetos":[...até 18 peças]}'''


def historia_explica(tema, n, modo):
    extra = ('Explique COMO FUNCIONA a coisa do tema, passo a passo, do jeito mais visual possível.' if modo == 'explica3d'
             else 'É um "E se...?": explique com ciência real o que aconteceria nessa situação, passo a passo.')
    prompt = f"""Você é roteirista e diretor de arte de vídeos virais curtos em 3D low-poly (estilo maquete colorida, visão em corte), em português do Brasil.
Tema: "{tema}". {extra}
Regras do roteiro:
- Só fatos reais e corretos. Nada inventado. Linguagem simples e animada, como quem conta um segredo.
- Exatamente {n} falas de narração, de 8 a 22 palavras cada. A fala 1 é o GANCHO com a pergunta. {FINAIS.get(env('FINAL'), FINAIS['Pergunta pra comentar'])}
- "pergunta": o título curto do vídeo (até 40 caracteres), em forma de pergunta.
Regras da cena 3D de cada fala ("spec"):
- Monte a cena com as peças do kit abaixo. Pense como um diorama: mostre o objeto aberto (paredes de "vidro" ou cortadas) pra ver o mecanismo por dentro.
- 6 a 18 peças por cena. Peças grandes primeiro (paredes, chão do cenário), depois o mecanismo, depois 1 a 3 etiquetas "texto" nomeando as partes.
- Use cores vivas e contrastantes (laranja, azul, amarelo, verde, vermelho) e um boneco quando fizer sentido. Nunca esconda o boneco ou o mecanismo dentro de uma caixa sólida: use "vidro" pra mostrar o que está dentro.
- Pelo menos uma peça com "anim" em cada cena, mostrando a ação que a fala descreve (o elevador subindo, a água caindo, a engrenagem girando...).
- Mantenha o mesmo cenário base entre as cenas seguidas, mudando o que acontece e o ângulo da câmera, pra parecer um vídeo contínuo.
{KIT_3D}
Responda só em JSON: {{"titulo": str, "pergunta": str, "cenas": [{{"fala": str, "destaque": str, "spec": {{...cena...}}}}], "legenda": str}}
("destaque" é a palavra mais forte da fala; "legenda" é a legenda do post com 2 a 3 frases, um emoji, chamada pra comentar e 4 hashtags.)"""
    h = gemini_texto(prompt)
    h['cenas'] = h['cenas'][:n]
    for c in h['cenas']:
        c['cena3d'] = 'livre'
        c['texto3d'] = c.get('destaque', '')
    return h


KIT_HIST = '''Peças extras de HISTÓRIA (mesmas coordenadas):
- {"tipo":"boneco","quem":"nome do elenco","pos":[x,0,z],"rot":[0,graus,0],"emocao":"feliz|bravo|triste|assustado|surpreso|chorando|malandro|neutro"}  (personagem do elenco; rot y 0 = olhando pra câmera, 90 = pra direita da tela, -90 = pra esquerda)
- {"tipo":"balao","pos":[x,y,z],"texto":"até 4 palavras"}  (balão de fala; ponha uns 2,6 m acima do pé de quem fala)
- Cenário pronto (pos é o pé/base): "casa" (fachada com telhado, 3 m), "parede" (fundo de cômodo, 8 m), "janela", "porta", "sofa", "cama", "mesa", "tv", "roteador", "celular", "arvore", "carro", "poste", "nuvem", "dinheiro". Ex.: {"tipo":"sofa","pos":[-1,0,0],"cor":"#e76f51"}. Aceitam "escala" e "rot".
- Atuação ("anim" no boneco): "andar" (com "para"), "tremer" (medo/raiva), "pular" (alegria), "acenar", "apontar", "virar" (com "graus").'''


def historia_conto3d(tema, n):
    prompt = f"""Você é roteirista e diretor de animação de histórias virais curtas em 3D low-poly (bonecos de bloco, cores vivas, tipo maquete), em português do Brasil.
Tema: "{tema}".
Regras do roteiro:
- Uma história de ficção do dia a dia, com 2 ou 3 personagens, conflito claro e uma virada. Narrador fala com quem assiste.
- Exatamente {n} falas, de 8 a 20 palavras. Fala 1 = GANCHO com o conflito inteiro. Depois: contexto, escalada, fala do outro personagem (entre aspas, cara de pau), virada surpreendente, consequência. {FINAIS.get(env('FINAL'), FINAIS['Pergunta pra comentar'])}
- "pergunta": título curto (até 40 caracteres).
- "elenco": {{"Nome": {{"roupa":"#hex","calca":"#hex","pele":"#hex","cabelo":"#hex","cabeloLongo":bool,"bone":"#hex ou omitir"}}}}. Cores bem diferentes entre os personagens. Nomes simples.
Regras da cena 3D de cada fala ("spec"):
- Use SEMPRE os personagens do elenco pelo "quem", com a emoção certa da fala. O mesmo cenário base em cenas seguidas, mudando ângulo e ação.
- Câmera de CINEMA: "dist" entre 3 e 6 pra close no rosto (alvo na altura da cabeça, y≈1.7), 7 a 10 pra plano aberto. Alterne close e aberto. "altura" 5 a 15, "lado" -30 a 30.
- 5 a 16 peças. Pelo menos um boneco com "anim" de atuação por cena. Use "balao" quando um personagem fala. Fundo e chão com cores que combinem com o lugar (sala, rua, quarto).
{KIT_3D}
{KIT_HIST}
Responda só em JSON: {{"titulo": str, "pergunta": str, "elenco": {{...}}, "cenas": [{{"fala": str, "destaque": str, "spec": {{...cena...}}}}], "legenda": str}}
("destaque" é a palavra mais forte da fala; "legenda": 2 a 3 frases, um emoji, chamada pra comentar e 4 hashtags.)"""
    h = gemini_texto(prompt)
    h['cenas'] = h['cenas'][:n]
    el = h.get('elenco') or {}
    for c in h['cenas']:
        c['cena3d'] = 'livre'
        c['texto3d'] = c.get('destaque', '')
        sp = c.get('spec') or {}
        sp['elenco'], sp['historia'] = el, True
        c['spec'] = sp
    return h


def historia():
    tema, estilo = env('TEMA'), env('ESTILO', 'drama')
    if estilo in ('explica3d', 'ese3d', 'historia3d'):
        n = CENAS_POR_DURACAO.get(env('DURACAO', '45s'), 7)
        h = historia_conto3d(tema, n) if estilo == 'historia3d' else historia_explica(tema, n, estilo)
        (SAIDA / 'historia.json').write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding='utf-8')
        log('3D explicativo:', h.get('pergunta') or h['titulo'], '|', len(h['cenas']), 'cenas')
        return
    if estilo == 'curio3d':
        n = CENAS_POR_DURACAO.get(env('DURACAO', '45s'), 7)
        h = historia_3d(tema, n)
        (SAIDA / 'historia.json').write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding='utf-8')
        log('Curiosidade 3D:', h['titulo'], '|', ', '.join(c['cena3d'] for c in h['cenas']))
        return
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
- Para cada cena, "imagem": descrição VISUAL curta em inglês (até 40 palavras: quem aparece, expressão, ação simples, enquadramento), sem texto escrito na imagem. Chame cada personagem SEMPRE pelo mesmo nome do "elenco" (ex.: "Lucas", "Mia"); nunca escreva "he/she/the girl" sozinho. No máximo 2 personagens por imagem, cada um uma vez só. Prefira poses simples (evite mãos segurando várias coisas, abraços em grupo e muitas pessoas).
- "personagem": descrição visual fixa, em inglês, do protagonista (nome, idade, cabelo, roupa, traços), pra manter igual em todas as cenas.
- "elenco": os OUTROS personagens que aparecem (no máximo 3), cada um com "nome" e "visual" (descrição fixa em inglês: idade, cabelo, roupa, traços). Pode ser lista vazia.
- "cenario": o lugar principal da história, descrito em inglês com detalhes fixos (tipo de lugar, cores, luz, objetos marcantes), pra todas as cenas parecerem no mesmo lugar. Se uma cena acontecer em outro lugar, diga o lugar dentro do "imagem".
- "legenda": legenda do post em português com 2 a 3 frases curtas, um emoji, uma chamada pra comentar e 4 hashtags.
Responda só em JSON: {{"titulo": str, "personagem": str, "elenco": [{{"nome": str, "visual": str}}], "cenario": str, "cenas": [{{"fala": str, "imagem": str, "destaque": str}}], "legenda": str}}
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


class ImagemRecusada(RuntimeError):
    pass


def _cf_chamar(prompt, w, h, refs, seed):
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
                    raise ImagemRecusada('resposta sem imagem')
                return base64.b64decode(img)
            return bruto
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')[:300]
            log(f'   imagem: HTTP {e.code} {msg.replace(conta, "<conta>")}')
            if 'daily free allocation' in msg or '4006' in msg:
                raise RuntimeError('A cota grátis de imagens de hoje acabou. Ela renova às 21h (horário de Brasília).')
            if e.code in (401, 403):
                raise RuntimeError('O token da Cloudflare foi recusado. Refaça o passo 2 da ativação.')
            if e.code in (400, 413, 422):
                raise ImagemRecusada(f'HTTP {e.code} {msg[:120]}')   # pedido recusado: não adianta repetir igual
            if t < 2:
                time.sleep(15 * (t + 1)); continue
            raise RuntimeError(f'O gerador de imagens da Cloudflare está instável agora (HTTP {e.code}). Tente de novo em alguns minutos.')
        except (urllib.error.URLError, TimeoutError) as e:
            if t < 2:
                time.sleep(10); continue
            raise RuntimeError(f'O gerador de imagens da Cloudflare não respondeu ({e}). Tente de novo em alguns minutos.')


def _simplificar(prompt):
    """Prompt mais curto e neutro (o gerador recusa pedido longo demais ou com palavra sensível)."""
    p = re.sub(r'\s+', ' ', prompt)
    p = re.sub(r'(?i)\b(blood\w*|gun\w*|weapon\w*|knife|kill\w*|dead|death|naked|nude|sexy|drunk|drugs?|beer|alcohol|cigarett\w*|violen\w*|fight\w*|punch\w*)\b', '', p)
    return p[:700]


def cf_imagem(prompt, w, h, refs=(), seed=None):
    """Gera a imagem. Se a Cloudflare recusar (HTTP 400), tenta de novo com prompt menor e outra seed.
    Com personagem de referência, NUNCA tira a referência: sem ela o personagem e o estilo mudam no meio do vídeo.
    Se mesmo assim recusar, quem chamou reaproveita outra cena."""
    curto = _simplificar(prompt)
    tentativas = [(prompt, seed), (curto, (seed or 0) + 7777), (curto[:450], (seed or 0) + 31)]
    ultimo = None
    for n, (p, sd) in enumerate(tentativas):
        try:
            return _cf_chamar(p, w, h, refs, sd)
        except ImagemRecusada as e:
            ultimo = e; log(f'   imagem recusada (tentativa {n + 1}, prompt de {len(p)} letras), ajustando o pedido: {e}')
    raise ImagemRecusada(f'o gerador recusou essa imagem ({ultimo})')


PASTA_3D = RAIZ / 'r3d'
TEX_3D = 'https://raw.githubusercontent.com/mrdoob/three.js/r158/examples/textures/planets/'


def preparar_3d():
    """Instala o motor 3D (three.js + Chrome sem tela) e baixa as texturas dos planetas."""
    import shutil
    (PASTA_3D / 'tex').mkdir(parents=True, exist_ok=True)
    if not (PASTA_3D / 'node_modules' / 'puppeteer-core').exists():
        subprocess.run(['npm', 'init', '-y'], cwd=PASTA_3D, capture_output=True)
        subprocess.run(['npm', 'i', '--silent', '--no-audit', '--no-fund', 'three@0.158.0', 'puppeteer-core@21.11.0'], cwd=PASTA_3D, check=True)
    for arq in ('cena3d.html', 'cena3d.js'):
        shutil.copy(RAIZ / 'robo' / arq, PASTA_3D / arq)
    shutil.copy(PASTA_3D / 'node_modules' / 'three' / 'build' / 'three.min.js', PASTA_3D / 'three.min.js')
    if _fonte() != 'DejaVu Sans':
        shutil.copy(RAIZ / 'robo' / 'Montserrat-ExtraBold.ttf', PASTA_3D / 'Montserrat-ExtraBold.ttf')
    for t in ('earth_atmos_2048.jpg', 'earth_specular_2048.jpg', 'earth_normal_2048.jpg', 'earth_clouds_1024.png', 'moon_1024.jpg'):
        if not (PASTA_3D / 'tex' / t).exists():
            try:
                (PASTA_3D / 'tex' / t).write_bytes(http(TEX_3D + t, timeout=60)[0])
            except Exception as e:  # noqa: BLE001
                log('   textura', t, 'falhou:', e)
    log('motor 3D pronto')


def _curto(txt, n):
    """Corta a descrição no fim de uma frase/vírgula, sem passar de n caracteres."""
    txt = re.sub(r'\s+', ' ', str(txt or '')).strip()
    if len(txt) <= n:
        return txt
    corte = txt[:n]
    pos = max(corte.rfind('. '), corte.rfind(', '))
    return (corte[:pos] if pos > n // 2 else corte).rstrip(' ,.')


def prompt_cena(h, c, arte):
    """Prompt de uma cena com continuidade, curto (prompt longo demais é recusado ou piora a anatomia).
    Só frases positivas: o gerador não entende "não faça X" e às vezes faz justamente o X."""
    cena = _curto(c.get('imagem', ''), 300)
    outros = [e for e in (h.get('elenco') or []) if isinstance(e, dict) and e.get('nome') and e.get('nome', '').lower() in cena.lower()][:1]
    partes = [f"{arte}. Vertical 9:16 frame. Scene: {cena}.",
              f"Setting with a detailed background: {_curto(h['cenario'], 200)}." if h.get('cenario') else 'Detailed background environment.',
              f"Main character, same as the reference image (face, hair, clothes, colors): {_curto(h['personagem'], 220)}.",
              ' '.join(f"{e['nome']}: {_curto(e.get('visual', ''), 140)}." for e in outros),
              "Each person shown one time, natural anatomy with two arms and two hands each, clean simple poses.",
              f"Same art style in every scene: {arte}. Image without any text."]
    return ' '.join(x for x in partes if x)


def imagens():
    h = json.loads((SAIDA / 'historia.json').read_text(encoding='utf-8'))
    if env('ESTILO') in ESTILOS_3D:
        preparar_3d()
        return
    arte = ARTES.get(env('ARTE', 'Cartoon 2D'), ARTES['Cartoon 2D'])
    seed = sum(map(ord, h['titulo'])) % 100000
    ref = SAIDA / 'personagem.png'
    try:
        ref.write_bytes(cf_imagem(f"{arte}. Character reference, single character, full body, neutral pose, plain light background. {h['personagem']}", 768, 768, seed=seed))
        refs = [ref]
        log('personagem ok')
    except ImagemRecusada as e:
        refs = []; log('   personagem recusado, sigo sem referência:', e)
    feitas = []
    for i, c in enumerate(h['cenas']):
        p = prompt_cena(h, c, arte)
        destino = SAIDA / f'cena_{i:02d}.png'
        try:
            destino.write_bytes(cf_imagem(p, 720, 1280, refs=refs, seed=seed + i + 1))
            feitas.append(destino); log(f'cena {i + 1}/{len(h["cenas"])} ok')
        except ImagemRecusada as e:
            log(f'   cena {i + 1} recusada, vou reaproveitar outra cena:', e)
    if not feitas:
        raise RuntimeError('O gerador de imagens recusou todas as cenas dessa história. Tente outro tema ou mude o estilo do desenho.')
    for i in range(len(h['cenas'])):   # cena recusada usa a cena anterior (ou a seguinte) pro vídeo sair inteiro
        destino = SAIDA / f'cena_{i:02d}.png'
        if not destino.exists():
            antes = [f for f in feitas if int(f.stem[-2:]) < i]
            destino.write_bytes((antes[-1] if antes else feitas[0]).read_bytes())


VOZES = {'Masculina': 'Puck', 'Feminina': 'Kore', 'Grave de suspense': 'Charon', 'Jovem animada': 'Leda'}
TOM = {'drama': 'dramático e envolvente', 'medo': 'baixo, tenso e misterioso', 'risada': 'divertido e animado', 'emocao': 'emocionado e suave',
       'suspense': 'tenso e misterioso', 'romance': 'suave e apaixonado', 'motiva': 'inspirador e forte', 'curio': 'curioso e animado',
       'decide': 'envolvente, de quem conta uma fofoca', 'familia': 'indignado e engraçado', 'curio3d': 'curioso, animado e impressionado',
       'explica3d': 'curioso e didático, como quem revela um segredo', 'ese3d': 'curioso, intrigado e animado', 'historia3d': 'contador de história envolvente, com suspense e humor'}
ESTILOS_3D = ('curio3d', 'explica3d', 'ese3d', 'historia3d')


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
    # só o texto da história vai para a voz (instrução de estilo era lida em voz alta em alguns casos)
    (SAIDA / 'voz.pcm').write_bytes(tts('\n\n'.join(falas), VOZES.get(env('VOZ'), 'Puck')))
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


FONTSOURCE = 'https://cdn.jsdelivr.net/fontsource/fonts/'
GFONTS = 'https://raw.githubusercontent.com/google/fonts/main/'
# nome no app -> (arquivo fontsource, arquivo google fonts, tamanho da legenda)
FONTES_LEG = {'classica': ('montserrat@latest/latin-800-normal.ttf', '', 78),
              'impacto': ('anton@latest/latin-400-normal.ttf', 'ofl/anton/Anton-Regular.ttf', 92),
              'alta': ('bebas-neue@latest/latin-400-normal.ttf', 'ofl/bebasneue/BebasNeue-Regular.ttf', 100),
              'redonda': ('poppins@latest/latin-700-normal.ttf', 'ofl/poppins/Poppins-Bold.ttf', 74),
              'gibi': ('bangers@latest/latin-400-normal.ttf', 'ofl/bangers/Bangers-Regular.ttf', 96),
              'divertida': ('luckiest-guy@latest/latin-400-normal.ttf', 'apache/luckiestguy/LuckiestGuy-Regular.ttf', 72)}
# cor -> (cor do texto, cor da palavra em destaque) no formato do ASS (&HBBGGRR)
CORES_LEG = {'branco': ('&H00FFFFFF', '&H3FD2FF'), 'amarelo': ('&H003FD2FF', '&HFFFFFF'), 'verde': ('&H006AFF39', '&HFFFFFF'),
             'rosa': ('&H00D84FFF', '&HFFFFFF'), 'azul': ('&H00FFC34F', '&HFFFFFF'), 'laranja': ('&H001F8AFF', '&HFFFFFF')}
PASTA_FONTES = SAIDA / 'fontes'


def _baixar_fonte(chave):
    """Baixa a fonte pra saida/fontes e devolve o nome da família (o que o ASS usa)."""
    fs, gf, _ = FONTES_LEG.get(chave, FONTES_LEG['classica'])
    PASTA_FONTES.mkdir(exist_ok=True)
    arq = PASTA_FONTES / f'{chave}.ttf'
    if not arq.exists():
        for url in ([FONTSOURCE + fs] if fs else []) + ([GFONTS + gf] if gf else []):
            try:
                arq.write_bytes(http(url, timeout=60)[0]); break
            except Exception as e:  # noqa: BLE001
                log('   fonte', chave, 'falhou:', e)
    if not arq.exists():
        return None
    try:
        from PIL import ImageFont
        fam, est = ImageFont.truetype(str(arq)).getname()
        return fam if est in ('Regular', 'Normal', 'Book') else f'{fam} {est}'   # nome completo: o libass acha o peso certo
    except Exception:  # noqa: BLE001
        return None


def _fonte():
    """Fonte padrão (Montserrat ExtraBold), usada também pelos textos das cenas 3D."""
    nome = _baixar_fonte('classica')
    f = RAIZ / 'robo' / 'Montserrat-ExtraBold.ttf'
    if nome and not f.exists():
        f.write_bytes((PASTA_FONTES / 'classica.ttf').read_bytes())
    return nome or 'DejaVu Sans'


def _t(s):
    h, r = divmod(max(0, s), 3600); m, s2 = divmod(r, 60)
    return f'{int(h)}:{int(m):02d}:{s2:05.2f}'


def estilo_leg(fonte):
    """Fonte, tamanho e cores escolhidos no app (vale pra legenda e pro título das perguntas 3D)."""
    chave = LEG['fonte'] if LEG['fonte'] in FONTES_LEG else 'classica'
    nome_leg = fonte if chave == 'classica' else (_baixar_fonte(chave) or fonte)
    tam = FONTES_LEG[chave][2] if nome_leg != 'DejaVu Sans' else 74
    cor, cor_dest = CORES_LEG.get(LEG['cor'], CORES_LEG['branco'])
    return nome_leg, tam, cor, cor_dest


def legendas(tempos, fonte, arq):
    """Legenda da fala. Fonte e cor vêm da escolha no app (LEG); 'sem' = vídeo sem legenda."""
    nome_leg, tam, cor, cor_dest = estilo_leg(fonte)
    negrito = -1 if nome_leg == 'DejaVu Sans' else 0
    linhas = ['[Script Info]', 'ScriptType: v4.00+', 'PlayResX: 1080', 'PlayResY: 1920', '',
              '[V4+ Styles]', 'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
              f'Style: L,{nome_leg},{tam},{cor},{cor},&H00000000,&H64000000,{negrito},0,0,0,100,100,1,0,1,7,3,2,70,70,520,1', '',
              '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    volta = '&H' + cor[4:]
    for t in ([] if LEG['fonte'] == 'sem' else tempos):
        palavras = t['fala'].split()
        blocos = [palavras[i:i + 3] for i in range(0, len(palavras), 3)]
        dur = max(0.5, (t['fim'] - 0.4) - t['ini'])
        pesos = [sum(len(w) + 1 for w in b) for b in blocos]
        dest = (t.get('destaque') or '').strip('.,!?…').lower()
        ac = t['ini']
        for b, p in zip(blocos, pesos):
            d = dur * p / sum(pesos)
            txt = ' '.join(('{\\c' + cor_dest + '&}' + w + '{\\c' + volta + '&}') if dest and w.strip('.,!?…').lower() == dest else w for w in b)
            linhas.append(f'Dialogue: 0,{_t(ac)},{_t(ac + d)},L,,0,0,0,,{{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{txt}')
            ac += d
    arq.write_text('\n'.join(linhas) + '\n', encoding='utf-8')


def montar():
    tempos = json.loads((SAIDA / 'tempos.json').read_text(encoding='utf-8'))
    fonte = _fonte()
    partes = []
    if env('ESTILO') in ESTILOS_3D:
        h = json.loads((SAIDA / 'historia.json').read_text(encoding='utf-8'))
        if not (PASTA_3D / 'cena3d.html').exists():
            preparar_3d()
        plano = {'pasta': str(PASTA_3D), 'w': 576, 'h': 1024, 'fps': 30, 'partes': []}
        for i, t in enumerate(tempos):
            d = t['fim'] - t['ini'] + (0.4 if i < len(tempos) - 1 else 0)
            c = h['cenas'][i] if i < len(h['cenas']) else {}
            plano['partes'].append({'cena': c.get('cena3d', 'espaco'), 'texto': c.get('texto3d', ''), 'dur': round(d, 3), 'seed': sum(map(ord, h['titulo'])) % 997 + i,
                                    'spec': c.get('spec'), 'saida': str(SAIDA / f'parte_{i:02d}.mp4')})
            partes.append((SAIDA / f'parte_{i:02d}.mp4', d))
        (SAIDA / 'plano3d.json').write_text(json.dumps(plano, ensure_ascii=False), encoding='utf-8')
        subprocess.run(['node', str(PASTA_3D / 'cena3d.js'), str(SAIDA / 'plano3d.json')], cwd=PASTA_3D, check=True)
    for i, t in enumerate(tempos if not partes else []):
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
    try:
        perg = json.loads((SAIDA / 'historia.json').read_text(encoding='utf-8')).get('pergunta') if env('ESTILO') in ('explica3d', 'ese3d', 'historia3d') else ''
    except Exception:  # noqa: BLE001
        perg = ''
    if perg and LEG['fonte'] != 'sem':   # o título segue a escolha da legenda (sem legenda = sem título)
        ass = (SAIDA / 'leg.ass').read_text(encoding='utf-8')
        nome_t, tam_t, cor_t, _ = estilo_leg(fonte)
        neg_t = -1 if nome_t == 'DejaVu Sans' else 0
        estilo_t = f'Style: T,{nome_t},{round(tam_t * 76 / 78)},&H00111111,&H00111111,{cor_t},{cor_t},{neg_t},0,0,0,100,100,0,0,3,22,0,8,80,80,190,1'
        ass = ass.replace('\n\n[Events]', '\n' + estilo_t + '\n\n[Events]', 1)
        ass += f'Dialogue: 1,{_t(0)},{_t(tempos[-1]["fim"] + 1)},T,,0,0,0,,{perg.replace(chr(10), " ")}\n'
        (SAIDA / 'leg.ass').write_text(ass, encoding='utf-8')
    fontes = f":fontsdir={PASTA_FONTES}" if PASTA_FONTES.exists() else ''
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
