// StructorTray — a macOS menu-bar status + controller for Structor.
//
// Shows store totals from /api/structor/status, lets you start/stop the local
// server, the structor-cli watcher and the two structor-lance replicas (the Bun
// one and the Python one), switch between targets (local, kvmlab1), open the
// dashboard / PocketBase admin / either LanceDB admin, and ask the Python
// replica a grounded question from a small floating panel.
//
// Config: ~/.config/structor/tray.json
// {
//   "targets": [
//     {"name": "local",   "url": "http://127.0.0.1:8091", "email": "admin@structor.local", "password": "structor-dev-password"},
//     {"name": "kvmlab1", "url": "http://<haos-host>:8090", "email": "…", "password": "…"}
//   ],
//   "current": "local",
//   "serverBinary": "/path/to/app/bin/structor",
//   "cliBinary": "/path/to/app/bin/structor-cli",
//   "dataDir": "/path/to/app/pb_data",
//   "watchDir": "~/.claude/projects"
// }
// Missing file → sensible defaults relative to the repo the app was built in.
//
// The remaining keys are optional and may be absent from a config written
// before the replicas existed; each falls back to a computed default, so an
// older file keeps working untouched:
//   "lanceUrl":    "http://127.0.0.1:8092"       admin of the Bun replica
//   "lanceBind":   "127.0.0.1:8092"              host:port a tray-started Bun replica binds
//   "bunBinary":   "/Users/you/.bun/bin/bun"     default: first of ~/.bun/bin/bun,
//                                                /opt/homebrew/bin/bun, /usr/local/bin/bun
//   "lanceDir":    "/path/to/app/lance"          the structor-lance package dir
//   "lancePyUrl":  "http://127.0.0.1:8094"       admin of the Python replica
//   "lancePyBind": "127.0.0.1:8094"              host:port a tray-started Python replica binds
//   "uvBinary":    "/opt/homebrew/bin/uv"        default: first of ~/.local/bin/uv,
//                                                /opt/homebrew/bin/uv, /usr/local/bin/uv
//   "lancePyDir":  "/path/to/app/lance-py"       the Python structor-lance project dir
// Adding a required key here would be a trap: Config.load() rewrites the file
// with defaults whenever decoding fails, which would silently drop the targets
// and their credentials. New keys stay Optional for that reason.

import AppKit
import Foundation

struct Target: Codable, Equatable {
    var name: String
    var url: String
    var email: String
    var password: String
}

struct Config: Codable {
    var targets: [Target]
    var current: String
    var serverBinary: String
    var cliBinary: String
    var dataDir: String
    var watchDir: String
    // LanceDB keys, added later — Optional so a config file written before them
    // still decodes. See the note at the top of this file.
    var lanceUrl: String?
    var lanceBind: String?
    var bunBinary: String?
    var lanceDir: String?
    // The Python edition of the replica, added later still. Same rule: Optional,
    // computed defaults, an existing tray.json decodes untouched.
    var lancePyUrl: String?
    var lancePyBind: String?
    var uvBinary: String?
    var lancePyDir: String?

