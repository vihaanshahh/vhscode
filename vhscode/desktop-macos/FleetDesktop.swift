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

struct FleetAgentLaneSelection: Equatable {
    let id = UUID()
    let agent: String
    let lane: Int
    var action: FleetAgentAction? = nil
}

struct FleetAgentLane: Hashable {
    let agent: String
    let lane: Int
    var defaultLabel: String {
        let provider = agent == "codex" ? "Codex" : "Cursor"
        return lane == 1 ? provider : "\(provider) \(lane)"
    }
}

enum FleetBridge {
    static let resources = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")

    static func makeProcess(_ arguments: [String] = []) -> Process {
        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-E", "-s", "-B", resources.appendingPathComponent("fleet/fleet_desktop.py").path] + arguments
        process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = resources.appendingPathComponent("bin").path + ":" + (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        process.environment = environment
        return process
    }

    static func call(_ host: FleetHost, _ request: [String: Any]) async throws -> [String: Any] {
        let payload: [String: Any] = ["host": host.dictionary, "request": request]
        if host.ssh.isEmpty, let op = request["op"] as? String,
           ["sidebar_agents", "detail", "snapshot", "files", "read_file", "git_status", "workspace_settings", "workspace_agents"].contains(op) {
            let input = try JSONSerialization.data(withJSONObject: payload)
            return try decode(await LocalReadWorker.shared.read(input))
        }
        return try await send(payload)
    }

    @MainActor
    static func openTerminal(_ host: FleetHost, agent: String? = nil, session: String? = nil) async throws {
        let workspace = NSWorkspace.shared
        guard let terminal = workspace.urlForApplication(withBundleIdentifier: "com.apple.Terminal") else {
            throw FleetError(message: "Terminal could not be found.")
        }
        var arguments = [resources.appendingPathComponent("bin/vhscode-fleet").path]
        if let agent {
            arguments += ["--native", "--agent", agent]
            if let session, !session.isEmpty { arguments.append("--session=" + session) }
        } else {
            arguments.append("--shell")
        }
        if !host.ssh.isEmpty { arguments += ["--ssh=" + host.ssh] }
        arguments += ["--", host.repo]
        let command = arguments.map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }.joined(separator: " ")
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let script = directory.appendingPathComponent("Fleet Workbench.command")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        do {
            try """
            #!/bin/sh
            /bin/rm -f -- "$0"
            /bin/rmdir -- "${0%/*}"
            exec \(command)

            """.write(to: script, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                workspace.open([script], withApplicationAt: terminal, configuration: .init()) { _, error in
                    if let error {
                        continuation.resume(throwing: error)
                        return
                    }
                    continuation.resume()
                }
            }
        } catch {
            try? FileManager.default.removeItem(at: directory)
            throw error
        }
    }

    static func send(_ payload: [String: Any]) async throws -> [String: Any] {
        let input = try JSONSerialization.data(withJSONObject: payload)
        if payload["op"] as? String == "hosts" {
            return try decode(await LocalReadWorker.shared.read(input))
        }
        let data = try await Task.detached(priority: Task.currentPriority) { () throws -> Data in
            let process = makeProcess()
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
        return try decode(data)
    }

    private static func decode(_ data: Data) throws -> [String: Any] {
        guard let reply = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw FleetError(message: "The host returned an unreadable response.")
        }
        if let error = reply["error"] as? String { throw FleetError(message: error) }
        return reply
    }

}

