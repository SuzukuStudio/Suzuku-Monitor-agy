import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import esbuild from 'esbuild';

const __dirname = dirname(fileURLToPath(import.meta.url));
const outHtmlPath = resolve(__dirname, '../plugin/ui/app.html');

const result = await esbuild.build({
  entryPoints: [resolve(__dirname, 'index.ts')],
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2022'],
  write: false,
});

const jsCode = result.outputFiles[0].text;
const cssCode = readFileSync(resolve(__dirname, 'styles.css'), 'utf8');

const html = `<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Suzuku Monitor</title>
<style>
${cssCode}
</style>
</head>
<body>
<div id="app"></div>
<script>
${jsCode}
</script>
</body>
</html>
`;

mkdirSync(dirname(outHtmlPath), { recursive: true });
writeFileSync(outHtmlPath, html, 'utf8');
console.log(`Built ${outHtmlPath} (${Buffer.byteLength(html, 'utf8')} bytes)`);
