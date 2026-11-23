import SwiftUI

enum FleetAgentAction { case rename, delete }

struct FleetActivityAgent: Identifiable {
    let host: FleetHost
    let lane: FleetAgentLane
    let label: String
    let status: String
    let statusLabel: String
    let title: String?
    let action: String?
    let runID: String?
    let output: [FleetActivityEvent]
    let reachable: Bool

    var id: String { "\(host.id)/\(lane.agent)/\(lane.lane)" }
    var working: Bool { reachable && FleetRunStatus.isActive(status) }
}

struct FleetLiveRun: Decodable, Identifiable, Equatable {
    let id: String
    let agent: String
    let repo: String
    let title: String
    let action: String?
    let schedule: String?
    let output: [FleetActivityEvent]?
}

struct ActivityDashboard: View {
    let agents: [FleetActivityAgent]
    let runs: [(host: FleetHost, run: FleetLiveRun)]
    let unavailable: [String]
    let loading: Bool
    let selectedAgentID: String?
    let onOpenAgent: (FleetActivityAgent, FleetAgentAction?) -> Void
    let onOpenRun: (FleetHost, String) -> Void
    let onShowRuns: () -> Void
    let onCloseAgent: (() -> Void)?

    @Environment(\.workbenchPalette) private var palette
    private let columns = [GridItem(.adaptive(minimum: 238, maximum: 420), spacing: 12, alignment: .top)]

