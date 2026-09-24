"""Shorts 用（1080x1920・約 38 秒・日本語）の素材を撮る。盤の画面はデモモードなので顧客名は架空。

2026-09-23 に全面的に作り直した。前の版を codex に 13 フレーム見せて判定させたところ、
出せない理由が次のように出たため（実測値も一緒に渡した）:
  - **訂正の話なのに数字が閉じていない**。22 秒で「989 件」46 秒で「1,040 件」と変わり、
    「戻れた」と「座っていた」という別の指標に切り替わるのに、画面に説明が無い
  - 盤の画面が読めない。枠に寄せて 1.9 倍したらカードが左右で切れ、語頭が欠けていた
    （「st on checkout」「xport for reports」）。会話パネルは右半分で極小
  - 「63／件」「気づけませんで／した」と、見出しの改行が語の途中で割れていた
  - 60 秒のうち 29 秒（48%）が静止。無音なのに同じ画面を長く置いていた

作り直しの方針:
  1. **話を 1 本にする。** 公開した数字と訂正後を並べるのはやめた。元データがもう無く
     （Claude Code は cleanupPeriodDays 既定 30 日で会話記録を消す）、対比を正直に見せられないため。
     今日測った 1 組の数字だけを、足し算が合う形で出す。
  2. **枠でなくカード 1 枚に寄せる**（fitTo は任意のノードを取る）。縦画面でも切れない。
  3. 改行は自分で入れる（<br>）。数字と単位は nowrap で割らない。
  4. 尺は 38 秒。無音で置いておく時間を作らない。

2026-09-24、作り直した版をもう一度 codex に見せて出た指摘も入れた:
  - **カウントアップをやめた。** 静止を減らすつもりで入れたが、途中のフレームが「104 回」のような
    集計として矛盾した数字を映す。サムネイルや一時停止でそこが取られる。静止率より害が大きい
  - **「351 時間」は延べだった。** 1 件ずつ足していたので、同時に 2 本止まっていれば 2 倍に乗る。
    実測すると 30 分以上の 110 件は延べ 387 時間・重なりを 1 回だけ数えた実時間は 194 時間で、半分が重複。
    人が失った時間には読めないので、実時間を出して延べは併記にした
  - **「気づけた」ではなく「反応するまで」。** 記録にあるのは次の発言だけで、見て放置したかは分からない
  - 静止率は追わない（codex: 「0.8% を目標にする根拠はない。文字に揺れを足しても説明は改善しない」）

  python3 scripts/demo/record_short.py [出力先ディレクトリ]
録画（webm）は消さずに残す。MP4 にするのは呼び出し側（ffmpeg はフックで承認が要る）。
"""
import os
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8791/?lang=ja&demo=1"
W, H = 1080, 1920
FONT = "'Hiragino Kaku Gothic ProN','Yu Gothic',sans-serif"

# 画面いっぱいの文字札。num を渡すとその数字を 0 から数え上げる（静止を減らす）
CARD_JS = """([big, sub, note, num]) => { let c = document.getElementById('shCard');
  if (!c) { c = document.createElement('div'); c.id = 'shCard';
    Object.assign(c.style, {position:'fixed', inset:0, zIndex:100, display:'grid', placeItems:'center',
      background:'#0b0f17', transition:'opacity .25s', pointerEvents:'none', padding:'0 72px'});
    document.body.appendChild(c); }
  c.style.opacity = big ? 1 : 0;
  if (!big) return;
  c.innerHTML = `<div style="text-align:center;font-family:FONT;max-width:900px">
    <div id="shBig" style="font-size:78px;line-height:1.3;font-weight:900;color:#e7ecf3;text-wrap:balance">${big}</div>
    ${sub ? `<div style="font-size:42px;line-height:1.55;color:#97a3b6;margin-top:34px">${sub}</div>` : ''}
    ${note ? `<div style="margin-top:40px;font:600 34px/1.5 ui-monospace,Menlo,monospace;color:#f5c542">${note}</div>` : ''}</div>`;
  if (num) { const el = c.querySelector('[data-n]'); if (el) {
    const t0 = performance.now(), dur = 700;
    const tick = () => { const p = Math.min(1, (performance.now() - t0) / dur);
      el.textContent = Math.round(num * (1 - Math.pow(1 - p, 3))).toLocaleString('en-US');
      if (p < 1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick); } } }""".replace("FONT", FONT)

# 下の帯（実画面の上に出す説明）
CAP_JS = """(t) => { let c = document.getElementById('shCap');
  if (!c) { c = document.createElement('div'); c.id = 'shCap';
    Object.assign(c.style, {position:'fixed', left:'40px', right:'40px', bottom:'110px', zIndex:99, textAlign:'center',
      padding:'20px 24px', borderRadius:'16px', background:'rgba(8,11,18,.92)', border:'1px solid #243049',
      color:'#e7ecf3', fontFamily:"FONT", fontSize:'44px', fontWeight:'700', lineHeight:'1.4',
      transition:'opacity .2s'});
    document.body.appendChild(c); }
  c.style.opacity = t ? 1 : 0; if (t) c.innerHTML = t; }""".replace("FONT", FONT)