    static var path: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/structor/tray.json")
    }

    /// The app/ directory of the repo. In order: `StructorAppDir` from the
    /// bundle's Info.plist (stamped by scripts/bundle-tray.sh, the only thing
    /// that works for /Applications/StructorTray.app); the path above `tray/`
    /// when running out of the SwiftPM build tree (`.build/release` is a
    /// symlink, so counting path components does not); else the executable's
    /// grandparent, which is at least not somewhere else's directory.
    static var appDir: URL {
        if let s = Bundle.main.infoDictionary?["StructorAppDir"] as? String, !s.isEmpty {
            return URL(fileURLWithPath: s)
        }
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().path
        if let r = exe.range(of: "/tray/.build/") {
            return URL(fileURLWithPath: String(exe[exe.startIndex..<r.lowerBound]))
        }
        return URL(fileURLWithPath: exe).deletingLastPathComponent().deletingLastPathComponent()
    }

    static func load() -> Config {
        let exists = FileManager.default.fileExists(atPath: path.path)
        if exists, let data = try? Data(contentsOf: path), let c = try? JSONDecoder().decode(Config.self, from: data) {
            return c
        }
        let appDir = Config.appDir
        let c = Config(
            targets: [Target(name: "local", url: "http://127.0.0.1:8091", email: "admin@structor.local", password: "structor-dev-password")],
            current: "local",
            serverBinary: appDir.appendingPathComponent("bin/structor").path,
            cliBinary: appDir.appendingPathComponent("bin/structor-cli").path,
            dataDir: appDir.appendingPathComponent("pb_data").path,
            watchDir: "~/.claude/projects"
        )
        if exists {
            // A file that does not decode holds someone's targets and passwords:
            // keep it, copy it aside for inspection, use defaults for this run only.
            let bad = path.deletingLastPathComponent().appendingPathComponent("tray.json.bad")
            try? FileManager.default.removeItem(at: bad)
            try? FileManager.default.copyItem(at: path, to: bad)
        } else {
            c.save()
        }
        return c
    }

    func save() {
        try? FileManager.default.createDirectory(at: Config.path.deletingLastPathComponent(), withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let d = try? enc.encode(self) { try? d.write(to: Config.path) }
    }

    var target: Target { targets.first { $0.name == current } ?? targets[0] }

    /// ~/.config/structor/lance.json "targets" (a list or a comma string) as a
    /// comma list, or nil — the narrowing scripts/agent.sh hands the replicas.
    static func lanceTargetsCSV() -> String? {
        let url = path.deletingLastPathComponent().appendingPathComponent("lance.json")
        guard let d = try? Data(contentsOf: url), let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
        if let list = j["targets"] as? [Any] { return list.map { "\($0)" }.filter { !$0.isEmpty }.joined(separator: ",") }
        if let s = j["targets"] as? String { return s }
        return nil
    }

    /// Admin of the LanceDB replica, loopback by default. Any trailing slash is
    /// dropped so callers can append "/" or "/api/status" without doubling it.
    var lanceEndpoint: String {
        var u = lanceUrl?.trimmingCharacters(in: .whitespaces) ?? ""
        while u.hasSuffix("/") { u.removeLast() }
        return u.isEmpty ? "http://127.0.0.1:8092" : u
    }

    /// The bun that runs structor-lance. Without a config key, take the first
    /// install that actually exists; if none does, name the usual one so the
    /// failure to launch says something useful.
    var bunPath: String {
        if let b = bunBinary, !b.isEmpty { return (b as NSString).expandingTildeInPath }
        let candidates = ["~/.bun/bin/bun", "/opt/homebrew/bin/bun", "/usr/local/bin/bun"]
            .map { ($0 as NSString).expandingTildeInPath }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? candidates[0]
    }

    /// Working directory for `bun src/main.ts` — the structor-lance package,
    /// sibling of bin/ in the same app directory the server binary comes from.
    var lanceWorkDir: String {
        if let d = lanceDir, !d.isEmpty { return (d as NSString).expandingTildeInPath }
        return Config.appDir.appendingPathComponent("lance").path
    }

    /// host:port a tray-started replica binds. Separate from lanceUrl so the
    /// tray can be pointed at a replica on another machine without trying to
    /// bind that machine's address here.
    var lanceBindAddr: String {
        let b = lanceBind?.trimmingCharacters(in: .whitespaces) ?? ""
        return b.isEmpty ? "127.0.0.1:8092" : b
    }

    /// Admin of the Python replica — a second, independent process on its own
    /// port, so everything below mirrors the Bun keys rather than sharing them.
    var lancePyEndpoint: String {
        var u = lancePyUrl?.trimmingCharacters(in: .whitespaces) ?? ""
        while u.hasSuffix("/") { u.removeLast() }
        return u.isEmpty ? "http://127.0.0.1:8094" : u
    }

    /// host:port a tray-started Python replica binds.
    var lancePyBindAddr: String {
        let b = lancePyBind?.trimmingCharacters(in: .whitespaces) ?? ""
        return b.isEmpty ? "127.0.0.1:8094" : b
    }

    /// The uv that runs the Python package. Same shape as bunPath: first
    /// install that exists, else the usual one so the launch error names it.
    var uvPath: String {
        if let u = uvBinary, !u.isEmpty { return (u as NSString).expandingTildeInPath }
        let candidates = ["~/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
            .map { ($0 as NSString).expandingTildeInPath }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? candidates[0]
    }

    /// Project directory for `uv run --project` — the Python structor-lance
    /// package, sibling of lance/ in the same app directory.
    var lancePyWorkDir: String {
        if let d = lancePyDir, !d.isEmpty { return (d as NSString).expandingTildeInPath }
        return Config.appDir.appendingPathComponent("lance-py").path
    }
}

struct Status: Decodable {
    var projects: Int
    var sessions: Int
    var events: Int
    var session_weeks: Int
    var last_ingest: String
    var version: String
}

/// The little the tray needs out of structor-lance's GET /api/status. Every
/// field is optional and everything else in that payload is ignored, so the
/// replica can grow its response without breaking the menu — and a table that
/// reports an error instead of a row count simply reads as no rows.
struct LanceStatus: Decodable {
    /// `vectors` is the count of embedded rows. Only the Python replica reports
    /// it; on the Bun one it is simply absent, which is why it is Optional like
    /// everything else here.
    struct Events: Decodable {
        var rows: Int?
        var vectors: Int?
    }
    struct Tables: Decodable { var events: Events? }
    struct Sync: Decodable {
        var lastRun: String?
        var lastError: String?
    }
    struct TargetStatus: Decodable {
        var name: String?
        var tables: Tables?
        var sync: Sync?
    }
    var targets: [TargetStatus]?
}

/// The reply of POST /api/<target>/ask on the Python replica. Everything is
/// Optional on purpose: the same struct has to decode a full answer, the
/// "no matching events" reply (answer empty, `note` set) and the plain
/// {"error": …} bodies the route returns for 400/404/502/503/504.
struct AskReply: Decodable {
    struct Source: Decodable {
        var n: Int?
        var ts: String?
        var role: String?
        var session_id: String?
        var project: String?
        var text: String?
    }
    struct Plan: Decodable {
        var queries: [String]?
        var since: String?
    }
    var answer: String?
    var sources: [Source]?
    var model: String?
    var chat_url: String?
    var plan: Plan?
    var note: String?
    var error: String?
}

final class Child {
    let label: String
    private var process: Process?
    private(set) var log: [String] = []
    var isRunning: Bool { process?.isRunning ?? false }

    init(label: String) { self.label = label }

    func start(_ binary: String, _ args: [String], env: [String: String] = [:], cwd: String? = nil) {
        guard !isRunning else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: binary)
        p.arguments = args
        if let cwd = cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
        var e = ProcessInfo.processInfo.environment
        env.forEach { e[$0.key] = $0.value }
        p.environment = e
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                self?.log.append(contentsOf: s.split(separator: "\n").map(String.init))
                if let n = self?.log.count, n > 200 { self?.log.removeFirst(n - 200) }
            }
        }
        do { try p.run(); process = p } catch { log.append("failed to start \(label): \(error)") }
    }

    func stop() {
        guard let p = process, p.isRunning else { return }
        p.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { if p.isRunning { kill(p.processIdentifier, SIGKILL) } }
    }
}

