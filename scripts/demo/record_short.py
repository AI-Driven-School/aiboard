"""Shorts 用（1080x1920・60 秒・日本語）の素材を撮る。デモモードなので顧客名・依頼文は架空。

16:9 の site/demo.mp4 とは別物。縦では盤全体を映すと文字が読めないので、**1 つの枠に寄せて**撮る。
話の順番（台本と同じ）:
  ①つかみ（文字）②盤＝実装/反証/判定の並走（実画面）③数字が縮んだ話（文字＋実画面）
  ④効いたのは独立していること（文字）⑤締め（文字）
録画（webm）は消さずに残す。MP4 にするのは呼び出し側（ffmpeg はフックで承認が要る）。
  python3 scripts/demo/record_short.py [出力先ディレクトリ]
"""
import os
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8791/?lang=ja&demo=1"
W, H = 1080, 1920

# 画面いっぱいの文字札（縦なので大きく）
CARD_JS = """([big, sub, note]) => { let c = document.getElementById('shCard');
  if (!c) { c = document.createElement('div'); c.id = 'shCard';
    Object.assign(c.style, {position:'fixed', inset:0, zIndex:100, display:'grid', placeItems:'center',
      background:'#0b0f17', transition:'opacity .3s', pointerEvents:'none', padding:'0 64px'});
    document.body.appendChild(c); }
  c.style.opacity = big ? 1 : 0;
  if (big) c.innerHTML = `<div style="text-align:center;font-family:'Hiragino Kaku Gothic ProN','Yu Gothic',sans-serif">
    <div style="font-size:84px;line-height:1.25;font-weight:900;color:#e7ecf3">${big}</div>
    ${sub ? `<div style="font-size:44px;line-height:1.5;color:#8c97a8;margin-top:36px">${sub}</div>` : ''}
    ${note ? `<div style="margin-top:44px;font:600 36px ui-monospace,Menlo,monospace;color:#f5c542">${note}</div>` : ''}</div>`; }"""

# 下の帯（実画面の上に出す説明）
CAP_JS = """(t) => { let c = document.getElementById('shCap');
  if (!c) { c = document.createElement('div'); c.id = 'shCap';
    Object.assign(c.style, {position:'fixed', left:'40px', right:'40px', bottom:'120px', zIndex:99, textAlign:'center',
      padding:'20px 24px', borderRadius:'16px', background:'rgba(8,11,18,.9)', border:'1px solid #243049',
      color:'#e7ecf3', font:'700 46px/1.4 "Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif', transition:'opacity .25s'});
    document.body.appendChild(c); }
  c.style.opacity = t ? 1 : 0; if (t) c.textContent = t; }"""


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
        pg.evaluate("""() => { const hide = ['#notifyWarn'];
            document.querySelectorAll('#toolbar .row.main > *').forEach(e => { if (/応答しません|通知が止め/.test(e.textContent || '')) e.style.display = 'none'; });
            hide.forEach(q => { const e = document.querySelector(q); if (e) e.style.display = 'none'; }); }""")
        pg.evaluate(CARD_JS, ["「人が戻れたのは 63 件」", "その大半は、<br>そもそも人が居ませんでした", ""])
        t0 = time.time()

        def at(sec):
            time.sleep(max(0, sec - (time.time() - t0)))

        # ② 盤（実画面）。縦では全体を映すと読めないので 1 つの枠に寄せる
        at(5.0)
        pg.evaluate(CARD_JS, ["", "", ""])
        # 縦画面では 1 枚も読めないので、判断待ちのカードがある枠まで寄る（board.fitTo は枠の配列を取る）
        # 会話ビューを見せるので、**記録のあるセッション**を選ぶ（デモの作り物カードは会話が空になる）
        pg.evaluate("""() => { const s = (board.snap().sessions || []).filter(x => x.transcript && x.sid && (x.state === '確認待ち' || x.state === '返答待ち'));
            window._shConv = s.length ? s[0].sid : null; }""")
        pg.evaluate("""() => { const el = (window._shConv && document.querySelector(`.card.live[data-id="${window._shConv}"]`))
                || document.querySelector('.card.live.turn') || document.querySelector('.card.live.yourturn') || document.querySelector('.card.live');
            window._shCard = el && el.dataset.id;
            const v = (board.vis() || []).find(x => x.id === window._shCard);
            const f = (board.frames() || []).find(fr => (fr.nodes || []).includes(v));
            if (f) board.fitTo([f]); }""")
        pg.wait_for_timeout(900)
        pg.evaluate("() => board.zoomBy(1.9)")   # 枠に合わせると 64%。縦画面でカードの字が読めるのは 120% 前後
        pg.evaluate(CAP_JS, "AI の作業台を作っています")
        at(8.5); pg.evaluate(CAP_JS, "実装＝Claude　反証＝Codex　判定＝Jev")
        at(12.5); pg.evaluate(CAP_JS, "赤＝判断待ち　黄＝あなたの番　緑＝作業中")
        # 会話ビュー（縦だと全幅になり、文字がいちばん読める）
        at(16.0); pg.evaluate("() => board.select(window._shConv || window._shCard)"); pg.evaluate(CAP_JS, "押すと会話。その場で答えられます")
        # ③ 数字が違っていた話（Codex の指摘で全部言い直した。数字は 2026-09-23 の実測）
        at(20.0); pg.evaluate(CAP_JS, ""); pg.evaluate("board.closePanel && board.closePanel()")
        pg.evaluate(CARD_JS, ["こう書いて公開しました", "「認証エラーで 989 件止まり<br>人が戻れたのは 63 件」", ""])
        at(26.5); pg.evaluate(CARD_JS, ["別のモデルの指摘", "「重複を除いた件数か。<br>母集団は何か」", ""])
        at(32.5); pg.evaluate(CARD_JS, ["分け直しました", "止まり 1,656 件のうち<br>無人実行 881・判別不能 299", "人が座っていたのは 476"])
        at(39.0); pg.evaluate(CARD_JS, ["認証だけで見ると", "1,040 件のうち<br>人が座っていたのは 81 件", "公開していた数字は人の話ではなかった"])
        at(45.5); pg.evaluate(CARD_JS, ["「取り戻した時間」も却下", "「止まり→次の発言は<br>離席も含む経過時間だ」", ""])
        # ④ まとめ（1 件の経験として言う。比較実験はしていない）
        at(51.0); pg.evaluate(CARD_JS, ["自分では気づけませんでした", "別のモデルに読ませて<br>初めて出てきました", "※ 1 件の経験。比較実験はしていません"])
        # ⑤ 締め
        at(56.0); pg.evaluate(CARD_JS, ["AIBoard", "どの AI が待っているかの盤", "MIT · github.com/AI-Driven-School/aiboard"])
        at(61.0)
        video = pg.video.path()
        ctx.close(); b.close()
    print("raw:", video)
    print("dir:", outdir)


if __name__ == "__main__":
    main()
