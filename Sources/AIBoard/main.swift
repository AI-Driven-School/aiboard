import AppKit
import WebKit
import UserNotifications
import GhosttyTerminal

// AIBoard — 盤(全体地図)と端末を1つにした Mac アプリ。iTerm の代わりに使う。
//
// 左: 盤(~/.claude/tools の overview サーバを WebKit で表示)  右: 端末(libghostty。アプリが PTY を持つ)
// アプリが端末を持つので、AppleScript 経由の画面取得や「ウィンドウ番号-タブ番号」の宛先ずれが構造ごと無くなる。
// 盤との連絡:
//   アプリ → 盤側の道具: ~/.claude/tools/app_panes.json に {pane, tty, ...} を書く。cs.py は tty でプロセスを引くので、
//                       iTerm のタブと同じ扱いで状態(作業中/返答待ち/モデル/顧客)が出る。タブ番号は "0-<pane>"。
//   盤 → アプリ: window.webkit.messageHandlers.aiboard.postMessage({type: "focus" | "resume" | "sessions", ...})

let HOME = NSHomeDirectory()
let STATE_DIR = (ProcessInfo.processInfo.environment["AIBOARD_DATA"] ?? HOME + "/.aiboard")   // 盤サーバ(board/aiboard_paths.py)と同じ置き場
let PANES_FILE = STATE_DIR + "/app_panes.json"
let STATE_FILE = STATE_DIR + "/state.json"
/// 盤サーバのコード。.app なら同梱(Contents/Resources/board)、開発中は AIBOARD_BOARD か リポジトリの board/
let BOARD_DIR: String = {
    if let p = ProcessInfo.processInfo.environment["AIBOARD_BOARD"] { return p }
    if let r = Bundle.main.resourcePath, FileManager.default.fileExists(atPath: r + "/board/cs.py") { return r + "/board" }
    return HOME + "/aiboard/board"
}()
/// 自己試験(UAT)で動かしているか。試験中はダイアログを出さない(出すとアプリが終わらず、試験が時間切れになる)
let SELF_TEST: Bool = ["AIBOARD_SELFTEST", "AIBOARD_RESTORE_TEST", "AIBOARD_SWITCH_TEST", "AIBOARD_JS_TEST", "AIBOARD_SHOT",
     "AIBOARD_NOTIFY_TEST", "AIBOARD_ASK_TEST"]
    .contains { ProcessInfo.processInfo.environment[$0] != nil }

let BOARD_URL = URL(string: "http://127.0.0.1:\(ProcessInfo.processInfo.environment["OVERVIEW_PORT"] ?? "8791")/")!   // 試験では別ポート

func shellQuote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
/// 表示言語。既定は英語、システムの第一言語が日本語なら日本語(盤も同じ規則。AIBOARD_LANG で強制できる)
let LANG: String = { if let l = ProcessInfo.processInfo.environment["AIBOARD_LANG"] { return l }
    return (Locale.preferredLanguages.first ?? "en").hasPrefix("ja") ? "ja" : "en" }()
func L(_ en: String, _ ja: String) -> String { LANG == "ja" ? ja : en }

// MARK: - 端末1枚(libghostty)

@MainActor
final class Pane: NSObject, TerminalSurfaceTitleDelegate, TerminalSurfaceCloseDelegate, TerminalSurfaceBellDelegate,
                  TerminalSurfaceDesktopNotificationDelegate {
    let id: Int
    let view: AppTerminalView
    var kind: String            // claude / codex / shell
    var cwd: String
    var title: String
    var tty = ""
    var pid: Int = 0
    var sid = ""                // 盤が教えてくれる(復元に使う)
    var deleg = ""              // 「任せる」で起こした端末の控えの id(結果の突き合わせを推定でなく一致で行う)
    var alive = true
    weak var manager: PaneManager?

    init(id: Int, kind: String, cwd: String, command: String?, manager: PaneManager) {
        self.id = id; self.kind = kind; self.cwd = cwd; self.manager = manager
        self.title = (cwd as NSString).lastPathComponent
        view = AppTerminalView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        super.init()
        view.controller = manager.controller
        view.delegate = self
        view.autoresizingMask = [.width, .height]
        let dir = FileManager.default.fileExists(atPath: cwd) ? cwd : HOME
        // 起動スクリプトを常に使う(libghostty の command は空白で分けるので引用符入りの 1 行は渡さない)。
        // スクリプトの名前 pane-<id>.sh が login ラッパーの引数に残るので、それで tty を引ける
        // (envVars は /usr/bin/login を経由すると子に届かない。2026-09-18 実測)。
        // -i で .zshrc の関数(claude のアカウント切替など)が効く。終わったら素のシェルに戻る。
        let dirp = STATE_DIR + "/launch"; try? FileManager.default.createDirectory(atPath: dirp, withIntermediateDirectories: true)
        var path = dirp + "/pane-\(id).sh"
        var body = command ?? ""
        if ProcessInfo.processInfo.environment["AIBOARD_DRY"] != nil, !body.isEmpty { body = "echo WOULD_RUN: " + shellQuote(body) }   // 試験: 実行しない
        // 起動スクリプトは自分で走る形にする(#!/bin/zsh -il で .zshrc も読む)。
        // libghostty に渡す command は空白で分けられるので、渡すのはこのパス 1 つだけにする
        // (以前は "zsh -l -i -c source<NBSP>path" と書いていたが、NBSP が引数に残って
        //  zsh が "source path" という名前のコマンドを探し、端末が即終了していた。2026-09-18 実測)
        // 自己試験だけ、起動の速いシェルにできる(利用者の .zshrc は機械が混んでいると 1 分かかる。2026-09-18 実測)
        let sh = ProcessInfo.processInfo.environment["AIBOARD_FAST_SHELL"] != nil ? "/bin/zsh -f" : "/bin/zsh -il"
        // AIBoard 自身が Claude Code の中から起動されると、その印(CLAUDECODE・CLAUDE_CODE_CHILD_SESSION など)が
        // 端末に受け継がれ、ここで開いた claude は「子のセッション」として記録を書かない＝盤から見えなくなる(2026-09-19 実測)。
        // 端末は独立したセッションなので、印は消してから始める。アカウントの指定(CLAUDE_CONFIG_DIR)は消さない
        let scrub = "unset CLAUDECODE CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_EXECPATH CLAUDE_CODE_SESSION_ID " +
                    "CLAUDE_CODE_SESSION_ATTENDED CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_DISABLE_TERMINAL_TITLE CLAUDE_PID CLAUDE_EFFORT\n"
        let script = "#!" + sh + "\n" + scrub + "export AIBOARD_PANE=\(id) TERM_PROGRAM=AIBoard\ncd " + shellQuote(dir) + "\n" + body + "\nexec " + sh + "\n"
        if path.contains(" ") {   // 空白を含む置き場だと command が分割されるので /tmp に逃がす
            let alt = "/tmp/aiboard-\(getuid())"
            try? FileManager.default.createDirectory(atPath: alt, withIntermediateDirectories: true)
            path = alt + "/pane-\(id).sh"
        }
        try? script.write(toFile: path, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: path)
        let cmdline = path
        view.configuration = TerminalSurfaceOptions(
            backend: .exec, workingDirectory: dir,
            envVars: ["LANG": "ja_JP.UTF-8", "LC_CTYPE": "ja_JP.UTF-8", "TERM_PROGRAM": "AIBoard", "AIBOARD_PANE": "\(id)"],
            command: cmdline, waitAfterCommand: false)
        resolveTty(attempt: 0)
    }

    /// 盤から送る文は端末ごとに 1 件ずつ順に流す。
    /// 貼り付けは即座には終わらないので、Enter は少し置いてから送り、次の送信はそれを待つ
    /// (待たずに続けると、長い文の途中に次の行が割り込んで混ざる。3500 字で再現。2026-09-18 実測)。
    private var sendQueue: [(String, Bool)] = []
    private var sending = false

    func enqueue(text: String, enter: Bool) {
        sendQueue.append((text, enter))
        pump()
    }

    private func pump() {
        guard !sending, !sendQueue.isEmpty else { return }
        sending = true
        let (text, enter) = sendQueue.removeFirst()
        _ = view.paste(text: text)
        let wait = min(0.8, 0.08 + Double(text.count) / 6000.0)
        DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in
            guard let self else { return }
            if enter { _ = self.view.sendKey(.enter) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                self.sending = false
                self.pump()
            }
        }
    }

    /// 端末のプロセスは libghostty が起こすので pid は見えない。このアプリの子(login ラッパー)のうち、
    /// 引数に pane-<id>.sh を持つものの tty を取る。起動直後は居ないことがあるので数回試す。
    private func resolveTty(attempt: Int) {
        DispatchQueue.global().asyncAfter(deadline: .now() + (attempt == 0 ? 0.6 : 1.5)) { [weak self] in
            guard let self else { return }
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/ps")
            p.arguments = ["-ww", "-o", "pid=,ppid=,tty=,command=", "-u", String(getuid())]
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
            // 出力が 64KB を超えるので、読み切ってから待つ(先に waitUntilExit するとパイプが詰まって固まる。2026-09-18 実測)
            guard (try? p.run()) != nil else { return }
            let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit()
            var found: (Int, String)? = nil
            let me = ProcessInfo.processInfo.processIdentifier
            let needle = "/pane-\(self.id).sh"
            for line in out.split(separator: "\n") {
                guard line.contains(needle) else { continue }
                let parts = line.trimmingCharacters(in: .whitespaces).split(separator: " ", maxSplits: 3, omittingEmptySubsequences: true)
                guard parts.count >= 3, let pid = Int(parts[0]), let ppid = Int32(parts[1]), ppid == me, parts[2].hasPrefix("ttys") else { continue }
                found = (pid, String(parts[2])); break
            }
            DispatchQueue.main.async {
                if let (pid, tty) = found { self.pid = pid; self.tty = tty; self.manager?.publish() }
                else if attempt < 6 { self.resolveTty(attempt: attempt + 1) }
            }
        }
    }

    func terminalDidChangeTitle(_ t: String) { title = t; manager?.titleChanged(self) }
    func terminalDidClose(processAlive: Bool) { alive = false; manager?.paneEnded(self) }
    func terminalDidRingBell() { NSSound.beep() }
    /// Claude Code などが OSC 9/777 で出す通知をそのまま macOS 通知に(盤を見ていなくても気づける)
    func terminalDidRequestDesktopNotification(title: String, body: String) {
        let c = UNMutableNotificationContent(); c.title = title.isEmpty ? "Terminal \(id)" : title; c.body = String(body.prefix(140)); c.sound = .default
        c.userInfo = ["tab": "0-\(id)", "sid": sid]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "pane-\(id)-\(Int(Date().timeIntervalSince1970))", content: c, trigger: nil))
    }
    func terminate() { view.removeFromSuperview() }   // surface が解放され、子プロセスに HUP が届く
}

