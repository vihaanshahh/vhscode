import AppKit
import SwiftUI

struct WorkspaceView: View {
    let host: FleetHost
    var monitorRuns = true
    var selectedAgentLane: FleetAgentLaneSelection? = nil
    var onAgentLanesChange: ([FleetAgentLane]) -> Void = { _ in }
    var onActiveAgentLaneChange: (FleetAgentLane) -> Void = { _ in }
    @State private var workspaces: [WorkspaceScope: WorkspaceModel] = [:]

    private var scope: WorkspaceScope {
        WorkspaceScope(id: host.id, ssh: host.ssh, repo: host.repo)
    }

    var body: some View {
        Group {
            if let model = workspaces[scope] {
                WorkspacePane(host: host, monitorRuns: monitorRuns, model: model, selectedAgentLane: selectedAgentLane,
                              onAgentLanesChange: onAgentLanesChange, onActiveAgentLaneChange: onActiveAgentLaneChange)
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .task(id: scope) {
                        if workspaces[scope] == nil {
                            workspaces[scope] = WorkspaceModel(host: host)
                        }
                    }
            }
        }
        .id(scope)
    }
}

private struct WorkspaceScope: Hashable {
    let id: String
    let ssh: String
    let repo: String
}

private struct ProjectEntry: Identifiable {
    let name: String
    let path: String
    let isDirectory: Bool
    var id: String { path }

    init?(_ value: [String: Any]) {
        guard let name = value["name"] as? String, let path = value["path"] as? String,
              let kind = value["kind"] as? String else { return nil }
        self.name = name
        self.path = path
        self.isDirectory = kind == "directory"
    }
}

private struct DirectoryPage {
    var entries: [ProjectEntry] = []
    var currentCursor: String?
    var nextCursor: String?
    var previousCursors: [String?] = []
    var isLoading = false
    var error: String?
    var retry = FolderNavigation.first
}

private enum FolderNavigation { case first, previous, next }

private struct WorkspaceFailure: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

private struct AgentEvent: Identifiable {
    let id: Int
    let kind: String
    let text: String
}

private struct AgentApproval: Identifiable {
    let id: String
    let title: String
    let detail: String
    let decision: String?
}

private struct WorkspaceTurn: Identifiable {
    let id: String
    var prompt: String
    let reporting: String
    var status = "queued"
    var action = ""
    var risk = ""
    var outcome = ""
    var events: [AgentEvent] = []
    var approvals: [AgentApproval] = []
    var isActive: Bool { ["queued", "running"].contains(status) }
    var requiresCursorTrust: Bool {
        events.contains { ["error", "risk"].contains($0.kind) && $0.text.localizedCaseInsensitiveContains("Workspace Trust Required") }
            || risk.localizedCaseInsensitiveContains("Workspace Trust Required")
    }
}

private struct WorkspaceAgent: Hashable, Identifiable {
    let harness: String
    let lane: Int
    var id: Self { self }
    var label: String { "\(harness == "codex" ? "Codex" : "Cursor") \(lane)" }
    var sidebarLane: FleetAgentLane { FleetAgentLane(agent: harness, lane: lane) }
}

private struct AgentDraft {
    var text = ""
    var tags = Set<String>()
    var reporting = "actions"
    var filterProgress = false
}

private enum ComposerCommand: String, CaseIterable, Identifiable {
    case resume, new, status, command, native, shell, help

    var id: Self { self }
    var token: String { "/\(rawValue)" }
    var usage: String {
        switch self {
        case .resume: "\(token) [session ID]"
        case .new, .status, .command, .native, .shell, .help: token
        }
    }

    var description: String {
        switch self {
        case .resume: "Continue a previous CLI session"
        case .new: "Start a fresh conversation in this agent"
        case .status: "Show the selected agent’s state"
        case .command: "Edit the command used to launch this agent"
        case .native: "Open the agent’s interactive CLI in Terminal"
        case .shell: "Open a shell in this workspace"
        case .help: "Show available commands"
        }
    }

    static func parse(_ input: String) -> (command: Self, argument: String)? {
        let line = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.contains(where: \.isNewline) else { return nil }
        let parts = line.split(maxSplits: 1, whereSeparator: \.isWhitespace)
        guard let first = parts.first, first.hasPrefix("/"),
              let command = Self(rawValue: first.dropFirst().lowercased()) else { return nil }
        return (command, parts.count == 2 ? String(parts[1]).trimmingCharacters(in: .whitespacesAndNewlines) : "")
    }

    static func suggestions(_ input: String) -> [Self] {
        let prefix = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard prefix.hasPrefix("/"), !prefix.contains(where: \.isWhitespace) else { return [] }
        return allCases.filter { $0.token.hasPrefix(prefix) }
    }
}

private struct NativeSessionOption: Identifiable {
    let id: String
    let title: String
}

@MainActor
private final class WorkspaceModel: ObservableObject {
    let host: FleetHost
    @Published var folders: [String: DirectoryPage] = [:]
    @Published var expanded = Set<String>()
    @Published var buffers: [SourceBuffer] = []
    @Published var selectedPath: String?
    @Published var loadingFiles = Set<String>()
    @Published var fileErrors: [String: String] = [:]
    @Published var selectedAgent = WorkspaceAgent(harness: "codex", lane: 1)
    @Published private(set) var agents = [WorkspaceAgent(harness: "codex", lane: 1)]
    @Published private var drafts: [WorkspaceAgent: AgentDraft] = [:]
    @Published private var submitting = Set<WorkspaceAgent>()
    @Published private var conversations: [WorkspaceAgent: [WorkspaceTurn]] = [:]
    @Published private var composerErrors: [WorkspaceAgent: String] = [:]
    @Published private var streamErrors: [WorkspaceAgent: String] = [:]
    @Published private var pendingDetails = Set<String>()
    @Published private(set) var loadError: String?
    @Published private var settingsError: String?
    @Published private(set) var cursorSigningIn = false
    @Published private(set) var cursorLoginMessage: String?
    @Published private(set) var agentsLoaded = false
    @Published var stopping = Set<String>()
    @Published private(set) var answeringApprovals = Set<String>()
    @Published var notice: String?
    @Published private(set) var workspaceModels: [String: String] = [:]
    @Published private(set) var savingModels = Set<String>()
    @Published private(set) var modelErrors: [String: String] = [:]
    @Published private(set) var sessions: [WorkspaceAgent: String] = [:]
    @Published private var resetting = Set<WorkspaceAgent>()
    @Published private(set) var modelCatalogs: [String: NativeModelCatalog] = [:]
    @Published private(set) var cursorTrusted = false
    @Published private(set) var savingCursorTrust = false
    @Published private(set) var cursorTrustError: String?
    private var cursorTrustChanged = false
    private var modelsChanged = Set<String>()
    private var loadedSettings = false
    private var checkedWelcome = false
    private var lastAgentSelection: UUID?
    private var lastDirty: Bool?
    private var folderLoads: [String: UUID] = [:]
    private var catalogLoads: [String: UUID] = [:]

    init(host: FleetHost) { self.host = host }

    var selectedBuffer: SourceBuffer? { buffers.first { $0.path == selectedPath } }
    var turns: [WorkspaceTurn] { conversations[selectedAgent] ?? [] }
    var tagPaths: Set<String> { drafts[selectedAgent]?.tags ?? [] }
    var composerError: String? { composerErrors[selectedAgent] ?? streamErrors[selectedAgent] ?? settingsError }
    var canSend: Bool { canSend(from: selectedAgent) }
    var canReset: Bool { canReset(selectedAgent) }

    func draftBinding<Value>(for agent: WorkspaceAgent, _ keyPath: WritableKeyPath<AgentDraft, Value>) -> Binding<Value> {
        Binding(get: { self.drafts[agent, default: AgentDraft()][keyPath: keyPath] },
                set: { self.drafts[agent, default: AgentDraft()][keyPath: keyPath] = $0 })
    }

    private func canSend(from agent: WorkspaceAgent) -> Bool {
        canReset(agent) && !savingModels.contains(agent.harness)
            && (agent.harness != "cursor" || !savingCursorTrust)
            && drafts[agent]?.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
    }

    func input(for agent: WorkspaceAgent) -> String { drafts[agent]?.text ?? "" }

    func finishCommand(for agent: WorkspaceAgent, error: String? = nil) {
        composerErrors[agent] = error
        if error == nil { drafts[agent]?.text = "" }
    }

    func status(for agent: WorkspaceAgent) -> String {
        let state = if resetting.contains(agent) { "Switching session" }
        else if submitting.contains(agent) { "Starting" }
        else { conversations[agent]?.last?.status.capitalized ?? "Idle" }
        return "\(agent.label) · \(state)\nModel: \(workspaceModels[agent.harness] ?? "CLI default")\nSession: \(sessions[agent] ?? "None")"
    }