// Serialize short local reads so only one interpreter is resident. Longer operations use their own process.
private actor LocalReadWorker {
    static let shared = LocalReadWorker()
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?

    func read(_ data: Data) throws -> Data {
        try Task.checkCancellation()
        do {
            return try exchange(data)
        } catch {
            close()
            try Task.checkCancellation()
            // The idle worker may exit between the liveness check and the write.
            do { return try exchange(data) } catch {
                close()
                throw error
            }
        }
    }

    private func exchange(_ data: Data) throws -> Data {
        if process?.isRunning != true {
            close()
            let child = FleetBridge.makeProcess(["--read-stream"])
            let stdin = Pipe(), stdout = Pipe()
            child.standardInput = stdin
            child.standardOutput = stdout
            child.standardError = FileHandle.nullDevice
            try child.run()
            process = child
            input = stdin.fileHandleForWriting
            output = stdout.fileHandleForReading
        }
        guard let input, let output else { throw FleetError(message: "Workspace reader is unavailable.") }
        var line = data
        line.append(10)
        try input.write(contentsOf: line)
        let header = output.readData(ofLength: 4)
        let size = header.reduce(0) { $0 * 256 + Int($1) }
        guard header.count == 4, size > 0, size <= 16 * 1024 * 1024 else {
            throw FleetError(message: "The workspace reader returned an invalid response.")
        }
        var result = Data()
        result.reserveCapacity(size)
        while result.count < size {
            guard let chunk = try output.read(upToCount: size - result.count), !chunk.isEmpty else {
                throw FleetError(message: "Workspace response was interrupted.")
            }
            result.append(chunk)
        }
        return result
    }

    private func close() {
        try? input?.close()
        try? output?.close()
        if let process, process.isRunning { process.terminate() }
        input = nil
        output = nil
        process = nil
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

@main
struct FleetDesktop: App {
    @NSApplicationDelegateAdaptor(FleetAppDelegate.self) private var delegate
    @AppStorage("FleetTheme") private var theme = WorkbenchTheme.midnight
    @AppStorage("FleetCodeFont") private var family = WorkbenchCodeFont.system
    @AppStorage("FleetCodeFontSize") private var size = 13

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
            FleetWindow()
                .environment(\.workbenchPalette, theme.palette)
                .environment(\.workbenchTypography, WorkbenchTypography(family: family, size: size))
                .preferredColorScheme(theme.scheme)
                .tint(theme.palette.tint)
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
        Settings { WorkbenchSettings() }
    }
}

private struct FleetWindow: View {
    private struct SidebarAgent: Decodable, Identifiable, Equatable {
        let agent: String
        let lane: Int
        let status: String
        let title: String?
        let action: String?
        let schedule: String?
        let name: String?
        var id: String { schedule.map { "schedule-\($0)" } ?? "\(agent)-\(lane)" }
        var label: String {
            if schedule != nil { return title ?? (agent == "codex" ? "Codex schedule" : "Cursor schedule") }
            return name ?? FleetAgentLane(agent: agent, lane: lane).defaultLabel
        }
        var statusLabel: String {
            switch status {
            case "running": "Running"
            case "completed": "Done"
            case "failed": "Failed"
            case "cancelled": "Stopped"
            case "paused": "Paused"
            case "scheduled": "Scheduled"
            case "ready": "Ready"
            default: "—"
            }
        }
        func statusColor(_ palette: WorkbenchPalette) -> Color {
            switch status {
            case "running": palette.tint
            case "completed": palette.positive
            case "failed": palette.caution
            default: .secondary
            }
        }
    }

    private struct SidebarQuery: Equatable {
        let active: Bool
        let dashboard: Bool
        let revision: UUID
        let hosts: [FleetHost]
    }

    private struct SidebarRoster: Decodable {
        let workspaces: [String: [SidebarAgent]]
        let liveRuns: [FleetLiveRun]?

        enum CodingKeys: String, CodingKey {
            case workspaces
            case liveRuns = "live_runs"
        }
    }

    @Environment(\.workbenchPalette) private var palette
    @Environment(\.scenePhase) private var scenePhase
    @State private var hosts: [FleetHost] = []
    @AppStorage("FleetLastWorkspace") private var selectedHost = "local"
    @State private var collapsedGroups = Set<String>()
    @State private var openedRepo: String?
    @State private var section = "Workspace"
    @State private var activityAgentOpen = false
    @State private var activityRunsOpen = false
    @State private var activityRunID: String?
    @State private var draft: FleetHost?
    @State private var error = ""
    @State private var confirmingRemoval = false
    @State private var dirty = false
    @State private var expandedAgents: [String: Bool] = [:]
    @State private var sidebarAgents: [FleetHost: [SidebarAgent]] = [:]
    @State private var liveRuns: [String: [FleetLiveRun]] = [:]
    @State private var reachable: [String: Bool] = [:]
    @State private var laneSelection: FleetAgentLaneSelection?
    @State private var rosterRevision = UUID()
    @State private var activeAgentLane: [FleetHost: FleetAgentLane] = [:]
    private var current: FleetHost? {
        guard var host = hosts.first(where: { $0.id == selectedHost }) ?? hosts.first else { return nil }
        if let openedRepo, host.id == selectedHost { host.repo = openedRepo }
        return host
    }
    private var sidebarQuery: SidebarQuery {
        SidebarQuery(active: scenePhase == .active, dashboard: section == "Activity" && !activityRunsOpen,
                     revision: rosterRevision, hosts: hosts.map(sidebarHost))
    }
    private var activityAgents: [FleetActivityAgent] {
        sidebarQuery.hosts.flatMap { host in
            agentRows(host).compactMap { agent in
                guard agent.schedule == nil else { return nil }
                return FleetActivityAgent(host: host, lane: FleetAgentLane(agent: agent.agent, lane: agent.lane),
                                          label: agent.label, status: agent.status, statusLabel: agent.statusLabel,
                                          title: agent.title, action: agent.action, reachable: reachable[host.ssh] != false)
            }
        }
    }
    private var activityHosts: [FleetHost] {
        var seen = Set<String>()
        return hosts.filter { seen.insert($0.ssh).inserted }
    }

    private let sections = [("Activity", "waveform.path.ecg"), ("Schedules", "calendar.badge.clock"), ("Risks", "exclamationmark.triangle")]

    var body: some View {
        let showsFiles = section == "Workspace"
        let showsDashboard = section == "Activity" && !activityRunsOpen
        let showsActivityAgent = showsDashboard && activityAgentOpen && current != nil
        let showsWorkspace = showsFiles || showsActivityAgent
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 6) {
                    Image(systemName: "square.3.layers.3d")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(palette.tint)
                        .accessibilityHidden(true)
                    Text("Fleet").font(.system(size: 15, weight: .semibold, design: .rounded))
                }
                .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading)
                .padding(.bottom, 6)
                ForEach(sections, id: \.0) { item in
                    railButton(item.0, item.1, selected: section == item.0) {
                        if item.0 == "Activity" { activityRunsOpen = false }
                        section = item.0
                    }
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
                }.padding(.top, 11).padding(.bottom, 6)
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
                palette.separator.frame(height: 1).padding(.vertical, 8)
                if let host = current {
                    railButton("Workspace settings", "slider.horizontal.3", selected: false) {
                        if !dirty || mayDiscardEdits() { draft = host }
                    }
                }
                SettingsLink {
                    Label("Appearance", systemImage: "paintpalette")
                        .font(.system(size: 12))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 8)
                        .frame(height: 30)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Change theme and code font")
            }
            .padding(.horizontal, 13).padding(.top, 34).padding(.bottom, 12)
            .frame(width: 180)
            .background(palette.navigation)
            palette.separator.frame(width: 1)
            if showsDashboard {
                dashboard(showsAgent: showsActivityAgent)
                    .frame(width: showsActivityAgent ? 360 : nil)
                if showsActivityAgent { palette.separator.frame(width: 1) }
            } else if !showsFiles {
                overview
            }
            ZStack {
                if let host = current {
                    WorkspaceView(host: host, monitorRuns: showsWorkspace && scenePhase == .active, showsFiles: showsFiles,
                                  onOpenFiles: { openWorkspace(host) }, selectedAgentLane: laneSelection, onAgentLabelsChange: { labels in
                        if labels != nil { rosterRevision = UUID() }
                    }, onActiveAgentLaneChange: { lane in
                        if activeAgentLane[host] != lane { activeAgentLane[host] = lane }
                    }).id(host)
                } else if showsFiles {
                    ProgressView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .frame(width: showsWorkspace ? nil : 0)
            .opacity(showsWorkspace ? 1 : 0)
            .allowsHitTesting(showsWorkspace)
            .accessibilityHidden(!showsWorkspace)
        }
        .background(palette.editor)
        .frame(minWidth: 960, minHeight: 620)
        .task { await loadHosts() }
        .task(id: sidebarQuery) { await monitorSidebar(sidebarQuery) }
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
                await loadHosts(); selectedHost = saved.id; openedRepo = nil; section = "Workspace"; laneSelection = nil; draft = nil
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
                        selectedHost = "local"; openedRepo = nil; laneSelection = nil; await loadHosts()
                    } catch { self.error = error.localizedDescription }
                }
            }
        }
    }

    private func addSSH() {
        draft = FleetHost(id: UUID().uuidString, label: "", ssh: "", repo: "~")
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
                selectedHost = id; openedRepo = nil; section = "Workspace"; laneSelection = nil
                if let group = current?.group { collapsedGroups.remove(group) }
            }
        } catch { self.error = error.localizedDescription }
    }

    private func dashboard(showsAgent: Bool) -> some View {
        let agents = activityAgents
        let activeAgent = showsAgent ? agents.first { $0.host == current && $0.lane == activeAgentLane[$0.host] } : nil
        let endpoints = activityHosts
        let running = endpoints.filter { reachable[$0.ssh] == true }.flatMap { host in
            (liveRuns[host.ssh] ?? []).map { (host, $0) }
        }
        return ActivityDashboard(agents: agents, runs: running,
                                 unavailable: endpoints.filter { reachable[$0.ssh] == false }.map(\.label),
                                 loading: endpoints.contains { reachable[$0.ssh] == nil },
                                 selectedAgentID: activeAgent?.id, onOpenAgent: { agent, action in
            openWorkspace(agent.host, lane: agent.lane, inActivity: true, action: action)
        }, onOpenRun: { showRuns($0) }, onShowRuns: { showRuns(nil) },
           onCloseAgent: showsAgent ? { activityAgentOpen = false } : nil)
    }

    private var overview: some View {
        FleetOverview(hosts: hosts, section: section == "Activity" ? "Runs" : section, agents: activityAgents,
                      requestedRunID: section == "Activity" ? activityRunID : nil,
                      onShowAgents: section == "Activity" ? { activityRunsOpen = false } : nil,
                      onOpen: { openWorkspace($0) }, onSelectAgent: {
            openWorkspace($0.host, lane: $0.lane, inActivity: true)
        })
    }

    private func hostRow(_ host: FleetHost) -> some View {
        let workspace = sidebarHost(host)
        let selected = section == "Workspace" && workspace.id == selectedHost
        let expanded = expandedAgents[workspace.id] ?? selected
        return VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 1) {
                Button {
                    expandedAgents[workspace.id] = !expanded
                } label: {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .frame(width: 15, height: 31)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel(expanded ? "Hide agents for \(workspace.label)" : "Show agents for \(workspace.label)")
                railButton(workspace.label, workspace.ssh.isEmpty ? "folder" : "server.rack",
                           selected: selected) { openWorkspace(workspace) }
                    .help(workspace.label)
            }
            .contextMenu {
                Button("Workspace settings…") { if !dirty || mayDiscardEdits() { draft = workspace } }
                if workspace.id != "local" {
                    Button("Remove workspace…") {
                        if !dirty || mayDiscardEdits() {
                            selectedHost = workspace.id
                            laneSelection = nil
                            confirmingRemoval = true
                        }
                    }
                }
            }
            if expanded {
                ForEach(agentRows(workspace)) { agent in
                    let lane = FleetAgentLane(agent: agent.agent, lane: agent.lane)
                    let focused = (selected || (section == "Activity" && activityAgentOpen && current == workspace))
                        && agent.schedule == nil && activeAgentLane[workspace] == lane
                    Button {
                        if agent.schedule != nil { section = "Schedules" }
                        else { openWorkspace(workspace, lane: lane, inActivity: section == "Activity") }
                    } label: {
                        HStack(spacing: 4) {
                            Circle().fill(agent.statusColor(palette)).frame(width: 5, height: 5)
                            Text(agent.label).lineLimit(1)
                            Spacer(minLength: 1)
                            Text(agent.statusLabel).font(.system(size: 9)).foregroundStyle(agent.statusColor(palette))
                        }
                        .font(.system(size: 11, weight: focused ? .semibold : .regular))
                        .padding(.horizontal, 6).frame(height: 26)
                        .background(focused ? palette.selection : .clear, in: RoundedRectangle(cornerRadius: 4))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .padding(.leading, 19)
                    .accessibilityLabel("\(agent.label), \(agent.statusLabel == "—" ? "status unavailable" : agent.statusLabel)")
                    .help(agent.label)
                    .contextMenu {
                        if agent.schedule == nil {
                            Button("Rename Agent…") {
                                openWorkspace(workspace, lane: lane, inActivity: section == "Activity", action: .rename)
                            }
                            Button("Delete Agent…", role: .destructive) {
                                openWorkspace(workspace, lane: lane, inActivity: section == "Activity", action: .delete)
                            }
                            .disabled(agent.status == "running")
                        }
                    }
                }
            }
        }
    }

    private func sidebarHost(_ host: FleetHost) -> FleetHost {
        current?.id == host.id ? (current ?? host) : host
    }

    private func agentRows(_ workspace: FleetHost) -> [SidebarAgent] {
        (sidebarAgents[workspace] ?? []).filter {
            $0.schedule != nil || (["codex", "cursor"].contains($0.agent) && $0.lane > 0)
        }
    }

    private func monitorSidebar(_ query: SidebarQuery) async {
        guard query.active, !query.hosts.isEmpty else { return }
        let currentHosts = Set(query.hosts)
        sidebarAgents = sidebarAgents.filter { currentHosts.contains($0.key) }
        let destinations = Set(query.hosts.map(\.ssh))
        reachable = reachable.filter { destinations.contains($0.key) }
        liveRuns = liveRuns.filter { destinations.contains($0.key) }
        await withTaskGroup(of: Void.self) { group in
            for workspaces in Dictionary(grouping: query.hosts, by: \.ssh).values {
                group.addTask { await monitorSidebarDestination(workspaces, query: query) }
            }
        }
    }

    private func monitorSidebarDestination(_ workspaces: [FleetHost], query: SidebarQuery) async {
        guard let target = workspaces.first else { return }
        let repos = Set(workspaces.map(\.repo)).sorted()
        while !Task.isCancelled {
            let roster: SidebarRoster?
            do {
                let reply = try await FleetBridge.call(target, ["op": "sidebar_agents", "repos": repos, "live": query.dashboard])
                roster = try JSONDecoder().decode(SidebarRoster.self, from: JSONSerialization.data(withJSONObject: reply))
            } catch {
                roster = nil
            }
            guard !Task.isCancelled, query == sidebarQuery else { return }
            if reachable[target.ssh] != (roster != nil) { reachable[target.ssh] = roster != nil }
            if let roster {
                var updated = sidebarAgents
                for workspace in workspaces {
                    updated[workspace] = roster.workspaces[workspace.repo]
                }
                if sidebarAgents != updated { sidebarAgents = updated }
                if let runs = roster.liveRuns, liveRuns[target.ssh] != runs { liveRuns[target.ssh] = runs }
            }
            let running = roster?.workspaces.values.contains { $0.contains { $0.status == "running" } } == true
            do { try await Task.sleep(for: .seconds(query.dashboard ? 2 : running ? 3 : 8)) } catch { return }
        }
    }

    private func railButton(_ text: String, _ symbol: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbol).frame(width: 15)
                Text(text).lineLimit(1)
                Spacer()
            }.font(.system(size: 12, weight: selected ? .semibold : .regular))
                .foregroundStyle(selected ? palette.tint : Color.primary.opacity(0.76))
                .padding(.horizontal, 8).frame(height: 31)
                .background(selected ? palette.selection : .clear, in: RoundedRectangle(cornerRadius: 6))
                .contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func showRuns(_ id: String?) {
        activityRunID = id
        activityRunsOpen = true
        activityAgentOpen = false
    }

    private func openWorkspace(_ host: FleetHost, lane: FleetAgentLane? = nil,
                               inActivity: Bool = false, action: FleetAgentAction? = nil) {
        if current != host && dirty && !mayDiscardEdits() { return }
        if let lane {
            laneSelection = FleetAgentLaneSelection(agent: lane.agent, lane: lane.lane, action: action)
        } else if current != host {
            laneSelection = nil
        }
        openedRepo = host.repo
        selectedHost = host.id
        activityAgentOpen = inActivity
        if inActivity { activityRunsOpen = false }
        section = inActivity ? "Activity" : "Workspace"
    }

    private func loadHosts() async {
        do {
            let result = try await FleetBridge.send(["op": "hosts"])
            let data = try JSONSerialization.data(withJSONObject: result["hosts"] ?? [])
            hosts = try JSONDecoder().decode([FleetHost].self, from: data)
            if !hosts.contains(where: { $0.id == selectedHost }) {
                selectedHost = hosts.first?.id ?? "local"
                openedRepo = nil
                laneSelection = nil
            }
        } catch { self.error = error.localizedDescription }
    }
}