// MARK: - ask panel

/// Escape has to close the panel from anywhere inside it. The field editor
/// swallows the key while you are typing (the text field's delegate handles
/// that case); everywhere else it walks the responder chain to the window.
final class AskPanel: NSPanel {
    override func cancelOperation(_ sender: Any?) { close() }
}

/// One floating window that asks the Python replica a question. It is created
/// once and kept by the delegate, so closing and reopening it keeps the last
/// answer on screen; nothing here ever sees the PocketBase token, the replica
/// admin is loopback and unauthenticated.
final class AskPanelController: NSObject, NSTextFieldDelegate {
    private weak var owner: AppDelegate?
    private let panel: AskPanel
    private let question = NSTextField()
    private let askButton = NSButton()
    private let statusLabel = NSTextField(labelWithString: "")
    private let answerView = NSTextView()
    private let sourcesView = NSTextView()
    private var task: URLSessionDataTask?
    /// The store this panel asks. Stamped by show()/retarget(), never read off
    /// the config at send time, so the title and the request always agree.
    private(set) var target = ""
    /// Bumped on every submit and target change; a reply carrying an older
    /// number belongs to a store this panel no longer shows and is dropped.
    private var generation = 0
    /// Remembered from the previous reply, so the next ask can say which model
    /// on which box it is waiting for instead of a bare "asking…".
    private var lastModel: String?
    private var lastHost: String?

    init(owner: AppDelegate) {
        self.owner = owner
        panel = AskPanel(contentRect: NSRect(x: 0, y: 0, width: 560, height: 420),
                         styleMask: [.titled, .closable, .resizable, .utilityWindow],
                         backing: .buffered, defer: false)
        super.init()

        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.minSize = NSSize(width: 420, height: 300)
        panel.center()
        guard let content = panel.contentView else { return }

        question.frame = NSRect(x: 12, y: 380, width: 458, height: 24)
        question.autoresizingMask = [.width, .minYMargin]
        question.placeholderString = "ask the replica about the sessions…"
        question.delegate = self
        question.target = self
        question.action = #selector(submit)   // NSTextField fires its action on Return
        content.addSubview(question)

        askButton.frame = NSRect(x: 478, y: 378, width: 70, height: 28)
        askButton.autoresizingMask = [.minXMargin, .minYMargin]
        askButton.bezelStyle = .rounded
        askButton.title = "Ask"
        askButton.target = self
        askButton.action = #selector(submit)
        content.addSubview(askButton)

        statusLabel.frame = NSRect(x: 14, y: 356, width: 534, height: 16)
        statusLabel.autoresizingMask = [.width, .minYMargin]
        statusLabel.font = .systemFont(ofSize: 11)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.lineBreakMode = .byTruncatingTail
        content.addSubview(statusLabel)

        let answerScroll = AskPanelController.scroller(answerView, size: 13, mono: false)
        answerScroll.frame = NSRect(x: 12, y: 140, width: 536, height: 210)
        answerScroll.autoresizingMask = [.width, .height]
        content.addSubview(answerScroll)

        let sourcesScroll = AskPanelController.scroller(sourcesView, size: 10, mono: true)
        sourcesScroll.frame = NSRect(x: 12, y: 12, width: 536, height: 120)
        sourcesScroll.autoresizingMask = [.width, .maxYMargin]
        content.addSubview(sourcesScroll)
    }