    private func canReset(_ agent: WorkspaceAgent) -> Bool {
        agentsLoaded && !submitting.contains(agent) && activeRun(agent) == nil && !resetting.contains(agent)
    }

    func activeRun(_ agent: WorkspaceAgent) -> String? {
        guard let last = conversations[agent]?.last, last.isActive else { return nil }
        return last.id
    }

    func observedRun(_ agent: WorkspaceAgent) -> String? {
        guard let last = conversations[agent]?.last,
              last.isActive || (agent == selectedAgent && pendingDetails.contains(last.id)) else { return nil }
        return last.id
    }

    func availableAgent(_ harness: String) -> WorkspaceAgent? {
        (1...4).map { WorkspaceAgent(harness: harness, lane: $0) }.first { !agents.contains($0) }
    }

    func addAgent(_ harness: String) {
        guard agentsLoaded, let agent = availableAgent(harness) else { return }
        agents.append(agent)
        selectedAgent = agent
    }

    func selectAgent(_ selection: FleetAgentLaneSelection?) {
        guard let selection, selection.id != lastAgentSelection,
              ["codex", "cursor"].contains(selection.agent), (1...4).contains(selection.lane) else { return }
        lastAgentSelection = selection.id
        let agent = WorkspaceAgent(harness: selection.agent, lane: selection.lane)
        if !agents.contains(agent) { agents.append(agent) }
        selectedAgent = agent
    }

    func loadFolder(_ directory: String, direction: FolderNavigation = .first) async {
        let previousPage = folders[directory]
        var page = previousPage ?? DirectoryPage()
        guard !page.isLoading else { return }
        let cursor: String?
        switch direction {
        case .first:
            cursor = nil
        case .previous:
            guard let previous = page.previousCursors.last else { return }
            cursor = previous
        case .next:
            guard let next = page.nextCursor else { return }
            cursor = next
        }
        page.isLoading = true
        page.error = nil
        folders[directory] = page
        let load = UUID()
        folderLoads[directory] = load
        var request: [String: Any] = ["op": "files", "path": directory, "limit": 100]
        var cancelled = false
        if let cursor { request["cursor"] = cursor }
        defer {
            if folderLoads[directory] == load {
                folderLoads[directory] = nil
                page.isLoading = false
                folders[directory] = cancelled ? previousPage : page
            }
        }
        do {
            let reply = try await FleetBridge.call(host, request)
            try Task.checkCancellation()
            guard folderLoads[directory] == load else { return }
            guard let entries = reply["entries"] as? [[String: Any]] else {
                throw WorkspaceFailure("The workspace returned an invalid folder page.")
            }
            var paths = Set<String>()
            let newEntries = entries.compactMap(ProjectEntry.init).filter {
                paths.insert($0.path).inserted
            }
            for entry in page.entries where entry.isDirectory && !paths.contains(entry.path) {
                if expanded.contains(entry.path) { collapseFolder(entry.path) }
            }
            page.entries = newEntries
            switch direction {
            case .first: page.previousCursors = []
            case .previous: page.previousCursors.removeLast()
            case .next: page.previousCursors.append(page.currentCursor)
            }
            page.currentCursor = cursor
            let next = reply["cursor"] as? String
            page.nextCursor = next == cursor ? nil : next
        } catch is CancellationError {
            cancelled = true
        } catch {
            page.error = error.localizedDescription
            page.retry = direction
        }
    }

    func toggleFolder(_ path: String) async {
        if expanded.contains(path) {
            collapseFolder(path)
            return
        }
        expanded.insert(path)
        if folders[path] == nil { await loadFolder(path) }
    }

    private func collapseFolder(_ path: String) {
        let prefix = path + "/"
        expanded = expanded.filter { $0 != path && !$0.hasPrefix(prefix) }
        folders = folders.filter { $0.key != path && !$0.key.hasPrefix(prefix) }
        folderLoads = folderLoads.filter { $0.key != path && !$0.key.hasPrefix(prefix) }
    }

    func openFile(_ path: String) async {
        let previous = selectedPath
        selectedPath = path
        guard !buffers.contains(where: { $0.path == path }), !loadingFiles.contains(path) else { return }
        if buffers.count + loadingFiles.count >= 24 {
            guard let clean = buffers.first(where: { $0.path != previous && !$0.isDirty && !$0.isSaving }) else {
                fileErrors[path] = "Save or close an open file before opening another."
                return
            }
            close(clean)
        }
        loadingFiles.insert(path)
        fileErrors[path] = nil
        defer { loadingFiles.remove(path) }
        do {
            let (text, revision) = try await readFile(path)
            buffers.append(SourceBuffer(path: path, text: text, revision: revision))
        } catch {
            fileErrors[path] = error.localizedDescription
        }
    }

    func openWelcomeFile() async {
        guard !checkedWelcome else { return }
        checkedWelcome = true
        guard selectedPath == nil else { return }
        let path = folders[""]?.entries.first {
            !$0.isDirectory && $0.name.lowercased() == "readme.md"
        }?.path ?? "README.md"
        do {
            let (text, revision) = try await readFile(path)
            try Task.checkCancellation()
            guard selectedPath == nil, !buffers.contains(where: { $0.path == path }) else { return }
            buffers.append(SourceBuffer(path: path, text: text, revision: revision))
            selectedPath = path
        } catch is CancellationError {
            checkedWelcome = false
        } catch {
            // A workspace without a readable README uses the project overview.
        }
    }

    private func readFile(_ path: String) async throws -> (String, String) {
        let reply = try await FleetBridge.call(host, ["op": "read_file", "path": path])
        guard let text = reply["text"] as? String, let revision = reply["revision"] as? String else {
            throw WorkspaceFailure("The workspace returned an invalid file.")
        }
        return (text, revision)
    }

    func save(_ buffer: SourceBuffer, replace: Bool = false) async {
        guard (buffer.isDirty || replace), !buffer.isSaving, buffer.diskRevision == nil || replace else { return }
        let text = buffer.text
        let revision = replace ? (buffer.diskRevision ?? buffer.revision) : buffer.revision
        notice = nil
        buffer.isSaving = true
        defer { buffer.isSaving = false }
        do {
            let reply = try await FleetBridge.call(host, [
                "op": "save_file", "path": buffer.path, "text": text, "revision": revision,
            ])
            guard let newRevision = reply["revision"] as? String else {
                throw WorkspaceFailure("The workspace did not confirm the save.")
            }
            if reply["conflict"] as? Bool == true {
                buffer.diskRevision = newRevision
                return
            }
            buffer.didSave(text, revision: newRevision)
            publishDirty()
        } catch {
            notice = error.localizedDescription
        }
    }

    func reload(_ buffer: SourceBuffer) async {
        guard !buffer.isSaving else { return }
        let previousText = buffer.text
        buffer.isSaving = true
        notice = nil
        defer { buffer.isSaving = false }
        do {
            let (text, revision) = try await readFile(buffer.path)
            guard buffer.text == previousText else {
                notice = "The buffer changed during reload. Your edits are intact."
                return
            }
            buffer.reload(text, revision: revision)
            publishDirty()
        } catch {
            notice = error.localizedDescription
        }
    }

    func close(_ buffer: SourceBuffer) {
        guard let index = buffers.firstIndex(where: { $0.path == buffer.path }) else { return }
        buffers.remove(at: index)
        if selectedPath == buffer.path {
            selectedPath = buffers.isEmpty ? nil : buffers[min(index, buffers.count - 1)].path
        }
        publishDirty()
    }

    func publishDirty(force: Bool = false) {
        let dirty = buffers.contains(where: \.isDirty)
        guard force || dirty != lastDirty else { return }
        lastDirty = dirty
        NotificationCenter.default.post(name: .init("FleetWorkspaceDirty"), object: dirty)
    }

    func toggleTag(_ path: String) {
        if drafts[selectedAgent, default: AgentDraft()].tags.remove(path) == nil {
            drafts[selectedAgent, default: AgentDraft()].tags.insert(path)
        }
    }

    func loadAgents() async {
        guard !agentsLoaded else { return }
        loadError = nil
        do {
            let reply = try await FleetBridge.call(host, ["op": "workspace_agents"])
            try Task.checkCancellation()
            guard let instances = reply["instances"] as? [[String: Any]] else {
                throw WorkspaceFailure("The workspace did not return its agents.")
            }
            for instance in instances {
                guard let harness = instance["agent"] as? String, ["codex", "cursor"].contains(harness),
                      let lane = instance["lane"] as? Int, (1...4).contains(lane) else { continue }
                let agent = WorkspaceAgent(harness: harness, lane: lane)
                if !agents.contains(agent) { agents.append(agent) }
                sessions[agent] = instance["session"] as? String
                guard let run = instance["run"] as? [String: Any], let id = run["id"] as? String else { continue }
                var turn = WorkspaceTurn(id: id, prompt: run["title"] as? String ?? "", reporting: run["view"] as? String ?? "actions")
                turn.status = run["status"] as? String ?? "queued"
                turn.action = run["action"] as? String ?? ""
                turn.risk = run["risk"] as? String ?? ""
                turn.outcome = run["outcome"] as? String ?? ""
                conversations[agent] = [turn]
                pendingDetails.insert(id)
            }
            if lastAgentSelection == nil, turns.isEmpty, (drafts[selectedAgent]?.text ?? "").isEmpty, tagPaths.isEmpty,
               let active = agents.first(where: { activeRun($0) != nil }) {
                selectedAgent = active
            }
            agentsLoaded = true
        } catch is CancellationError {
            return
        } catch {
            loadError = "Could not load workspace agents. \(error.localizedDescription)"
        }
    }

