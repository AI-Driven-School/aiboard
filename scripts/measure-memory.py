"""AIBoard のメモリ内訳を測る(アプリ本体・WebKit の 3 プロセス・盤サーバ)。

  python3 scripts/measure-memory.py [秒]

計測用に .app を 1 つ起動して測り、終わったら自分で終了する(動いているアプリには触れない)。
RSS(ps)と footprint(実メモリ)の両方を出す。RSS は圧縮メモリを隠すので、判断は footprint で行う。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAIT = int(sys.argv[1]) if len(sys.argv) > 1 else 25
PORT = os.environ.get("MEASURE_PORT", "8794")


def procs():
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)", line)
        if m:
            rows.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "rss_mb": round(int(m.group(3)) / 1024), "cmd": m.group(4)})
    return rows


def footprint_mb(pid):
    r = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True)
    m = re.search(r"Footprint:\s+([\d.]+)\s*(MB|GB|KB)", r.stdout)
    if not m:
        return None
    v = float(m.group(1))
    return round(v * (1024 if m.group(2) == "GB" else 1 if m.group(2) == "MB" else 1 / 1024))


def main():
    data = tempfile.mkdtemp(prefix="aiboard-mem-")
    for n in ("index.db", "clients.json"):
        src = os.path.join(os.path.expanduser("~"), ".aiboard", n)
        if os.path.exists(src):
            os.symlink(src, os.path.join(data, n))
    env = dict(os.environ, AIBOARD_DATA=data, OVERVIEW_PORT=PORT, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=os.path.join(ROOT, "board"),
               AIBOARD_JS_TEST=os.path.join(data, "out.json"), AIBOARD_JS="return 1", AIBOARD_JS_WAIT=str(WAIT + 10))
    app = subprocess.Popen([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(WAIT)
    rows = procs()
    mine = [r for r in rows if r["pid"] == app.pid]
    kids = [r for r in rows if r["ppid"] == app.pid or r["ppid"] == 1]
    def pick(pat, rs):
        return [r for r in rs if re.search(pat, r["cmd"])]
    groups = {
        "app (Swift + 端末)": pick(r"AIBoard\.app/Contents/MacOS/AIBoard$", mine),
        "WebKit WebContent (盤の画面)": pick(r"WebKit\.WebContent", rows),
        "WebKit GPU": pick(r"WebKit\.GPU", rows),
        "WebKit Networking": pick(r"WebKit\.Networking", rows),
        f"盤サーバ (python, :{PORT})": [r for r in rows if "overview_server.py" in r["cmd"] and data in (subprocess.run(["/bin/ps", "eww", "-p", str(r["pid"])], capture_output=True, text=True).stdout or "")],
    }
    out, total_rss, total_fp = [], 0, 0
    for name, rs in groups.items():
        for r in rs:
            fp = footprint_mb(r["pid"])
            out.append({"group": name, "pid": r["pid"], "rss_mb": r["rss_mb"], "footprint_mb": fp})
            total_rss += r["rss_mb"]
            total_fp += fp or 0
    app.terminate()
    print(json.dumps({"measured_at_wait_s": WAIT, "rows": out, "total_rss_mb": total_rss, "total_footprint_mb": total_fp}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
