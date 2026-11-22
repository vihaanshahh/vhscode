import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct FleetHost: Codable, Hashable, Identifiable {
    var id: String
    var label: String
    var ssh: String
    var repo: String
    var group: String? = nil
    var dictionary: [String: String] { ["id": id, "label": label, "ssh": ssh, "repo": repo, "group": group ?? ""] }
}

struct FleetError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum FleetBridge {
    static let resources = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")

    static func call(_ host: FleetHost, _ request: [String: Any]) async throws -> [String: Any] {
        try await send(["host": host.dictionary, "request": request])
    }

    static func send(_ payload: [String: Any]) async throws -> [String: Any] {
        let input = try JSONSerialization.data(withJSONObject: payload)
        let data = try await Task.detached(priority: .userInitiated) { () throws -> Data in
            let process = Process()
            process.executableURL = resources.appendingPathComponent("python/bin/python3")
            process.arguments = ["-E", "-s", "-B", resources.appendingPathComponent("fleet/fleet_desktop.py").path]
            process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = resources.appendingPathComponent("bin").path + ":" + (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
            environment.removeValue(forKey: "PYTHONHOME")
            environment.removeValue(forKey: "PYTHONPATH")
            process.environment = environment
            let stdin = Pipe(), output = Pipe()
            process.standardInput = stdin
            process.standardOutput = output
            process.standardError = output
            try process.run()
            defer { if process.isRunning { process.terminate() } }
            try stdin.fileHandleForWriting.write(contentsOf: input)
            try stdin.fileHandleForWriting.close()
            let result = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            if process.terminationStatus != 0 {
                throw FleetError(message: String(decoding: result.suffix(1200), as: UTF8.self))
            }
            return result
        }.value
        guard let reply = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw FleetError(message: "The host returned an unreadable response.")
        }
        if let error = reply["error"] as? String { throw FleetError(message: error) }
        return reply
    }

}

private func mayDiscardEdits() -> Bool {
    let alert = NSAlert()
    alert.messageText = "Discard unsaved file changes?"
    alert.informativeText = "Return to the editor and save to keep these edits."
    alert.addButton(withTitle: "Keep editing")
    alert.addButton(withTitle: "Discard changes")
    return alert.runModal() == .alertSecondButtonReturn
}

private final class FleetAppDelegate: NSObject, NSApplicationDelegate {
    var dirty = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        (!dirty || mayDiscardEdits()) ? .terminateNow : .terminateCancel
    }
}

private let accent = Color(red: 0.43, green: 0.82, blue: 0.75)

@main
struct FleetDesktop: App {
    @NSApplicationDelegateAdaptor(FleetAppDelegate.self) private var delegate
    init() {
        signal(SIGPIPE, SIG_IGN)
        if CommandLine.arguments.dropFirst().first == "--ssh-askpass" { Self.askPass() }
    }
    private static func askPass() -> Never {
        NSApplication.shared.setActivationPolicy(.accessory)
        NSApplication.shared.activate(ignoringOtherApps: true)
        let prompt = CommandLine.arguments.dropFirst(2).joined(separator: " ")
        let confirmation = ProcessInfo.processInfo.environment["SSH_ASKPASS_PROMPT"] == "confirm"
            || prompt.contains("(yes/no")
        let alert = NSAlert()
        alert.messageText = confirmation ? "Confirm SSH connection" : "SSH authentication"
        alert.informativeText = prompt
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 310, height: 24))
        if !confirmation { alert.accessoryView = field; alert.window.initialFirstResponder = field }
        alert.addButton(withTitle: confirmation ? "Connect" : "Continue")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { exit(1) }
        print(confirmation ? "yes" : field.stringValue)
        exit(0)
    }
    private func post(_ name: String) { NotificationCenter.default.post(name: .init(name), object: nil) }
    var body: some Scene {
        Window("Fleet Workbench", id: "fleet") {
            FleetWindow().preferredColorScheme(.dark).tint(accent)
                .onReceive(NotificationCenter.default.publisher(for: .init("FleetWorkspaceDirty"))) {
                    delegate.dirty = $0.object as? Bool ?? false
                }
        }
        .defaultSize(width: 1330, height: 820)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Open Folder…") { post("FleetOpenFolder") }.keyboardShortcut("o")
                Button("Open Workspace from File…") { post("FleetOpenWorkspace") }
                    .keyboardShortcut("o", modifiers: [.command, .shift])
                Button("Add SSH Workspace…") { post("FleetAddSSH") }
            }
            CommandGroup(replacing: .printItem) {
                Button("Quick Open File…") { post("FleetQuickOpen") }.keyboardShortcut("p")
                Button("Toggle File Explorer") { post("FleetToggleExplorer") }.keyboardShortcut("b")
                Button("Close Editor Tab") { post("FleetCloseTab") }.keyboardShortcut("w")
            }
        }
    }
}