    func loadSettings() async {
        guard !loadedSettings else { return }
        settingsError = nil
        do {
            let reply = try await FleetBridge.call(host, ["op": "workspace_settings", "include_cursor_trust": true])
            try Task.checkCancellation()
            for agent in ["codex", "cursor"] {
                guard let preferences = reply[agent] as? [String: Any] else { continue }
                if !modelsChanged.contains(agent) { workspaceModels[agent] = preferences["model"] as? String }
            }
            if !cursorTrustChanged, let cursor = reply["cursor"] as? [String: Any] {
                cursorTrusted = cursor["trusted"] as? Bool ?? false
            }
            loadedSettings = true
        } catch is CancellationError {
            return
        } catch {
            settingsError = "Could not load agent settings. \(error.localizedDescription)"
        }
    }

    func setCursorTrust(_ trusted: Bool) async {
        guard !savingCursorTrust else { return }
        savingCursorTrust = true
        cursorTrustError = nil
        defer { savingCursorTrust = false }
        do {
            let reply = try await FleetBridge.call(host, ["op": "set_cursor_trust", "trusted": trusted])
            guard let saved = reply["trusted"] as? Bool, saved == trusted else {
                throw WorkspaceFailure("The host could not verify the workspace trust setting.")
            }
            cursorTrustChanged = true
            cursorTrusted = saved
        } catch {
            cursorTrustError = error.localizedDescription
        }
    }

    func setModel(_ id: String, agent: String) async {
        guard savingModels.insert(agent).inserted else { return }
        defer { savingModels.remove(agent) }
        modelErrors[agent] = nil
        do {
            _ = try await FleetBridge.call(host, ["op": "set_model", "agent": agent, "model": id])
            modelsChanged.insert(agent)
            workspaceModels[agent] = id.isEmpty ? nil : id
        } catch {
            modelErrors[agent] = "Could not save the model. \(error.localizedDescription)"
        }
    }

    func signInToCursor() async {
        guard !cursorSigningIn else { return }
        cursorSigningIn = true
        cursorLoginMessage = "Complete Cursor sign-in in your browser."
        defer { cursorSigningIn = false }
        do {
            let reply = try await FleetBridge.send(["op": "cursor_login", "host": host.dictionary])
            guard reply["verified"] as? Bool == true else {
                throw WorkspaceFailure("Cursor could not verify this login.")
            }
            cursorLoginMessage = "Signed in to Cursor on " + host.label + "."
            await loadModelOptions(for: "cursor", refresh: true)
        } catch {
            cursorLoginMessage = error.localizedDescription
        }
    }

    func loadModelOptions(for agent: String, refresh: Bool = false) async {
        var catalog = modelCatalogs[agent] ?? NativeModelCatalog()
        guard refresh || !catalog.loading else { return }
        if !refresh, let loadedAt = catalog.loadedAt, loadedAt.duration(to: .now) < .seconds(60) { return }
        let request = UUID()
        catalogLoads[agent] = request
        catalog.loading = true
        catalog.error = nil
        modelCatalogs[agent] = catalog
        do {
            let reply = try await FleetBridge.call(host, ["op": "models", "agent": agent])
            guard let listed = reply["models"] as? [[String: Any]] else {
                throw WorkspaceFailure("The host returned an unreadable model list.")
            }
            var seen = Set<String>()
            catalog.options = listed.compactMap { entry -> NativeModelOption? in
                guard let id = entry["id"] as? String, !id.isEmpty, seen.insert(id).inserted else { return nil }
                return NativeModelOption(id: id, label: entry["label"] as? String ?? id)
            }
            catalog.error = catalog.options.isEmpty ? "The CLI returned no models." : nil
            catalog.loadedAt = catalog.options.isEmpty ? nil : .now
        } catch {
            catalog.error = "Could not load models. \(error.localizedDescription)"
            catalog.loadedAt = nil
        }
        guard catalogLoads[agent] == request else { return }
        catalogLoads[agent] = nil
        catalog.loading = false
        modelCatalogs[agent] = catalog
    }

    func newConversation(_ agent: WorkspaceAgent, commandInput: String? = nil) async {
        guard canReset(agent), resetting.insert(agent).inserted else {
            if commandInput != nil { finishCommand(for: agent, error: "Wait for this agent’s current operation to finish.") }
            return
        }
        defer { resetting.remove(agent) }
        composerErrors[agent] = nil
        do {
            _ = try await FleetBridge.call(host, ["op": "reset_session", "agent": agent.harness, "lane": agent.lane])
            setSession(nil, for: agent)
            if let commandInput, input(for: agent) == commandInput { finishCommand(for: agent) }
        } catch {
            composerErrors[agent] = error.localizedDescription
        }
    }

    func listSessions(for agent: WorkspaceAgent) async throws -> [NativeSessionOption] {
        let reply = try await FleetBridge.call(host, ["op": "list_sessions", "agent": agent.harness, "lane": agent.lane])
        try Task.checkCancellation()
        guard let rows = reply["sessions"] as? [[String: Any]] else {
            throw WorkspaceFailure("The workspace did not return its sessions.")
        }
        var seen = Set<String>()
        return rows.compactMap { row in
            guard let id = row["id"] as? String, !id.isEmpty, seen.insert(id).inserted else { return nil }
            return NativeSessionOption(id: id, title: row["title"] as? String ?? "")
        }
    }

    func resumeSession(_ id: String, for agent: WorkspaceAgent) async throws {
        guard canReset(agent), resetting.insert(agent).inserted else {
            throw WorkspaceFailure("Wait for this agent’s current operation to finish.")
        }
        defer { resetting.remove(agent) }
        guard sessions[agent] != id else { return }
        let reply = try await FleetBridge.call(host, [
            "op": "resume_session", "agent": agent.harness, "lane": agent.lane, "session": id,
        ])
        setSession(reply["session"] as? String ?? id, for: agent)
    }

    private func setSession(_ id: String?, for agent: WorkspaceAgent) {
        sessions[agent] = id
        if let run = conversations[agent]?.last?.id { pendingDetails.remove(run) }
        conversations[agent] = []
        streamErrors[agent] = nil
        composerErrors[agent] = nil
    }

    func submit(from agent: WorkspaceAgent, approvalCards: Bool) async {
        guard canSend(from: agent), let draft = drafts[agent] else { return }
        let prompt = draft.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ComposerCommand.parse(prompt) == nil else { return }
        let request: [String: Any] = [
            "op": "submit", "agent": agent.harness, "lane": agent.lane, "prompt": prompt,
            "tags": draft.tags.sorted(), "view": draft.reporting, "filter": draft.filterProgress,
            "approval_cards": agent.harness == "codex" && approvalCards,
        ]
        submitting.insert(agent)
        composerErrors[agent] = nil
        streamErrors[agent] = nil
        defer { submitting.remove(agent) }
        do {
            let reply = try await FleetBridge.call(host, request)
            guard let id = reply["id"] as? String else {
                throw WorkspaceFailure("The workspace did not return the new run.")
            }
            if let previousID = conversations[agent]?.last?.id { pendingDetails.remove(previousID) }
            conversations[agent, default: []].append(WorkspaceTurn(id: id, prompt: prompt, reporting: draft.reporting))
            let count = conversations[agent]?.count ?? 0
            if count > 24 { conversations[agent]?.removeFirst(count - 24) }
            if drafts[agent]?.text == draft.text { drafts[agent]?.text = "" }
        } catch {
            composerErrors[agent] = error.localizedDescription
        }
    }

    func stop(_ id: String, agent: WorkspaceAgent) async {
        guard activeRun(agent) == id, stopping.insert(id).inserted else { return }
        composerErrors[agent] = nil
        do {
            _ = try await FleetBridge.call(host, ["op": "cancel", "id": id])
        } catch {
            stopping.remove(id)
            if activeRun(agent) == id { composerErrors[agent] = error.localizedDescription }
        }
    }

