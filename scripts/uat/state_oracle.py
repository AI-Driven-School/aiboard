"""状態の期待値 — docs/state-spec.md の 3 節を、board/decide.py を見ずに書き下ろしたもの。

decide.py と同じ表を写すと、表の誤りごと「一致」してしまう。ここでは仕様の行を if で順に書き、
出力は仕様の列（kind / badge / 音 / 小窓 / Dock / 通知 / attn / 一手）だけを返す。試験 SM-01 が全組み合わせで突き合わせる。
"""

ALERT = dict(sound=True, popup=True, dock=True, notify=True, attn=0)
QUIET = dict(sound=False, popup=False, dock=False, notify=False, attn=None)


def expect(proc=True, stop="", hook="", loop=False, trust=None, idle=None, stale=None, answerable=True, others=0):
    def o(kind, badge, action, **ch):
        return {"kind": kind, "badge": badge, "action": action, **ch}

    if stop == "apikey":
        return o("auth", "auth", "key", **ALERT)
    if stop in ("login", "oauth", "expired"):
        return o("auth", "auth", "login", **ALERT)
    if stop == "credits":
        return o("billing", "billing", "billing", **ALERT)
    if stop == "error":
        return o("error", "err", "resume", **dict(ALERT, popup=False))
    if proc and hook == "waiting":
        if answerable:
            return o("need", "need", "answer", **ALERT)
        return o("need", "need", "look", **dict(ALERT, popup=False))
    if proc and trust == "seen":
        return o("need", "need", "trust", **ALERT)
    if stop in ("five_hour", "seven_day", "overage"):
        return o("limited", "lim", "move_or_wait", **QUIET)
    if stop == "limit_reset":
        return o("resumable", "turn", "reply", **dict(QUIET, notify=True, attn=1))
    if not proc:
        return o("ended", "", "resume", **QUIET)
    if trust == "suspect" and hook == "":
        return o("need", "need", "trust", **dict(QUIET, attn=2))
    if stop == "transient":
        if stale is not None and stale >= 600:
            return o("yourturn", "turn", "reply", **dict(QUIET, attn=0))
        return o("work", "", "none", **QUIET)
    if hook == "working":
        return o("work", "", "none", **QUIET)
    if hook == "replied":
        if loop:
            return o("loop", "", "none", **QUIET)
        return o("yourturn", "turn", "reply", **dict(QUIET, attn=1))
    if hook == "" and idle is not None and idle < 5:
        return o("work", "", "none", **QUIET)
    if hook == "" and idle is not None and idle >= 30:
        return o("idle", "look", "look", **dict(QUIET, attn=2))
    return o("starting", "", "look", **dict(QUIET, attn=2))


# 全組み合わせの値域（仕様 1 節）。数値は境目の前後を入れる
DOMAIN = {
    "proc": (True, False),
    "stop": ("", "five_hour", "seven_day", "overage", "limit_reset", "credits", "login", "oauth", "expired", "apikey", "transient", "error"),
    "hook": ("", "working", "waiting", "replied"),
    "loop": (False, True),
    "trust": (None, "suspect", "seen"),
    "idle": (None, 0, 4.9, 5, 29.9, 30, 600),
    "stale": (None, 0, 599, 600),
    "answerable": (True, False),
    "others": (0, 2),
}