private struct FleetWindow: View {
    @State private var hosts: [FleetHost] = []
    @AppStorage("FleetLastWorkspace") private var selectedHost = "local"
    @State private var collapsedGroups = Set<String>()
    @State private var openedRepo: String?
    @State private var section = "Workspace"
    @State private var draft: FleetHost?
    @State private var error = ""
    @State private var confirmingRemoval = false
    @State private var dirty = false
    private var current: FleetHost? {
        guard var host = hosts.first(where: { $0.id == selectedHost }) ?? hosts.first else { return nil }
        if let openedRepo, host.id == selectedHost { host.repo = openedRepo }
        return host
    }
    private let sections = [("Activity", "waveform.path.ecg"), ("Schedules", "calendar.badge.clock"), ("Risks", "exclamationmark.triangle")]

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 12) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 8).fill(accent.opacity(0.13))
                        Image(systemName: "square.3.layers.3d").font(.system(size: 22, weight: .medium)).foregroundStyle(accent)
                    }.frame(width: 38, height: 38)
                    Text("Fleet").font(.system(size: 26, weight: .semibold, design: .rounded))
                }.padding(.bottom, 32)
                ForEach(sections, id: \.0) { item in
                    railButton(item.0, item.1, selected: section == item.0) { section = item.0 }
                }
                HStack {
                    Text("WORKSPACES").font(.system(size: 10, weight: .semibold)).tracking(1.5).foregroundStyle(.secondary)
                    Spacer()
                    Menu {
                        Button("Open Folder…") { chooseWorkspace(file: false) }
                        Button("Open Workspace from File…") { chooseWorkspace(file: true) }
                        Button("Add SSH Workspace…") { addSSH() }
                    } label: { Image(systemName: "plus") }
                        .menuStyle(.borderlessButton).frame(width: 28).help("Add or import a workspace")
                }.padding(.top, 26).padding(.bottom, 8)
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(hosts.filter { ($0.group ?? "").isEmpty }) { host in hostRow(host) }
                        ForEach(Array(Set(hosts.compactMap(\.group).filter { !$0.isEmpty })).sorted(), id: \.self) { group in
                            Button {
                                if !collapsedGroups.insert(group).inserted { collapsedGroups.remove(group) }
                            } label: {
                                Label(group, systemImage: collapsedGroups.contains(group) ? "chevron.right" : "chevron.down")
                                    .font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                                    .padding(.vertical, 5)
                            }.buttonStyle(.plain)
                            if !collapsedGroups.contains(group) {
                                ForEach(hosts.filter { $0.group == group }) { host in hostRow(host).padding(.leading, 5) }
                            }
                        }
                    }
                }
                Button { chooseWorkspace(file: false) } label: {
                    Label("Open folder…", systemImage: "folder.badge.plus")
                        .font(.system(size: 12)).frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).padding(.vertical, 9)
                if let host = current {
                    Divider().padding(.bottom, 12)
                    Button { if !dirty || mayDiscardEdits() { draft = host } } label: {
                        Label("Workspace settings", systemImage: "slider.horizontal.3")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }.buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 18).padding(.top, 46).padding(.bottom, 20)
            .frame(width: 205)
            .background(Color(red: 0.095, green: 0.103, blue: 0.117))
            Divider()
            ZStack {
                if let host = current {
                    WorkspaceView(host: host).id(host)
                        .opacity(section == "Workspace" ? 1 : 0)
                        .allowsHitTesting(section == "Workspace")
                        .accessibilityHidden(section != "Workspace")
                } else if section == "Workspace" {
                    ProgressView()
                }
                if section != "Workspace" {
                    FleetOverview(hosts: hosts, section: section, onOpen: openWorkspace)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Color(red: 0.075, green: 0.083, blue: 0.095))
        .frame(minWidth: 960, minHeight: 620)
        .task { await loadHosts() }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetOpenFolder"))) { _ in chooseWorkspace(file: false) }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetOpenWorkspace"))) { _ in chooseWorkspace(file: true) }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetAddSSH"))) { _ in addSSH() }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetQuickOpen"))) { _ in section = "Workspace" }
        .onOpenURL { url in Task { await importRoots([url]) } }
        .dropDestination(for: URL.self) { urls, _ in
            guard !urls.isEmpty else { return false }
            Task { await importRoots(urls) }
            return true
        }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetWorkspaceDirty"))) {
            dirty = $0.object as? Bool ?? false
        }
        .sheet(item: $draft) { host in
            HostForm(host: host, remote: !host.ssh.isEmpty || !hosts.contains(where: { $0.id == host.id })) { saved in
                await loadHosts(); selectedHost = saved.id; openedRepo = nil; section = "Workspace"; draft = nil
            }
        }
        .alert("Workspace error", isPresented: Binding(get: { !error.isEmpty }, set: { if !$0 { error = "" } })) {
            Button("OK") {}
        } message: { Text(error) }
        .confirmationDialog("Remove this workspace from Fleet?", isPresented: $confirmingRemoval) {
            Button("Remove workspace", role: .destructive) {
                Task {
                    do {
                        _ = try await FleetBridge.send(["op": "remove_host", "id": selectedHost])
                        selectedHost = "local"; openedRepo = nil; await loadHosts()
                    } catch { self.error = error.localizedDescription }
                }
            }
        }
    }

    private func addSSH() {
        draft = FleetHost(id: UUID().uuidString, label: "", ssh: "", repo: "")
    }

    private func chooseWorkspace(file: Bool) {
        let panel = NSOpenPanel()
        panel.title = file ? "Open Workspace from File" : "Open Folder"
        panel.canChooseFiles = file
        panel.canChooseDirectories = !file
        panel.allowsMultipleSelection = true
        if file, let type = UTType(filenameExtension: "code-workspace") {
            panel.allowedContentTypes = [type]
        }
        if panel.runModal() == .OK { Task { await importRoots(panel.urls) } }
    }

    private func importRoots(_ urls: [URL]) async {
        guard urls.allSatisfy(\.isFileURL), !urls.isEmpty else { return }
        if dirty && !mayDiscardEdits() { return }
        do {
            let result = try await FleetBridge.send(["op": "import_workspaces", "paths": urls.map(\.path)])
            let data = try JSONSerialization.data(withJSONObject: result["hosts"] ?? [])
            hosts = try JSONDecoder().decode([FleetHost].self, from: data)
            if let id = result["selected"] as? String {
                selectedHost = id; openedRepo = nil; section = "Workspace"
                if let group = current?.group { collapsedGroups.remove(group) }
            }
        } catch { self.error = error.localizedDescription }
    }

    private func hostRow(_ host: FleetHost) -> some View {
        railButton(host.label, host.ssh.isEmpty ? "folder" : "server.rack",
                   selected: section == "Workspace" && host.id == selectedHost) { openWorkspace(host) }
            .contextMenu {
                Button("Workspace settings…") { if !dirty || mayDiscardEdits() { draft = host } }
                if host.id != "local" {
                    Button("Remove workspace…") {
                        if !dirty || mayDiscardEdits() {
                            selectedHost = host.id
                            confirmingRemoval = true
                        }
                    }
                }
            }
    }

    private func railButton(_ text: String, _ symbol: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 11) {
                Image(systemName: symbol).frame(width: 17)
                Text(text).lineLimit(1)
                Spacer()
            }.font(.system(size: 13, weight: selected ? .semibold : .regular))
                .foregroundStyle(selected ? accent : Color.primary.opacity(0.76))
                .padding(.horizontal, 10).frame(height: 34)
                .background(selected ? accent.opacity(0.105) : .clear, in: RoundedRectangle(cornerRadius: 6))
                .contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func openWorkspace(_ host: FleetHost) {
        if current != host && dirty && !mayDiscardEdits() { return }
        openedRepo = host.repo
        selectedHost = host.id
        section = "Workspace"
    }

    private func loadHosts() async {
        do {
            let result = try await FleetBridge.send(["op": "hosts"])
            let data = try JSONSerialization.data(withJSONObject: result["hosts"] ?? [])
            hosts = try JSONDecoder().decode([FleetHost].self, from: data)
            if !hosts.contains(where: { $0.id == selectedHost }) {
                selectedHost = hosts.first?.id ?? "local"
                openedRepo = nil
            }
        } catch { self.error = error.localizedDescription }
    }
}

