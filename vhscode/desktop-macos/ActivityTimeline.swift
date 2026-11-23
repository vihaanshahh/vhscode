import SwiftUI

struct FleetActivityEvent: Decodable, Equatable, Identifiable {
    let id: Int
    let kind: String
    let text: String
    let date: Date?

    private enum CodingKeys: String, CodingKey {
        case id = "seq"
        case kind, text
        case timestamp = "at"
    }

    init(from decoder: Decoder) throws {
        let value = try decoder.container(keyedBy: CodingKeys.self)
        id = try value.decode(Int.self, forKey: .id)
        kind = try value.decode(String.self, forKey: .kind)
        text = try value.decode(String.self, forKey: .text)
        date = try value.decodeIfPresent(Double.self, forKey: .timestamp).map(Date.init(timeIntervalSince1970:))
    }

    init?(index: Int, value: [String: Any]) {
        guard let kind = value["kind"] as? String, let text = value["text"] as? String else { return nil }
        self.kind = kind
        self.text = text
        id = value["seq"] as? Int ?? index
        date = (value["at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
    }
}

struct FleetActivityStep: Identifiable {
    enum Kind: Equatable {
        case request, plan, update, actions, risk, error, result, details

        var label: String {
            switch self {
            case .request: "Request"
            case .plan: "Plan"
            case .update: "Agent update"
            case .actions: "Actions"
            case .risk: "Flagged"
            case .error: "Error"
            case .result: "Result"
            case .details: "Technical details"
            }
        }

        var symbol: String {
            switch self {
            case .request: "person"
            case .plan: "list.bullet.rectangle"
            case .update: "bubble.left"
            case .actions: "terminal"
            case .risk: "exclamationmark.triangle"
            case .error: "xmark"
            case .result: "checkmark"
            case .details: "doc.text"
            }
        }

        func color(_ palette: WorkbenchPalette) -> Color {
            switch self {
            case .risk: palette.caution
            case .error: palette.negative
            case .result: palette.positive
            case .actions, .details: .secondary
            default: palette.tint
            }
        }

        init(event: FleetActivityEvent) {
            switch event.kind {
            case "prompt": self = .request
            case "plan": self = .plan
            case "info" where !event.text.hasPrefix("CLI token usage for this turn:"): self = .update
            case "action", "log": self = .actions
            case "risk": self = .risk
            case "error": self = .error
            case "result": self = .result
            default: self = .details
            }
        }
    }

    let kind: Kind
    var events: [FleetActivityEvent]
    var id: Int { events[0].id }
    var date: Date? { events[0].date }
    var isGrouped: Bool { kind == .details || (kind == .actions && !(events.count == 1 && events[0].kind == "action")) }

    static func timeline(_ events: [FleetActivityEvent]) -> [Self] {
        var steps: [Self] = []
        for event in events where !event.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let kind = Kind(event: event)
            if (kind == .actions || kind == .details), steps.last?.kind == kind {
                steps[steps.count - 1].events.append(event)
            } else {
                steps.append(Self(kind: kind, events: [event]))
            }
        }
        return steps
    }
}

struct FleetActivityTimeline: View {
    let steps: [FleetActivityStep]

    var body: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(steps) { step in
                FleetActivityTimelineRow(step: step, connectsToNext: step.id != steps.last?.id)
            }
        }
    }
}

private struct FleetActivityTimelineRow: View {
    @Environment(\.workbenchPalette) private var palette
    let step: FleetActivityStep
    let connectsToNext: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: step.kind.symbol)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(step.kind.color(palette))
                .frame(width: 24, height: 24)
                .background(step.kind.color(palette).opacity(0.12), in: Circle())
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(step.kind.label)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(step.kind.color(palette))
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: 0)
                    if let date = step.date {
                        Text(date.formatted(.dateTime.hour().minute().second()))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
                if step.isGrouped {
                    groupedEvents
                } else if let event = step.events.first {
                    eventText(event)
                }
            }
            .padding(.top, 4)
            .padding(.bottom, 15)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(alignment: .topLeading) {
            if connectsToNext {
                Rectangle().fill(palette.separator)
                    .frame(width: 1)
                    .padding(.leading, 11.5)
                    .padding(.top, 25)
                    .accessibilityHidden(true)
            }
        }
    }

    private var groupedEvents: some View {
        let entryCount = step.events.count == 1 ? "1 entry" : "\(step.events.count) entries"
        return DisclosureGroup {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(step.events) { event in
                    VStack(alignment: .leading, spacing: 3) {
                        if event.kind != "action" {
                            Text(event.kind == "log" ? "Output" : (event.kind == "info" ? "Info" : event.kind.capitalized))
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(.secondary)
                        }
                        eventText(event)
                    }
                }
            }
            .padding(.top, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            if step.kind == .actions, let first = step.events.first(where: { $0.kind == "action" }) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(first.text)
                        .font(.system(size: 11, design: .monospaced))
                        .lineLimit(2)
                    Text(entryCount)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            } else {
                Text(step.kind == .actions ? "Command output" : entryCount)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(9)
        .background(palette.editor, in: RoundedRectangle(cornerRadius: 7))
    }

    @ViewBuilder private func eventText(_ event: FleetActivityEvent) -> some View {
        if AgentMarkdown.rendersEvent(event.kind) {
            let text = event.kind == "plan" && event.text.hasPrefix("### Plan\n\n")
                ? String(event.text.dropFirst("### Plan\n\n".count)) : event.text
            AgentMarkdown(source: text, font: .system(size: 13)).equatable()
        } else {
            Text(verbatim: event.text)
                .font(.system(size: event.kind == "prompt" ? 13 : 11,
                              design: event.kind == "prompt" ? .default : .monospaced))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
    }
}