    func answerApproval(_ approval: AgentApproval, run: String, agent: WorkspaceAgent, accept: Bool) async {
        guard activeRun(agent) == run, answeringApprovals.insert(approval.id).inserted else { return }
        composerErrors[agent] = nil
        defer { answeringApprovals.remove(approval.id) }
        do {
            _ = try await FleetBridge.call(host, [
                "op": "decide_approval", "id": run, "approval": approval.id,
                "decision": accept ? "accept" : "decline",
            ])
            if let index = conversations[agent]?.firstIndex(where: { $0.id == run }),
               let card = conversations[agent]?[index].approvals.firstIndex(where: { $0.id == approval.id }) {
                conversations[agent]?[index].approvals[card] = AgentApproval(
                    id: approval.id, title: approval.title, detail: approval.detail, decision: accept ? "accept" : "decline"
                )
            }
        } catch {
            composerErrors[agent] = error.localizedDescription
        }
    }

    func monitor(_ id: String?, agent: WorkspaceAgent) async {
        guard let id else { return }
        var revision = ""
        while !Task.isCancelled, observedRun(agent) == id {
            do {
                let reply = try await FleetBridge.call(host, ["op": "detail", "id": id, "revision": revision])
                try Task.checkCancellation()
                guard observedRun(agent) == id else { return }
                if streamErrors[agent] != nil { streamErrors[agent] = nil }
                if reply["unchanged"] as? Bool != true {
                    guard let run = reply["run"] as? [String: Any],
                          let status = run["status"] as? String,
                          let index = conversations[agent]?.firstIndex(where: { $0.id == id }),
                          var turn = conversations[agent]?[index] else {
                        throw WorkspaceFailure("This run is not available yet.")
                    }
                    turn.status = status
                    turn.action = run["action"] as? String ?? ""
                    turn.risk = run["risk"] as? String ?? ""
                    turn.outcome = run["outcome"] as? String ?? ""
                    turn.approvals = (reply["approvals"] as? [[String: Any]] ?? []).compactMap { row in
                        guard let id = row["id"] as? String, let title = row["title"] as? String,
                              let detail = row["detail"] as? String else { return nil }
                        return AgentApproval(id: id, title: title, detail: detail, decision: row["decision"] as? String)
                    }
                    let visibleKinds: Set<String>? = switch turn.reporting {
                    case "summary": ["result", "error", "risk"]
                    case "risks": ["error", "risk"]
                    default: nil
                    }
                    let events = reply["events"] as? [[String: Any]] ?? []
                    if pendingDetails.contains(id), let prompt = events.first(where: { ($0["kind"] as? String) == "prompt" })?["text"] as? String {
                        turn.prompt = prompt
                    }
                    turn.events = events.enumerated().compactMap { index, value in
                        guard let kind = value["kind"] as? String, let text = value["text"] as? String,
                              !["prompt", "status", "session"].contains(kind),
                              visibleKinds?.contains(kind) ?? true else { return nil }
                        return AgentEvent(id: index, kind: kind, text: text)
                    }
                    if let session = run["session"] as? String, !session.isEmpty, sessions[agent] != session {
                        sessions[agent] = session
                    }
                    if pendingDetails.contains(id) { pendingDetails.remove(id) }
                    conversations[agent]?[index] = turn
                    if !turn.isActive {
                        stopping.remove(id)
                        return
                    }
                    revision = reply["revision"] as? String ?? ""
                }
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled, observedRun(agent) == id else { return }
                streamErrors[agent] = error.localizedDescription
            }
            do {
                try await Task.sleep(for: .seconds(streamErrors[agent] != nil ? 4 : selectedAgent == agent ? 1 : 3))
            } catch { return }
        }
    }
}