private struct HostForm: View {
    private struct FolderPage {
        let directory: String
        let parent: String?
        let folders: [String]
        let cursor: String?
    }

    private struct FolderFailure {
        let message: String
        let path: String
        let cursor: String?
    }

    @State var host: FleetHost
    let remote: Bool
    let saved: (FleetHost) async -> Void
    @Environment(\.workbenchPalette) private var palette
    @Environment(\.dismiss) private var dismiss
    @State private var saving = false
    @State private var issue = ""
    @State private var testing = false
    @State private var connected = false
    @State private var destinationRevision = UUID()
    @State private var browsing = false
    @State private var folderPage: FolderPage?
    @State private var folderIssue: FolderFailure?
    @State private var folderRequest: UUID?

    private var destination: String { host.ssh.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var repository: String { host.repo.isEmpty ? "~" : host.repo }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Workspace settings").font(.title2.bold())
            Form {
                TextField("Name", text: $host.label)
                if remote { TextField("SSH destination", text: $host.ssh, prompt: Text("user@host or SSH alias")) }
                HStack {
                    TextField(remote ? "Remote folder" : "Repository", text: $host.repo)
                    if remote {
                        Button(browsing ? "Hide folders" : "Browse…") {
                            if browsing {
                                closeBrowser()
                            } else {
                                browsing = true
                                browse(repository)
                            }
                        }.disabled(destination.isEmpty || testing)
                    } else {
                        Button("Browse…") {
                            let panel = NSOpenPanel()
                            panel.canChooseDirectories = true
                            panel.canChooseFiles = false
                            panel.allowsMultipleSelection = false
                            if panel.runModal() == .OK, let folder = panel.url { host.repo = folder.path }
                        }
                    }
                }
            }.disabled(saving)
            if browsing { remoteFolders.disabled(saving) }
            if !issue.isEmpty { Text(issue).font(.caption).foregroundStyle(palette.caution) }
            HStack {
                if remote {
                    Button(testing ? "Connecting…" : "Test SSH") { testSSH() }
                        .disabled(saving || testing || folderRequest != nil || destination.isEmpty)
                    Button("Open Terminal") {
                        var target = host
                        target.ssh = destination
                        target.repo = repository
                        Task {
                            do { try await FleetBridge.openTerminal(target) }
                            catch { issue = error.localizedDescription }
                        }
                    }.disabled(saving || destination.isEmpty)
                    if connected { Image(systemName: "checkmark.circle.fill").foregroundStyle(palette.positive) }
                }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save workspace") {
                    saving = true
                    var workspace = host
                    workspace.ssh = destination
                    Task {
                        do {
                            _ = try await FleetBridge.send(["op": "save_host", "host": workspace.dictionary])
                            await saved(workspace)
                        } catch { issue = error.localizedDescription; saving = false }
                    }
                }.keyboardShortcut(.defaultAction).disabled(saving || host.label.isEmpty || host.repo.isEmpty || (remote && destination.isEmpty))
            }
        }.padding(24).frame(width: 490)
            .onChange(of: destination) {
                destinationRevision = UUID()
                connected = false
                testing = false
                issue = ""
                closeBrowser()
            }
    }

    private var remoteFolders: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button { browse("~") } label: {
                    Image(systemName: "house")
                }.help("Remote home folder").accessibilityLabel("Remote home folder")
                if let parent = folderPage?.parent {
                    Button { browse(parent) } label: {
                        Image(systemName: "arrow.up")
                    }.help("Parent folder").accessibilityLabel("Parent folder")
                }
                Text(folderPage?.directory ?? repository)
                    .font(.caption.monospaced())
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                Spacer()
                if folderRequest != nil { ProgressView().controlSize(.small) }
            }.disabled(testing || folderRequest != nil)

            if let folderIssue {
                Text(folderIssue.message).font(.caption).foregroundStyle(palette.caution).textSelection(.enabled)
                if !connected {
                    Text("Use Test SSH if the host needs authentication.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button("Retry") { browse(folderIssue.path, cursor: folderIssue.cursor) }
                    .disabled(testing || folderRequest != nil)
            }
            if let page = folderPage {
                if page.folders.isEmpty {
                    Text("No subfolders").font(.caption).foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(page.folders, id: \.self) { name in
                                Button {
                                    let separator = page.directory.hasSuffix("/") ? "" : "/"
                                    browse(page.directory + separator + name)
                                } label: {
                                    HStack {
                                        Label(name, systemImage: "folder")
                                        Spacer()
                                        Image(systemName: "chevron.right").font(.caption)
                                    }.padding(5).contentShape(Rectangle())
                                }.buttonStyle(.plain)
                            }
                        }
                    }.frame(height: 150).disabled(testing || folderRequest != nil)
                }
                HStack {
                    if let cursor = page.cursor {
                        Button("Load More") { browse(page.directory, cursor: cursor) }
                    }
                    Spacer()
                    Button("Use This Folder") {
                        host.repo = page.directory
                        closeBrowser()
                    }
                }.disabled(testing || folderRequest != nil)
            }
        }.padding(10).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 7))
    }

    private func closeBrowser() {
        browsing = false
        folderPage = nil
        folderIssue = nil
        folderRequest = nil
    }

    private func browse(_ path: String, cursor: String? = nil) {
        guard !destination.isEmpty, folderRequest == nil else { return }
        let token = UUID()
        folderRequest = token
        folderIssue = nil
        var request: [String: Any] = ["op": "browse_folders", "path": path]
        request["cursor"] = cursor
        let target = connectionHost
        Task {
            defer { if folderRequest == token { folderRequest = nil } }
            do {
                let reply = try await FleetBridge.call(target, request)
                guard folderRequest == token, destination == target.ssh else { return }
                guard let directory = reply["directory"] as? String,
                      let folders = reply["folders"] as? [String] else {
                    throw FleetError(message: "The SSH host returned an unreadable folder list.")
                }
                let previous = cursor == nil ? [] : (folderPage?.folders ?? [])
                var seen = Set(previous)
                folderPage = FolderPage(directory: directory, parent: reply["parent"] as? String,
                                        folders: previous + folders.filter { seen.insert($0).inserted },
                                        cursor: reply["more"] as? Bool == true ? reply["cursor"] as? String : nil)
            } catch {
                guard folderRequest == token, destination == target.ssh else { return }
                folderIssue = FolderFailure(message: error.localizedDescription, path: path, cursor: cursor)
            }
        }
    }

    private var connectionHost: FleetHost {
        var target = host
        target.ssh = destination
        if target.label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { target.label = destination }
        target.repo = repository
        return target
    }

    private func testSSH() {
        testing = true
        connected = false
        issue = ""
        let target = connectionHost
        let revision = destinationRevision
        Task {
            defer { if destinationRevision == revision { testing = false } }
            do {
                _ = try await FleetBridge.send(["op": "connect_host", "host": target.dictionary])
                guard destinationRevision == revision, destination == target.ssh else { return }
                connected = true
                if browsing, let folderIssue { browse(folderIssue.path, cursor: folderIssue.cursor) }
            } catch {
                guard destinationRevision == revision, destination == target.ssh else { return }
                issue = error.localizedDescription
            }
        }
    }
}
