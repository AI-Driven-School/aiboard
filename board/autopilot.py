"""真理値表が決めた一手のうち、**システムがやってよいものだけ**を実行する。

やれるのは今のところ 1 つだけ: 上限で止まった会話に「解除時刻に自動で続ける」予約を入れる。
ログイン・鍵・請求・判断への返事・信頼の確認は**人しかできない**ので、システムは触らない（決して勝手に答えない）。

約束:
  - 既定はオフ。config.json の {"autopilot": {"resume_when_reset": true}} で入れる
  - 同じ会話に二重に予約しない。作った予約は「1 回だけ」で、走ったら自分を止まる
  - やったこと・やらなかった理由を全部 ~/.aiboard/actions.jsonl に残す（盤の「やったこと」で読める）
"""
import json
import os
import time

LOG = "actions.jsonl"
KEYS = ("resume_when_reset",)


def log_path():
    import aiboard_paths as ap
    return ap.data(LOG)


def policy():
    import aiboard_paths as ap
    c = (ap.config() or {}).get("autopilot") or {}
    return {k: bool(c.get(k)) for k in KEYS}


def set_policy(patch):
    import aiboard_paths as ap
    p = ap.data("config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    a = dict(cfg.get("autopilot") or {})
    for k in KEYS:
        if k in (patch or {}):
            a[k] = bool(patch[k])
    cfg["autopilot"] = a
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    ap._cfg = None
    return policy()


def note(kind, sid, text, done=True, **extra):
    """やったこと・やらなかった理由を残す。盤の「やったこと」はこれを読む。"""
    rec = {"t": time.time(), "kind": kind, "sid": sid, "text": text, "done": bool(done), **extra}
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return rec


def recent(limit=50):
    try:
        with open(log_path(), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []
    return rows[-limit:][::-1]


def tick(sessions, now=None):
    """snapshot のたびに呼ぶ。表の一手のうち、やってよいものだけ実行して、やったことを返す。"""
    import overview
    now = now or time.time()
    pol = policy()
    did = []
    for s in sessions or []:
        ui = s.get("ui") or {}
        if ui.get("auto") != "resume_when_reset":
            continue
        sid, lim = s.get("sid") or "", (s.get("limit") or {})
        at = lim.get("resets_at")
        if not sid or sid.startswith("tty:") or not at:
            continue
        if not pol.get("resume_when_reset"):
            continue        # 既定はオフ。人が押すまで何もしない
        jobs = overview.read_schedule()
        if any(j.get("resume") == sid and j.get("enabled", True) and not j.get("last_run") for j in jobs):
            continue        # もう予約してある(二重に入れない)
        try:
            job = overview.save_job({"key": s.get("project_hint") or s.get("project") or "",
                                     "prompt": "上限が解けたので続けてください。直前までの作業をそのまま進めてください。",
                                     "cwd": s.get("cwd") or "", "ai": "Codex" if (s.get("ai") or "").startswith("Codex") else "Claude",
                                     "once_at": float(at) + 60, "resume": sid})
        except ValueError as e:
            did.append(note("resume_when_reset", sid, f"予約できなかった: {e}", done=False))
            continue
        did.append(note("resume_when_reset", sid,
                        f"上限が解ける {time.strftime('%H:%M', time.localtime(float(at) + 60))} に、この会話の続きを自動で開く予約を入れた",
                        done=True, job=job["id"]))
    return did