private struct HostForm: View {
    @State var host: FleetHost
    let remote: Bool
    let saved: (FleetHost) async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var saving = false
    @State private var issue = ""
    @State private var testing = false
    @State private var connected = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Workspace settings").font(.title2.bold())
            Form {
                TextField("Name", text: $host.label)
                if remote { TextField("SSH destination", text: $host.ssh, prompt: Text("user@host or SSH alias")) }
                HStack {
                    TextField("Repository", text: $host.repo)
                    if !remote {
                        Button("Browse…") {
                            let panel = NSOpenPanel()
                            panel.canChooseDirectories = true
                            panel.canChooseFiles = false
                            panel.allowsMultipleSelection = false
                            if panel.runModal() == .OK, let folder = panel.url { host.repo = folder.path }
                        }
                    }
                }
            }
            if !issue.isEmpty { Text(issue).font(.caption).foregroundStyle(.orange) }
            HStack {
                if remote {
                    Button(testing ? "Connecting…" : "Test SSH") {
                        testing = true; connected = false; issue = ""
                        Task {
                            do {
                                _ = try await FleetBridge.send(["op": "connect_host", "host": host.dictionary])
                                connected = true
                            } catch { issue = error.localizedDescription }
                            testing = false
                        }
                    }.disabled(testing || host.ssh.isEmpty || host.repo.isEmpty || host.label.isEmpty)
                    if connected { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green) }
                }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save workspace") {
                    saving = true
                    Task {
                        do {
                            _ = try await FleetBridge.send(["op": "save_host", "host": host.dictionary])
                            await saved(host)
                        } catch { issue = error.localizedDescription; saving = false }
                    }
                }.keyboardShortcut(.defaultAction).disabled(saving || host.label.isEmpty || host.repo.isEmpty || (remote && host.ssh.isEmpty))
            }
        }.padding(24).frame(width: 490).preferredColorScheme(.dark)
    }
}