/// 端末を入れる器。大きさが変わるたびに中の端末を全部同じ大きさにする(autoresizingMask は初回 0 のままになった)
final class PaneContainer: NSView {
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); subviews.forEach { $0.frame = bounds } }
}

// MARK: - 端末の集合

@MainActor
final class PaneStripTarget: NSObject {
    unowned let pm: PaneManager
    init(pm: PaneManager) { self.pm = pm }
    @objc func tap(_ b: NSButton) {
        guard let p = pm.panes.first(where: { $0.id == b.tag }) else { return }
        pm.select(p)                 // 右で人が選んだ: 入力先も端末へ
        pm.onUserSelect?(p)          // 左の会話も追従させる
        // 左の会話を開く処理(盤の JS)が入力先を盤に戻すことがあるので、一拍おいて端末に戻す
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { p.view.window?.makeFirstResponder(p.view) }
    }
    @objc func plus(_ b: NSButton) { pm.open(kind: "shell", cwd: HOME, command: nil) }
    @objc func openLeft(_ m: NSMenuItem) {
        guard let p = pm.panes.first(where: { $0.id == m.tag }) else { return }
        pm.select(p); pm.onUserSelect?(p)
    }
    @objc func closeTab(_ m: NSMenuItem) {
        guard let p = pm.panes.first(where: { $0.id == m.tag }) else { return }
        if p.kind != "shell" && !SELF_TEST {   // AI が動いている端末は、閉じる前に聞く(シェルはそのまま)
            let a = NSAlert()
            a.messageText = L("Close this terminal?", "この端末を閉じますか？")
            a.informativeText = L("The session in it will be terminated. You can resume it from History.", "中の AI は終了します。「過去」から再開できます。")
            a.addButton(withTitle: L("Close", "閉じる")); a.addButton(withTitle: L("Cancel", "やめる"))
            if a.runModal() != .alertFirstButtonReturn { return }
        }
        pm.close(p)
    }
    func menu(for p: Pane) -> NSMenu {
        let m = NSMenu()
        let a = NSMenuItem(title: L("Open the conversation on the left", "左で会話を開く"), action: #selector(openLeft(_:)), keyEquivalent: "")
        a.target = self; a.tag = p.id; m.addItem(a)
        m.addItem(.separator())
        let c = NSMenuItem(title: L("Close terminal", "端末を閉じる"), action: #selector(closeTab(_:)), keyEquivalent: "")
        c.target = self; c.tag = p.id; m.addItem(c)
        return m
    }
}

@MainActor
final class PaneManager {
    var panes: [Pane] = []
    var selected: Pane?
    var nextId = 1
    var fontSize: CGFloat = 13
    let container = PaneContainer()
    /// libghostty の設定は controller が持つ(全端末で共有)。地の色は盤の端末パネルと同じ #05070C
    lazy var controller: TerminalController = TerminalController(theme: theme(size: Float(fontSize)))
    func theme(size: Float) -> TerminalTheme {
        let c = TerminalConfiguration().fontFamily("Menlo").fontSize(size).background("#05070C").foreground("#D5DBE5")
            .cursorColor("#7DD3FC").windowPaddingX(8).windowPaddingY(6)
        return TerminalTheme(light: c, dark: c)
    }
    let header = NSTextField(labelWithString: "")
    var onChange: (() -> Void)?
    /// 盤と同じ名前と色(「Opus 5 · homepage」)。左の会話と右の端末が同じものだと一目で分かるように、両方に同じ札を出す
    var labels: [Int: (text: String, rgb: [Int])] = [:]
    var states: [Int: String] = [:]                 // 盤が知っている状態(確認待ち など)。タブにバッジを出す
    /// 右の端末の一覧(タブ列)。左のカードと同じ名前・色・バッジ。押すとその端末へ(左の会話も追従する)
    let strip = NSStackView()
    var onUserSelect: ((Pane) -> Void)?

    func rebuildStrip() {
        strip.arrangedSubviews.forEach { strip.removeArrangedSubview($0); $0.removeFromSuperview() }
        for p in panes {
            let b = NSButton(title: "", target: self, action: #selector(PaneStripTarget.tap(_:)))
            b.target = stripTarget; b.tag = p.id
            b.bezelStyle = .accessoryBarAction
            b.setButtonType(.pushOnPushOff)
            b.state = (p === selected) ? .on : .off
            let a = NSMutableAttributedString()
            let font = NSFont.systemFont(ofSize: 11, weight: p === selected ? .semibold : .regular)
            if let l = labels[p.id] {
                let c = NSColor(red: CGFloat(l.rgb[0]) / 255, green: CGFloat(l.rgb[1]) / 255, blue: CGFloat(l.rgb[2]) / 255, alpha: 1)
                a.append(NSAttributedString(string: "● ", attributes: [.foregroundColor: c, .font: font]))
                a.append(NSAttributedString(string: String(l.text.prefix(28)), attributes: [.font: font, .foregroundColor: NSColor.labelColor]))
            } else {
                let mark = p.kind == "claude" ? "◉ " : p.kind == "codex" ? "◉ " : "○ "
                let name = p.title.isEmpty ? (p.kind == "shell" ? "shell" : p.kind) : String(p.title.prefix(24))
                a.append(NSAttributedString(string: mark + name, attributes: [.font: font, .foregroundColor: NSColor.labelColor]))
            }
            if let st = states[p.id] {
                if st == "確認待ち" { a.append(NSAttributedString(string: "  !", attributes: [.foregroundColor: NSColor.systemRed, .font: NSFont.boldSystemFont(ofSize: 12)])) }
                else if st == "返答待ち" || st == "codex 返答待ち" { a.append(NSAttributedString(string: "  ●", attributes: [.foregroundColor: NSColor.systemYellow, .font: font])) }
            }
            if linkedTab == p.id { a.append(NSAttributedString(string: " ⇄", attributes: [.foregroundColor: NSColor.systemTeal, .font: font])) }
            b.attributedTitle = a
            b.toolTip = "\(p.id): \(p.cwd)"
            b.menu = stripTarget.menu(for: p)     // 右クリック: 左で会話を開く / 端末を閉じる
            strip.addArrangedSubview(b)
        }
        let plus = NSButton(title: "＋", target: stripTarget, action: #selector(PaneStripTarget.plus(_:)))
        plus.bezelStyle = .accessoryBarAction
        plus.toolTip = L("New shell (⇧⌘T) · ⌘T Claude · ⌥⌘T Codex", "新しいシェル(⇧⌘T)・⌘T Claude・⌥⌘T Codex")
        strip.addArrangedSubview(plus)
        strip.layoutSubtreeIfNeeded()
        // 端末が多くて列が長い時は、選んでいる端末が見える所まで横に送る
        if let sel = selected, let b = strip.arrangedSubviews.compactMap({ $0 as? NSButton }).first(where: { $0.tag == sel.id }) {
            b.scrollToVisible(b.bounds)
        }
    }
    lazy var stripTarget = PaneStripTarget(pm: self)
    /// 左の会話ビューで開いている端末(⇄ の印を出す)。空なら無し
    var linkedTab = 0

    @discardableResult
    func open(kind: String, cwd: String, command: String?) -> Pane {
        let p = Pane(id: nextId, kind: kind, cwd: cwd, command: command, manager: self)
        nextId += 1
        panes.append(p)
        p.view.isHidden = true
        container.addSubview(p.view)
        container.needsLayout = true
        select(p)
        publish()
        return p
    }

    func select(_ p: Pane, focus: Bool = true) {
        selected?.view.isHidden = true
        selected = p
        p.view.isHidden = false
        p.view.frame = container.bounds
        panes.forEach { $0.view.setSurfaceVisible($0 === p) }   // 見えない端末は描画を止める(セッションは続く)
        p.view.needsDisplay = true
        if focus { p.view.window?.makeFirstResponder(p.view) }   // 左で読んでいるだけの時は、入力先を奪わない
        titleChanged(p)
        rebuildStrip()
        onChange?()
    }

    func pane(tab: String) -> Pane? {
        guard tab.hasPrefix("0-"), let n = Int(tab.dropFirst(2)) else { return nil }
        return panes.first { $0.id == n }
    }

    func step(_ d: Int) {
        guard let s = selected, let i = panes.firstIndex(where: { $0 === s }), !panes.isEmpty else { return }
        select(panes[(i + d + panes.count) % panes.count])
    }

    func titleChanged(_ p: Pane) {
        if p === selected {
            let mark = p.kind == "claude" ? "◉ CLAUDE" : p.kind == "codex" ? "◉ CODEX" : "○ SHELL"
            let a = NSMutableAttributedString()
            if let l = labels[p.id] {
                // 盤のカードと同じ色の ● と同じ名前。左の会話と同じものならその印
                let c = NSColor(red: CGFloat(l.rgb[0]) / 255, green: CGFloat(l.rgb[1]) / 255, blue: CGFloat(l.rgb[2]) / 255, alpha: 1)
                a.append(NSAttributedString(string: "● ", attributes: [.foregroundColor: c]))
                a.append(NSAttributedString(string: l.text))
                a.append(NSAttributedString(string: "  ·  \(p.id)/\(panes.count)", attributes: [.foregroundColor: NSColor.secondaryLabelColor]))
            } else {
                a.append(NSAttributedString(string: "\(mark)  ·  \(p.id)/\(panes.count)  ·  \(p.title)"))
            }
            if linkedTab == p.id {
                a.append(NSAttributedString(string: L("   ⇄ the conversation on the left", "   ⇄ 左の会話と同じ"), attributes: [.foregroundColor: NSColor.systemTeal]))
            }
            header.attributedStringValue = a
        }
        rebuildStrip()
        publish()
    }

    func paneEnded(_ p: Pane) {
        p.view.removeFromSuperview()
        panes.removeAll { $0 === p }
        if selected === p { selected = nil; if let last = panes.last { select(last) } else { header.stringValue = L("No terminal — ⌘T Claude · ⌥⌘T Codex · ⇧⌘T shell", "端末なし — ⌘T で Claude、⌥⌘T で Codex、⇧⌘T でシェル") } }
        publish(); onChange?()
    }

    func close(_ p: Pane) { p.terminate(); paneEnded(p) }

    func setFont(delta: CGFloat) {
        fontSize = max(9, min(24, fontSize + delta))
        _ = controller.setTheme(theme(size: Float(fontSize)))
    }

    /// 盤側の道具(cs.py)に、アプリが持っている端末を知らせる。復元用の状態も書く。
    var terminating = false
    func publish() {
        let list: [[String: Any]] = panes.filter { !$0.tty.isEmpty }.map {
            ["pane": $0.id, "tty": $0.tty, "title": $0.title, "kind": $0.kind, "cwd": $0.cwd, "pid": $0.pid, "deleg": $0.deleg] }
        write(["updated": Date().timeIntervalSince1970, "app_pid": Int(ProcessInfo.processInfo.processIdentifier),
               "panes": list, "notify_auth": NotifyAuth.status], to: PANES_FILE)
        if terminating { return }   // 終了時に端末が 1 枚ずつ閉じるたびに書き直すと、保存した一覧が空になる
        let st: [[String: Any]] = panes.map { ["kind": $0.kind, "cwd": $0.cwd, "sid": $0.sid] }
        try? FileManager.default.createDirectory(atPath: STATE_DIR, withIntermediateDirectories: true)
        write(["saved": Date().timeIntervalSince1970, "panes": st], to: STATE_FILE)
    }

    private func write(_ obj: [String: Any], to path: String) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted]) else { return }
        let tmp = path + ".\(ProcessInfo.processInfo.processIdentifier).tmp"
        try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        if (try? d.write(to: URL(fileURLWithPath: tmp))) != nil {
            // replaceItemAt は置き換え先が無いと失敗するので、初回は普通に移す(念のため)
            if FileManager.default.fileExists(atPath: path) {
                _ = try? FileManager.default.replaceItemAt(URL(fileURLWithPath: path), withItemAt: URL(fileURLWithPath: tmp))
            } else {
                _ = try? FileManager.default.moveItem(atPath: tmp, toPath: path)
            }
        }
    }
}

// MARK: - 見張り(盤を見ていなくても気づける: macOS 通知と Dock バッジ)

/// 盤サーバの snapshot を 3 秒ごとに読み、「判断待ち」「停止」になった瞬間に通知する。
/// 返答済みは、このアプリの中で動いている端末だけ通知する(iTerm 側のまで鳴ると多すぎる)。
/// 通知の本文はサーバ側で秘密を伏せたもの(redact 済み)。外部には何も送らない。
@MainActor
/// 判断待ちになった時に鳴らす音。通知を切られていても鳴る(通知の音とは別)。
/// 鳴りすぎないように 8 秒に 1 回まで。設定で切れる。
enum Chime {
    nonisolated(unsafe) static var enabled = true
    nonisolated(unsafe) static var last = 0.0
    nonisolated(unsafe) static var log: [String] = []      // 自己試験用
    nonisolated static func play(_ name: String) {
        let now = Date().timeIntervalSince1970
        guard enabled, now - last > 8 else { return }
        last = now
        log.append(name)
        if ProcessInfo.processInfo.environment["AIBOARD_NO_SOUND"] != nil || SELF_TEST { return }
        NSSound(named: NSSound.Name(name))?.play()
    }
}

/// 通知の許可の状態。止められていると「あなたを待っている」を知らせる術が無くなるので、盤に出して気づけるようにする
enum NotifyAuth {
    nonisolated(unsafe) static var status = "unknown"
    nonisolated static func refresh() {
        UNUserNotificationCenter.current().getNotificationSettings { st in
            status = ["notDetermined", "denied", "authorized", "provisional", "ephemeral"][min(st.authorizationStatus.rawValue, 4)]
        }
    }
}


final class Watcher {
    private var timer: Timer?
    private var known: [String: String] = [:]     // sid → state
    private var primed = false                     // 最初の 1 回は「今の状態」を覚えるだけ(起動時に通知の嵐を出さない)
    var onCount: ((Int) -> Void)?
    var onWaiting: (([[String: Any]]) -> Void)?    // いま判断待ちのセッション(小窓が使う)
    var onOpen: ((String, String) -> Void)?        // (tab, sid)
    let center = UNUserNotificationCenter.current()
    var dryRun = false                             // 自己試験: 実際には出さず dryLog に積む
    var dryLog: [String] = []
    func feed(_ sessions: [[String: Any]]) { update(sessions) }   // 自己試験用

