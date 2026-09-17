"""30 秒のデモ動画を撮る(デモモード: 顧客名・依頼文は架空)。

Playwright で盤を 1600x1000 で開き、絵コンテの 6 カットを JS で順に動かしながら録画し、ffmpeg で MP4(H.264, 30fps)にする。
カット: ①題字 ②盤の全体→状態の色 ③判断待ちの枠へ寄る ④押す→会話ビューで返信を打つ ⑤「過去」で 30 日 ⑥締め
元の録画(webm)は消さずに残す。
使い方: python3 scripts/demo/record.py [出力.mp4]
"""
import os
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "site", "demo.mp4")
URL = "http://127.0.0.1:8791/?lang=en&demo=1"
W, H = 1600, 1000

CAPTION_JS = """(t) => { let c = document.getElementById('demoCap');
  if (!c) { c = document.createElement('div'); c.id = 'demoCap'; Object.assign(c.style, {position:'fixed', left:'50%', bottom:'54px', transform:'translateX(-50%)', zIndex:99,
    padding:'12px 22px', borderRadius:'10px', background:'rgba(10,10,10,.88)', border:'1px solid #3A3A3A', color:'#F2F2F2', font:'600 26px -apple-system,system-ui,sans-serif', transition:'opacity .25s', whiteSpace:'nowrap'}); document.body.appendChild(c); }
  c.style.opacity = t ? 1 : 0; if (t) c.textContent = t; }"""
CARD_JS = """([title, sub, cmd]) => { let c = document.getElementById('demoCard');
  if (!c) { c = document.createElement('div'); c.id = 'demoCard'; Object.assign(c.style, {position:'fixed', inset:0, zIndex:100, display:'grid', placeItems:'center', background:'#161616', transition:'opacity .35s', pointerEvents:'none'}); document.body.appendChild(c); }
  c.style.opacity = title ? 1 : 0;
  if (title) c.innerHTML = `<div style="text-align:center;font-family:-apple-system,system-ui,sans-serif">
    <div style="display:inline-grid;grid-template-columns:1fr 1fr;gap:6px;width:64px;height:64px;margin-bottom:22px"><b style="background:#0D99FF;border-radius:6px"></b><b style="background:#9747FF;border-radius:6px"></b><b style="background:#14AE5C;border-radius:6px"></b><b style="background:#F24822;border-radius:6px"></b></div>
    <div style="font-size:64px;font-weight:700;color:#fff">${title}</div>
    <div style="font-size:28px;color:#B3B3B3;margin-top:12px">${sub}</div>
    ${cmd ? `<div style="margin-top:34px;font:500 26px ui-monospace,Menlo,monospace;color:#fff;background:#0B0B0B;border:1px solid #3A3A3A;border-radius:10px;padding:14px 22px;display:inline-block">${cmd}</div>` : ''}</div>`; }"""


def main():
    raw_dir = tempfile.mkdtemp(prefix="aiboard-demo-", dir=os.environ.get("AIBOARD_DEMO_TMP"))
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, locale="en-US",
                            record_video_dir=raw_dir, record_video_size={"width": W, "height": H})
        pg = ctx.new_page()
        pg.goto(URL)
        pg.evaluate("localStorage.setItem('tour_done','1'); localStorage.setItem('mode','now')")
        pg.reload()
        pg.wait_for_function("document.querySelectorAll('.card.live').length>2", timeout=60000)
        pg.evaluate(CARD_JS, ["AIBoard", "The whiteboard for your coding agents", ""])
        lead = time.time()   # ここから 30 秒が本編(前の読み込み待ちは切る)
        t0 = time.time()

        def at(sec):
            time.sleep(max(0, sec - (time.time() - t0)))

        at(3.0); pg.evaluate(CARD_JS, ["", "", ""]); pg.evaluate("document.querySelector('#btnFit').click()")
        pg.evaluate(CAPTION_JS, "Every Claude Code & Codex session, grouped by client")
        at(6.5); pg.evaluate(CAPTION_JS, "Yellow = your turn · Red = needs a decision · Green = working")
        at(9.5); pg.evaluate(CAPTION_JS, "")
        pg.evaluate("""() => { const el = document.querySelector('.card.turn') || document.querySelector('.card.yourturn') || document.querySelector('.card.live');
            const v = window._vis.find(x => x.id === el.dataset.id); const f = window._frames.find(fr => fr.nodes.includes(v)); board.fitTo([f]); window._demoCard = el.dataset.id; }""")
        at(12.5); pg.evaluate(CAPTION_JS, "Click a card — read the conversation, reply in one line")
        pg.evaluate("board.select(window._demoCard)")
        at(15.0); pg.type("#sendIn", "run the tests again and fix the flaky one", delay=35)
        at(20.0); pg.evaluate("board.closePanel()"); pg.evaluate(CAPTION_JS, "History: your last 30 days, one click to resume")
        pg.click("[data-mode=history]")
        at(22.5); pg.evaluate("board.zoomBy(0.7)")
        at(25.0); pg.evaluate(CAPTION_JS, ""); pg.evaluate(CARD_JS, ["Native macOS · open source · local only", "No telemetry. Runs next to iTerm.", "git clone … && ./make_app.sh"])
        at(30.0)
        video = pg.video.path()
        ctx.close(); b.close()
    # 録画の頭(ページ読み込み)を切る: 録画開始からの経過を実測値で
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", video],
                               capture_output=True, text=True).stdout.strip() or 0)
    skip = max(0.0, dur - 30.2)
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{skip:.2f}", "-i", video, "-t", "30", "-vf", "fps=30,scale=1600:-2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-preset", "slow", "-movflags", "+faststart", "-an", OUT], check=True)
    print("raw:", video, f"({dur:.1f}s, skipped {skip:.1f}s)")
    print("out:", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