    /// A non-editable text view inside its own scroller. Set up by hand because
    /// the sizing flags an NSTextView needs to scroll are not the defaults.
    private static func scroller(_ tv: NSTextView, size: CGFloat, mono: Bool) -> NSScrollView {
        let sv = NSScrollView()
        sv.hasVerticalScroller = true
        sv.autohidesScrollers = true
        sv.borderType = .bezelBorder
        sv.drawsBackground = true
        tv.isEditable = false
        tv.isSelectable = true
        tv.isRichText = false
        tv.drawsBackground = true
        tv.backgroundColor = .textBackgroundColor
        tv.textColor = .labelColor
        tv.font = mono ? .monospacedSystemFont(ofSize: size, weight: .regular) : .systemFont(ofSize: size)
        tv.textContainerInset = NSSize(width: 6, height: 6)
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.minSize = NSSize(width: 0, height: 0)
        tv.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        sv.documentView = tv
        return sv
    }

    /// Show or bring forward. The app is an accessory (no Dock icon), so
    /// without activating it explicitly the panel appears behind whatever the
    /// user is in and never takes the keyboard.
    func show(target: String) {
        retarget(target)
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(question)
    }

    /// Called when the tray's current target changes, whether or not the panel is
    /// visible: the title follows, an in-flight ask for the old store is
    /// abandoned, and its stale answer is cleared so nothing from one store is
    /// ever shown under another's name.
    func retarget(_ name: String) {
        guard name != target else { return }
        target = name
        panel.title = "Ask Structor · \(name)"
        if task != nil {
            task?.cancel(); task = nil
            askButton.isEnabled = true
            generation += 1
            setStatus("target changed to \(name) — ask again")
        }
        set(answerView, ""); set(sourcesView, "")
    }