@MainActor
private struct WorkspacePane: View {
    let host: FleetHost
    var monitorRuns: Bool
    @ObservedObject var model: WorkspaceModel
    let selectedAgentLane: FleetAgentLaneSelection?
    let onAgentLanesChange: ([FleetAgentLane]) -> Void
    let onActiveAgentLaneChange: (FleetAgentLane) -> Void
    @State private var closing: SourceBuffer?
    @State private var panel: WorkspacePanel?
    @State private var showingCommands: WorkspaceAgent?
    @State private var showingStatus: WorkspaceAgent?
    @State private var confirmingCursorTrust = false
    @State private var terminalError: String?
    @AppStorage("FleetExplorerVisible") private var explorerVisible = true
    @AppStorage("FleetCodexApprovalCards") private var codexApprovalCards = true
    private let accent = Color(nsColor: .systemTeal)

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HSplitView {
                if explorerVisible { browser.frame(minWidth: 145, idealWidth: 188) }
                editor.frame(minWidth: 280, maxWidth: .infinity, maxHeight: .infinity)
                composer.frame(minWidth: 255, idealWidth: 316)
            }
        }
        .task {
            if model.folders[""] == nil { await model.loadFolder("") }
            await model.openWelcomeFile()
        }
        .task { await model.loadSettings() }
        .task { await model.loadAgents() }
        .onChange(of: selectedAgentLane, initial: true) { _, selection in model.selectAgent(selection) }
        .onChange(of: model.agents, initial: true) { _, agents in onAgentLanesChange(agents.map(\.sidebarLane)) }
        .onChange(of: model.selectedAgent, initial: true) { _, agent in onActiveAgentLaneChange(agent.sidebarLane) }
        .onAppear { model.publishDirty(force: true) }
        .onDisappear { NotificationCenter.default.post(name: .init("FleetWorkspaceDirty"), object: false) }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetQuickOpen"))) { _ in
            panel = .quickOpen
        }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetToggleExplorer"))) { _ in
            explorerVisible.toggle()
        }
        .onReceive(NotificationCenter.default.publisher(for: .init("FleetCloseTab"))) { _ in
            if let buffer = model.selectedBuffer { requestClose(buffer) }
        }
        .sheet(item: $panel) { panel in
            switch panel {
            case .quickOpen:
                QuickOpenPanel(host: host, openFiles: model.buffers.map(\.path).reversed()) { path in
                    self.panel = nil
                    Task { await model.openFile(path) }
                }
            case let .resume(agent, session):
                ResumeSessionPanel(model: model, agent: agent, session: session)
            case let .command(agent):
                AgentCommandPanel(host: host, agent: agent) {
                    Task { await model.loadModelOptions(for: agent.harness, refresh: true) }
                }
            }
        }
        .background {
            ZStack {
                ForEach(model.agents) { agent in
                    let run = monitorRuns ? model.observedRun(agent) : nil
                    Color.clear.task(id: run) { await model.monitor(run, agent: agent) }
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
        .alert("Discard unsaved changes?", isPresented: Binding(
            get: { closing != nil }, set: { if !$0 { closing = nil } }
        ), presenting: closing) { buffer in
            Button("Discard Edits", role: .destructive) {
                model.close(buffer)
                closing = nil
            }
            Button("Keep Editing", role: .cancel) { closing = nil }
        } message: { buffer in
            Text("The changes to \(buffer.name) will be lost.")
        }
        .alert("Trust This Workspace for Cursor?", isPresented: $confirmingCursorTrust) {
            Button("Cancel", role: .cancel) {}
            Button("Trust Workspace") { Task { await model.setCursorTrust(true) } }
        } message: {
            Text("Cursor will use its workspace trust option for \(host.repo) on \(host.label). Only trust workspaces you recognize. You can revoke trust from Agent options.")
        }
        .alert("Could Not Open Terminal", isPresented: Binding(
            get: { terminalError != nil }, set: { if !$0 { terminalError = nil } }
        )) {
            Button("OK", role: .cancel) { terminalError = nil }
        } message: {
            Text(terminalError ?? "")
        }
    }

    private func openTerminal(agent: WorkspaceAgent? = nil) {
        Task {
            do {
                try await FleetBridge.openTerminal(host, agent: agent?.harness,
                                                   session: agent.flatMap { model.sessions[$0] })
            } catch {
                terminalError = error.localizedDescription
            }
        }
    }

    private func requestClose(_ buffer: SourceBuffer) {
        guard !buffer.isSaving else { return }
        if buffer.isDirty { closing = buffer } else { model.close(buffer) }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 20, weight: .medium))
                .foregroundStyle(accent)
                .frame(width: 34, height: 34)
                .background(accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 9))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(host.label).font(.headline)
                Text(host.repo)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            Button { explorerVisible.toggle() } label: {
                Image(systemName: explorerVisible ? "sidebar.left" : "sidebar.leading")
            }
            .help(explorerVisible ? "Hide Explorer (⌘B)" : "Show Explorer (⌘B)")
            .accessibilityLabel(explorerVisible ? "Hide Explorer" : "Show Explorer")
            Button { panel = .quickOpen } label: {
                Label("Quick Open", systemImage: "magnifyingglass")
            }
            .help("Find a file by name or path (⌘P)")
            Menu {
                Button("Codex CLI") { model.addAgent("codex") }
                    .disabled(model.availableAgent("codex") == nil)
                Button("Cursor Agent") { model.addAgent("cursor") }
                    .disabled(model.availableAgent("cursor") == nil)
            } label: {
                Label("New Agent", systemImage: "plus").font(.callout.weight(.semibold))
            }
            .menuStyle(.borderedButton)
            .menuOrder(.fixed)
            .fixedSize()
            .help("Create an agent in \(host.label)")
            .accessibilityLabel("New Agent")
            .disabled(!model.agentsLoaded || (model.availableAgent("codex") == nil && model.availableAgent("cursor") == nil))
        }
        .padding(.horizontal, 15)
        .padding(.vertical, 10)
    }

    private var browser: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Explorer").font(.subheadline.weight(.semibold))
                Spacer()
                Button {
                    Task { await model.loadFolder("") }
                } label: {
                    Image(systemName: "arrow.clockwise").frame(width: 20, height: 20)
                }
                .buttonStyle(.plain)
                .help("Refresh files")
                .accessibilityLabel("Refresh files")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    FileBranch(model: model, directory: "", depth: 0)
                }
                .padding(.vertical, 6)
            }
        }
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.7))
    }

    private var editor: some View {
        VStack(spacing: 0) {
            if !model.buffers.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 4) {
                        ForEach(model.buffers) { buffer in
                            FileTab(buffer: buffer, selected: model.selectedPath == buffer.path) {
                                model.selectedPath = buffer.path
                            } close: {
                                requestClose(buffer)
                            }
                        }
                    }
                    .padding(6)
                }
                Divider()
            }
            if let path = model.selectedPath { breadcrumbs(path) }
            if let buffer = model.selectedBuffer {
                SourceEditor(model: model, buffer: buffer)
                    .id(buffer.path)
            } else if let path = model.selectedPath, model.loadingFiles.contains(path) {
                ProgressView("Loading file…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let path = model.selectedPath, let error = model.fileErrors[path] {
                ContentUnavailableView {
                    Label("File unavailable", systemImage: "doc.badge.ellipsis")
                } description: {
                    Text(error)
                } actions: {
                    Button("Retry") { Task { await model.openFile(path) } }
                }
            } else {
                projectOverview
            }
            if let notice = model.notice {
                Divider()
                HStack(alignment: .firstTextBaseline) {
                    Text(notice).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    Spacer()
                    Button { model.notice = nil } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Dismiss")
                }
                .padding(8)
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
    }

    private func breadcrumbs(_ path: String) -> some View {
        let segments = path.split(separator: "/").map(String.init)
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                Image(systemName: "folder").accessibilityHidden(true)
                ForEach(segments.indices, id: \.self) { index in
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .accessibilityHidden(true)
                    Text(segments[index])
                        .foregroundStyle(index == segments.count - 1 ? Color.primary : .secondary)
                        .lineLimit(1)
                }
            }
            .font(.system(.caption, design: .monospaced))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
        }
        .help(path)
        .accessibilityLabel(path)
    }

    private var projectOverview: some View {
        let entries = (model.folders[""]?.entries ?? []).filter { !$0.isDirectory }.prefix(7)
        return ScrollView {
            VStack(alignment: .leading, spacing: 13) {
                Image(systemName: "folder.badge.gearshape")
                    .font(.system(size: 36, weight: .light))
                    .foregroundStyle(accent)
                Text((host.repo as NSString).lastPathComponent.isEmpty ? host.label : (host.repo as NSString).lastPathComponent)
                    .font(.system(.largeTitle, design: .rounded).weight(.semibold))
                Text("Open a file from the project tree or send a prompt from the agent panel.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                if !entries.isEmpty {
                    Divider().padding(.vertical, 3)
                    ForEach(entries) { entry in
                        Button {
                            Task { await model.openFile(entry.path) }
                        } label: {
                            Label(entry.name, systemImage: "doc")
                                .font(.system(.callout, design: .monospaced))
                        }
                        .buttonStyle(.plain)
                    }
                } else if model.folders[""]?.isLoading == true {
                    ProgressView("Loading project…").controlSize(.small)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var composer: some View {
        let agent = model.selectedAgent
        return VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(agent.label).font(.subheadline.weight(.semibold))
                let running = model.agents.filter { model.activeRun($0) != nil }.count
                if running > 0 { Text("\(running) running").font(.caption).foregroundStyle(.secondary) }
                Spacer()
                if let id = model.activeRun(agent) {
                    Button {
                        Task { await model.stop(id, agent: agent) }
                    } label: {
                        Label(model.stopping.contains(id) ? "Stopping…" : "Stop", systemImage: "stop.fill")
                    }
                    .controlSize(.small)
                    .disabled(model.stopping.contains(id))
                    .help("Stop this task")
                }
                Menu {
                    Button("Edit Agent Command…") { panel = .command(agent) }
                    Button("Open Agent in Terminal") { openTerminal(agent: agent) }
                        .disabled(!model.canReset)
                    Button("Open Workspace Shell") { openTerminal() }
                    Divider()
                    if agent.harness == "codex" {
                        Toggle("Approval Cards", isOn: $codexApprovalCards)
                    }
                    if agent.harness == "cursor" {
                        Button("Sign In to Cursor…") { Task { await model.signInToCursor() } }
                            .disabled(model.cursorSigningIn)
                        if model.cursorTrusted {
                            Button("Revoke Cursor Workspace Trust") { Task { await model.setCursorTrust(false) } }
                                .disabled(model.savingCursorTrust)
                        } else {
                            Button("Trust Workspace for Cursor…") { confirmingCursorTrust = true }
                                .disabled(model.savingCursorTrust)
                        }
                    }
                    Toggle("Filter routine progress", isOn: model.draftBinding(for: agent, \.filterProgress))
                    Button("Resume Session…") { panel = .resume(agent, "") }
                        .disabled(!model.canReset)
                    Button("New Conversation") { Task { await model.newConversation(agent) } }
                        .disabled(!model.canReset)
                        .help(model.sessions[agent] == nil ? "New conversation" : "Start a fresh CLI session")
                    Button("Composer Commands") { showingCommands = agent }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .help("Agent options")
                .accessibilityLabel("Agent options")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if model.turns.isEmpty {
                        Text("Send a prompt or attach files from the tree.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(model.turns) { turn in
                        let offerTrust = agent.harness == "cursor" && !model.cursorTrusted && turn.requiresCursorTrust
                        TurnCard(turn: turn, trustWorkspace: offerTrust ? { confirmingCursorTrust = true } : nil,
                                 openTerminal: turn.status == "failed" && model.canReset ? { openTerminal(agent: agent) } : nil,
                                 answering: model.answeringApprovals) { approval, accept in
                            Task { await model.answerApproval(approval, run: turn.id, agent: agent, accept: accept) }
                        }
                    }
                }
                .padding(12)
            }
            .defaultScrollAnchor(.bottom)
            .id(agent)
            Divider()
            VStack(alignment: .leading, spacing: 10) {
                if !model.tagPaths.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 5) {
                            ForEach(model.tagPaths.sorted(), id: \.self) { path in
                                HStack(spacing: 5) {
                                    Button {
                                        Task { await model.openFile(path) }
                                    } label: {
                                        Text((path as NSString).lastPathComponent)
                                            .font(.caption)
                                            .lineLimit(1)
                                    }
                                    .buttonStyle(.plain)
                                    .help(path)
                                    Button {
                                        model.toggleTag(path)
                                    } label: {
                                        Image(systemName: "xmark").font(.caption2.weight(.semibold))
                                    }
                                    .buttonStyle(.plain)
                                    .help("Detach \(path)")
                                    .accessibilityLabel("Detach \(path)")
                                }
                                .padding(.horizontal, 7)
                                .padding(.vertical, 4)
                                .background(accent.opacity(0.12), in: Capsule())
                            }
                        }
                    }
                }
                TextEditor(text: model.draftBinding(for: agent, \.text))
                    .id(agent)
                    .font(.system(.body))
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 64, idealHeight: 82, maxHeight: 100)
                    .padding(5)
                    .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 7))
                    .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(.quaternary))
                    .overlay(alignment: .topLeading) {
                        if model.input(for: agent).isEmpty {
                            Text("Type a prompt or / for commands")
                                .foregroundStyle(.tertiary)
                                .padding(.leading, 11).padding(.top, 6)
                                .allowsHitTesting(false).accessibilityHidden(true)
                        }
                    }
                    .accessibilityLabel("Agent prompt")
                commandChoices(for: agent)
                composerControls(agent)
                if showingStatus == agent, model.input(for: agent).isEmpty {
                    Text(model.status(for: agent)).font(.caption).textSelection(.enabled)
                }
                if let error = model.modelErrors[agent.harness] ?? model.modelCatalogs[agent.harness]?.error {
                    if agent.harness == "cursor", error.localizedCaseInsensitiveContains("sign in") || error.localizedCaseInsensitiveContains("authentication") {
                        Button("Sign In to Cursor…") { Task { await model.signInToCursor() } }
                            .disabled(model.cursorSigningIn)
                            .help("Use the browser on this Mac to sign in on " + host.label)
                    } else {
                        Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    }
                }
                if agent.harness == "cursor", let message = model.cursorLoginMessage {
                    HStack(spacing: 5) {
                        if model.cursorSigningIn { ProgressView().controlSize(.small) }
                        Text(message).font(.caption).textSelection(.enabled)
                    }
                }
                if let error = model.loadError {
                    Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    Button("Retry Loading Agents") { Task { await model.loadAgents() } }
                }
                if let error = model.composerError {
                    Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                }
                if agent.harness == "cursor", let error = model.cursorTrustError {
                    Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                }
            }
            .padding(11)
        }
        .background(Color(nsColor: .controlBackgroundColor))
        .task(id: agent.harness, priority: .utility) { await model.loadModelOptions(for: agent.harness) }
    }

    private func composerControls(_ agent: WorkspaceAgent) -> some View {
        let isCommand = ComposerCommand.parse(model.input(for: agent)) != nil
        return HStack(alignment: .bottom, spacing: 7) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) {
                    providerControls
                    AgentModelMenu(model: model, harness: agent.harness)
                    viewControl(agent)
                }.fixedSize(horizontal: true, vertical: false)

                VStack(alignment: .leading, spacing: 6) {
                    providerControls
                    HStack(alignment: .top, spacing: 6) {
                        AgentModelMenu(model: model, harness: agent.harness)
                        viewControl(agent)
                    }
                }
            }
            Spacer(minLength: 0)
            Button {
                send(from: agent)
            } label: {
                Label("Send", systemImage: "arrow.up").labelStyle(.iconOnly)
            }
            .buttonStyle(.borderedProminent)
            .tint(accent)
            .fixedSize()
            .disabled(!model.canSend && !isCommand)
            .keyboardShortcut(.return, modifiers: .command)
            .help(isCommand ? "Run command (⌘↩)" : "Send prompt (⌘↩)")
            .accessibilityLabel(isCommand ? "Run command" : "Send prompt")
        }
    }

    @ViewBuilder private func commandChoices(for agent: WorkspaceAgent) -> some View {
        let input = model.input(for: agent)
        let commands = showingCommands == agent && input.isEmpty
            ? ComposerCommand.allCases : ComposerCommand.suggestions(input)
        if !commands.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(commands) { command in
                    Button {
                        model.draftBinding(for: agent, \.text).wrappedValue = command.token
                        send(from: agent)
                    } label: {
                        HStack(alignment: .top, spacing: 6) {
                            Text(command.usage).font(.system(.caption, design: .monospaced).weight(.medium))
                            Text(command.description).font(.caption).foregroundStyle(.secondary)
                            Spacer(minLength: 0)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(7)
            .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 6))
        }
    }

    private func send(from agent: WorkspaceAgent) {
        let input = model.input(for: agent)
        showingCommands = nil
        showingStatus = nil
        guard let invocation = ComposerCommand.parse(input) else {
            Task { await model.submit(from: agent, approvalCards: codexApprovalCards) }
            return
        }
        if invocation.command != .resume, !invocation.argument.isEmpty {
            model.finishCommand(for: agent, error: "Use \(invocation.command.usage) without arguments.")
            return
        }
        switch invocation.command {
        case .resume:
            guard model.canReset else {
                model.finishCommand(for: agent, error: "Wait for this agent’s current operation to finish.")
                return
            }
            model.finishCommand(for: agent)
            panel = .resume(agent, invocation.argument)
        case .new:
            Task { await model.newConversation(agent, commandInput: input) }
        case .status:
            model.finishCommand(for: agent)
            showingStatus = agent
        case .command:
            model.finishCommand(for: agent)
            panel = .command(agent)
        case .native:
            guard model.canReset else {
                model.finishCommand(for: agent, error: "Wait for this agent’s current operation to finish.")
                return
            }
            model.finishCommand(for: agent)
            openTerminal(agent: agent)
        case .shell:
            model.finishCommand(for: agent)
            openTerminal()
        case .help:
            model.finishCommand(for: agent)
            showingCommands = agent
        }
    }

    private var providerControls: some View {
        HStack(spacing: 5) {
            Text("Provider").font(.caption).foregroundStyle(.secondary)
            Picker("Provider", selection: $model.selectedAgent) {
                ForEach(model.agents) { instance in
                    Text(instance.label).tag(instance)
                }
            }
            .labelsHidden()
            .accessibilityLabel("Provider and agent instance")
        }
    }

    private func viewControl(_ agent: WorkspaceAgent) -> some View {
        HStack(spacing: 4) {
            Text("View").font(.caption).foregroundStyle(.secondary)
            Picker("View", selection: model.draftBinding(for: agent, \.reporting)) {
                Text("Actions").tag("actions")
                Text("Summary").tag("summary")
                Text("Risks").tag("risks")
            }
            .labelsHidden()
        }.fixedSize(horizontal: true, vertical: false)
    }

}