    func start() {
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in NotifyAuth.refresh() }
        NotifyAuth.refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func poll() {
        var req = URLRequest(url: BOARD_URL.appendingPathComponent("api/snapshot")); req.timeoutInterval = 4
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self, let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let sessions = o["sessions"] as? [[String: Any]] else { return }
            if let snd = o["sound"] as? Bool { Chime.enabled = snd }
            DispatchQueue.main.async { self.update(sessions) }
        }.resume()
    }

    private func update(_ sessions: [[String: Any]]) {
        var now: [String: String] = [:]
        var needs = 0
        var waiting: [[String: Any]] = []
        for s in sessions {
            guard let sid = s["sid"] as? String, !sid.isEmpty, let state = s["state"] as? String else { continue }
            now[sid] = state
            let tab = s["tab"] as? String ?? ""
            let inApp = tab.hasPrefix("0-")
            let model = (s["model_style"] as? [String: Any])?["label"] as? String ?? (s["ai"] as? String ?? "AI")
            let where_ = (s["project"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? tab
            let doing = (s["doing"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            if state == "確認待ち" {
                needs += 1
                waiting.append(["sid": sid, "tab": tab, "model": model, "where": where_, "doing": doing,
                                "task": (s["task"] as? String ?? ""), "inApp": inApp])
            }
            guard primed, known[sid] != state else { continue }
            if state == "確認待ち" {
                notify(id: sid, title: L("\(model) needs you · \(where_)", "\(model) があなたの判断待ち · \(where_)"), body: doing, tab: tab, sid: sid)
                Chime.play("Glass")       // 通知が切られていても気づけるように
            } else if state == "codex 停止" || doing.hasPrefix("⛔") {
                notify(id: sid, title: L("\(model) stopped · \(where_)", "\(model) が停止 · \(where_)"), body: doing, tab: tab, sid: sid)
                Chime.play("Basso")
            } else if inApp, state == "返答待ち" || state == "codex 返答待ち" {
                notify(id: sid, title: L("\(model) replied · \(where_)", "\(model) が返答 · \(where_)"), body: L("Your turn.", "あなたの番です。"), tab: tab, sid: sid)
            }
        }
        known = now
        primed = true
        onCount?(needs)
        onWaiting?(waiting)
    }

    private func notify(id: String, title: String, body: String, tab: String, sid: String) {
        if dryRun { dryLog.append("\(title) | \(body) | tab=\(tab)"); return }
        let c = UNMutableNotificationContent()
        c.title = title; c.body = String(body.prefix(140)); c.sound = .default
        c.userInfo = ["tab": tab, "sid": sid]
        center.add(UNNotificationRequest(identifier: id + "-" + String(Int(Date().timeIntervalSince1970)), content: c, trigger: nil))
    }
}

// MARK: - アプリ

/// 判断待ちに答えるための、最前面に浮かぶ小窓。
///
/// なぜ要るか(2026-09-18): 知らせる手段が OS 通知と Dock の数字しか無かったが、
/// この Mac では通知が denied で 1 通も出ていなかった(誰も気づけない)。通知は利用者が切れるが、
/// 自前の小窓は切られない。出すのは「確認待ち」だけ。答えると消える。
@MainActor
final class AskPanel: NSObject, NSWindowDelegate {
    let panel: NSPanel
    private let title = NSTextField(labelWithString: "")
    private let body = NSTextField(wrappingLabelWithString: "")
    private let row = NSStackView()
    private let input = NSTextField()
    private var current: [String: Any] = [:]
    /// 答えを届ける先: (tab, sid, key, text)。アプリの端末と iTerm のタブで道が違うので、外から渡す
    var send: ((String, String, String, String) -> Void)?
    var onOpen: ((String) -> Void)?          // 「端末を見る」
    var log: [String] = []                   // 自己試験用(何を出して、何を送ったか)
    private(set) var shownSid = ""
    var state: [String: Any] { ["shown": shownSid, "visible": panel.isVisible,
                                "title": title.stringValue, "body": body.stringValue,
                                "buttons": row.arrangedSubviews.compactMap { ($0 as? NSButton)?.identifier?.rawValue }] }

    override init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 380, height: 150),
                        styleMask: [.titled, .closable, .nonactivatingPanel, .utilityWindow],
                        backing: .buffered, defer: false)
        super.init()
        panel.title = L("Waiting for you", "あなたの判断待ち")
        panel.level = .floating                      // 他のアプリの上に出す(前面を奪わない)
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]   // 同時指定は不可(moveToActiveSpace と排他)
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = true          // 文字を打つときだけキー入力を受ける
        panel.delegate = self
        let v = NSStackView(views: [title, body, row, input])
        v.orientation = .vertical; v.alignment = .leading; v.spacing = 8
        v.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        body.font = .systemFont(ofSize: 12)
        body.textColor = .secondaryLabelColor
        body.preferredMaxLayoutWidth = 350
        row.orientation = .horizontal; row.spacing = 6
        input.placeholderString = L("or type an answer…", "自由に答える…")
        input.target = self; input.action = #selector(sendTyped)
        input.widthAnchor.constraint(equalToConstant: 350).isActive = true
        for (label, key) in [("1", "1"), ("2", "2"), (L("No (Esc)", "いいえ (Esc)"), "esc"), (L("Terminal", "端末を見る"), "open")] {
            let b = NSButton(title: label, target: self, action: #selector(tap(_:)))
            b.identifier = NSUserInterfaceItemIdentifier(key)
            if key == "1" { b.keyEquivalent = "\r" }
            row.addArrangedSubview(b)
        }
        panel.contentView = v
    }

    /// 判断待ちの一覧を受け取り、先頭 1 件を出す。0 件になったら閉じる。
    func update(_ waiting: [[String: Any]], offscreen: Bool) {
        guard let w = waiting.first, let sid = w["sid"] as? String else {
            if panel.isVisible || !shownSid.isEmpty { log.append("hide"); shownSid = "" }
            panel.orderOut(nil)
            return
        }
        if sid != shownSid {
            log.append("show \(sid) tab=\(w["tab"] as? String ?? "")")
            shownSid = sid
        }
        current = w
        let more = waiting.count > 1 ? " ＋\(waiting.count - 1)" : ""
        title.stringValue = "\(w["model"] as? String ?? "AI") · \(w["where"] as? String ?? "")\(more)"
        let doing = (w["doing"] as? String ?? "").isEmpty ? (w["task"] as? String ?? "") : (w["doing"] as? String ?? "")
        body.stringValue = String(doing.prefix(200))
        panel.setContentSize(NSSize(width: 380, height: 150))
        if offscreen {      // 自己試験: 画面を奪わない
            panel.setFrameOrigin(NSPoint(x: -5000, y: -5000))
            panel.orderFront(nil)
            return
        }
        if !panel.isVisible, let vis = NSScreen.main?.visibleFrame {
            panel.setFrameTopLeftPoint(NSPoint(x: vis.maxX - 400, y: vis.maxY - 20))   // 右上
        }
        panel.orderFrontRegardless()
    }

    /// 試験用: ボタンと同じ道で押す(1 / 2 / esc / open)
    func tapKey(_ key: String) {
        for v in row.arrangedSubviews {
            if let b = v as? NSButton, b.identifier?.rawValue == key { tap(b); return }
        }
    }

    /// 試験用: 自由入力の欄から送る
    func typeAnswer(_ text: String) { input.stringValue = text; sendTyped() }

    @objc private func tap(_ b: NSButton) {
        let key = b.identifier?.rawValue ?? ""
        let tab = current["tab"] as? String ?? "", sid = current["sid"] as? String ?? ""
        if key == "open" { log.append("open \(tab)"); onOpen?(tab); return }
        log.append("answer \(key) tab=\(tab)")
        send?(tab, sid, key == "esc" ? "esc" : "", key == "esc" ? "" : key)
        panel.orderOut(nil); shownSid = ""
    }

    @objc private func sendTyped() {
        let text = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let tab = current["tab"] as? String ?? "", sid = current["sid"] as? String ?? ""
        log.append("answer text tab=\(tab)")
        send?(tab, sid, "", text)
        input.stringValue = ""
        panel.orderOut(nil); shownSid = ""
    }
}


final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, NSSplitViewDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    let watcher = Watcher()
    let split = NSSplitView()
    var web: WKWebView!
    let pm = PaneManager()
    var boardHidden = false
    var lastState: [[String: Any]] = []
    let ask = AskPanel()
    var scheduleTimer: Timer?
    var scheduleLog: [String] = []

