import SwiftUI

private struct FleetRunRow: Identifiable {
    let host: FleetHost
    let key: String
    let title: String
    let repository: String
    let agent: String
    let lane: Int?
    let view: String
    let status: String
    let started: Date
    let action: String
    let outcome: String
    let risk: String

    var id: String { host.id + "/" + key }
    var hasRisk: Bool { !risk.isEmpty || status == "failed" }

    init?(host: FleetHost, value: [String: Any]) {
        guard let key = value["id"] as? String else { return nil }
        self.host = host
        self.key = key
        title = value["title"] as? String ?? "Agent run"
        repository = value["repo"] as? String ?? host.repo
        agent = value["agent"] as? String ?? ""
        lane = value["detached"] as? Int == 1 ? value["lane"] as? Int : nil
        view = value["view"] as? String ?? "actions"
        status = value["status"] as? String ?? "unknown"
        started = Date(timeIntervalSince1970: (value["started"] as? NSNumber)?.doubleValue ?? 0)
        action = value["action"] as? String ?? ""
        outcome = value["outcome"] as? String ?? ""
        risk = value["risk"] as? String ?? ""
    }
}

private struct FleetScheduleRow: Identifiable {
    let host: FleetHost
    let key: String
    let repository: String
    let agent: String
    let view: String
    let prompt: String
    let minutes: Int
    let enabled: Bool
    let next: Date?

    var id: String { host.id + "/" + key }
    var title: String { prompt.split(whereSeparator: \.isNewline).first.map(String.init) ?? "Scheduled task" }