# 盤の画面はデモ用の作り物。数字は実測なので、混ざらないように画面に書いておく
MARK_JS = """(on) => { let m = document.getElementById('shMark');
  if (!m) { m = document.createElement('div'); m.id = 'shMark';
    Object.assign(m.style, {position:'fixed', top:'120px', right:'40px', zIndex:99, padding:'10px 18px',
      borderRadius:'999px', background:'rgba(8,11,18,.9)', border:'1px solid #3a4considered',
      color:'#97a3b6', fontFamily:"FONT", fontSize:'28px', fontWeight:'600'});
    m.textContent = '画面はデモ用の作り物データ';
    document.body.appendChild(m); }
  m.style.opacity = on ? 1 : 0; }""".replace("FONT", FONT).replace("#3a4considered", "#2b3750")


def wait_cards(pg, n=2, limit=60):
    """カードが出るまで待つ。盤は CSP で eval を禁じているので wait_for_function(文字列)は使えない
    (2026-09-23: EvalError。evaluate に関数の形で渡して自分で回す)。"""
    end = time.time() + limit
    while time.time() < end:
        if pg.evaluate("() => document.querySelectorAll('.card.live').length") > n:
            return True
        pg.wait_for_timeout(500)
    raise RuntimeError("カードが出ない(盤サーバは動いていますか)")


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="aiboard-short-")
    os.makedirs(outdir, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, locale="ja-JP",
                            record_video_dir=outdir, record_video_size={"width": W, "height": H})
        pg = ctx.new_page()
        pg.goto(URL)
        pg.evaluate("localStorage.setItem('tour_done','1'); localStorage.setItem('mode','now')")
        pg.reload()
        wait_cards(pg)
        # 警告の帯（iTerm 応答なし・通知が切れている）は宣伝の画では邪魔なので隠す。中身は変えない
        pg.evaluate("""() => { document.querySelectorAll('#toolbar .row.main > *').forEach(e => {
              if (/応答しません|通知が止め/.test(e.textContent || '')) e.style.display = 'none'; });
            const n = document.querySelector('#notifyWarn'); if (n) n.style.display = 'none'; }""")
        pg.evaluate(CARD_JS, ["AI が止まっていた時間を<br>数えました", "この Mac・30 日ぶん", "", 0])
        t0 = time.time()

        def at(sec):
            time.sleep(max(0, sec - (time.time() - t0)))

        # ① 数字（1 回の計測から。428 のうち 214 が 10 分以内、110 が 30 分以上。110 ⊂ 428）
        at(4.0)
        pg.evaluate(CARD_JS, ["人が座っていた対話で<br>428 回 止まりました",
                              "10 分以内に次の発言があったのは 214 回",
                              "「気づくまで」ではなく「反応するまで」", 0])
        at(9.5)
        pg.evaluate(CARD_JS, ["そのうち 110 回は<br>30 分以上あいた",
                              "どれかが止まっていた実時間は<br>夜を除いて 194 時間",
                              "1 件ずつ足すと 387 時間。半分は重なり", 0])
        # ② 盤（実画面）。枠でなく**カード 1 枚**に寄せるので縦画面でも切れない
        at(15.0)
        pg.evaluate(CARD_JS, ["", "", "", 0])
        pg.evaluate(MARK_JS, True)
        pg.evaluate("() => board.fitAll()")
        pg.evaluate(CAP_JS, "止まった AI は、何も言いません")
        at(19.0)
        pg.evaluate("""() => { const v = board.vis() || [];
            const pick = v.find(n => n.live && /判断待ち|確認待ち/.test(n.state || ''))
                      || v.find(n => n.live && /あなたの番|返答待ち/.test(n.state || ''))
                      || v.find(n => n.live);
            if (pick) board.fitTo([pick]); }""")
        pg.evaluate(CAP_JS, "どれが待っているかを、1 枚にしました")
        at(23.5)
        pg.evaluate(CAP_JS, "赤＝判断待ち　黄＝あなたの番　緑＝作業中")
        at(27.0)
        pg.evaluate("() => board.fitAll()")
        pg.evaluate(CAP_JS, "端末は今までどおり。盤は隣に置いておくもの")
        # ③ 限界を先に言う（この数字は放置の上限値で、後から検算もできない）
        at(31.0)
        pg.evaluate(CAP_JS, "")
        pg.evaluate(MARK_JS, False)
        pg.evaluate(CARD_JS, ["これは放置の“上限値”です",
                              "席を外していた時間も混ざります。<br>会話記録は 30 日で消えるので、<br>この数字は後から検算できません",
                              "2026-09-24 測定 / 1 台ぶん", 0])
        # ④ 締め
        at(35.0)
        pg.evaluate(CARD_JS, ["AIBoard", "動いている AI を 1 枚の盤に", "MIT · github.com/AI-Driven-School/aiboard", 0])
        at(38.5)
        video = pg.video.path()
        ctx.close()
        b.close()
    print("raw:", video)
    print("dir:", outdir)


if __name__ == "__main__":
    main()