private enum WorkspacePanel: Identifiable {
    case quickOpen
    case resume(WorkspaceAgent, String)
    case command(WorkspaceAgent)

    var id: String {
        switch self {
        case .quickOpen: "open"
        case let .resume(agent, _): "resume-\(agent.harness)-\(agent.lane)"
        case let .command(agent): "command-\(agent.harness)"
        }
    }
}

@MainActor
private struct AgentCommandPanel: View {
    let host: FleetHost
    let agent: WorkspaceAgent
    let didSave: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var command = ""
    @State private var ready = false
    @State private var busy = true
    @State private var result: String?

    private var name: String { agent.harness == "codex" ? "Codex" : "Cursor" }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("\(name) Command").font(.headline)
            Text("Used by every \(name) agent and schedule in \(host.label). Leave blank for automatic discovery.")
                .foregroundStyle(.secondary)
            TextField("Command", text: $command, prompt: Text(agent.harness == "codex" ? "codex" : "cursor-agent or agent"))
                .font(.system(.body, design: .monospaced))
                .disabled(!ready || busy)
                .onChange(of: command) { _, _ in result = nil }
            Text("Enter an executable name or path, with optional arguments. Quote paths that contain spaces.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if let result { Text(result).font(.caption).textSelection(.enabled) }
            HStack {
                if busy { ProgressView().controlSize(.small) }
                if ready {
                    Button("Use Automatic") { command = "" }.disabled(busy || command.isEmpty)
                    Button("Test Command") { Task { await update("test_cli_command") } }.disabled(busy)
                }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") { Task { await update("set_cli_command") } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!ready || busy)
            }
        }
        .padding(18)
        .frame(width: 490)
        .task {
            defer { busy = false }
            do {
                let reply = try await FleetBridge.call(host, ["op": "cli_command", "agent": agent.harness])
                command = reply["command"] as? String ?? ""
                ready = true
            } catch {
                result = error.localizedDescription
            }
        }
    }

    private func update(_ operation: String) async {
        guard !busy else { return }
        busy = true
        result = nil
        defer { busy = false }
        do {
            let reply = try await FleetBridge.call(host, ["op": operation, "agent": agent.harness, "command": command])
            if operation == "set_cli_command" {
                didSave()
                dismiss()
            } else if let executable = reply["executable"] as? String {
                result = "Responded to --version at \(executable)"
            }
        } catch {
            result = error.localizedDescription
        }
    }
}

@MainActor
private struct ResumeSessionPanel: View {
    @ObservedObject var model: WorkspaceModel
    let agent: WorkspaceAgent
    @Environment(\.dismiss) private var dismiss
    @State private var sessionID: String
    @State private var options: [NativeSessionOption] = []
    @State private var loading = true
    @State private var resuming = false
    @State private var listError: String?
    @State private var resumeError: String?