    init?(host: FleetHost, value: [String: Any]) {
        guard let key = value["id"] as? String else { return nil }
        self.host = host
        self.key = key
        repository = value["repo"] as? String ?? host.repo
        agent = value["agent"] as? String ?? "codex"
        view = value["view"] as? String ?? "actions"
        prompt = value["prompt"] as? String ?? ""
        minutes = value["minutes"] as? Int ?? 60
        enabled = value["enabled"] as? Bool ?? false
        next = (value["next_run"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
    }
}

private struct FleetSnapshot {
    let runs: [FleetRunRow]
    let schedules: [FleetScheduleRow]
    let error: String?
}

private struct FleetRunDetail {
    let row: FleetRunRow
    let steps: [FleetActivityStep]
    let pendingApprovals: [String]
}

private struct ScheduleDraft: Identifiable {
    let id = UUID()
    let key: String?
    var hostID: String
    var repository: String
    var agent: String
    var view: String
    var prompt: String
    var minutes: String

    init(host: FleetHost, schedule: FleetScheduleRow? = nil) {
        key = schedule?.key
        hostID = host.id
        repository = schedule?.repository ?? host.repo
        agent = schedule?.agent ?? "codex"
        view = schedule?.view ?? "actions"
        prompt = schedule?.prompt ?? ""
        minutes = String(schedule?.minutes ?? 60)
    }
}

struct FleetOverview: View {
    let hosts: [FleetHost]
    let section: String
    let agents: [FleetActivityAgent]
    var requestedRunID: String? = nil
    var onShowAgents: (() -> Void)? = nil
    let onOpen: (FleetHost) -> Void
    let onSelectAgent: (FleetActivityAgent) -> Void

    @Environment(\.workbenchPalette) private var palette
    @Environment(\.scenePhase) private var scenePhase
    @State private var snapshots: [String: FleetSnapshot] = [:]
    @State private var refreshing = false
    @State private var runSelection: String?
    @State private var scheduleSelection: String?
    @State private var runDetail: FleetRunDetail?
    @State private var timelineError: (id: String, message: String)?
    @State private var timelineRefresh = UUID()
    @State private var draft: ScheduleDraft?
    @State private var deleting: FleetScheduleRow?
    @State private var operationError: String?
    @State private var busy = false

    private var schedulesOnly: Bool { section == "Schedules" }
    private var risksOnly: Bool { section == "Risks" }
    private var endpoints: [FleetHost] {
        var seen = Set<String>()
        return hosts.filter { seen.insert($0.ssh).inserted }
    }
    private var refreshID: [String] { endpoints.map { "\($0.id)/\($0.ssh)" } }
    private var allRuns: [FleetRunRow] {
        endpoints.flatMap { snapshots[$0.id]?.runs ?? [] }.sorted { $0.started > $1.started }
    }
    private var runs: [FleetRunRow] { risksOnly ? allRuns.filter(\.hasRisk) : allRuns }
    private var schedules: [FleetScheduleRow] { endpoints.flatMap { snapshots[$0.id]?.schedules ?? [] } }
    private var selectedRun: FleetRunRow? { runs.first { $0.id == runSelection } ?? runs.first }
    private var selectedSchedule: FleetScheduleRow? {
        schedules.first { $0.id == scheduleSelection } ?? schedules.first
    }

    private var timelineQuery: String? {
        guard scenePhase == .active, !schedulesOnly, let row = selectedRun else { return nil }
        return "\(row.id):\(row.status):\(timelineRefresh)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 17) {
            header
            if !hosts.isEmpty {
                if schedulesOnly || risksOnly { metrics }
                connectionNotice
            }
            GeometryReader { geometry in
                if geometry.size.width >= 735 {
                    HStack(spacing: 12) {
                        if schedulesOnly {
                            list.frame(minWidth: 370, maxWidth: .infinity)
                            detail.frame(width: min(380, geometry.size.width * 0.43))
                        } else {
                            list.frame(width: min(350, geometry.size.width * 0.38))
                            detail.frame(maxWidth: .infinity, maxHeight: .infinity)
                        }
                    }
                } else {
                    VStack(spacing: 12) {
                        list.frame(minHeight: 135)
                        if schedulesOnly ? selectedSchedule != nil : selectedRun != nil {
                            detail.frame(minHeight: 160, maxHeight: 265)
                        }
                    }
                }
            }
        }
        .padding(22)
        .task(id: scenePhase == .active ? refreshID : nil) {
            guard scenePhase == .active else { return }
            await refresh(waitForCurrent: true)
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(20)) } catch { break }
                await refresh()
            }
        }
        .task(id: timelineQuery) {
            if let query = timelineQuery, let row = selectedRun { await monitorTimeline(row, query: query) }
        }
        .onChange(of: requestedRunID, initial: true) { _, id in
            if let id { runSelection = id }
        }
        .sheet(item: $draft) { draft in
            ScheduleEditor(draft: draft, hosts: endpoints) {
                self.draft = nil
                Task { await refresh(waitForCurrent: true) }
            }
        }
        .confirmationDialog(
            "Delete schedule?",
            isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
            presenting: deleting
        ) { row in
            Button("Delete Schedule", role: .destructive) { change(row, operation: "delete") }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("Any active run for this schedule will be cancelled.")
        }
        .alert("Fleet Request Failed", isPresented: Binding(
            get: { operationError != nil }, set: { if !$0 { operationError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(operationError ?? "")
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 5) {
                Text(section)
                    .font(.system(size: 28, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                if schedulesOnly || risksOnly {
                    Text(schedulesOnly ? "Manage recurring work on your Fleet hosts."
                         : "Inspect failures and breakage reported by agents.")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 4)
            if refreshing {
                ProgressView().controlSize(.small)
                    .accessibilityLabel("Refreshing hosts")
            }
            Button {
                Task { await refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
                    .frame(width: 17, height: 17)
            }
            .help("Refresh hosts")
            .accessibilityLabel("Refresh hosts")
            .disabled(refreshing || hosts.isEmpty)
            if let onShowAgents {
                Button(action: onShowAgents) { Label("Back to Activity", systemImage: "arrow.left") }
            }
            if schedulesOnly {
                Button {
                    if let host = endpoints.first { draft = ScheduleDraft(host: host) }
                } label: {
                    Label("New Schedule", systemImage: "plus")
                }
                .buttonStyle(.borderedProminent)
                .disabled(endpoints.isEmpty)
            }
        }
    }

    private var metrics: some View {
        HStack(spacing: 9) {
            if schedulesOnly {
                metric("Enabled", schedules.filter(\.enabled).count, icon: "clock", color: palette.tint)
                metric("Paused", schedules.filter { !$0.enabled }.count, icon: "pause", color: .secondary)
            } else if risksOnly {
                metric("Flagged runs", runs.count, icon: "exclamationmark.triangle", color: palette.caution)
                metric("Failed", runs.filter { $0.status == "failed" }.count, icon: "xmark.circle", color: palette.negative)
            }
        }
    }

    private func metric(_ label: String, _ count: Int, icon: String, color: Color) -> some View {
        HStack(spacing: 9) {
            Image(systemName: icon).foregroundStyle(color).frame(width: 16)
            Text(count, format: .number)
                .font(.system(size: 16, weight: .semibold, design: .monospaced))
            Text(label).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
        .fleetPanel(palette)
    }

    @ViewBuilder private var connectionNotice: some View {
        let failures = endpoints.compactMap { host in snapshots[host.id]?.error.map { "\(host.label): \($0)" } }
        if !failures.isEmpty {
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(failures.enumerated()), id: \.offset) { _, failure in
                        Text(failure).textSelection(.enabled)
                    }
                }
                .padding(.top, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: {
                Label("\(failures.count) host\(failures.count == 1 ? "" : "s") unavailable",
                      systemImage: "wifi.exclamationmark")
            }
            .font(.system(size: 12))
            .foregroundStyle(palette.caution)
            .padding(10)
            .fleetPanel(palette)
        }
    }

    private var list: some View {
        VStack(alignment: .leading, spacing: 0) {
            listHeading(schedulesOnly ? "All Schedules" : risksOnly ? "Reported Risks" : "Recent Runs",
                        count: schedulesOnly ? schedules.count : runs.count)
            if hosts.isEmpty {
                empty("No hosts configured", "Add a host to view Fleet activity.", icon: "server.rack")
            } else if schedulesOnly, schedules.isEmpty {
                empty(refreshing ? "Loading schedules…" : "No schedules yet",
                      refreshing ? "" : "Create a schedule to run a task at an interval.", icon: "calendar")
            } else if !schedulesOnly, runs.isEmpty {
                empty(refreshing ? "Loading runs…" : risksOnly ? "No reported risks" : "No activity yet",
                      refreshing ? "" : risksOnly ? "Risks appear when a run flags a problem." : "Run an agent task to see it here.",
                      icon: risksOnly ? "checkmark.shield" : "waveform.path")
            } else {
                ScrollView {
                    LazyVStack(spacing: 1) {
                        if schedulesOnly {
                            ForEach(schedules) { row in
                                rowButton(
                                    row.title,
                                    metadata: "\(row.host.label) · \(displayAgent(row.agent)) · every \(row.minutes) min",
                                    note: row.repository,
                                    status: row.enabled ? "Enabled" : "Paused",
                                    color: row.enabled ? palette.tint : .secondary,
                                    active: selectedSchedule?.id == row.id
                                ) { scheduleSelection = row.id }
                            }
                        } else {
                            ForEach(runs) { row in
                                rowButton(
                                    row.title,
                                    metadata: "\(row.host.label) · \(displayAgent(row.agent)) · \(row.started.formatted(.relative(presentation: .named)))",
                                    note: risksOnly ? (row.risk.isEmpty ? row.outcome : row.risk) : row.action,
                                    markdownNote: true,
                                    status: FleetRunStatus.label(row.status),
                                    color: statusColor(row.status),
                                    active: selectedRun?.id == row.id
                                ) {
                                    runSelection = row.id
                                }
                            }
                        }
                    }
                    .padding(5)
                }
            }
        }
        .fleetPanel(palette)
    }

    private func listHeading(_ title: String, count: Int) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text(title).font(.system(size: 12, weight: .semibold))
                Spacer()
                Text(count, format: .number).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 10)
            Rectangle().fill(palette.separator).frame(height: 1)
        }
    }

    private func rowButton(_ title: String, metadata: String, note: String, markdownNote: Bool = false, status: String, color: Color,
                           active: Bool, select: @escaping () -> Void) -> some View {
        Button(action: select) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                    Text(metadata).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                    if !note.isEmpty {
                        let noteText = markdownNote ? AgentMarkdownDocument.preview(note) : AttributedString(note)
                        Text(noteText).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer(minLength: 4)
                statusPill(status, color: color)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
            .background(active ? palette.selection : .clear, in: RoundedRectangle(cornerRadius: 6))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }

    @ViewBuilder private var detail: some View {
        if schedulesOnly, let row = selectedSchedule {
            detailPanel(title: "Schedule Details") { scheduleDetails(row) }
        } else if !schedulesOnly, let row = selectedRun {
            runDetails(row).id(row.id).fleetPanel(palette)
        } else {
            empty(schedulesOnly ? "Select a schedule" : "Select a run", "", icon: "sidebar.trailing")
                .fleetPanel(palette)
        }
    }

    private func detailPanel<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.system(size: 12, weight: .semibold))
                .padding(.horizontal, 13)
                .padding(.vertical, 10)
            Rectangle().fill(palette.separator).frame(height: 1)
            content()
        }
        .fleetPanel(palette)
    }

    private func runDetails(_ selected: FleetRunRow) -> some View {
        let timeline = runDetail?.row.id == selected.id ? runDetail : nil
        let row = timeline?.row ?? selected
        let steps = timeline?.steps ?? []
        return VStack(spacing: 0) {
            runHeader(row, pendingApprovals: timeline?.pendingApprovals ?? [])
            Rectangle().fill(palette.separator).frame(height: 1)
            ScrollViewReader { scroll in
                ScrollView {
                    runTimeline(row, steps: steps, loaded: timeline != nil)
                        .padding(16)
                    Color.clear.frame(height: 0).id("latest-event").accessibilityHidden(true)
                }
                .frame(maxHeight: .infinity)
                Rectangle().fill(palette.separator).frame(height: 1)
                HStack {
                    if !steps.isEmpty {
                        Button { scroll.scrollTo("latest-event", anchor: .bottom) } label: {
                            Label("Latest", systemImage: "arrow.down.to.line")
                        }
                        .help("Jump to the latest event")
                    }
                    Spacer()
                    openRunButton(row)
                }
                .controlSize(.small)
                .padding(9)
            }
        }
    }

    private func runHeader(_ row: FleetRunRow, pendingApprovals: [String]) -> some View {
        let host = hostForRepository(row.host, repo: row.repository)
        return VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(row.title)
                    .font(.system(size: 17, weight: .semibold))
                    .lineLimit(2)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                statusPill(FleetRunStatus.label(row.status), color: statusColor(row.status))
            }
            Text("\(displayAgent(row.agent)) · \(host.label) · \(row.started.formatted(date: .abbreviated, time: .shortened))")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(2)
            HStack(spacing: 8) {
                Text(row.repository)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .help(row.repository)
                Spacer(minLength: 0)
                statusPill(row.view.capitalized, color: .secondary)
                    .help("Reporting mode")
            }
            if row.status == FleetRunStatus.needsAttention {
                Label(pendingApprovals.isEmpty ? "Waiting for approval" : pendingApprovals.joined(separator: " · "),
                      systemImage: "hand.raised.fill")
                    .lineLimit(2)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(palette.caution)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(palette.caution.opacity(0.09), in: RoundedRectangle(cornerRadius: 6))
            }
        }
        .padding(14)
    }

    private func runTimeline(_ row: FleetRunRow, steps: [FleetActivityStep], loaded: Bool) -> some View {
        VStack(alignment: .leading, spacing: 13) {
            if let failure = timelineError, failure.id == row.id {
                Text(failure.message).font(.caption).foregroundStyle(palette.caution)
                Button("Retry") { timelineRefresh = UUID() }
                    .controlSize(.small)
            } else if !loaded {
                ProgressView().controlSize(.small).accessibilityLabel("Loading timeline")
            }
            if !row.risk.isEmpty && !steps.contains(where: { $0.kind == .risk }) {
                field("Flagged", row.risk, color: palette.caution, markdown: true)
            }
            if !steps.isEmpty {
                FleetActivityTimeline(steps: steps)
            } else if !row.outcome.isEmpty || !row.action.isEmpty {
                let value = row.outcome.isEmpty ? row.action : row.outcome
                field(FleetRunStatus.isActive(row.status) ? "Current action" : "Outcome", value, markdown: true)
            } else if loaded {
                Text("No events recorded.").font(.callout).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func openRunButton(_ row: FleetRunRow) -> some View {
        let host = hostForRepository(row.host, repo: row.repository)
        if let agent = agents.first(where: {
            $0.host.ssh == host.ssh && ($0.runID == row.key
                || ($0.host == host && $0.lane.agent == row.agent && $0.lane.lane == row.lane))
        }) {
            Button("Open Agent") { onSelectAgent(agent) }
                .help("Open \(agent.label)’s current conversation")
        } else {
            Button { onOpen(host) } label: { Label("Open Workspace", systemImage: "folder") }
        }
    }

    private func scheduleDetails(_ row: FleetScheduleRow) -> some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 13) {
                    Text(row.title).font(.system(size: 15, weight: .semibold)).lineLimit(3)
                    statusPill(row.enabled ? "Enabled" : "Paused", color: row.enabled ? palette.tint : .secondary)
                    field("Host", row.host.label)
                    field("Repository", row.repository, monospaced: true)
                    field("Agent", displayAgent(row.agent))
                    field("Reporting", row.view.capitalized)
                    field("Interval", "\(row.minutes) min")
                    if let next = row.next { field("Next run", next.formatted(date: .abbreviated, time: .shortened)) }
                    field("Prompt preview", row.prompt)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(13)
            }
            Rectangle().fill(palette.separator).frame(height: 1)
            HStack {
                Menu {
                    Button("Delete Schedule…", role: .destructive) { deleting = row }
                } label: {
                    Image(systemName: "ellipsis")
                        .accessibilityLabel("Schedule actions")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                Spacer()
                Button(row.enabled ? "Pause" : "Resume") { change(row, operation: row.enabled ? "pause" : "resume") }
                Button("Edit…") { edit(row) }
            }
            .controlSize(.small)
            .disabled(busy)
            .padding(9)
        }
    }

    private func field(_ label: String, _ value: String, monospaced: Bool = false, color: Color = .primary, markdown: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            if markdown {
                AgentMarkdown(source: value, font: .system(size: 12)).equatable().foregroundStyle(color)
            } else {
                Text(value)
                    .font(.system(size: 12, design: monospaced ? .monospaced : .default))
                    .foregroundStyle(color)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
    }

    private func empty(_ title: String, _ description: String, icon: String) -> some View {
        VStack(spacing: 7) {
            Image(systemName: icon).font(.system(size: 24)).foregroundStyle(palette.tint.opacity(0.7))
            Text(title).font(.system(size: 13, weight: .semibold))
            if !description.isEmpty {
                Text(description).font(.system(size: 12)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func statusPill(_ title: String, color: Color) -> some View {
        Text(title)
            .font(.system(size: 10, weight: .medium))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: Capsule())
            .fixedSize()
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running": palette.tint
        case FleetRunStatus.needsAttention: palette.caution
        case "failed": palette.negative
        case "cancelled": palette.caution
        default: .secondary
        }
    }

    private func hostForRepository(_ host: FleetHost, repo: String) -> FleetHost {
        if let configured = hosts.first(where: { $0.ssh == host.ssh && $0.repo == repo }) {
            return configured
        }
        var target = host
        target.repo = repo
        return target
    }

    @MainActor private func refresh(waitForCurrent: Bool = false) async {
        while waitForCurrent && refreshing {
            do { try await Task.sleep(for: .milliseconds(100)) } catch { return }
        }
        guard !refreshing, !endpoints.isEmpty else { return }
        refreshing = true
        defer { refreshing = false }
        let current = endpoints
        await withTaskGroup(of: (String, FleetSnapshot).self) { group in
            var waiting = current.makeIterator()
            for _ in 0..<min(4, current.count) {
                if let host = waiting.next() { group.addTask { await Self.snapshot(host) } }
            }
            for await (id, response) in group {
                guard !Task.isCancelled else {
                    group.cancelAll()
                    return
                }
                if let error = response.error, let previous = snapshots[id] {
                    snapshots[id] = FleetSnapshot(runs: previous.runs, schedules: previous.schedules, error: error)
                } else {
                    snapshots[id] = response
                }
                if let host = waiting.next() { group.addTask { await Self.snapshot(host) } }
            }
        }
    }

    private static func snapshot(_ host: FleetHost) async -> (String, FleetSnapshot) {
        do {
            let value = try await FleetBridge.call(host, ["op": "snapshot"])
            let runs = (value["runs"] as? [[String: Any]] ?? []).compactMap { FleetRunRow(host: host, value: $0) }
            let schedules = (value["schedules"] as? [[String: Any]] ?? []).compactMap { FleetScheduleRow(host: host, value: $0) }
            return (host.id, FleetSnapshot(runs: runs, schedules: schedules, error: nil))
        } catch {
            return (host.id, FleetSnapshot(runs: [], schedules: [], error: error.localizedDescription))
        }
    }

    @MainActor private func monitorTimeline(_ row: FleetRunRow, query: String) async {
        var revision = ""
        var active = FleetRunStatus.isActive(row.status)
        timelineError = nil
        repeat {
            do {
                let value = try await FleetBridge.call(row.host, ["op": "detail", "id": row.key, "revision": revision])
                guard !Task.isCancelled, timelineQuery == query else { return }
                if value["unchanged"] as? Bool != true {
                    guard let metadata = value["run"] as? [String: Any],
                          let latest = FleetRunRow(host: row.host, value: metadata) else {
                        throw FleetError(message: "The run is no longer available on this host.")
                    }
                    let events = (value["events"] as? [[String: Any]] ?? []).enumerated().compactMap {
                        FleetActivityEvent(index: $0.offset, value: $0.element)
                    }
                    let pending = (value["approvals"] as? [[String: Any]] ?? []).compactMap { approval in
                        approval["decision"] is String ? nil : approval["title"] as? String
                    }
                    runDetail = FleetRunDetail(row: latest, steps: FleetActivityStep.timeline(events), pendingApprovals: pending)
                    revision = value["revision"] as? String ?? ""
                    active = FleetRunStatus.isActive(latest.status)
                }
                if timelineError != nil { timelineError = nil }
            } catch {
                guard !Task.isCancelled, timelineQuery == query else { return }
                timelineError = (row.id, error.localizedDescription)
            }
            if active {
                do { try await Task.sleep(for: .seconds(row.host.ssh.isEmpty ? 2 : 4)) } catch { return }
            }
        } while active && !Task.isCancelled && timelineQuery == query
    }

    private func change(_ row: FleetScheduleRow, operation: String) {
        busy = true
        Task {
            defer { busy = false }
            do {
                _ = try await FleetBridge.call(row.host, ["op": operation, "id": row.key])
                await refresh(waitForCurrent: true)
            } catch {
                operationError = error.localizedDescription
            }
        }
    }

    private func edit(_ row: FleetScheduleRow) {
        busy = true
        Task {
            defer { busy = false }
            do {
                let value = try await FleetBridge.call(row.host, ["op": "schedule", "id": row.key])
                guard let data = value["schedule"] as? [String: Any],
                      let schedule = FleetScheduleRow(host: row.host, value: data) else {
                    operationError = "The host did not return the schedule."
                    return
                }
                draft = ScheduleDraft(host: row.host, schedule: schedule)
            } catch {
                operationError = error.localizedDescription
            }
        }
    }
}

private struct ScheduleEditor: View {
    @State var draft: ScheduleDraft
    let hosts: [FleetHost]
    let onSave: () -> Void
    @Environment(\.workbenchPalette) private var palette
    @Environment(\.dismiss) private var dismiss
    @State private var error: String?
    @State private var saving = false

    private var selectedHost: FleetHost? { hosts.first { $0.id == draft.hostID } }
    private var interval: Int? {
        guard let value = Int(draft.minutes.trimmingCharacters(in: .whitespaces)), (1...525_600).contains(value) else { return nil }
        return value
    }
    private var canSave: Bool {
        selectedHost != nil && interval != nil && !draft.repository.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !draft.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !draft.prompt.contains("\0") && draft.prompt.unicodeScalars.count <= 32_000
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(draft.key == nil ? "New Schedule" : "Edit Schedule")
                .font(.system(size: 20, weight: .semibold))
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 11) {
                GridRow {
                    caption("Host")
                    Picker("Host", selection: $draft.hostID) {
                        ForEach(hosts, id: \.id) { host in Text(host.label).tag(host.id) }
                    }
                    .labelsHidden()
                    .disabled(draft.key != nil)
                    .onChange(of: draft.hostID) {
                        if draft.key == nil, let selectedHost { draft.repository = selectedHost.repo }
                    }
                }
                GridRow {
                    caption("Repository")
                    TextField("Path on the selected host", text: $draft.repository)
                        .font(.system(size: 12, design: .monospaced))
                        .textFieldStyle(.roundedBorder)
                }
                GridRow {
                    caption("Agent")
                    Picker("Agent", selection: $draft.agent) {
                        Text("Codex").tag("codex")
                        Text("Cursor").tag("cursor")
                    }
                    .labelsHidden()
                }
                GridRow {
                    caption("Reporting")
                    Picker("Reporting", selection: $draft.view) {
                        Text("Actions").tag("actions")
                        Text("Summary").tag("summary")
                        Text("Risks").tag("risks")
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                }
                GridRow {
                    caption("Interval")
                    HStack(spacing: 7) {
                        TextField("60", text: $draft.minutes)
                            .frame(width: 78)
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel("Interval in minutes")
                        Text("minutes").font(.system(size: 12)).foregroundStyle(.secondary)
                        if interval == nil {
                            Text("Enter 1–525,600 minutes.").font(.system(size: 11)).foregroundStyle(palette.caution)
                        }
                    }
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                caption("Prompt")
                TextEditor(text: $draft.prompt)
                    .font(.system(size: 13))
                    .scrollContentBackground(.hidden)
                    .padding(5)
                    .frame(minHeight: 104, maxHeight: 150)
                    .accessibilityLabel("Prompt")
                    .fleetPanel(palette)
            }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 12))
                    .foregroundStyle(palette.caution)
            }
            HStack(spacing: 10) {
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Button(draft.key == nil ? "Create Schedule" : "Save Changes") { save() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canSave || saving)
            }
        }
        .padding(20)
        .frame(width: 475)
        .disabled(saving)
        .interactiveDismissDisabled(saving)
    }

    private func caption(_ label: String) -> some View {
        Text(label).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
    }

    private func save() {
        guard let selectedHost, let interval else { return }
        saving = true
        error = nil
        Task {
            defer { saving = false }
            var request: [String: Any] = [
                "op": draft.key == nil ? "create" : "update",
                "repo": draft.repository, "agent": draft.agent, "view": draft.view,
                "minutes": interval, "prompt": draft.prompt
            ]
            if let key = draft.key { request["id"] = key }
            do {
                _ = try await FleetBridge.call(selectedHost, request)
                onSave()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

private func displayAgent(_ value: String) -> String {
    value == "codex" ? "Codex" : value == "cursor" ? "Cursor" : value.capitalized
}

private extension View {
    func fleetPanel(_ palette: WorkbenchPalette) -> some View {
        background(palette.surface, in: RoundedRectangle(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8).strokeBorder(palette.separator)
                    .allowsHitTesting(false)
            }
    }
}
