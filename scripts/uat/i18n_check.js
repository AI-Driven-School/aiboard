// 盤の T('日本語') のうち英語辞書に無いものを列挙する。0 件で exit 0
const h = require('fs').readFileSync(process.argv[2], 'utf8');
const m = h.match(/<script>([\s\S]*)<\/script>/)[1];
const start = m.indexOf('const EN = {'); let i = m.indexOf('{', start), d = 0, j = i;
for (; j < m.length; j++) { if (m[j] === '{') d++; else if (m[j] === '}') { d--; if (!d) break; } }
const EN = eval('(' + m.slice(i, j + 1) + ')');
const keys = new Set(); for (const r of m.matchAll(/\bT\('((?:[^'\\]|\\.)*)'\)/g)) keys.add(r[1]);
const miss = [...keys].filter(k => /[぀-ヿ一-鿿]/.test(k) && EN[k] === undefined);
console.log(JSON.stringify({keys: keys.size, missing: miss}));
process.exit(miss.length ? 1 : 0);