    var body: some View {
        let working = agents.filter(\.working)
        let others = agents.filter { !$0.working }
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Activity").font(.title2.weight(.semibold)).accessibilityAddTraits(.isHeader)
                Spacer()
                Button(action: onShowRuns) { Label("Runs", systemImage: "clock.arrow.circlepath") }
                    .help("Browse run history")
                if let onCloseAgent {
                    Button(action: onCloseAgent) { Image(systemName: "sidebar.trailing") }
                        .help("Close conversation pane")
                        .accessibilityLabel("Close conversation pane")
                }
            }
            if !unavailable.isEmpty {
                Label(unavailable.joined(separator: ", ") + " unavailable", systemImage: "wifi.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(palette.caution)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if agents.isEmpty && runs.isEmpty {
                if loading {
                    ProgressView("Loading agents…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ContentUnavailableView("No agents to show", systemImage: "person.2",
                                           description: Text("Open a workspace and choose New Agent."))
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        VStack(alignment: .leading, spacing: 8) {
                            heading("Working", count: working.count + runs.count)
                            if working.isEmpty && runs.isEmpty {
                                Text("No active runs").font(.callout).foregroundStyle(.secondary)
                                    .padding(.vertical, 4)
                            } else {
                                LazyVGrid(columns: columns, alignment: .leading, spacing: 12) {
                                    ForEach(working) { agent in agentCard(agent) }
                                    ForEach(runs, id: \.run.id) { item in
                                        Button {
                                            onOpenRun(item.host, item.run.id)
                                        } label: {
                                            VStack(alignment: .leading, spacing: 8) {
                                                cardHeader(item.run.schedule == nil ? provider(item.run.agent) : "\(provider(item.run.agent)) schedule",
                                                           status: "Running", color: palette.tint)
                                                metadata(item.host, repo: item.run.repo)
                                                currentWork(item.run.title, action: item.run.action, output: item.run.output ?? [])
                                            }
                                            .padding(12)
                                            .contentShape(Rectangle())
                                        }
                                        .buttonStyle(.plain)
                                        .help("Open run details")
                                        .modifier(ActivityCard(selected: false))
                                    }
                                }
                            }
                        }
                        if !others.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                heading("Other Agents", count: others.count)
                                LazyVGrid(columns: columns, alignment: .leading, spacing: 12) {
                                    ForEach(others) { agent in agentCard(agent) }
                                }
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func heading(_ title: String, count: Int) -> some View {
        HStack(spacing: 7) {
            Text(title).font(.headline)
            Text(count, format: .number).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private func agentCard(_ agent: FleetActivityAgent) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                onOpenAgent(agent, nil)
            } label: {
                VStack(alignment: .leading, spacing: 8) {
                    let color = switch agent.status {
                    case "running": palette.tint
                    case FleetRunStatus.needsAttention: palette.caution
                    case "completed": palette.positive
                    case "failed": palette.negative
                    case "cancelled": palette.caution
                    default: Color.secondary
                    }
                    cardHeader(agent.label, status: agent.reachable ? agent.statusLabel : "Offline",
                               color: agent.reachable ? color : palette.caution)
                    metadata(agent.host, repo: agent.host.repo, providerName: provider(agent.lane.agent))
                    if agent.reachable && (agent.working || !agent.output.isEmpty) {
                        currentWork(agent.title, action: agent.action, output: agent.output)
                    }
                }
                .padding(12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Open \(agent.label)’s conversation")
            .accessibilityAddTraits(selectedAgentID == agent.id ? .isSelected : [])
            HStack(spacing: 3) {
                Spacer(minLength: 0)
                if let runID = agent.runID {
                    Button { onOpenRun(agent.host, runID) } label: {
                        Image(systemName: "clock").frame(width: 25, height: 23).contentShape(Rectangle())
                    }
                    .help("View \(agent.label)’s latest run")
                    .accessibilityLabel("View \(agent.label)’s latest run")
                }
                Button { onOpenAgent(agent, .rename) } label: {
                    Image(systemName: "pencil").frame(width: 25, height: 23).contentShape(Rectangle())
                }
                .help("Rename \(agent.label)")
                .accessibilityLabel("Rename \(agent.label)")
                Button(role: .destructive) { onOpenAgent(agent, .delete) } label: {
                    Image(systemName: "trash").frame(width: 25, height: 23).contentShape(Rectangle())
                }
                .disabled(FleetRunStatus.isActive(agent.status))
                .help(FleetRunStatus.isActive(agent.status) ? "Stop this agent before deleting it" : "Delete \(agent.label)")
                .accessibilityLabel("Delete \(agent.label)")
            }
            .buttonStyle(.plain)
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .disabled(!agent.reachable)
            .padding(.trailing, 8)
            .padding(.bottom, 6)
        }
        .modifier(ActivityCard(selected: selectedAgentID == agent.id))
    }

    private func cardHeader(_ name: String, status: String, color: Color) -> some View {
        HStack(spacing: 8) {
            Text(name).font(.system(size: 14, weight: .semibold)).lineLimit(1)
            Spacer(minLength: 0)
            HStack(spacing: 4) {
                Circle().fill(color).frame(width: 5, height: 5).accessibilityHidden(true)
                Text(status).font(.caption.weight(.medium))
            }
            .foregroundStyle(color)
            .fixedSize()
        }
    }

    private func metadata(_ host: FleetHost, repo: String, providerName: String? = nil) -> some View {
        let folder = (repo as NSString).lastPathComponent
        let parts = [providerName, host.label, folder == host.label || folder.isEmpty ? nil : folder]
        return Text(parts.compactMap { $0 }.joined(separator: " · "))
            .font(.caption)
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .help(repo)
    }

    @ViewBuilder private func currentWork(_ title: String?, action: String?, output: [FleetActivityEvent]) -> some View {
        if let title, !title.isEmpty {
            Text(title).font(.callout).lineLimit(1)
        }
        if let action, !action.isEmpty, action != title, action != output.last?.text {
            Text(AgentMarkdownDocument.preview(action)).font(.caption).foregroundStyle(.secondary).lineLimit(2)
        }
        if let latest = output.last {
            let kind = FleetActivityStep.Kind(event: latest)
            Label(kind.label, systemImage: kind.symbol)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(kind.color(palette))
            Text(AgentMarkdownDocument.preview(latest.text, blockLimit: 2))
                .font(.caption).foregroundStyle(.secondary).lineLimit(3)
        }
    }

    private func provider(_ name: String) -> String { name == "codex" ? "Codex" : "Cursor" }
}

private struct ActivityCard: ViewModifier {
    @Environment(\.workbenchPalette) private var palette
    let selected: Bool

    func body(content: Content) -> some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(palette.surface, in: RoundedRectangle(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8).strokeBorder(selected ? palette.tint : palette.separator)
                    .allowsHitTesting(false)
            }
    }
}
