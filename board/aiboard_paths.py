"""aiboard_paths.py — AIBoard の置き場所を 1 か所で決める。

  コード   : このフォルダ(リポジトリの board/、または .app の Contents/Resources/board)
  データ   : ~/.aiboard/(索引 index.db・端末台帳 app_panes.json・ログ・pid)。AIBOARD_DATA で変えられる
  設定     : ~/.aiboard/clients.json(顧客の判定規則。無ければ空)・~/.aiboard/config.json(遠隔ホストなど)
Claude Code / Codex 自身のファイル(~/.claude, ~/.codex)は読むだけで、場所は変えない。
"""
import json
import os

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("AIBOARD_DATA") or os.path.join(HOME, ".aiboard")
os.makedirs(DATA, exist_ok=True)


def data(name):
    return os.path.join(DATA, name)


_cfg = None


def config():
    """~/.aiboard/config.json。無ければ空。壊れていれば空(黙って落ちない: 理由は _config_error に残す)。"""
    global _cfg
    if _cfg is None:
        _cfg = {}
        p = data("config.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    _cfg = json.load(f)
            except (OSError, ValueError) as e:
                _cfg = {"_config_error": f"{p}: {e}"}
    return _cfg


def clients_file():
    """顧客の判定規則。利用者のもの(~/.aiboard/clients.json)が無ければ、同梱の空の例。"""
    p = data("clients.json")
    return p if os.path.exists(p) else os.path.join(HERE, "clients.example.json")
