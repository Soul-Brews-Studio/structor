// StructorTray — a macOS menu-bar status + controller for Structor.
//
// Shows store totals from /api/structor/status, lets you start/stop the local
// server and the structor-cli watcher, switch between targets (local, kvmlab1),
// and open the dashboard / PocketBase admin.
//
// Config: ~/.config/structor/tray.json
// {
//   "targets": [
//     {"name": "local",   "url": "http://127.0.0.1:8091", "email": "admin@structor.local", "password": "structor-dev-password"},
//     {"name": "kvmlab1", "url": "http://kvmlab1.oracle.netbird:8090", "email": "…", "password": "…"}
//   ],
//   "current": "local",
//   "serverBinary": "/path/to/app/bin/structor",
//   "cliBinary": "/path/to/app/bin/structor-cli",
//   "dataDir": "/path/to/app/pb_data",
//   "watchDir": "~/.claude/projects"
// }
// Missing file → sensible defaults relative to the repo the app was built in.

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

    static var path: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/structor/tray.json")
    }

    static func load() -> Config {
        if let data = try? Data(contentsOf: path), let c = try? JSONDecoder().decode(Config.self, from: data) {
            return c
        }
        // defaults: repo layout, discovered from the executable's location (.build/<cfg>/StructorTray)
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let appDir = exe.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let c = Config(
            targets: [Target(name: "local", url: "http://127.0.0.1:8091", email: "admin@structor.local", password: "structor-dev-password")],
            current: "local",
            serverBinary: appDir.appendingPathComponent("bin/structor").path,
            cliBinary: appDir.appendingPathComponent("bin/structor-cli").path,
            dataDir: appDir.appendingPathComponent("pb_data").path,
            watchDir: "~/.claude/projects"
        )
        c.save()
        return c
    }

    func save() {
        try? FileManager.default.createDirectory(at: Config.path.deletingLastPathComponent(), withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let d = try? enc.encode(self) { try? d.write(to: Config.path) }
    }

    var target: Target { targets.first { $0.name == current } ?? targets[0] }
}

struct Status: Decodable {
    var projects: Int
    var sessions: Int
    var events: Int
    var session_weeks: Int
    var last_ingest: String
    var version: String
}

final class Child {
    let label: String
    private var process: Process?
    private(set) var log: [String] = []
    var isRunning: Bool { process?.isRunning ?? false }

    init(label: String) { self.label = label }

    func start(_ binary: String, _ args: [String], env: [String: String] = [:]) {
        guard !isRunning else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: binary)
        p.arguments = args
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

final class AppDelegate: NSObject, NSApplicationDelegate {
    var config = Config.load()
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let server = Child(label: "server")
    let watcher = Child(label: "watcher")
    var token: String?
    var tokenFor: String?
    var status: Status?
    var lastError: String?
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item.button?.title = "⌂ Structor"
        item.menu = NSMenu()
        rebuildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func applicationWillTerminate(_ n: Notification) {
        watcher.stop(); server.stop()
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

    func refresh() {
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

    // MARK: menu

    func fmt(_ n: Int) -> String {
        let f = NumberFormatter(); f.numberStyle = .decimal
        return f.string(from: NSNumber(value: n)) ?? "\(n)"
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
        m.addItem(.separator())

        let srv = NSMenuItem(title: server.isRunning ? "Stop local server" : "Start local server", action: #selector(toggleServer), keyEquivalent: "s")
        srv.target = self
        m.addItem(srv)
        let w = NSMenuItem(title: watcher.isRunning ? "Stop watcher (~/.claude/projects)" : "Start watcher (~/.claude/projects)", action: #selector(toggleWatcher), keyEquivalent: "w")
        w.target = self
        m.addItem(w)
        let scan = NSMenuItem(title: "Scan once now", action: #selector(scanOnce), keyEquivalent: "r")
        scan.target = self
        m.addItem(scan)
        m.addItem(.separator())

        let open = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard), keyEquivalent: "o")
        open.target = self
        m.addItem(open)
        let admin = NSMenuItem(title: "Open PocketBase admin", action: #selector(openAdmin), keyEquivalent: "a")
        admin.target = self
        m.addItem(admin)
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
        for line in (server.log.suffix(8) + watcher.log.suffix(8)) { logs.addItem(withTitle: String(line.prefix(120)), action: nil, keyEquivalent: "") }
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
            watcher.start(config.cliBinary, ["--url", t.url, "--email", t.email, "--password", t.password, "watch", dir])
        }
        rebuildMenu()
    }

    @objc func scanOnce() {
        let t = config.target
        let dir = (config.watchDir as NSString).expandingTildeInPath
        let once = Child(label: "scan")
        once.start(config.cliBinary, ["--url", t.url, "--email", t.email, "--password", t.password, "scan", dir])
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
    }

    @objc func openDashboard() { if let u = URL(string: config.target.url + "/") { NSWorkspace.shared.open(u) } }
    @objc func openAdmin() { if let u = URL(string: config.target.url + "/_/") { NSWorkspace.shared.open(u) } }
    @objc func pickTarget(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        config.current = name; config.save(); token = nil; status = nil; refresh(); rebuildMenu()
    }
    @objc func editConfig() { NSWorkspace.shared.open(Config.path) }
    @objc func doRefresh() { token = nil; refresh() }
    @objc func quit() { watcher.stop(); server.stop(); NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