    func applicationDidFinishLaunching(_ n: Notification) {
        if let d = FileManager.default.contents(atPath: STATE_FILE),
           let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { lastState = o["panes"] as? [[String: Any]] ?? [] }
        buildMenu()
        let frame = NSScreen.main?.visibleFrame.insetBy(dx: 40, dy: 30) ?? NSRect(x: 0, y: 0, width: 1500, height: 900)
        window = NSWindow(contentRect: frame, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "AIBoard"
        window.setFrameAutosaveName("AIBoardMain")
        // 前回の大きさを覚えるが、盤が読めない大きさ(画面の半分未満)で開いてしまうのは直す
        if let vis = NSScreen.main?.visibleFrame {
            let f = window.frame
            if f.width < max(1100, vis.width * 0.5) || f.height < max(700, vis.height * 0.5) {
                window.setFrame(vis.insetBy(dx: 40, dy: 30), display: false)
            }
        }
        // 盤と端末と同じ「夜空」の地。タイトルバーは透かして内容を上端まで敷く
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(srgbRed: 0.027, green: 0.039, blue: 0.071, alpha: 1)   // #070A12
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.styleMask.insert(.fullSizeContentView)

        let conf = WKWebViewConfiguration()
        conf.userContentController.add(self, name: "aiboard")
        conf.userContentController.add(self, name: "log")
        conf.userContentController.addUserScript(WKUserScript(source: """
            window.AIBOARD = true; window.AIBOARD_LANG = '\(LANG)';
            (function(){ const send = (k, a) => { try { window.webkit.messageHandlers.log.postMessage(k + ': ' + [...a].map(x => { try { return typeof x === 'string' ? x : JSON.stringify(x); } catch (e) { return String(x); } }).join(' ')); } catch (e) {} };
              const ce = console.error.bind(console); console.error = (...a) => { send('error', a); ce(...a); };
              window.addEventListener('error', e => send('pageerror', [e.message, e.filename + ':' + e.lineno]));
              window.addEventListener('unhandledrejection', e => send('rejection', [String(e.reason)]));
              window.addEventListener('click', e => send('click', [e.clientX, e.clientY, e.isTrusted, (e.target.className || e.target.tagName)]), true); })();
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: conf)
        web.navigationDelegate = self
        web.uiDelegate = self   // confirm()/alert()/prompt() を出す。無いと WebKit は黙って「いいえ」を返す

        let right = NSView()
        right.wantsLayer = true
        right.layer?.backgroundColor = NSColor(srgbRed: 0.02, green: 0.027, blue: 0.047, alpha: 1).cgColor   // #05070C 端末と同じ地
        pm.header.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)
        pm.header.textColor = NSColor(srgbRed: 0.545, green: 0.58, blue: 0.655, alpha: 1)   // #8B94A7
        pm.header.lineBreakMode = .byTruncatingTail
        pm.header.stringValue = L("No terminal — ⌘T Claude · ⌥⌘T Codex · ⇧⌘T shell", "端末なし — ⌘T で Claude、⌥⌘T で Codex、⇧⌘T でシェル")
        pm.header.translatesAutoresizingMaskIntoConstraints = false
        pm.container.translatesAutoresizingMaskIntoConstraints = false
        pm.strip.orientation = .horizontal; pm.strip.spacing = 4; pm.strip.alignment = .centerY
        pm.strip.translatesAutoresizingMaskIntoConstraints = false
        // 端末が多い時は横に送れるようにする(つまみは出さない。選ぶと見える所まで送る)
        let stripScroll = NSScrollView()
        stripScroll.translatesAutoresizingMaskIntoConstraints = false
        stripScroll.hasHorizontalScroller = false; stripScroll.hasVerticalScroller = false
        stripScroll.drawsBackground = false; stripScroll.horizontalScrollElasticity = .allowed
        stripScroll.documentView = pm.strip
        pm.strip.setHuggingPriority(.required, for: .horizontal)
        right.addSubview(pm.header); right.addSubview(stripScroll); right.addSubview(pm.container)
        NSLayoutConstraint.activate([
            pm.header.topAnchor.constraint(equalTo: right.topAnchor, constant: 34),   // 透かしたタイトルバーの下
            pm.header.leadingAnchor.constraint(equalTo: right.leadingAnchor, constant: 10),
            pm.header.trailingAnchor.constraint(equalTo: right.trailingAnchor, constant: -10),
            stripScroll.topAnchor.constraint(equalTo: pm.header.bottomAnchor, constant: 4),
            stripScroll.leadingAnchor.constraint(equalTo: right.leadingAnchor, constant: 8),
            stripScroll.trailingAnchor.constraint(equalTo: right.trailingAnchor, constant: -8),
            stripScroll.heightAnchor.constraint(equalToConstant: 24),
            pm.strip.heightAnchor.constraint(equalToConstant: 24),
            pm.container.topAnchor.constraint(equalTo: stripScroll.bottomAnchor, constant: 4),
            pm.container.leadingAnchor.constraint(equalTo: right.leadingAnchor, constant: 6),
            pm.container.trailingAnchor.constraint(equalTo: right.trailingAnchor),
            pm.container.bottomAnchor.constraint(equalTo: right.bottomAnchor),
        ])
        split.isVertical = true
        split.dividerStyle = .thin
        split.wantsLayer = true
        split.layer?.backgroundColor = NSColor(srgbRed: 0.027, green: 0.039, blue: 0.071, alpha: 1).cgColor
        split.delegate = self
        split.addArrangedSubview(web); split.addArrangedSubview(right)
        split.frame = window.contentView!.bounds
        split.autoresizingMask = [.width, .height]
        window.contentView!.addSubview(split)
        if SELF_TEST {
            // 自己試験では画面を奪わない: Dock にも出さず、前面化もせず、見えない場所で動かす
            NSApp.setActivationPolicy(.accessory)
            window.setFrame(NSRect(x: -9000, y: -9000, width: 1400, height: 900), display: false)
            window.orderBack(nil)
        } else {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        window.contentView?.layoutSubtreeIfNeeded()
        split.setPosition(split.bounds.width * 0.5, ofDividerAt: 0)
        startServerThenLoad()
        // 右の端末は空にしない: iTerm と同じく、起動したらシェルを 1 枚開いて打てる状態にする(復元の試験中は除く)
        if ProcessInfo.processInfo.environment["AIBOARD_RESTORE_TEST"] == nil && ProcessInfo.processInfo.environment["AIBOARD_SWITCH_TEST"] == nil && ProcessInfo.processInfo.environment["AIBOARD_NO_SHELL"] == nil && pm.panes.isEmpty {
            pm.open(kind: "shell", cwd: HOME, command: nil)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.offerHookIfNeeded() }
        // 通知と Dock バッジ(判断待ちの数)。通知を押すと、その端末へ
        pm.onUserSelect = { [weak self] p in
            self?.web.evaluateJavaScript("window.board && board.openFromRight && board.openFromRight(\(String(reflecting: "0-\(p.id)")))") { _, _ in }
        }
        let prevChange = pm.onChange
        pm.onChange = { [weak self] in
            prevChange?()
            guard let self else { return }
            let tab = self.pm.selected.map { "0-\($0.id)" } ?? ""
            self.web.evaluateJavaScript("window.board && board.setRightTab && board.setRightTab(\(String(reflecting: tab)))") { _, _ in }
        }
        watcher.center.delegate = self
        watcher.onCount = { n in NSApp.dockTile.badgeLabel = n > 0 ? String(n) : nil }
        watcher.onOpen = { [weak self] tab, _ in self?.open(tab: tab) }
        // 判断待ちは、最前面の小窓でも知らせる(通知を切られていても届く)
        ask.onOpen = { [weak self] tab in self?.open(tab: tab) }
        ask.send = { [weak self] tab, sid, key, text in self?.answer(tab: tab, sid: sid, key: key, text: text) }
        watcher.onWaiting = { [weak self] rows in
            guard let self else { return }
            if ProcessInfo.processInfo.environment["AIBOARD_NO_ASK"] != nil { return }
            self.ask.update(rows, offscreen: SELF_TEST)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self.watcher.start() }
        if ProcessInfo.processInfo.environment["AIBOARD_NO_SCHEDULE"] == nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) { self.startSchedule() }
        }
        if let cmd = ProcessInfo.processInfo.environment["AIBOARD_SELFTEST"] { selfTest(cmd) }
        if let out = ProcessInfo.processInfo.environment["AIBOARD_JS_TEST"], let js = ProcessInfo.processInfo.environment["AIBOARD_JS"] {
            // UAT 用: 盤が読み込まれてから JS(async 可)を実行し、戻り値を JSON で書いて終わる
            let wait = Double(ProcessInfo.processInfo.environment["AIBOARD_JS_WAIT"] ?? "8") ?? 8
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) {
                self.web.callAsyncJavaScript(js, arguments: [:], in: nil, in: .page) { r in
                    var rep: [String: Any] = [:]
                    switch r {
                    case .success(let v): rep["ok"] = true; rep["value"] = v ?? NSNull()
                    // アプリ側の状態(JS からは見えない): 端末の枚数・選択中・キーボードの入力先
                    rep["panes"] = self.pm.panes.map { ["tab": "0-\($0.id)", "kind": $0.kind, "cwd": $0.cwd, "tty": $0.tty] }
                    rep["selected"] = self.pm.selected.map { "0-\($0.id)" } ?? NSNull()
                    rep["firstResponderIsTerminal"] = self.pm.selected.map { self.window.firstResponder === $0.view } ?? false
                    rep["terminalVisible"] = !self.split.isSubviewCollapsed(self.split.arrangedSubviews[1])
                    rep["chimes"] = Chime.log
                    rep["paneHeader"] = self.pm.header.stringValue
                    rep["firstResponder"] = self.window.firstResponder.map { String(describing: type(of: $0)) } ?? "nil"
                    rep["strip"] = self.pm.strip.arrangedSubviews.compactMap { ($0 as? NSButton) }.map { ["tag": $0.tag, "title": $0.attributedTitle.string, "on": $0.state == .on,
                        "menu": ($0.menu?.items.map { $0.title } ?? []), "visible": $0.visibleRect.width > 0] }
                    rep["stripWidth"] = self.pm.strip.fittingSize.width
                    rep["stripVisibleWidth"] = self.pm.strip.enclosingScrollView?.contentView.bounds.width ?? 0
                    rep["linkedTab"] = self.pm.linkedTab
                    case .failure(let e): rep["ok"] = false; rep["error"] = String(describing: e)
                    }
                    if !JSONSerialization.isValidJSONObject(rep) { rep["value"] = String(describing: rep["value"] ?? "") }
                    if let d = try? JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted]) { try? d.write(to: URL(fileURLWithPath: out)) }
                    NSApp.terminate(nil)
                }
            }
        }
        if let n = ProcessInfo.processInfo.environment["AIBOARD_CHIME_TEST"], let times = Int(n) {
            for _ in 0..<times { Chime.play("Glass") }   // 自己試験: 間隔の規則を確かめる(音は出さない)
        }
        if let out = ProcessInfo.processInfo.environment["AIBOARD_ASK_TEST"] {
            // UAT 用: 判断待ちの一覧を流し込み、小窓の出方と答えの届き先を書き出す(画面は奪わない)
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) {   // 端末のシェルが立ち上がるのを待つ
                var rep: [String: Any] = [:]
                let tab = ProcessInfo.processInfo.environment["AIBOARD_ASK_TAB"] ?? "9-9"
                let row: [String: Any] = ["sid": "ask-uat", "tab": tab, "model": "Opus 5", "where": "uat",
                                          "doing": "⚠ Bash(npm test) を許可しますか", "task": "試験を流して", "inApp": tab.hasPrefix("0-")]
                let row2: [String: Any] = ["sid": "ask-uat-2", "tab": "9-8", "model": "Sonnet 5", "where": "uat2",
                                           "doing": "⚠ 2 件目", "task": "", "inApp": false]
                self.ask.update([], offscreen: true)
                rep["empty"] = self.ask.state
                self.ask.update([row, row2], offscreen: true)
                rep["one"] = self.ask.state
                if let k = ProcessInfo.processInfo.environment["AIBOARD_ASK_TAP"] { self.ask.tapKey(k) }
                if let t = ProcessInfo.processInfo.environment["AIBOARD_ASK_TYPE"] { self.ask.typeAnswer(t) }
                rep["after"] = self.ask.state
                self.ask.update([], offscreen: true)
                rep["closed"] = self.ask.state
                rep["log"] = self.ask.log
                DispatchQueue.main.asyncAfter(deadline: .now() + 8) {   // 端末へ渡すのは貼り付け→Enter の順で少し待つ
                    rep["log"] = self.ask.log
                    if let d = try? JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted]) {
                        try? d.write(to: URL(fileURLWithPath: out))
                    }
                    NSApp.terminate(nil)
                }
            }
        }
        if let out = ProcessInfo.processInfo.environment["AIBOARD_NOTIFY_TEST"] {
            // UAT 用: 通知が本当に macOS に届くかを確かめる(許可の状態・配信された ID・押した時の動き)。
            // 出した通知はすぐ取り下げるので、通知センターには残らない。
            let center = UNUserNotificationCenter.current()
            let id = "aiboard-uat-" + String(Int(Date().timeIntervalSince1970))
            center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in
                center.getNotificationSettings { st in
                    let auth = ["notDetermined", "denied", "authorized", "provisional", "ephemeral"][min(st.authorizationStatus.rawValue, 4)]
                    let c = UNMutableNotificationContent()
                    c.title = L("AIBoard self-test", "AIBoard 自己試験")
                    c.body = L("checking notification delivery", "通知が届くかの確認")
                    c.userInfo = ["tab": "0-1", "sid": "uat"]
                    center.add(UNNotificationRequest(identifier: id, content: c, trigger: nil)) { addErr in
                        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                            center.getDeliveredNotifications { ns in
                                let ids = ns.map { $0.request.identifier }
                                center.removeDeliveredNotifications(withIdentifiers: [id])
                                DispatchQueue.main.async {
                                    self.open(tab: "0-1")   // 通知を押した時と同じ道(押す操作だけは人の手)
                                    let rep: [String: Any] = ["auth": auth, "id": id, "delivered": ids.contains(id),
                                                              "add_error": addErr.map { String(describing: $0) } ?? "",
                                                              "selected": self.pm.selected.map { "0-\($0.id)" } ?? "",
                                                              "terminal_visible": !self.split.isSubviewCollapsed(self.split.arrangedSubviews[1])]
                                    if let d = try? JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted]) {
                                        try? d.write(to: URL(fileURLWithPath: out))
                                    }
                                    NSApp.terminate(nil)
                                }
                            }
                        }
                    }
                }
            }
        }
        if let out = ProcessInfo.processInfo.environment["AIBOARD_SWITCH_TEST"] {
            // 盤から switchAccount を送ったのと同じ経路で動かし、コピー先と起動コマンドを書き出す(AIBOARD_DRY と併用)
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                let e = ProcessInfo.processInfo.environment
                self.web.evaluateJavaScript("window.webkit.messageHandlers.aiboard.postMessage({type:'switchAccount', sid:'\(e["T_SID"] ?? "")', transcript:'\(e["T_TR"] ?? "")', configDir:'\(e["T_DIR"] ?? "")', cwd:'/tmp'}); 1") { _, _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                        var rep = self.lastSwitch
                        if let p = self.pm.panes.last { rep["script"] = (try? String(contentsOfFile: STATE_DIR + "/launch/pane-\(p.id).sh", encoding: .utf8)) ?? "" }
                        if let d = try? JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted]) { try? d.write(to: URL(fileURLWithPath: out)) }
                        NSApp.terminate(nil)
                    }
                }
            }
        }
        if let out = ProcessInfo.processInfo.environment["AIBOARD_RESTORE_TEST"] {
            // 読み込んだ state.json から復元し、各端末の起動スクリプトの中身を書き出して終わる
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                let before = self.lastState.count
                self.restoreLast(nil)
                let rows = self.pm.panes.map { p -> [String: Any] in
                    let script = (try? String(contentsOfFile: STATE_DIR + "/launch/pane-\(p.id).sh", encoding: .utf8)) ?? ""
                    return ["kind": p.kind, "cwd": p.cwd, "script": script]
                }
                let rep: [String: Any] = ["loaded": before, "opened": self.pm.panes.count, "panes": rows]
                if let d = try? JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted]) { try? d.write(to: URL(fileURLWithPath: out)) }
                NSApp.terminate(nil)
            }
        }
        if let shot = ProcessInfo.processInfo.environment["AIBOARD_SHOT"] {
            // 実画面の確認用。自分のウィンドウは画面収録の権限なしで撮れる(cacheDisplay はレイヤーの中身を落とすことがある)
            let wait = Double(ProcessInfo.processInfo.environment["AIBOARD_SHOT_WAIT"] ?? "8") ?? 8
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) { self.screenshot(to: shot); NSApp.terminate(nil) }
        }
    }

    // MARK: 盤のサーバ
    func startServerThenLoad() {
        DispatchQueue.global().async {
            // GUI アプリの PATH は最小なので、ログインシェル経由で呼ぶ(pyenv の python3 を拾う)
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/zsh")
            p.arguments = ["-l", "-c", "python3 " + shellQuote(BOARD_DIR + "/cs.py") + " web --no-open"]
            p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
            try? p.run(); p.waitUntilExit()
            DispatchQueue.main.async { self.web.load(URLRequest(url: BOARD_URL)) }
        }
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError e: Error) {
        w.loadHTMLString("<body style='font:14px -apple-system;padding:40px;background:#070A12;color:#ddd'>" + L("Cannot reach the board server. Press ⌘R to retry.", "盤のサーバに繋がらない。⌘R で再試行。") + "<br><small>\(e.localizedDescription)</small></body>", baseURL: nil)
    }

    // MARK: 盤からの連絡
    var webLog: [String] = []
    var lastFocusedTab = ""
    var lastSwitch: [String: Any] = [:]
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if m.name == "log" { webLog.append(String(describing: m.body)); if webLog.count > 200 { webLog.removeFirst(100) }; return }
        guard let b = m.body as? [String: Any], let type = b["type"] as? String else { return }
        switch type {
        case "_test_stripMenu":
            guard SELF_TEST, let id = b["id"] as? Int, let p = pm.panes.first(where: { $0.id == id }) else { return }
            let m = pm.stripTarget.menu(for: p)
            let title = (b["item"] as? String) ?? ""
            if let it = m.items.first(where: { $0.title.contains(title) }), let a = it.action { _ = pm.stripTarget.perform(a, with: it) }
        case "_test_stripTap":
            // 自己試験: 右のタブ列を押したのと同じ道(人の操作と同じ処理)
            guard SELF_TEST, let id = b["id"] as? Int, let btn = pm.strip.arrangedSubviews.compactMap({ $0 as? NSButton }).first(where: { $0.tag == id }) else { return }
            pm.stripTarget.tap(btn)
        case "show":
            // 左で会話を開いた: 右の端末も同じセッションにする(入力先は奪わない)。tab が空なら結び付きを外す
            let tab = (b["tab"] as? String) ?? ""
            if let p = pm.pane(tab: tab) { pm.linkedTab = p.id; showTerminal(); pm.select(p, focus: false) }
            else { pm.linkedTab = 0; if let cur = pm.selected { pm.titleChanged(cur) } }
        case "focus":
            if let tab = b["tab"] as? String, let p = pm.pane(tab: tab) {
                showTerminal(); pm.select(p); lastFocusedTab = tab
                // 盤のボタンのクリック処理が終わると入力先が盤に戻ることがあるので、一拍おいて端末に入力を移し直す
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.window.makeFirstResponder(p.view) }
            }
        case "resume":
            guard let ai = b["ai"] as? String, let id = b["id"] as? String, id.range(of: "^[0-9a-fA-F-]{16,}$", options: .regularExpression) != nil else { return }
            let cwd = (b["cwd"] as? String) ?? HOME
            let codex = ai == "Codex"
            showTerminal()
            pm.open(kind: codex ? "codex" : "claude", cwd: cwd, command: codex ? "codex resume \(id)" : "claude --resume \(id)")
            if let p = pm.panes.last { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.window.makeFirstResponder(p.view) } }
        case "send":
            // 会話ビューからの入力を、アプリの端末へ。文章は貼り付け+Enter、1 文字はキーとして、esc はエスケープ
            guard let tab = b["tab"] as? String, let p = pm.pane(tab: tab) else { return }
            if let key = b["key"] as? String {
                // 選択肢の画面(フォルダ信頼の確認・承認)は矢印で選ぶので、上下も送れるようにしてある
                let keys: [String: TerminalKey] = ["esc": .escape, "enter": .enter, "down": .arrowDown, "up": .arrowUp]
                if let k = keys[key] { _ = p.view.sendKey(k) }
            } else if let text = b["text"] as? String, !text.isEmpty, text.count <= 4000 {
                let enter = (b["enter"] as? Bool) ?? true
                if text.count == 1, !enter, let ch = text.first, let kp = TerminalKeyPress(typing: ch) { _ = p.view.sendKey(kp) }
                else { p.enqueue(text: text, enter: enter) }
            }
        case "newInProject":
            guard let key = b["key"] as? String, key.count <= 80, !key.contains("/"), !key.contains(".."),
                  let cwd = b["cwd"] as? String, cwd.hasPrefix("/"), !cwd.contains("..") else { return }
            openInProject(key: key, cwd: cwd, ai: (b["ai"] as? String) == "Codex" ? "Codex" : "Claude")
        case "newWithAccount":
            // アカウント一覧から「このアカウントで開く」: Claude はプロファイル、Codex はそのまま
            let ai = (b["ai"] as? String) ?? "Claude"
            let profile = (b["profile"] as? String) ?? ""
            guard profile.range(of: "^[A-Za-z0-9_-]{0,32}$", options: .regularExpression) != nil else { return }
            let cwd0 = (b["cwd"] as? String) ?? HOME
            let dir = (cwd0.hasPrefix("/") && !cwd0.contains("..") && FileManager.default.fileExists(atPath: cwd0)) ? cwd0 : HOME
            let cmd2: String
            if ai == "Codex" { cmd2 = "codex" }
            else if profile.isEmpty { cmd2 = "env -u CLAUDE_CONFIG_DIR command claude" }
            else { cmd2 = "CLAUDE_CONFIG_DIR=\(shellQuote(HOME + "/.claude-profiles/" + profile)) command claude" }
            showTerminal()
            pm.open(kind: ai == "Codex" ? "codex" : "claude", cwd: dir, command: cmd2)
            if let p = pm.panes.last { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.window.makeFirstResponder(p.view) } }
        case "delegate":
            // 案件に仕事を任せる: 選ばれた AI(と Claude のアカウント)で端末を起こし、共通の指示つきで依頼文を渡す
            guard let key = b["key"] as? String, key.count <= 80, !key.contains("/"), !key.contains(".."),
                  let text = b["text"] as? String, !text.isEmpty, text.count <= 4000,
                  let cwd = b["cwd"] as? String, cwd.hasPrefix("/"), !cwd.contains("..") else { return }
            let isCodex = (b["ai"] as? String) == "Codex"
            let profile = (b["profile"] as? String) ?? ""
            guard profile.range(of: "^[A-Za-z0-9_-]{0,32}$", options: .regularExpression) != nil else { return }
            let brief = projectBrief(key)
            let dir = FileManager.default.fileExists(atPath: cwd) ? cwd : HOME
            var cmd: String
            if isCodex {
                let path = writeInstructions(key, (brief.isEmpty ? "" : "これはこの案件の前提です。\n\n" + brief + "\n\n---\n\n") + "依頼: " + text)
                cmd = "codex \"$(cat \(shellQuote(path)))\""
            } else {
                let env = profile.isEmpty ? "env -u CLAUDE_CONFIG_DIR " : "CLAUDE_CONFIG_DIR=\(shellQuote(HOME + "/.claude-profiles/" + profile)) "
                let sys = brief.isEmpty ? "" : "--append-system-prompt-file \(shellQuote(writeInstructions(key, brief))) "
                let ask = writeInstructions(key + "-ask", text)
                cmd = env + "command claude " + sys + "\"$(cat \(shellQuote(ask)))\""
            }
            showTerminal()
            let pane = pm.open(kind: isCodex ? "codex" : "claude", cwd: dir, command: cmd)
            if let dg = b["deleg"] as? String, dg.range(of: "^[A-Za-z0-9_-]{1,40}$", options: .regularExpression) != nil { pane.deleg = dg; pm.publish() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.window.makeFirstResponder(pane.view) }
        case "run":
            // 設定画面から: ログインなど各 CLI のコマンドをアプリの端末で動かす(認証は CLI 自身が行う。AIBoard は資格情報を触らない)
            guard let cmd = b["command"] as? String, !cmd.isEmpty, cmd.count < 600,
                  ["command claude auth", "command codex log", "env -u CLAUDE_CONFIG_DIR command claude auth",
                   "CLAUDE_CONFIG_DIR=", "mkdir -p ~/.claude-profiles/",
                   "command cursor-agent login", "command cursor-agent logout", "command cursor-agent status",
                   "command gemini", "command grok"].contains(where: { cmd.hasPrefix($0) }) else { return }   // 決まった形以外は動かさない
            showTerminal()
            pm.open(kind: "shell", cwd: HOME, command: cmd)
        case "open":
            guard let path = b["path"] as? String, path.hasPrefix(HOME) || path.hasPrefix("/Users/") else { return }
            if !FileManager.default.fileExists(atPath: path) {
                try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
                if path.hasSuffix(".json") { try? "{}\n".write(toFile: path, atomically: true, encoding: .utf8) }
            }
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        case "notifySettings":
            // 通知の許可はアプリからは変えられない。設定の該当画面を開くところまで
            if let u = URL(string: "x-apple.systempreferences:com.apple.preference.notifications") { NSWorkspace.shared.open(u) }
        case "hook":
            let op = (b["op"] as? String) == "uninstall" ? "--uninstall" : "--install"
            runHook(op) { [weak self] _, out in
                let a = NSAlert(); a.messageText = "hook: " + out.trimmingCharacters(in: .whitespacesAndNewlines); a.runModal()
                self?.web.evaluateJavaScript("document.querySelector('#btnSettings') && document.querySelector('#btnSettings').click()") { _, _ in }
            }
        case "switchAccount":
            // 上限に当たった会話を、別アカウントで続ける: 記録を相手のアカウントの同じプロジェクト置き場へコピーし、そのアカウントで --resume
            guard let sid = b["sid"] as? String, sid.range(of: "^[0-9a-fA-F-]{16,}$", options: .regularExpression) != nil,
                  let tr = b["transcript"] as? String, tr.hasSuffix(".jsonl"), FileManager.default.fileExists(atPath: tr),
                  let dir = b["configDir"] as? String, !dir.isEmpty else { return }
            let cwd = (b["cwd"] as? String) ?? HOME
            let proj = ((tr as NSString).deletingLastPathComponent as NSString).lastPathComponent
            let destDir = dir + "/projects/" + proj
            try? FileManager.default.createDirectory(atPath: destDir, withIntermediateDirectories: true)
            let dest = destDir + "/" + sid + ".jsonl"
            if !FileManager.default.fileExists(atPath: dest) { try? FileManager.default.copyItem(atPath: tr, toPath: dest) }
            // 既定の ~/.claude なら CLAUDE_CONFIG_DIR を外す。zshrc の claude 関数(フォルダで切替)を通さず command claude で呼ぶ
            let isDefault = (dir as NSString).standardizingPath == (HOME + "/.claude")
            let cmd = (isDefault ? "env -u CLAUDE_CONFIG_DIR " : "CLAUDE_CONFIG_DIR=" + shellQuote(dir) + " ") + "command claude --resume " + sid
            showTerminal()
            pm.open(kind: "claude", cwd: cwd, command: cmd)
            lastSwitch = ["dest": dest, "copied": FileManager.default.fileExists(atPath: dest), "cmd": cmd]
        case "sessions":
            // 盤が知っている sid を端末に結び付けておく(次回起動時の復元に使う)
            for s in (b["list"] as? [[String: Any]] ?? []) {
                guard let tab = s["tab"] as? String, let p = pm.pane(tab: tab) else { continue }
                if let sid = s["sid"] as? String, !sid.hasPrefix("tty:") { p.sid = sid }
                if let text = s["label"] as? String, !text.isEmpty {
                    pm.labels[p.id] = (text, (s["rgb"] as? [Int]) ?? [120, 120, 120])
                }
                if let st = s["state"] as? String { pm.states[p.id] = st }
            }
            if let cur = pm.selected { pm.titleChanged(cur) }
            pm.publish()
        default: break
        }
    }

    /// タブを前面に。アプリの端末ならその端末、iTerm のタブなら盤サーバ経由で iTerm を前に出す
    /// 予約(Autorun)を見張る。アプリが開いている間だけ走る。走った仕事は普通のセッションとして盤に出る。
    /// crontab や launchd は触らない(再起動で消える・場所によっては読めない)。
    func startSchedule() {
        runDueJobs()   // 開いた時点で期限が来ている分は、待たずに走らせる
        scheduleTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.runDueJobs() }
    }

    func runDueJobs() {
        var req = URLRequest(url: BOARD_URL.appendingPathComponent("api/schedule")); req.timeoutInterval = 4
        req.setValue("1", forHTTPHeaderField: "X-Overview")
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self, let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let jobs = o["jobs"] as? [[String: Any]] else { return }
            DispatchQueue.main.async {
                for j in jobs where (j["due"] as? Bool) == true {
                    guard let id = j["id"] as? String, let prompt = j["prompt"] as? String, !prompt.isEmpty else { continue }
                    self.startJob(id: id, key: (j["key"] as? String) ?? "", prompt: prompt,
                                  cwd: (j["cwd"] as? String) ?? HOME, ai: (j["ai"] as? String) ?? "Claude")
                }
            }
        }.resume()
    }

    func startJob(id: String, key: String, prompt: String, cwd: String, ai: String) {
        let isCodex = ai == "Codex"
        let brief = key.isEmpty ? "" : projectBrief(key)
        let dir = FileManager.default.fileExists(atPath: cwd) ? cwd : HOME
        let ask = writeInstructions("sched-" + id, (brief.isEmpty ? "" : brief + "\n\n---\n\n") + prompt)
        let cmd = isCodex ? "codex \"$(cat \(shellQuote(ask)))\""
                          : "command claude \"$(cat \(shellQuote(ask)))\""
        scheduleLog.append("run \(id) ai=\(ai) cwd=\(dir)")
        if ProcessInfo.processInfo.environment["AIBOARD_DRY"] == nil {
            showTerminal()
            pm.open(kind: isCodex ? "codex" : "claude", cwd: dir, command: cmd)
        }
        var req = URLRequest(url: BOARD_URL.appendingPathComponent("api/schedule")); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type"); req.setValue("1", forHTTPHeaderField: "X-Overview")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["op": "ran", "id": id])
        URLSession.shared.dataTask(with: req).resume()
    }

    /// 判断待ちへの答えを、その端末へ。アプリの端末は直接、iTerm のタブは盤サーバの /api/send に頼む。
    func answer(tab: String, sid: String, key: String, text: String) {
        if let p = pm.pane(tab: tab) {
            if key == "esc" { _ = p.view.sendKey(.escape) }
            else if text.count == 1, let ch = text.first, let kp = TerminalKeyPress(typing: ch) { _ = p.view.sendKey(kp) }
            else if !text.isEmpty { p.enqueue(text: text, enter: true) }
            return
        }
        var body: [String: Any] = ["tab": tab, "sid": sid]
        if key == "esc" { body["key"] = "esc" } else { body["text"] = text; body["enter"] = text.count > 1 }
        if ProcessInfo.processInfo.environment["AIBOARD_DRY"] != nil {   // 試験: 実際には送らず、送る中身を残す
            let j = (try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])).flatMap { String(data: $0, encoding: .utf8) } ?? ""
            ask.log.append("post " + j)
            return
        }
        var req = URLRequest(url: BOARD_URL.appendingPathComponent("api/send")); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type"); req.setValue("1", forHTTPHeaderField: "X-Overview")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req).resume()
    }

    func open(tab: String) {
        if let p = pm.pane(tab: tab) { NSApp.activate(ignoringOtherApps: true); showTerminal(); pm.select(p); return }
        var req = URLRequest(url: BOARD_URL.appendingPathComponent("api/go")); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type"); req.setValue("1", forHTTPHeaderField: "X-Overview")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["tab": tab])
        URLSession.shared.dataTask(with: req).resume()
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse, withCompletionHandler done: @escaping () -> Void) {
        if let tab = r.notification.request.content.userInfo["tab"] as? String, !tab.isEmpty { open(tab: tab) }
        done()
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification, withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])   // アプリが前面でも出す(盤を見ていない別ウィンドウのことがある)
    }

    // MARK: Claude Code の hook(同意を取ってから入れる)
    /// hook が無いと Claude の「作業中/判断待ち」が盤に出ない。初回に確認し、断られたら二度と聞かない(メニューから入れられる)
    func offerHookIfNeeded(force: Bool = false) {
        let declined = STATE_DIR + "/hook-declined"
        if !force && FileManager.default.fileExists(atPath: declined) { return }
        if SELF_TEST { return }
        runHook("--check") { rc, _ in
            guard rc != 0 || force else { return }
            if rc == 0 { let a = NSAlert(); a.messageText = L("The Claude Code hook is already installed.", "Claude Code の hook は入っています。"); a.runModal(); return }
            let a = NSAlert()
            a.messageText = L("Show what Claude Code is doing?", "Claude Code の状態を盤に出しますか？")
            a.informativeText = L("AIBoard adds a small hook to ~/.claude/settings.json so each session reports \"working / needs you / replied\". Your existing settings and hooks are kept, and a backup is saved next to the file. Nothing is sent anywhere. You can remove it later from the Terminal menu.",
                                  "~/.claude/settings.json に小さな hook を足し、各セッションが「作業中 / 判断待ち / 返答済み」を知らせるようにします。今の設定や hook はそのまま残し、同じ場所に控えを保存します。外部には何も送りません。あとで「端末」メニューから外せます。")
            a.addButton(withTitle: L("Install", "入れる")); a.addButton(withTitle: L("Not now", "今はしない"))
            if a.runModal() == .alertFirstButtonReturn {
                self.runHook("--install") { rc2, out in
                    let b = NSAlert(); b.messageText = rc2 == 0 ? L("Installed. New Claude Code sessions will appear with their state.", "入れました。新しく起動した Claude Code から状態が出ます。") : L("Could not install the hook.", "hook を入れられませんでした。")
                    if rc2 != 0 { b.informativeText = out }
                    b.runModal()
                }
            } else {
                try? "".write(toFile: declined, atomically: true, encoding: .utf8)
            }
        }
    }
    func runHook(_ op: String, done: @escaping (Int32, String) -> Void) {
        DispatchQueue.global().async {
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/zsh")
            p.arguments = ["-l", "-c", "python3 " + shellQuote(BOARD_DIR + "/install_hook.py") + " " + op]
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
            try? p.run()
            let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit()
            DispatchQueue.main.async { done(p.terminationStatus, out) }
        }
    }
    @objc func openSettings(_ s: Any?) { if boardHidden { toggleBoard(nil) }; web.evaluateJavaScript("document.querySelector('#btnSettings').click()") { _, _ in } }
    @objc func installHookMenu(_ s: Any?) { offerHookIfNeeded(force: true) }
    @objc func uninstallHookMenu(_ s: Any?) {
        runHook("--uninstall") { _, out in let a = NSAlert(); a.messageText = L("Hook: ", "hook: ") + out.trimmingCharacters(in: .whitespacesAndNewlines); a.runModal() }
    }

    // MARK: 操作
    func showTerminal() { if split.isSubviewCollapsed(split.arrangedSubviews[1]) { split.setPosition(split.bounds.width * 0.5, ofDividerAt: 0) } }
    func currentCwd() -> String { pm.selected?.cwd ?? HOME }
    @objc func newClaude(_ s: Any?) { showTerminal(); pm.open(kind: "claude", cwd: currentCwd(), command: "claude") }
    @objc func newCodex(_ s: Any?) { showTerminal(); pm.open(kind: "codex", cwd: currentCwd(), command: "codex") }
    @objc func newShell(_ s: Any?) { showTerminal(); pm.open(kind: "shell", cwd: currentCwd(), command: nil) }
    /// 同じリポジトリの新しい git worktree で Claude を起動(`claude --worktree`)。並列に回す人の基本形
    @objc func newClaudeWorktree(_ s: Any?) { showTerminal(); pm.open(kind: "claude", cwd: currentCwd(), command: "claude --worktree") }
    @objc func newInFolder(_ s: Any?) {
        let op = NSOpenPanel(); op.canChooseDirectories = true; op.canChooseFiles = false; op.prompt = L("Open Claude here", "ここで Claude を開く")
        op.directoryURL = URL(fileURLWithPath: currentCwd())
        if op.runModal() == .OK, let u = op.url { showTerminal(); pm.open(kind: "claude", cwd: u.path, command: "claude") }
    }
    @objc func closePane(_ s: Any?) { if let p = pm.selected { pm.close(p) } else { window.performClose(nil) } }
    /// ⌃⌘1…9 で n 枚目の端末へ(右のタブ列の並びと同じ番号)。左の会話も追従する
    @objc func paneByNumber(_ sender: Any?) {
        guard let it = sender as? NSMenuItem, it.tag >= 1, it.tag <= pm.panes.count else { return }
        let p = pm.panes[it.tag - 1]
        showTerminal(); pm.select(p); pm.onUserSelect?(p)
    }
    @objc func nextPane(_ s: Any?) { pm.step(1) }
    @objc func prevPane(_ s: Any?) { pm.step(-1) }
    @objc func bigger(_ s: Any?) { pm.setFont(delta: 1) }
    @objc func smaller(_ s: Any?) { pm.setFont(delta: -1) }
    @objc func reloadBoard(_ s: Any?) { web.load(URLRequest(url: BOARD_URL)) }
    @objc func toggleBoard(_ s: Any?) {
        boardHidden.toggle()
        split.setPosition(boardHidden ? 0 : split.bounds.width * 0.5, ofDividerAt: 0)
        if boardHidden, let p = pm.selected { window.makeFirstResponder(p.view) }
    }
    @objc func focusBoard(_ s: Any?) { if boardHidden { toggleBoard(nil) }; window.makeFirstResponder(web) }
    @objc func focusTerminal(_ s: Any?) { if let p = pm.selected { showTerminal(); window.makeFirstResponder(p.view) } }
    /// 案件(枠)ごとの共通の指示。~/.aiboard/groups.json に盤の設定画面から保存されたもの
    func projectInstructions(_ key: String) -> String {
        guard let d = FileManager.default.contents(atPath: STATE_DIR + "/groups.json"),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let g = o["groups"] as? [String: Any], let row = g[key] as? [String: Any],
              let t = row["instructions"] as? String, !t.isEmpty else { return "" }
        return t
    }

    /// 案件の申し送り(引き継ぎ)。盤の案件パネルから書かれたもの
    func projectNotes(_ key: String) -> String {
        let safe = String(key.map { "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_".contains($0) ? $0 : "_" }.prefix(64))
        return (try? String(contentsOfFile: STATE_DIR + "/projects/" + safe + ".notes.md", encoding: .utf8)) ?? ""
    }

    /// 端末に渡す前提(共通の指示 + 申し送り)。どちらも空なら空
    func projectBrief(_ key: String) -> String {
        let ins = projectInstructions(key), notes = projectNotes(key).trimmingCharacters(in: .whitespacesAndNewlines)
        if ins.isEmpty && notes.isEmpty { return "" }
        if notes.isEmpty { return ins }
        let head = ins.isEmpty ? "" : ins + "\n\n"
        return head + "## これまでの申し送り\n" + notes
    }

    /// 指示を書いたファイルの場所(端末に渡す。盤や他人には渡さない)
    func writeInstructions(_ key: String, _ text: String) -> String {
        let dir = STATE_DIR + "/projects"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let safe = key.map { "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_".contains($0) ? $0 : "_" }
        let path = dir + "/" + String(safe).prefix(64) + ".md"
        try? text.write(toFile: path, atomically: true, encoding: .utf8)
        return path
    }

    /// 案件の指示つきで端末を開く。Claude は --append-system-prompt-file、Codex は最初のメッセージとして渡す
    /// (Codex には同等の指定が無いため。2026-09-18 に CLI の help と実行で確認)
    func openInProject(key: String, cwd: String, ai: String) {
        let text = projectBrief(key)
        let dir = FileManager.default.fileExists(atPath: cwd) ? cwd : HOME
        var cmd = ai == "Codex" ? "codex" : "claude"
        if !text.isEmpty {
            let path = writeInstructions(key, ai == "Codex"
                ? "これはこの案件の前提です。読んで把握し、作業は次の指示を待ってください。\n\n" + text
                : text)
            cmd = ai == "Codex" ? "codex \"$(cat \(shellQuote(path)))\"" : "claude --append-system-prompt-file \(shellQuote(path))"
        }
        showTerminal()
        pm.open(kind: ai == "Codex" ? "codex" : "claude", cwd: dir, command: cmd)
        if let p = pm.panes.last { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.window.makeFirstResponder(p.view) } }
    }

    @objc func fitWindow(_ s: Any?) {
        guard let vis = NSScreen.main?.visibleFrame else { return }
        window.setFrame(vis.insetBy(dx: 40, dy: 30), display: true, animate: true)
    }

    @objc func restoreLast(_ s: Any?) {
        for st in lastState {
            let kind = st["kind"] as? String ?? "shell", cwd = st["cwd"] as? String ?? HOME, sid = st["sid"] as? String ?? ""
            // sid が分からない時は推測で最新を開かず、一覧から選ばせる(同じフォルダの 2 枚が同じ会話に化けるのを防ぐ)
            let cmd: String? = kind == "claude" ? (sid.isEmpty ? "claude --resume" : "claude --resume \(sid)")
                             : kind == "codex" ? (sid.isEmpty ? "codex resume" : "codex resume \(sid)") : nil
            pm.open(kind: kind, cwd: cwd, command: cmd)
        }
        lastState = []
    }

    func splitView(_ s: NSSplitView, canCollapseSubview v: NSView) -> Bool { true }
    func splitView(_ s: NSSplitView, constrainMinCoordinate p: CGFloat, ofSubviewAt i: Int) -> CGFloat { 0 }
    func splitView(_ s: NSSplitView, constrainMaxCoordinate p: CGFloat, ofSubviewAt i: Int) -> CGFloat { s.bounds.width - 360 }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        let busy = pm.panes.filter { $0.kind != "shell" }.count
        if busy == 0 || SELF_TEST { return .terminateNow }
        let a = NSAlert(); a.messageText = L("\(busy) AI terminal(s) are open", "AI の端末が \(busy) 枚開いています")
        a.informativeText = L("Quitting stops the sessions inside them (you can restore them next time with Restore last terminals).", "終了すると端末の中のセッションも止まります(次回「前回の端末を復元」で再開できます)。")
        a.addButton(withTitle: L("Quit", "終了する")); a.addButton(withTitle: L("Cancel", "やめる"))
        return a.runModal() == .alertFirstButtonReturn ? .terminateNow : .terminateCancel
    }
    func applicationWillTerminate(_ n: Notification) {
        pm.publish(); pm.terminating = true
        // 盤の道具に「もう端末は無い」と知らせる(古い一覧を残さない)
        try? FileManager.default.removeItem(atPath: PANES_FILE)
    }

    // MARK: メニュー(コピー/ペーストは標準のレスポンダ経由で端末に届く)
    func buildMenu() {
        let main = NSMenu()
        func menu(_ title: String, _ items: [(String, Selector?, String, NSEvent.ModifierFlags)]) {
            let mi = NSMenuItem(); let m = NSMenu(title: title)
            for (t, sel, key, mods) in items {
                if t == "-" { m.addItem(.separator()); continue }
                let it = NSMenuItem(title: t, action: sel, keyEquivalent: key); it.keyEquivalentModifierMask = mods; m.addItem(it)
            }
            if title == L("View", "表示") || title == L("Window", "ウインドウ") {
                // ⌃⌘1…9 で n 枚目の端末へ(右のタブ列と同じ番号)
                for n in 1...9 {
                    let it = NSMenuItem(title: L("Terminal \(n)", "端末 \(n)"), action: #selector(paneByNumber(_:)), keyEquivalent: String(n))
                    it.keyEquivalentModifierMask = [.control, .command]; it.tag = n; m.addItem(it)
                }
            }
            mi.submenu = m; main.addItem(mi)
        }
        menu("AIBoard", [(L("Settings…", "設定…"), #selector(openSettings(_:)), ",", .command), ("-", nil, "", []),
                         (L("Hide AIBoard", "AIBoard を隠す"), #selector(NSApplication.hide(_:)), "h", .command), ("-", nil, "", []),
                         (L("Quit AIBoard", "AIBoard を終了"), #selector(NSApplication.terminate(_:)), "q", .command)])
        menu(L("Terminal", "端末"), [(L("New Claude", "Claude を開く"), #selector(newClaude(_:)), "t", .command),
                     (L("New Codex", "Codex を開く"), #selector(newCodex(_:)), "t", [.command, .option]),
                     (L("New Shell", "シェルを開く"), #selector(newShell(_:)), "t", [.command, .shift]),
                     (L("New Claude in Git Worktree", "git worktree で Claude を開く"), #selector(newClaudeWorktree(_:)), "w", [.command, .shift]),
                     (L("Claude in Folder…", "フォルダを選んで Claude…"), #selector(newInFolder(_:)), "o", .command), ("-", nil, "", []),
                     (L("Restore Last Terminals", "前回の端末を復元"), #selector(restoreLast(_:)), "r", [.command, .shift]), ("-", nil, "", []),
                     (L("Fit Window to Screen", "窓を画面に合わせる"), #selector(fitWindow(_:)), "", []),
                     (L("Install Claude Code Hook…", "Claude Code の hook を入れる…"), #selector(installHookMenu(_:)), "", []),
                     (L("Remove Claude Code Hook", "Claude Code の hook を外す"), #selector(uninstallHookMenu(_:)), "", []), ("-", nil, "", []),
                     (L("Close Terminal", "この端末を閉じる"), #selector(closePane(_:)), "w", .command)])
        menu(L("Edit", "編集"), [(L("Copy", "コピー"), #selector(NSText.copy(_:)), "c", .command), (L("Paste", "ペースト"), #selector(NSText.paste(_:)), "v", .command),
                     (L("Select All", "すべて選択"), #selector(NSText.selectAll(_:)), "a", .command)])
        menu(L("View", "表示"), [(L("Toggle Board", "盤を隠す/出す"), #selector(toggleBoard(_:)), "b", .command),
                     (L("Focus Board", "盤へ"), #selector(focusBoard(_:)), "1", .command), (L("Focus Terminal", "端末へ"), #selector(focusTerminal(_:)), "2", .command), ("-", nil, "", []),
                     (L("Next Terminal", "次の端末"), #selector(nextPane(_:)), "]", [.command, .shift]), (L("Previous Terminal", "前の端末"), #selector(prevPane(_:)), "[", [.command, .shift]),
                     ("-", nil, "", []),
                     (L("Bigger Text", "文字を大きく"), #selector(bigger(_:)), "+", .command), (L("Smaller Text", "文字を小さく"), #selector(smaller(_:)), "-", .command), ("-", nil, "", []),
                     (L("Reload Board", "盤を読み込み直す"), #selector(reloadBoard(_:)), "r", .command)])
        NSApp.mainMenu = main
    }

    func screenshot(to path: String) {
        let wid = CGWindowID(window.windowNumber)
        guard let img = CGWindowListCreateImage(.null, .optionIncludingWindow, wid, [.boundsIgnoreFraming, .bestResolution]) else { return }
        let rep = NSBitmapImageRep(cgImage: img)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }

    // MARK: 自己試験(無人で通しを確かめる。AIBOARD_SELFTEST=<出力先の接頭辞>)
    func selfTest(_ prefix: String) {
        let p = pm.open(kind: "shell", cwd: HOME, command: "echo \"PANE=$AIBOARD_PANE TTY=$(tty)\"; echo 日本語 盤 🟣; sleep 120")
        let p2 = pm.open(kind: "shell", cwd: HOME, command: "sleep 120")
        let p3 = pm.open(kind: "shell", cwd: HOME, command: nil)     // 素のログインシェル(プロンプトが出るか)
        let p4 = pm.open(kind: "codex", cwd: HOME, command: "codex")   // Codex の TUI が端末内で描けるか
        // 盤の 3 秒周期 × 数回ぶん待ってから、盤 → アプリの focus を JS 側から起こす
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) {
            self.web.evaluateJavaScript("window.webkit.messageHandlers.aiboard.postMessage({type:'focus', tab:'0-\(p.id)'}); 1") { _, _ in }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.pm.select(p4) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            // 見張りの判定: 起動時の状態では鳴らさず、変化した時だけ鳴る
            let w = Watcher(); w.dryRun = true; var counts: [Int] = []; w.onCount = { counts.append($0) }
            let base: [[String: Any]] = [["sid": "A", "tab": "1-1", "state": "作業中", "doing": "Bash: npm test", "project": "acme"],
                                         ["sid": "B", "tab": "0-1", "state": "作業中", "doing": "考え中", "project": "globex"],
                                         ["sid": "C", "tab": "1-3", "state": "確認待ち", "doing": "Allow Edit?", "project": "infra"]]
            w.feed(base)
            var next = base; next[0]["state"] = "確認待ち"; next[0]["doing"] = "Allow Bash?"; next[1]["state"] = "返答待ち"; next[1]["doing"] = "✅ 返答済み（あなたの番）"
            w.feed(next)
            w.feed(next)   // 同じ状態が続いても鳴らない
            var rep: [String: Any] = ["watch_log": w.dryLog, "watch_counts": counts, "pane_tty": p.tty, "panes_file_exists": FileManager.default.fileExists(atPath: PANES_FILE),
                                      "pane_size": "\(Int(p.view.frame.width))x\(Int(p.view.frame.height))",
                                      "split_left": Int(self.split.arrangedSubviews[0].frame.width), "split_total": Int(self.split.bounds.width),
                                      "web_log": self.webLog, "focus_ok": self.lastFocusedTab == "0-\(p.id)", "second_pane": p2.id,
                                      "plain_shell_tty": p3.tty, "plain_shell_pid": p3.pid,
                                      "codex_tty": p4.tty, "codex_pid": p4.pid,
                                      "codex_running": p4.pid > 0 && !p4.tty.isEmpty]   // login ラッパーは root なので kill(pid,0) は EPERM になる
            rep["pane_pid"] = p.pid
            self.web.evaluateJavaScript("JSON.stringify({aiboard: window.AIBOARD === true, cards: document.querySelectorAll('.card').length, appCards: [...document.querySelectorAll('.card .st')].filter(e => /(タブ|tab) 0-/.test(e.textContent)).length, status: document.querySelector('#status').textContent})") { v, _ in
                rep["board"] = v as? String ?? "(評価できない)"
                if let d = try? JSONSerialization.data(withJSONObject: rep, options: [.prettyPrinted]) { try? d.write(to: URL(fileURLWithPath: prefix + ".json")) }
                self.screenshot(to: prefix + ".png")
                NSApp.terminate(nil)
            }
        }
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}


// MARK: - 盤の confirm / alert / prompt を macOS のダイアログで出す
// AIBOARD_DIALOG_AUTO=yes|no を付けると押さずに答える(自己試験用)。答えた内容は web ログに残す
extension AppDelegate: WKUIDelegate {
    private func dialogAuto() -> String? { ProcessInfo.processInfo.environment["AIBOARD_DIALOG_AUTO"] }

    private func logDialog(_ kind: String, _ msg: String, _ answer: String) {
        let line = "dialog: \(kind) answer=\(answer) message=\(msg.prefix(120))\n"
        let path = STATE_DIR + "/dialog.log"
        if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
        else { try? line.write(toFile: path, atomically: true, encoding: .utf8) }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        if dialogAuto() != nil { logDialog("alert", message, "ok"); completionHandler(); return }
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        if let auto = dialogAuto() { logDialog("confirm", message, auto); completionHandler(auto == "yes"); return }
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: L("OK", "OK")); a.addButton(withTitle: L("Cancel", "キャンセル"))
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        if let auto = dialogAuto() { logDialog("prompt", prompt, auto); completionHandler(auto == "no" ? nil : (defaultText ?? "")); return }
        let a = NSAlert(); a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24)); field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: L("OK", "OK")); a.addButton(withTitle: L("Cancel", "キャンセル"))
        a.window.initialFirstResponder = field
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? field.stringValue : nil) }
    }
}