    /// Escape while the field editor is up: the text field eats the key, so the
    /// close has to come from here. Return is left alone — it fires the action.
    func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        if selector == #selector(NSResponder.cancelOperation(_:)) {
            panel.close()
            return true
        }
        return false
    }

    private func setStatus(_ s: String, bad: Bool = false) {
        statusLabel.stringValue = s
        statusLabel.textColor = bad ? .systemRed : .secondaryLabelColor
    }

    private func set(_ tv: NSTextView, _ s: String) {
        tv.string = s
        tv.scrollRangeToVisible(NSRange(location: 0, length: 0))
    }

    /// "http://gpu2:11434/" → "gpu2", for the waiting line.
    private func host(_ url: String?) -> String? {
        guard var s = url?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
        for scheme in ["http://", "https://"] where s.hasPrefix(scheme) { s.removeFirst(scheme.count) }
        if let slash = s.firstIndex(of: "/") { s = String(s[s.startIndex..<slash]) }
        if let colon = s.lastIndex(of: ":") { s = String(s[s.startIndex..<colon]) }
        return s.isEmpty ? nil : s
    }

    @objc func submit() {
        guard task == nil, let owner = owner else { return }
        let q = question.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        let base = owner.config.lancePyEndpoint
        let name = target.isEmpty ? owner.config.current : target
        let path = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        guard let url = URL(string: base + "/api/" + path + "/ask") else {
            setStatus("bad lancePyUrl: \(base)", bad: true)
            return
        }
        var req = URLRequest(url: url, timeoutInterval: 300)   // an ask is 5–60 s, sometimes worse
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["question": q, "k": 10, "mode": "hybrid"])

        askButton.isEnabled = false
        if let m = lastModel, let h = lastHost { setStatus("asking \(m) on \(h)…") } else { setStatus("asking…") }
        generation += 1
        let mine = generation
        let t = URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            DispatchQueue.main.async {
                guard let self = self else { return }
                guard mine == self.generation else { return }   // the target moved on; this reply is for the old store
                self.task = nil
                self.askButton.isEnabled = true
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                guard let d = data, code != 0 else {
                    self.setStatus("lance-py offline (\(base))" + (err.map { " — \($0.localizedDescription)" } ?? ""), bad: true)
                    return
                }
                guard let r = try? JSONDecoder().decode(AskReply.self, from: d) else {
                    self.setStatus("HTTP \(code): unreadable reply", bad: true)
                    return
                }
                if let e = r.error, !e.isEmpty { return self.setStatus(e, bad: true) }
                if code != 200 { return self.setStatus("HTTP \(code)", bad: true) }
                self.fill(r)
            }
        }
        task = t
        t.resume()
    }

    private func fill(_ r: AskReply) {
        lastModel = r.model
        lastHost = host(r.chat_url)
        let answer = (r.answer ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        set(answerView, answer.isEmpty ? (r.note ?? "(empty answer)") : answer)
        set(sourcesView, AskPanelController.sourceLines(r.sources ?? []))
        let queries = (r.plan?.queries ?? []).joined(separator: " · ")
        var line = r.model ?? "answered"
        if !queries.isEmpty { line += " · searched: \(queries)" }
        if let since = r.plan?.since, !since.isEmpty { line += " · since \(since)" }
        setStatus(line)
    }

    /// "[n] ts role session_id project — text", one per line.
    static func sourceLines(_ sources: [AskReply.Source]) -> String {
        if sources.isEmpty { return "(no sources)" }
        return sources.map { s -> String in
            let head = ["[\(s.n.map(String.init) ?? "?")]",
                        s.ts.map { String($0.prefix(16)) } ?? "",
                        s.role ?? "",
                        s.session_id.map { String($0.prefix(8)) } ?? "",
                        s.project ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
            var text = (s.text ?? "")
                .replacingOccurrences(of: "\n", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if text.count > 120 { text = String(text.prefix(120)) + "…" }
            return text.isEmpty ? head : head + " — " + text
        }.joined(separator: "\n")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var config = Config.load()
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let server = Child(label: "server")
    let watcher = Child(label: "watcher")
    let lance = Child(label: "lance")
    let lancePy = Child(label: "lance-py")
    var token: String?
    var tokenFor: String?
    var status: Status?
    var lastError: String?
    var lanceStatus: LanceStatus?
    var lancePyStatus: LanceStatus?
    var askPanel: AskPanelController?
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = editOnlyMainMenu()   // ⌘C/⌘V/⌘A/⌘Z in the Ask panel route through here
        item.button?.title = "⌂ Structor"
        item.menu = NSMenu()
        rebuildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.refresh() }
    }

    /// An accessory app gets no main menu, and without one AppKit has nowhere to
    /// dispatch the standard editing key equivalents: the Ask panel's text field
    /// would ignore ⌘V. The menu never shows; it only carries the responders.
    func editOnlyMainMenu() -> NSMenu {
        let main = NSMenu()
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        return main
    }

    func applicationWillTerminate(_ n: Notification) {
        lancePy.stop(); lance.stop(); watcher.stop(); server.stop()
    }

    // MARK: networking

    func login(_ t: Target, completion: @escaping (String?) -> Void) {
        guard let url = URL(string: t.url + "/api/collections/_superusers/auth-with-password") else { return completion(nil) }
        var req = URLRequest(url: url, timeoutInterval: 5)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["identity": t.email, "password": t.password])
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            guard let d = data, (resp as? HTTPURLResponse)?.statusCode == 200,
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let tok = j["token"] as? String
            else { return completion(nil) }
            completion(tok)
        }.resume()
    }

    /// True when a structor-cli watcher this tray did not start is running
    /// (normally the launchd agents). Cheap pgrep, once per refresh.
    var externalWatcher = false

    func probeExternalWatcher() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        p.arguments = ["-f", "structor-cli .*watch"]
        p.standardOutput = Pipe(); p.standardError = Pipe()
        DispatchQueue.global().async { [weak self] in
            guard (try? p.run()) != nil else { return }
            p.waitUntilExit()
            let found = p.terminationStatus == 0
            DispatchQueue.main.async { self?.externalWatcher = found }
        }
    }

    func refresh() {
        fetchLanceStatus()
        fetchLancePyStatus()
        probeExternalWatcher()
        let t = config.target
        if token == nil || tokenFor != t.url {
            login(t) { [weak self] tok in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.token = tok; self.tokenFor = t.url
                    if tok == nil { self.status = nil; self.lastError = "login failed / unreachable"; self.rebuildMenu() }
                    else { self.fetchStatus() }
                }
            }
        } else {
            fetchStatus()
        }
    }

    func fetchStatus() {
        let t = config.target
        guard let tok = token, let url = URL(string: t.url + "/api/structor/status") else { return }
        var req = URLRequest(url: url, timeoutInterval: 5)
        req.setValue(tok, forHTTPHeaderField: "Authorization")
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let code = (resp as? HTTPURLResponse)?.statusCode, code == 401 { self.token = nil; self.lastError = "token expired"; self.rebuildMenu(); return }
                if let d = data, let s = try? JSONDecoder().decode(Status.self, from: d) {
                    self.status = s; self.lastError = nil
                } else {
                    self.status = nil; self.lastError = err?.localizedDescription ?? "bad response"
                }
                self.rebuildMenu()
            }
        }.resume()
    }

    /// Second, separate poll: the LanceDB admin is loopback-only and takes no
    /// credentials, so this never touches the PocketBase token. It is also kept
    /// apart from `status` on purpose — a Lance failure must never blank the
    /// PocketBase lines, it only turns its own line to "offline".
    func fetchLanceStatus() {
        guard let url = URL(string: config.lanceEndpoint + "/api/status") else {
            lanceStatus = nil
            return
        }
        var req = URLRequest(url: url, timeoutInterval: 5)
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let d = data, (resp as? HTTPURLResponse)?.statusCode == 200,
                   let s = try? JSONDecoder().decode(LanceStatus.self, from: d) {
                    self.lanceStatus = s
                } else {
                    self.lanceStatus = nil
                }
                self.rebuildMenu()
            }
        }.resume()
    }

    /// Third poll, for the Python replica. Deliberately a copy of the Bun one
    /// rather than a shared helper over both: they are separate processes that
    /// fail separately, and the whole point is that neither failure can blank
    /// the other's line or the PocketBase lines.
    func fetchLancePyStatus() {
        guard let url = URL(string: config.lancePyEndpoint + "/api/status") else {
            lancePyStatus = nil
            return
        }
        var req = URLRequest(url: url, timeoutInterval: 5)
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let d = data, (resp as? HTTPURLResponse)?.statusCode == 200,
                   let s = try? JSONDecoder().decode(LanceStatus.self, from: d) {
                    self.lancePyStatus = s
                } else {
                    self.lancePyStatus = nil
                }
                self.rebuildMenu()
            }
        }.resume()
    }

    // MARK: menu

    func fmt(_ n: Int) -> String {
        let f = NumberFormatter(); f.numberStyle = .decimal
        return f.string(from: NSNumber(value: n)) ?? "\(n)"
    }

    /// An ISO timestamp as local HH:MM; empty or unparseable reads as "never".
    func clock(_ iso: String?) -> String {
        guard let s = iso, !s.isEmpty else { return "never" }
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        guard let d = withFraction.date(from: s) ?? plain.date(from: s) else { return "never" }
        let out = DateFormatter(); out.dateFormat = "HH:mm"
        return out.string(from: d)
    }

    /// One line for the Lance replica of whichever target the tray is showing.
    func lanceLine() -> String {
        guard let ls = lanceStatus else { return "lance: offline" }
        let all = ls.targets ?? []
        guard let t = all.first(where: { $0.name == config.current }) ?? all.first else { return "lance: no targets" }
        let rows = t.tables?.events?.rows ?? 0
        var line = "lance: \(fmt(rows)) events · synced \(clock(t.sync?.lastRun))"
        if let e = t.sync?.lastError, !e.isEmpty { line += " · err \(e.prefix(40))" }
        return line
    }

    /// The same line for the Python replica, plus the embedded-row count it is
    /// the only one to report.
    func lancePyLine() -> String {
        guard let ls = lancePyStatus else { return "lance-py: offline" }
        let all = ls.targets ?? []
        guard let t = all.first(where: { $0.name == config.current }) else { return "lance-py: no \(config.current) replica" }
        let rows = t.tables?.events?.rows ?? 0
        let vectors = t.tables?.events?.vectors ?? 0
        var line = "lance-py: \(fmt(rows)) events · \(fmt(vectors)) vectors · synced \(clock(t.sync?.lastRun))"
        if let e = t.sync?.lastError, !e.isEmpty { line += " · err \(e.prefix(40))" }
        return line
    }

    func rebuildMenu() {
        let m = NSMenu()
        let t = config.target
        let dot = status != nil ? "●" : "○"
        item.button?.title = status.map { "\(dot) \(fmt($0.events))" } ?? "\(dot) Structor"

        m.addItem(withTitle: "Target: \(t.name) — \(t.url)", action: nil, keyEquivalent: "")
        if let s = status {
            m.addItem(withTitle: "\(fmt(s.sessions)) sessions · \(fmt(s.events)) events · \(fmt(s.session_weeks)) session-weeks", action: nil, keyEquivalent: "")
            m.addItem(withTitle: "last ingest \(s.last_ingest.isEmpty ? "never" : String(s.last_ingest.prefix(16)))  ·  v\(s.version)", action: nil, keyEquivalent: "")
        } else {
            m.addItem(withTitle: "offline: \(lastError ?? "…")", action: nil, keyEquivalent: "")
        }
        m.addItem(withTitle: lanceLine(), action: nil, keyEquivalent: "")
        m.addItem(withTitle: lancePyLine(), action: nil, keyEquivalent: "")
        m.addItem(.separator())

        // A process this tray did not spawn but which is demonstrably up (the
        // launchd agents, normally) gets a disabled "running (launchd)" item, so
        // the menu can never start a second copy of it.
        let localTarget = config.targets.first { $0.name == "local" } ?? t
        let serverElsewhere = !server.isRunning && status != nil && t.url == localTarget.url
        let srv = NSMenuItem(title: server.isRunning ? "Stop local server" : (serverElsewhere ? "Local server: running (launchd)" : "Start local server"),
                             action: serverElsewhere ? nil : #selector(toggleServer), keyEquivalent: "s")
        srv.target = self
        m.addItem(srv)
        let watcherElsewhere = !watcher.isRunning && externalWatcher
        let w = NSMenuItem(title: watcher.isRunning ? "Stop watcher (~/.claude/projects)" : (watcherElsewhere ? "Watcher: running (launchd)" : "Start watcher (~/.claude/projects)"),
                           action: watcherElsewhere ? nil : #selector(toggleWatcher), keyEquivalent: "w")
        w.target = self
        m.addItem(w)
        let scan = NSMenuItem(title: "Scan once now", action: #selector(scanOnce), keyEquivalent: "r")
        scan.target = self
        m.addItem(scan)
        let lanceElsewhere = !lance.isRunning && lanceStatus != nil
        let ln = NSMenuItem(title: lance.isRunning ? "Stop LanceDB replica + admin" : (lanceElsewhere ? "LanceDB replica: running (launchd)" : "Start LanceDB replica + admin"),
                            action: lanceElsewhere ? nil : #selector(toggleLance), keyEquivalent: "l")
        ln.target = self
        m.addItem(ln)
        // only a replica answering at the address this tray would bind counts as "ours, elsewhere";
        // a remote lancePyUrl says nothing about whether the local port is free
        let lancePyElsewhere = !lancePy.isRunning && lancePyStatus != nil && hostPort(config.lancePyEndpoint) == config.lancePyBindAddr
        let lp = NSMenuItem(title: lancePy.isRunning ? "Stop Python replica" : (lancePyElsewhere ? "Python replica: running (launchd)" : "Start Python replica"),
                            action: lancePyElsewhere ? nil : #selector(toggleLancePy), keyEquivalent: "p")
        lp.target = self
        m.addItem(lp)
        m.addItem(.separator())

        let open = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard), keyEquivalent: "o")
        open.target = self
        m.addItem(open)
        let admin = NSMenuItem(title: "Open PocketBase admin", action: #selector(openAdmin), keyEquivalent: "a")
        admin.target = self
        m.addItem(admin)
        let lanceConsole = NSMenuItem(title: "Open console on LanceDB", action: #selector(openLanceConsole), keyEquivalent: "c")
        lanceConsole.target = self
        m.addItem(lanceConsole)
        let lanceAdmin = NSMenuItem(title: "Open LanceDB admin", action: #selector(openLanceAdmin), keyEquivalent: "d")
        lanceAdmin.target = self
        m.addItem(lanceAdmin)
        let lancePyAdmin = NSMenuItem(title: "Open LanceDB admin (Python)", action: #selector(openLancePyAdmin), keyEquivalent: "e")
        lancePyAdmin.target = self
        m.addItem(lancePyAdmin)
        let lancePyConsole = NSMenuItem(title: "Open console on LanceDB (Python)", action: #selector(openLancePyConsole), keyEquivalent: "f")
        lancePyConsole.target = self
        m.addItem(lancePyConsole)
        let ask = NSMenuItem(title: "Ask Structor…", action: #selector(openAsk), keyEquivalent: "k")
        ask.target = self
        m.addItem(ask)
        m.addItem(.separator())

        let targets = NSMenu()
        for tg in config.targets {
            let mi = NSMenuItem(title: "\(tg.name) — \(tg.url)", action: #selector(pickTarget(_:)), keyEquivalent: "")
            mi.target = self
            mi.representedObject = tg.name
            mi.state = tg.name == config.current ? .on : .off
            targets.addItem(mi)
        }
        let tItem = NSMenuItem(title: "Target", action: nil, keyEquivalent: "")
        tItem.submenu = targets
        m.addItem(tItem)

        let logs = NSMenu()
        for line in (server.log.suffix(8) + watcher.log.suffix(8) + lance.log.suffix(8) + lancePy.log.suffix(8)) { logs.addItem(withTitle: String(line.prefix(120)), action: nil, keyEquivalent: "") }
        if logs.items.isEmpty { logs.addItem(withTitle: "(no output yet)", action: nil, keyEquivalent: "") }
        let lItem = NSMenuItem(title: "Recent output", action: nil, keyEquivalent: "")
        lItem.submenu = logs
        m.addItem(lItem)

        let cfg = NSMenuItem(title: "Edit config…", action: #selector(editConfig), keyEquivalent: ",")
        cfg.target = self
        m.addItem(cfg)
        let rl = NSMenuItem(title: "Refresh", action: #selector(doRefresh), keyEquivalent: "")
        rl.target = self
        m.addItem(rl)
        m.addItem(.separator())
        let q = NSMenuItem(title: "Quit Structor Tray", action: #selector(quit), keyEquivalent: "q")
        q.target = self
        m.addItem(q)
        item.menu = m
    }

    // MARK: actions

    @objc func toggleServer() {
        if server.isRunning { server.stop() } else {
            let local = config.targets.first { $0.name == "local" } ?? config.target
            let http = local.url.replacingOccurrences(of: "http://", with: "").replacingOccurrences(of: "https://", with: "")
            server.start(config.serverBinary, ["serve", "--http=\(http)", "--dir=\(config.dataDir)"],
                         env: ["STRUCTOR_ADMIN_EMAIL": local.email, "STRUCTOR_ADMIN_PASSWORD": local.password])
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.token = nil; self.refresh() }
        rebuildMenu()
    }

    @objc func toggleWatcher() {
        if watcher.isRunning { watcher.stop() } else {
            let t = config.target
            let dir = (config.watchDir as NSString).expandingTildeInPath
            // credentials through the environment, never on argv (visible in ps)
            watcher.start(config.cliBinary, ["watch", dir], env: cliEnv(t))
        }
        rebuildMenu()
    }

    func cliEnv(_ t: Target) -> [String: String] {
        ["STRUCTOR_URL": t.url, "STRUCTOR_EMAIL": t.email, "STRUCTOR_PASSWORD": t.password]
    }

    /// Start or stop `bun src/main.ts` in the structor-lance package — the
    /// replica sync and its admin UI are the same process.
    ///
    /// Day to day the launchd agent studio.soulbrews.structor.lance owns this
    /// process, exactly as the serve and watch agents own the other two; the
    /// toggle is for hand-runs (a debug session, a machine where the agent is
    /// unloaded). With the agent loaded, the port is already taken and the child
    /// will exit — read why under "Recent output".
    @objc func toggleLance() {
        if lance.isRunning { lance.stop() } else {
            lance.start(config.bunPath, ["src/main.ts", "--http", config.lanceBindAddr], cwd: config.lanceWorkDir)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.fetchLanceStatus() }
        rebuildMenu()
    }

    /// Start or stop the Python replica: `uv run --project <dir> structor-lance
    /// serve --http <bind>`. uv resolves the environment from the project dir,
    /// which is also the cwd so a relative path in its config lands right.
    ///
    /// As with the Bun one, day to day this process belongs to launchd
    /// (studio.soulbrews.structor.lance-py) and the menu item is a hand-run for
    /// debugging — with the agent loaded the port is taken and the child exits.
    @objc func toggleLancePy() {
        if lancePy.isRunning { lancePy.stop() } else {
            // same environment scripts/agent.sh lance-py builds: ~/.config/structor/lance.json "targets" narrows the stores
            var env: [String: String] = [:]
            if let t = Config.lanceTargetsCSV(), !t.isEmpty { env["STRUCTOR_LANCE_TARGETS"] = t }
            lancePy.start(config.uvPath,
                          ["run", "--project", config.lancePyWorkDir, "structor-lance", "serve", "--http", config.lancePyBindAddr],
                          env: env, cwd: config.lancePyWorkDir)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.fetchLancePyStatus() }
        rebuildMenu()
    }

    /// "http://127.0.0.1:8092/" → "127.0.0.1:8092", which is what --http wants.
    func hostPort(_ url: String) -> String {
        var s = url.replacingOccurrences(of: "http://", with: "").replacingOccurrences(of: "https://", with: "")
        if let slash = s.firstIndex(of: "/") { s = String(s[s.startIndex..<slash]) }
        return s
    }

    @objc func scanOnce() {
        let t = config.target
        let dir = (config.watchDir as NSString).expandingTildeInPath
        let once = Child(label: "scan")
        once.start(config.cliBinary, ["scan", dir], env: cliEnv(t))
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
    }

    @objc func openDashboard() { if let u = URL(string: config.target.url + "/") { NSWorkspace.shared.open(u) } }
    @objc func openAdmin() { if let u = URL(string: config.target.url + "/_/") { NSWorkspace.shared.open(u) } }
    @objc func openLanceAdmin() { if let u = URL(string: config.lanceEndpoint + "/") { NSWorkspace.shared.open(u) } }
    /// The same console pages as "Open dashboard", served by structor-lance
    /// over the replica of the current target — the second frontend.
    @objc func openLanceConsole() {
        let t = config.current.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? config.current
        if let u = URL(string: config.lanceEndpoint + "/console/" + t + "/") { NSWorkspace.shared.open(u) }
    }
    @objc func openLancePyAdmin() { if let u = URL(string: config.lancePyEndpoint + "/") { NSWorkspace.shared.open(u) } }
    /// The Python replica's console pages, over the replica of the current target.
    @objc func openLancePyConsole() {
        let t = config.current.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? config.current
        if let u = URL(string: config.lancePyEndpoint + "/console/" + t + "/") { NSWorkspace.shared.open(u) }
    }
    /// The ask panel is built once and kept: closing it is a hide, so the last
    /// answer is still there when it comes back.
    @objc func openAsk() {
        if askPanel == nil { askPanel = AskPanelController(owner: self) }
        askPanel?.show(target: config.current)
    }
    @objc func pickTarget(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        config.current = name; config.save(); token = nil; status = nil; refresh(); rebuildMenu()
        askPanel?.retarget(name)
    }
    @objc func editConfig() { NSWorkspace.shared.open(Config.path) }
    @objc func doRefresh() { token = nil; refresh() }
    @objc func quit() { lancePy.stop(); lance.stop(); watcher.stop(); server.stop(); NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