    init(model: WorkspaceModel, agent: WorkspaceAgent, session: String) {
        self.model = model
        self.agent = agent
        self._sessionID = State(initialValue: session)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Resume Session in \(agent.label)").font(.headline)
            TextField("Session ID", text: $sessionID, prompt: Text("Enter a session ID or choose one below"))
                .font(.system(.body, design: .monospaced))
                .textFieldStyle(.roundedBorder)
                .disabled(resuming)
            if loading {
                ProgressView("Loading sessions…").controlSize(.small)
            } else if let listError {
                Text(listError).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                Button("Retry") { Task { await load() } }
                    .disabled(resuming)
            } else if options.isEmpty {
                Text("No sessions found.").font(.caption).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(options) { option in
                            Button {
                                sessionID = option.id
                                resumeError = nil
                            } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: sessionID == option.id ? "largecircle.fill.circle" : "circle")
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(option.title.isEmpty ? option.id : option.title).lineLimit(1)
                                        if !option.title.isEmpty, option.title != option.id {
                                            Text(option.id).font(.system(.caption2, design: .monospaced)).foregroundStyle(.secondary)
                                        }
                                    }
                                    Spacer(minLength: 0)
                                    if model.sessions[agent] == option.id {
                                        Text("Current").font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                                .padding(5)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityAddTraits(sessionID == option.id ? .isSelected : [])
                        }
                    }
                }
                .frame(maxHeight: 230)
                .disabled(resuming)
            }
            if let resumeError {
                Text(resumeError).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(resuming)
                Button(resuming ? "Resuming…" : "Resume") { Task { await resume() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(resuming || sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(18)
        .frame(width: 460)
        .interactiveDismissDisabled(resuming)
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        listError = nil
        do {
            options = try await model.listSessions(for: agent)
        } catch is CancellationError {
            return
        } catch {
            listError = error.localizedDescription
        }
    }

    private func resume() async {
        let id = sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty, !resuming else { return }
        resuming = true
        defer { resuming = false }
        resumeError = nil
        do {
            try await model.resumeSession(id, for: agent)
            dismiss()
        } catch {
            resumeError = error.localizedDescription
        }
    }
}

private struct NativeModelOption: Identifiable {
    let id: String
    let label: String
}

private struct NativeModelCatalog {
    var options: [NativeModelOption] = []
    var loading = false
    var error: String?
    var loadedAt: ContinuousClock.Instant?
}

@MainActor
private struct AgentModelMenu: View {
    @ObservedObject var model: WorkspaceModel
    let harness: String

    private var current: String? { model.workspaceModels[harness] }
    private var saving: Bool { model.savingModels.contains(harness) }

    var body: some View {
        let catalog = model.modelCatalogs[harness] ?? NativeModelCatalog(loading: true)
        Menu {
            choice("CLI default", id: "")
            if let current, !catalog.options.contains(where: { $0.id == current }) {
                choice(current, id: current)
            }
            Divider()
            ForEach(catalog.options) { option in
                choice(option.label == option.id ? option.id : "\(option.label) · \(option.id)", id: option.id)
            }
            if !catalog.options.isEmpty { Divider() }
            if catalog.loading {
                Button("Loading models…") {}.disabled(true)
            } else {
                Button(catalog.error == nil ? "Refresh Models" : "Retry Loading Models") {
                    Task { await model.loadModelOptions(for: harness, refresh: true) }
                }
            }
        } label: {
            HStack(spacing: 6) {
                Label("Model", systemImage: "cpu")
                Text(saving ? "Saving…" : (current ?? "CLI default"))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: 110, alignment: .leading)
            }
        }
        .menuOrder(.fixed)
        .menuStyle(.borderedButton)
        .disabled(saving)
        .accessibilityLabel("Model for \(harness == "codex" ? "Codex" : "Cursor")")
        .accessibilityValue(current ?? "CLI default")
    }

    private func choice(_ title: String, id: String) -> some View {
        Button {
            guard id != (current ?? "") else { return }
            Task { await model.setModel(id, agent: harness) }
        } label: {
            if id == (current ?? "") {
                Label(title, systemImage: "checkmark")
            } else {
                Text(title)
            }
        }
    }
}

@MainActor
private final class QuickOpenSearch: ObservableObject {
    struct Response {
        let query: String
        let files: [String]
        let hasMore: Bool
        let error: String?
    }

    @Published private(set) var response: Response?
    private let host: FleetHost
    private var requested = ""
    private var worker: Task<Void, Never>?

    init(host: FleetHost) { self.host = host }

    func schedule(_ query: String) {
        requested = query
        guard !query.isEmpty, worker == nil else { return }
        worker = Task { await process() }
    }

    func cancel() {
        requested = ""
        worker?.cancel()
    }

    private func process() async {
        defer { worker = nil }
        while !requested.isEmpty && requested != response?.query && !Task.isCancelled {
            let query = requested
            do {
                try await Task.sleep(for: .milliseconds(180))
                guard requested == query else { continue }
                let reply = try await FleetBridge.call(host, ["op": "find_files", "query": query, "limit": 60])
                try Task.checkCancellation()
                guard requested == query else { continue }
                guard let files = reply["files"] as? [String] else {
                    throw WorkspaceFailure("The workspace returned an invalid file search.")
                }
                var seen = Set<String>()
                var matches: [String] = []
                for file in files where seen.insert(file).inserted {
                    matches.append(file)
                    if matches.count == 60 { break }
                }
                response = Response(query: query, files: matches,
                                    hasMore: reply["more"] as? Bool == true || files.count > 60, error: nil)
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled, requested == query else { continue }
                response = Response(query: query, files: [], hasMore: false, error: error.localizedDescription)
            }
        }
    }
}

@MainActor
private struct QuickOpenPanel: View {
    let openFiles: [String]
    let open: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @StateObject private var search: QuickOpenSearch
    @State private var query = ""
    @State private var selected: String?

    init(host: FleetHost, openFiles: [String], open: @escaping (String) -> Void) {
        self.openFiles = openFiles
        self.open = open
        _search = StateObject(wrappedValue: QuickOpenSearch(host: host))
    }

    private var searchQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var response: QuickOpenSearch.Response? {
        guard !searchQuery.isEmpty, search.response?.query == searchQuery else { return nil }
        return search.response
    }
    private var searching: Bool { !searchQuery.isEmpty && response == nil }
    private var files: [String] {
        if searchQuery.isEmpty { return openFiles }
        if let response { return response.files }
        return openFiles.filter { $0.localizedCaseInsensitiveContains(searchQuery) }
    }
    private var focusedFile: String? {
        if let selected, files.contains(selected) { return selected }
        return files.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                QuickOpenInput(query: $query, move: move, open: {
                    if let focusedFile { open(focusedFile) }
                }, cancel: { dismiss() })
                .frame(height: 23)
                if searching { ProgressView().controlSize(.small) }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 3) {
                        if searchQuery.isEmpty && !files.isEmpty {
                            Text("Open files").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                                .padding(.horizontal, 8).padding(.vertical, 3)
                        }
                        ForEach(files, id: \.self) { path in
                            Button { open(path) } label: { row(path) }
                                .buttonStyle(.plain)
                                .id(path)
                        }
                        if files.isEmpty {
                            Text(message).font(.callout).foregroundStyle(response?.error != nil ? .orange : .secondary)
                                .padding(8)
                        }
                    }
                }
                .onChange(of: focusedFile) { _, path in
                    if let path { proxy.scrollTo(path) }
                }
            }
            Divider()
            HStack {
                Text(response?.hasMore == true ? "Keep typing to narrow results" : "↑ ↓ Select   ↩ Open")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(16)
        .frame(width: 540, height: 365)
        .onChange(of: searchQuery) { _, value in
            selected = nil
            search.schedule(value)
        }
        .onDisappear { search.cancel() }
    }

    private var message: String {
        if searchQuery.isEmpty { return "Search by file name or path." }
        if searching { return "Searching files…" }
        return response?.error ?? "No matching files."
    }

    private func row(_ path: String) -> some View {
        let name = (path as NSString).lastPathComponent
        let folder = (path as NSString).deletingLastPathComponent
        return HStack(spacing: 8) {
            Image(systemName: "doc").foregroundStyle(.secondary).accessibilityHidden(true)
            Text(name).font(.system(.callout, design: .monospaced)).lineLimit(1)
            if !folder.isEmpty {
                Text(folder).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .background(focusedFile == path ? Color.accentColor.opacity(0.17) : .clear,
                    in: RoundedRectangle(cornerRadius: 5))
        .help(path)
    }

    private func move(_ offset: Int) {
        let files = self.files
        guard !files.isEmpty else { return }
        let index = focusedFile.flatMap { files.firstIndex(of: $0) } ?? 0
        selected = files[max(0, min(files.count - 1, index + offset))]
    }

}

@MainActor
private struct QuickOpenInput: NSViewRepresentable {
    @Binding var query: String
    let move: (Int) -> Void
    let open: () -> Void
    let cancel: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(input: self) }

    func makeNSView(context: Context) -> NSSearchField {
        let input = InitialSearchField()
        input.placeholderString = "File name or relative path"
        input.setAccessibilityLabel("Find a file by name or path")
        input.delegate = context.coordinator
        input.stringValue = query
        return input
    }

    func updateNSView(_ input: NSSearchField, context: Context) {
        context.coordinator.input = self
        if input.stringValue != query { input.stringValue = query }
    }

    final class InitialSearchField: NSSearchField {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            DispatchQueue.main.async { [weak self] in
                if let self { self.window?.makeFirstResponder(self) }
            }
        }
    }

    final class Coordinator: NSObject, NSSearchFieldDelegate {
        var input: QuickOpenInput

        init(input: QuickOpenInput) { self.input = input }

        func controlTextDidChange(_ notification: Notification) {
            if let control = notification.object as? NSSearchField { input.query = control.stringValue }
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            switch selector {
            case #selector(NSResponder.moveUp(_:)): input.move(-1)
            case #selector(NSResponder.moveDown(_:)): input.move(1)
            case #selector(NSResponder.insertNewline(_:)): input.open()
            case #selector(NSResponder.cancelOperation(_:)): input.cancel()
            default: return false
            }
            return true
        }
    }
}

@MainActor
private struct FileBranch: View {
    @ObservedObject var model: WorkspaceModel
    let directory: String
    let depth: Int

