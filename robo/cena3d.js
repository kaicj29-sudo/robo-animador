// Renderiza as cenas do estilo "Curiosidade 3D" quadro a quadro num Chrome sem tela e entrega um MP4 por cena.
// Uso: node cena3d.js plano.json
// plano.json: {"pasta": "...", "w": 720, "h": 1280, "fps": 30, "partes": [{"cena": "terra", "texto": "8 MIN", "dur": 5.2, "seed": 3, "saida": ".../parte_00.mp4"}]}
const fs = require('fs'), path = require('path'), {spawn} = require('child_process');
const puppeteer = require('puppeteer-core');

function chrome() {
  const c = [process.env.CHROME, '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'].filter(Boolean);
  for (const p of c) if (fs.existsSync(p)) return p;
  throw new Error('Chrome não encontrado');
}

(async () => {
  const plano = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const {w = 720, h = 1280, fps = 30} = plano;
  const nav = await puppeteer.launch({executablePath: chrome(), headless: 'new',
    args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--allow-file-access-from-files', `--window-size=${w},${h}`]});
  const pg = await nav.newPage();
  await pg.setViewport({width: w, height: h});
  pg.on('pageerror', (e) => console.log('   página:', e.message));
  for (const [i, p] of plano.partes.entries()) {
    const ini = Date.now();
    await pg.goto('file://' + path.join(plano.pasta, 'cena3d.html') + `?w=${w}&h=${h}`, {waitUntil: 'load'});
    const usada = await pg.evaluate((c, t, s, d, e) => window.setup(c, t, s, d, e), p.cena, p.texto || '', p.seed || i + 1, p.dur, p.spec || null);
    const n = Math.max(2, Math.round(p.dur * fps));
    const ff = spawn('ffmpeg', ['-y', '-v', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-',
      '-vf', 'scale=1080:1920:flags=lanczos,format=yuv420p', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19', p.saida], {stdio: ['pipe', 'inherit', 'inherit']});
    for (let f = 0; f < n; f++) {
      const url = await pg.evaluate((t) => window.frame(t), f / fps);
      if (!ff.stdin.write(Buffer.from(url.slice(url.indexOf(',') + 1), 'base64'))) await new Promise((ok) => ff.stdin.once('drain', ok));
    }
    ff.stdin.end();
    await new Promise((ok, erro) => ff.on('close', (c) => (c === 0 ? ok() : erro(new Error('ffmpeg falhou na cena ' + (i + 1))))));
    console.log(`cena 3D ${i + 1}/${plano.partes.length} (${usada}) ok em ${((Date.now() - ini) / 1000).toFixed(0)}s`);
  }
  await nav.close();
})().catch((e) => { console.error('ERRO 3D:', e.message); process.exit(1); });
