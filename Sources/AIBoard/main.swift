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
let BOARD_URL = URL(string: "http://127.0.0.1:8791/")!

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
        let path = dirp + "/pane-\(id).sh"
        var body = command ?? ""
        if ProcessInfo.processInfo.environment["AIBOARD_DRY"] != nil, !body.isEmpty { body = "echo WOULD_RUN: " + shellQuote(body) }   // 試験: 実行しない
        try? ("export AIBOARD_PANE=\(id) TERM_PROGRAM=AIBoard\ncd " + shellQuote(dir) + "\n" + body + "\n").write(toFile: path, atomically: true, encoding: .utf8)
        let cmdline = "/bin/zsh -l -i -c source\u{a0}\(path);exec\u{a0}/bin/zsh\u{a0}-l"
        view.configuration = TerminalSurfaceOptions(
            backend: .exec, workingDirectory: dir,
            envVars: ["LANG": "ja_JP.UTF-8", "LC_CTYPE": "ja_JP.UTF-8", "TERM_PROGRAM": "AIBoard", "AIBOARD_PANE": "\(id)"],
            command: cmdline, waitAfterCommand: false)
        resolveTty(attempt: 0)
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

    func select(_ p: Pane) {
        selected?.view.isHidden = true
        selected = p
        p.view.isHidden = false
        p.view.frame = container.bounds
        panes.forEach { $0.view.setSurfaceVisible($0 === p) }   // 見えない端末は描画を止める(セッションは続く)
        p.view.needsDisplay = true
        p.view.window?.makeFirstResponder(p.view)
        titleChanged(p)
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
            header.stringValue = "\(mark)  ·  \(p.id)/\(panes.count)  ·  \(p.title)"
        }
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
            ["pane": $0.id, "tty": $0.tty, "title": $0.title, "kind": $0.kind, "cwd": $0.cwd, "pid": $0.pid] }
        write(["updated": Date().timeIntervalSince1970, "app_pid": Int(ProcessInfo.processInfo.processIdentifier), "panes": list], to: PANES_FILE)
        if terminating { return }   // 終了時に端末が 1 枚ずつ閉じるたびに書き直すと、保存した一覧が空になる
        let st: [[String: Any]] = panes.map { ["kind": $0.kind, "cwd": $0.cwd, "sid": $0.sid] }
        try? FileManager.default.createDirectory(atPath: STATE_DIR, withIntermediateDirectories: true)
        write(["saved": Date().timeIntervalSince1970, "panes": st], to: STATE_FILE)
    }

    private func write(_ obj: [String: Any], to path: String) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted]) else { return }
        let tmp = path + ".\(ProcessInfo.processInfo.processIdentifier).tmp"
        if (try? d.write(to: URL(fileURLWithPath: tmp))) != nil {
            _ = try? FileManager.default.replaceItemAt(URL(fileURLWithPath: path), withItemAt: URL(fileURLWithPath: tmp))
        }
    }
}

// MARK: - 見張り(盤を見ていなくても気づける: macOS 通知と Dock バッジ)

/// 盤サーバの snapshot を 3 秒ごとに読み、「判断待ち」「停止」になった瞬間に通知する。
/// 返答済みは、このアプリの中で動いている端末だけ通知する(iTerm 側のまで鳴ると多すぎる)。
/// 通知の本文はサーバ側で秘密を伏せたもの(redact 済み)。外部には何も送らない。
@MainActor
final class Watcher {
    private var timer: Timer?
    private var known: [String: String] = [:]     // sid → state
    private var primed = false                     // 最初の 1 回は「今の状態」を覚えるだけ(起動時に通知の嵐を出さない)
    var onCount: ((Int) -> Void)?
    var onOpen: ((String, String) -> Void)?        // (tab, sid)
    let center = UNUserNotificationCenter.current()
    var dryRun = false                             // 自己試験: 実際には出さず dryLog に積む
    var dryLog: [String] = []
    func feed(_ sessions: [[String: Any]]) { update(sessions) }   // 自己試験用

    func start() {
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func poll() {
        var req = URLRequest(url: BOARD_URL.appendingPathComponent("api/snapshot")); req.timeoutInterval = 4
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self, let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let sessions = o["sessions"] as? [[String: Any]] else { return }
            DispatchQueue.main.async { self.update(sessions) }
        }.resume()
    }