    var body: some View {
        if let page = model.folders[directory] {
            ForEach(page.entries) { entry in
                entryRow(entry)
                if entry.isDirectory, model.expanded.contains(entry.path) {
                    FileBranch(model: model, directory: entry.path, depth: depth + 1)
                }
            }
            if page.isLoading {
                ProgressView().controlSize(.small).padding(.leading, indent)
            } else if let error = page.error {
                VStack(alignment: .leading, spacing: 4) {
                    Text(error).font(.caption).foregroundStyle(.orange)
                    Button("Retry") { Task { await model.loadFolder(directory, direction: page.retry) } }
                }
                .padding(.horizontal, 10).padding(.leading, indent)
            } else if page.nextCursor != nil || !page.previousCursors.isEmpty {
                HStack(spacing: 6) {
                    Button {
                        Task { await model.loadFolder(directory, direction: .previous) }
                    } label: {
                        Image(systemName: "chevron.left")
                    }
                    .disabled(page.previousCursors.isEmpty)
                    .help("Previous files")
                    .accessibilityLabel("Previous files")
                    Button {
                        Task { await model.loadFolder(directory, direction: .next) }
                    } label: {
                        Image(systemName: "chevron.right")
                    }
                    .disabled(page.nextCursor == nil)
                    .help("Next files")
                    .accessibilityLabel("Next files")
                }
                .controlSize(.small)
                .padding(.leading, indent)
            } else if page.entries.isEmpty {
                Text("Empty folder").font(.caption).foregroundStyle(.secondary).padding(.leading, indent)
            }
        }
    }

    private var indent: CGFloat { CGFloat(min(depth, 9) * 11 + 8) }

    private func entryRow(_ entry: ProjectEntry) -> some View {
        let tagged = model.tagPaths.contains(entry.path)
        let selected = model.selectedPath == entry.path
        return HStack(spacing: 3) {
            Button {
                Task {
                    if entry.isDirectory {
                        await model.toggleFolder(entry.path)
                    } else {
                        await model.openFile(entry.path)
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: entry.isDirectory
                          ? (model.expanded.contains(entry.path) ? "chevron.down" : "chevron.right")
                          : "doc")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(entry.isDirectory ? Color(nsColor: .systemTeal) : .secondary)
                        .frame(width: 12)
                    Text(entry.name).font(.system(size: 12)).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, minHeight: 24)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(entry.path)
            if !entry.isDirectory {
                Button { model.toggleTag(entry.path) } label: {
                    Image(systemName: tagged ? "paperclip.circle.fill" : "paperclip")
                        .font(.system(size: 11))
                        .foregroundStyle(tagged ? Color(nsColor: .systemTeal) : .secondary)
                        .frame(width: 20, height: 22)
                }
                .buttonStyle(.plain)
                .help(tagged ? "Detach \(entry.name)" : "Attach \(entry.name) to prompts")
                .accessibilityLabel(tagged ? "Detach \(entry.name)" : "Attach \(entry.name)")
            }
        }
        .padding(.leading, indent)
        .padding(.trailing, 4)
        .background(selected ? Color(nsColor: .systemTeal).opacity(0.16) : .clear,
                    in: RoundedRectangle(cornerRadius: 5))
        .padding(.horizontal, 4)
    }
}

@MainActor
private struct FileTab: View {
    @ObservedObject var buffer: SourceBuffer
    let selected: Bool
    let activate: () -> Void
    let close: () -> Void

    var body: some View {
        HStack(spacing: 5) {
            Button(action: activate) {
                HStack(spacing: 5) {
                    if buffer.isDirty {
                        Circle().fill(Color(nsColor: .systemTeal)).frame(width: 5, height: 5)
                            .accessibilityHidden(true)
                    }
                    Text(buffer.name).font(.caption).lineLimit(1)
                }
            }
            .buttonStyle(.plain)
            .help(buffer.path)
            Button(action: close) {
                Image(systemName: "xmark").font(.caption2).foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("Close \(buffer.name)")
            .accessibilityLabel("Close \(buffer.name)")
            .disabled(buffer.isSaving)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(selected ? Color(nsColor: .systemTeal).opacity(0.14) : .clear,
                    in: RoundedRectangle(cornerRadius: 5))
    }
}

@MainActor
private struct SourceEditor: View {
    @ObservedObject var model: WorkspaceModel
    @ObservedObject var buffer: SourceBuffer

    var body: some View {
        VStack(spacing: 0) {
            if buffer.diskRevision != nil {
                VStack(alignment: .leading, spacing: 7) {
                    Label("This file changed on disk. Your edits are intact.", systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                    HStack {
                        Button("Reload and Discard Edits") { Task { await model.reload(buffer) } }
                        Button("Save My Version") { Task { await model.save(buffer, replace: true) } }
                    }
                    .controlSize(.small)
                    .disabled(buffer.isSaving)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(9)
                Divider()
            }
            PlainSourceView(buffer: buffer, edited: { model.publishDirty() }) {
                Task { await model.save(buffer) }
            }
            Divider()
            HStack {
                Text(buffer.path)
                    .font(.system(.caption, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(.secondary)
                Spacer()
                if buffer.isSaving { ProgressView().controlSize(.small) }
                else if buffer.isDirty { Text("Unsaved").font(.caption).foregroundStyle(.secondary) }
                Button {
                    model.toggleTag(buffer.path)
                } label: {
                    Image(systemName: model.tagPaths.contains(buffer.path) ? "paperclip.circle.fill" : "paperclip")
                }
                .help(model.tagPaths.contains(buffer.path) ? "Detach file" : "Attach file to prompts")
                .accessibilityLabel(model.tagPaths.contains(buffer.path) ? "Detach file" : "Attach file to prompts")
                Button("Save") { Task { await model.save(buffer) } }
                    .disabled(!buffer.isDirty || buffer.isSaving || buffer.diskRevision != nil)
                    .keyboardShortcut("s", modifiers: .command)
                    .help("Save (⌘S)")
            }
            .controlSize(.small)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
        }
    }
}

@MainActor
private struct TurnCard: View {
    let turn: WorkspaceTurn
    var trustWorkspace: (() -> Void)?
    var openTerminal: (() -> Void)?
    let answering: Set<String>
    let answer: (AgentApproval, Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(turn.prompt)
                .font(.callout.weight(.medium))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color(nsColor: .systemTeal).opacity(0.11), in: RoundedRectangle(cornerRadius: 7))
            HStack(spacing: 6) {
                if turn.isActive { ProgressView().controlSize(.small) }
                Text(turn.status.capitalized).font(.caption.weight(.semibold))
                if turn.isActive, turn.reporting == "actions", !turn.action.isEmpty {
                    let isPlan = turn.events.contains { $0.kind == "plan" && $0.text.hasPrefix(turn.action) }
                    if !isPlan { Text(turn.action).font(.caption).lineLimit(2) }
                }
            }
            .foregroundStyle(.secondary)
            if !turn.risk.isEmpty, !turn.events.contains(where: { $0.kind == "risk" }) {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.triangle").accessibilityHidden(true)
                    AgentMarkdown(source: turn.risk).equatable()
                }.foregroundStyle(.orange)
            }
            if turn.reporting != "risks", !turn.outcome.isEmpty,
               !turn.events.contains(where: { $0.kind == "result" || $0.text == turn.outcome }) {
                AgentMarkdown(source: turn.outcome).equatable()
            }
            ForEach(turn.events) { event in
                VStack(alignment: .leading, spacing: 3) {
                    if event.kind != "result" {
                        Text(event.kind.capitalized).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    }
                    if AgentMarkdown.rendersEvent(event.kind) {
                        AgentMarkdown(source: event.text).equatable()
                            .foregroundStyle(event.kind == "error" || event.kind == "risk" ? .orange : .primary)
                    } else {
                        Text(event.text)
                            .font(.caption)
                            .foregroundStyle(event.kind == "error" ? .orange : .primary)
                            .textSelection(.enabled)
                    }
                }
            }
            ForEach(turn.approvals) { approval in
                VStack(alignment: .leading, spacing: 7) {
                    Label(approval.title, systemImage: "hand.raised.fill")
                        .font(.caption.weight(.semibold))
                    Text(approval.detail)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                    if approval.decision == nil && turn.isActive {
                        HStack {
                            Button("Decline") { answer(approval, false) }
                            Button("Approve Once") { answer(approval, true) }.buttonStyle(.borderedProminent)
                        }
                        .disabled(answering.contains(approval.id))
                    } else {
                        Text(approval.decision == "accept" ? "Approved" : approval.decision == "decline" ? "Declined" : "Request ended")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.orange.opacity(0.35)))
            }
            if let trustWorkspace {
                Button("Trust Workspace for Cursor…", action: trustWorkspace)
                    .help("Trust this workspace in Cursor, then send your prompt again")
            }
            if let openTerminal {
                Button("Open Agent in Terminal", action: openTerminal)
            }
        }
    }
}
