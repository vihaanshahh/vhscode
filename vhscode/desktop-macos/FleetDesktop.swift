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
           ["sidebar_agents", "detail", "snapshot", "files", "read_file", "workspace_settings", "workspace_agents"].contains(op) {
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
    private struct SidebarAgent: Decodable, Identifiable, Equatable {
        let agent: String
        let lane: Int
        let status: String
        let title: String?
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
        var statusColor: Color {
            switch status {
            case "running": .teal
            case "completed": .green
            case "failed": .orange
            default: .secondary
            }
        }
    }

    private struct SidebarQuery: Equatable {
        let active: Bool
        let hosts: [FleetHost]
    }

    @Environment(\.scenePhase) private var scenePhase
    @State private var hosts: [FleetHost] = []
    @AppStorage("FleetLastWorkspace") private var selectedHost = "local"
    @State private var collapsedGroups = Set<String>()
    @State private var openedRepo: String?
    @State private var section = "Workspace"
    @State private var activityAgentOpen = false
    @State private var draft: FleetHost?
    @State private var error = ""
    @State private var confirmingRemoval = false
    @State private var dirty = false
    @State private var expandedAgents: [String: Bool] = [:]
    @State private var sidebarAgents: [FleetHost: [SidebarAgent]] = [:]
    @State private var laneSelection: FleetAgentLaneSelection?
    @State private var localAgentLabels: [FleetHost: [FleetAgentLane: String]] = [:]
    @State private var activeAgentLane: [FleetHost: FleetAgentLane] = [:]
    private var current: FleetHost? {
        guard var host = hosts.first(where: { $0.id == selectedHost }) ?? hosts.first else { return nil }
        if let openedRepo, host.id == selectedHost { host.repo = openedRepo }
        return host
    }
    private var sidebarQuery: SidebarQuery {
        SidebarQuery(active: scenePhase == .active, hosts: hosts.map(sidebarHost))
    }
    private var activityAgents: [FleetActivityAgent] {
        sidebarQuery.hosts.flatMap { host in
            agentRows(host).compactMap { agent in
                guard agent.schedule == nil else { return nil }
                return FleetActivityAgent(host: host, lane: FleetAgentLane(agent: agent.agent, lane: agent.lane),
                                          label: agent.label, status: agent.status, statusLabel: agent.statusLabel)
            }
        }
    }
    private let sections = [("Activity", "waveform.path.ecg"), ("Schedules", "calendar.badge.clock"), ("Risks", "exclamationmark.triangle")]

    var body: some View {
        let showsFiles = section == "Workspace"
        let showsActivityAgent = section == "Activity" && activityAgentOpen && current != nil
        let showsWorkspace = showsFiles || showsActivityAgent
        let agents = section == "Activity" ? activityAgents : []
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 9) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 8).fill(accent.opacity(0.13))
                        Image(systemName: "square.3.layers.3d").font(.system(size: 17, weight: .medium)).foregroundStyle(accent)
                    }.frame(width: 30, height: 30)
                    Text("Fleet").font(.system(size: 22, weight: .semibold, design: .rounded))
                }.padding(.bottom, 17)
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
                }.padding(.top, 17).padding(.bottom, 6)
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
                if let host = current {
                    Divider().padding(.vertical, 8)
                    Button { if !dirty || mayDiscardEdits() { draft = host } } label: {
                        Label("Workspace settings", systemImage: "slider.horizontal.3")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }.buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 11).padding(.top, 42).padding(.bottom, 14)
            .frame(width: 164)
            .background(Color(red: 0.095, green: 0.103, blue: 0.117))
            Divider()
            if !showsFiles {
                let activeAgent = showsActivityAgent ? agents.first { $0.host == current && $0.lane == activeAgentLane[$0.host] } : nil
                FleetOverview(hosts: hosts, section: section, agents: agents, activeAgentID: activeAgent?.id,
                              showsAgent: showsActivityAgent, onOpen: { openWorkspace($0) }, onSelectAgent: {
                    openWorkspace($0.host, lane: $0.lane, inActivity: true)
                }, onShowRuns: { activityAgentOpen = false })
                .frame(width: showsActivityAgent ? 350 : nil)
                if showsActivityAgent { Divider() }
            }
            ZStack {
                if let host = current {
                    WorkspaceView(host: host, monitorRuns: showsWorkspace && scenePhase == .active, showsFiles: showsFiles,
                                  onOpenFiles: { openWorkspace(host) }, selectedAgentLane: laneSelection, onAgentLabelsChange: { labels in
                        if localAgentLabels[host] != labels {
                            localAgentLabels[host] = labels
                            if labels != nil { sidebarAgents[host] = agentRows(host) }
                        }
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
        .background(Color(red: 0.075, green: 0.083, blue: 0.095))
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
                        .frame(width: 15, height: 28)
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
                            Circle().fill(agent.statusColor).frame(width: 5, height: 5)
                            Text(agent.label).lineLimit(1)
                            Spacer(minLength: 1)
                            Text(agent.statusLabel).font(.system(size: 9)).foregroundStyle(agent.statusColor)
                        }
                        .font(.system(size: 11, weight: focused ? .semibold : .regular))
                        .padding(.horizontal, 5).frame(height: 22)
                        .background(focused ? accent.opacity(0.105) : .clear, in: RoundedRectangle(cornerRadius: 4))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .padding(.leading, 19)
                    .accessibilityLabel("\(agent.label), \(agent.statusLabel == "—" ? "status unavailable" : agent.statusLabel)")
                    .help(agent.label)
                }
            }
        }
    }

    private func sidebarHost(_ host: FleetHost) -> FleetHost {
        current?.id == host.id ? (current ?? host) : host
    }

    private func agentRows(_ workspace: FleetHost) -> [SidebarAgent] {
        let remote = sidebarAgents[workspace] ?? []
        let known = remote.filter { $0.schedule == nil && ["codex", "cursor"].contains($0.agent) && $0.lane > 0 }
        guard current == workspace, let local = localAgentLabels[workspace] else {
            return known + remote.filter { $0.schedule != nil }
        }
        let knownByLane = Dictionary(uniqueKeysWithValues: known.map {
            (FleetAgentLane(agent: $0.agent, lane: $0.lane), $0)
        })
        let manual = local.keys.sorted { ($0.agent, $0.lane) < ($1.agent, $1.lane) }.map { lane in
            SidebarAgent(agent: lane.agent, lane: lane.lane, status: knownByLane[lane]?.status ?? "ready",
                         title: knownByLane[lane]?.title, schedule: nil, name: local[lane])
        }
        return manual + remote.filter { $0.schedule != nil }
    }

    private func monitorSidebar(_ query: SidebarQuery) async {
        guard query.active, !query.hosts.isEmpty else { return }
        let currentHosts = Set(query.hosts)
        sidebarAgents = sidebarAgents.filter { currentHosts.contains($0.key) }
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
            let requestedLabels = localAgentLabels
            let rows: [String: [SidebarAgent]]
            do {
                let reply = try await FleetBridge.call(target, ["op": "sidebar_agents", "repos": repos])
                let data = try JSONSerialization.data(withJSONObject: reply["workspaces"] ?? NSNull(), options: .fragmentsAllowed)
                rows = try JSONDecoder().decode([String: [SidebarAgent]].self, from: data)
            } catch {
                rows = [:]
            }
            guard !Task.isCancelled, query == sidebarQuery else { return }
            var updated = sidebarAgents
            for workspace in workspaces where requestedLabels[workspace] == localAgentLabels[workspace] {
                updated[workspace] = rows[workspace.repo]
            }
            if sidebarAgents != updated { sidebarAgents = updated }
            let running = rows.values.contains { $0.contains { $0.status == "running" } }
            do { try await Task.sleep(for: .seconds(running ? 3 : 8)) } catch { return }
        }
    }

    private func railButton(_ text: String, _ symbol: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbol).frame(width: 15)
                Text(text).lineLimit(1)
                Spacer()
            }.font(.system(size: 12, weight: selected ? .semibold : .regular))
                .foregroundStyle(selected ? accent : Color.primary.opacity(0.76))
                .padding(.horizontal, 6).frame(height: 28)
                .background(selected ? accent.opacity(0.105) : .clear, in: RoundedRectangle(cornerRadius: 6))
                .contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func openWorkspace(_ host: FleetHost, lane: FleetAgentLane? = nil, inActivity: Bool = false) {
        if current != host && dirty && !mayDiscardEdits() { return }
        if let lane {
            laneSelection = FleetAgentLaneSelection(agent: lane.agent, lane: lane.lane)
        } else if current != host {
            laneSelection = nil
        }
        openedRepo = host.repo
        selectedHost = host.id
        activityAgentOpen = inActivity
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
            if !issue.isEmpty { Text(issue).font(.caption).foregroundStyle(.orange) }
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
                    if connected { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green) }
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
        }.padding(24).frame(width: 490).preferredColorScheme(.dark)
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
                Text(folderIssue.message).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
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