    private func update(_ sessions: [[String: Any]]) {
        var now: [String: String] = [:]
        var needs = 0
        for s in sessions {
            guard let sid = s["sid"] as? String, !sid.isEmpty, let state = s["state"] as? String else { continue }
            now[sid] = state
            let tab = s["tab"] as? String ?? ""
            let inApp = tab.hasPrefix("0-")
            let model = (s["model_style"] as? [String: Any])?["label"] as? String ?? (s["ai"] as? String ?? "AI")
            let where_ = (s["project"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? tab
            let doing = (s["doing"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            if state == "確認待ち" { needs += 1 }
            guard primed, known[sid] != state else { continue }
            if state == "確認待ち" {
                notify(id: sid, title: L("\(model) needs you · \(where_)", "\(model) があなたの判断待ち · \(where_)"), body: doing, tab: tab, sid: sid)
            } else if state == "codex 停止" || doing.hasPrefix("⛔") {
                notify(id: sid, title: L("\(model) stopped · \(where_)", "\(model) が停止 · \(where_)"), body: doing, tab: tab, sid: sid)
            } else if inApp, state == "返答待ち" || state == "codex 返答待ち" {
                notify(id: sid, title: L("\(model) replied · \(where_)", "\(model) が返答 · \(where_)"), body: L("Your turn.", "あなたの番です。"), tab: tab, sid: sid)
            }
        }
        known = now
        primed = true
        onCount?(needs)
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

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, NSSplitViewDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    let watcher = Watcher()
    let split = NSSplitView()
    var web: WKWebView!
    let pm = PaneManager()
    var boardHidden = false
    var lastState: [[String: Any]] = []

    func applicationDidFinishLaunching(_ n: Notification) {
        if let d = FileManager.default.contents(atPath: STATE_FILE),
           let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { lastState = o["panes"] as? [[String: Any]] ?? [] }
        buildMenu()
        let frame = NSScreen.main?.visibleFrame.insetBy(dx: 40, dy: 30) ?? NSRect(x: 0, y: 0, width: 1500, height: 900)
        window = NSWindow(contentRect: frame, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "AIBoard"
        window.setFrameAutosaveName("AIBoardMain")
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

        let right = NSView()
        right.wantsLayer = true
        right.layer?.backgroundColor = NSColor(srgbRed: 0.02, green: 0.027, blue: 0.047, alpha: 1).cgColor   // #05070C 端末と同じ地
        pm.header.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)
        pm.header.textColor = NSColor(srgbRed: 0.545, green: 0.58, blue: 0.655, alpha: 1)   // #8B94A7
        pm.header.lineBreakMode = .byTruncatingTail
        pm.header.stringValue = L("No terminal — ⌘T Claude · ⌥⌘T Codex · ⇧⌘T shell", "端末なし — ⌘T で Claude、⌥⌘T で Codex、⇧⌘T でシェル")
        pm.header.translatesAutoresizingMaskIntoConstraints = false
        pm.container.translatesAutoresizingMaskIntoConstraints = false
        right.addSubview(pm.header); right.addSubview(pm.container)
        NSLayoutConstraint.activate([
            pm.header.topAnchor.constraint(equalTo: right.topAnchor, constant: 34),   // 透かしたタイトルバーの下
            pm.header.leadingAnchor.constraint(equalTo: right.leadingAnchor, constant: 10),
            pm.header.trailingAnchor.constraint(equalTo: right.trailingAnchor, constant: -10),
            pm.container.topAnchor.constraint(equalTo: pm.header.bottomAnchor, constant: 6),
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
        window.makeKeyAndOrderFront(nil)
        window.contentView?.layoutSubtreeIfNeeded()
        split.setPosition(split.bounds.width * 0.5, ofDividerAt: 0)
        NSApp.activate(ignoringOtherApps: true)
        startServerThenLoad()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.offerHookIfNeeded() }
        // 通知と Dock バッジ(判断待ちの数)。通知を押すと、その端末へ
        watcher.center.delegate = self
        watcher.onCount = { n in NSApp.dockTile.badgeLabel = n > 0 ? String(n) : nil }
        watcher.onOpen = { [weak self] tab, _ in self?.open(tab: tab) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self.watcher.start() }
        if let cmd = ProcessInfo.processInfo.environment["AIBOARD_SELFTEST"] { selfTest(cmd) }
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
        case "focus":
            if let tab = b["tab"] as? String, let p = pm.pane(tab: tab) { showTerminal(); pm.select(p); lastFocusedTab = tab }
        case "resume":
            guard let ai = b["ai"] as? String, let id = b["id"] as? String, id.range(of: "^[0-9a-fA-F-]{16,}$", options: .regularExpression) != nil else { return }
            let cwd = (b["cwd"] as? String) ?? HOME
            let codex = ai == "Codex"
            showTerminal()
            pm.open(kind: codex ? "codex" : "claude", cwd: cwd, command: codex ? "codex resume \(id)" : "claude --resume \(id)")
        case "send":
            // 会話ビューからの入力を、アプリの端末へ。文章は貼り付け+Enter、1 文字はキーとして、esc はエスケープ
            guard let tab = b["tab"] as? String, let p = pm.pane(tab: tab) else { return }
            if let key = b["key"] as? String {
                if key == "esc" { _ = p.view.sendKey(.escape) } else if key == "enter" { _ = p.view.sendKey(.enter) }
            } else if let text = b["text"] as? String, !text.isEmpty, text.count <= 4000 {
                let enter = (b["enter"] as? Bool) ?? true
                if text.count == 1, !enter, let ch = text.first, let kp = TerminalKeyPress(typing: ch) { _ = p.view.sendKey(kp) }
                else { _ = p.view.paste(text: text); if enter { _ = p.view.sendKey(.enter) } }
            }
        case "run":
            // 設定画面から: ログインなど各 CLI のコマンドをアプリの端末で動かす(認証は CLI 自身が行う。AIBoard は資格情報を触らない)
            guard let cmd = b["command"] as? String, !cmd.isEmpty, cmd.count < 600,
                  cmd.hasPrefix("command claude auth") || cmd.hasPrefix("command codex log") || cmd.hasPrefix("env -u CLAUDE_CONFIG_DIR command claude auth")
                  || cmd.hasPrefix("CLAUDE_CONFIG_DIR=") || cmd.hasPrefix("mkdir -p ~/.claude-profiles/") else { return }   // 決まった形以外は動かさない
            showTerminal()
            pm.open(kind: "shell", cwd: HOME, command: cmd)
        case "open":
            guard let path = b["path"] as? String, path.hasPrefix(HOME) || path.hasPrefix("/Users/") else { return }
            if !FileManager.default.fileExists(atPath: path) {
                try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
                if path.hasSuffix(".json") { try? "{}\n".write(toFile: path, atomically: true, encoding: .utf8) }
            }
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
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
                if let tab = s["tab"] as? String, let sid = s["sid"] as? String, let p = pm.pane(tab: tab), !sid.hasPrefix("tty:") { p.sid = sid }
            }
            pm.publish()
        default: break
        }
    }

    /// タブを前面に。アプリの端末ならその端末、iTerm のタブなら盤サーバ経由で iTerm を前に出す
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
        if ProcessInfo.processInfo.environment["AIBOARD_SELFTEST"] != nil || ProcessInfo.processInfo.environment["AIBOARD_RESTORE_TEST"] != nil { return }
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
        if busy == 0 || ProcessInfo.processInfo.environment["AIBOARD_SELFTEST"] != nil { return .terminateNow }
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
                     (L("Install Claude Code Hook…", "Claude Code の hook を入れる…"), #selector(installHookMenu(_:)), "", []),
                     (L("Remove Claude Code Hook", "Claude Code の hook を外す"), #selector(uninstallHookMenu(_:)), "", []), ("-", nil, "", []),
                     (L("Close Terminal", "この端末を閉じる"), #selector(closePane(_:)), "w", .command)])
        menu(L("Edit", "編集"), [(L("Copy", "コピー"), #selector(NSText.copy(_:)), "c", .command), (L("Paste", "ペースト"), #selector(NSText.paste(_:)), "v", .command),
                     (L("Select All", "すべて選択"), #selector(NSText.selectAll(_:)), "a", .command)])
        menu(L("View", "表示"), [(L("Toggle Board", "盤を隠す/出す"), #selector(toggleBoard(_:)), "b", .command),
                     (L("Focus Board", "盤へ"), #selector(focusBoard(_:)), "1", .command), (L("Focus Terminal", "端末へ"), #selector(focusTerminal(_:)), "2", .command), ("-", nil, "", []),
                     (L("Next Terminal", "次の端末"), #selector(nextPane(_:)), "]", [.command, .shift]), (L("Previous Terminal", "前の端末"), #selector(prevPane(_:)), "[", [.command, .shift]), ("-", nil, "", []),
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
