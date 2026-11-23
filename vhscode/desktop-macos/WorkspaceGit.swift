import SwiftUI

struct WorkspaceGitChange: Decodable, Hashable, Identifiable {
    let path: String
    let code: String
    let original: String?

    var id: String { "\(code) \(path)" }
    var isUntracked: Bool { code == "??" }
    var isDeleted: Bool { code.last == "D" || code == "D." }
    var affectsFileTree: Bool { isUntracked || code.contains { "ADCRT".contains($0) } }
    private var isConflicted: Bool { ["DD", "AU", "UD", "UA", "DU", "AA", "UU"].contains(code) }

    var parentFolders: Set<String> {
        var folders = Set<String>()
        for path in [path, original].compactMap({ $0 }) {
            var components = path.split(separator: "/").dropLast()
            while !components.isEmpty {
                folders.insert(components.joined(separator: "/"))
                components.removeLast()
            }
        }
        return folders
    }

    var badge: String {
        if isUntracked { return "U" }
        if isConflicted { return "!" }
        return code.replacingOccurrences(of: ".", with: "")
    }

    func color(_ palette: WorkbenchPalette) -> Color {
        if isConflicted || isDeleted { return palette.negative }
        if isUntracked || code.contains("A") { return palette.positive }
        if code.contains("R") || code.contains("C") { return palette.tint }
        return palette.caution
    }

    var summary: String {
        if isUntracked { return "Untracked" }
        if isConflicted { return "Merge conflict" }
        let labels: [Character: String] = [
            "M": "Modified", "A": "Added", "D": "Deleted", "R": "Renamed", "C": "Copied", "T": "Type changed",
        ]
        return zip(code, ["staged", "working tree"]).compactMap { code, location in
            labels[code].map { "\($0) (\(location))" }
        }.joined(separator: ", ")
    }
}

struct WorkspaceGitStatus: Decodable, Equatable {
    let branch: String
    let head: String
    let upstream: String?
    let ahead: Int?
    let behind: Int?
    let changes: [WorkspaceGitChange]
    let truncated: Bool
}

@MainActor
final class WorkspaceGit: ObservableObject {
    let host: FleetHost
    @Published private(set) var status: WorkspaceGitStatus?
    @Published private(set) var statusError: String?
    @Published private(set) var fetchError: String?
    @Published private(set) var fetching = false
    private(set) var files: [String: WorkspaceGitChange] = [:]
    private(set) var folders = Set<String>()
    private var refreshing = false

    init(host: FleetHost) { self.host = host }

    func monitor() async {
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(for: .seconds(host.ssh.isEmpty ? 4 : 8)) } catch { return }
        }
    }

    func refresh() async {
        guard !refreshing, !fetching, !Task.isCancelled else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let reply = try await FleetBridge.call(host, ["op": "git_status"])
            try Task.checkCancellation()
            guard let rawStatus = reply["status"] else {
                throw FleetError(message: "The workspace did not return Git status.")
            }
            let snapshot = rawStatus is NSNull ? nil : try JSONDecoder().decode(
                WorkspaceGitStatus.self, from: JSONSerialization.data(withJSONObject: rawStatus)
            )
            if snapshot != status {
                let changes = snapshot?.changes ?? []
                files = changes.reduce(into: [:]) { $0[$1.path] = $1 }
                folders = changes.reduce(into: []) { $0.formUnion($1.parentFolders) }
                status = snapshot
            }
            if statusError != nil { statusError = nil }
        } catch is CancellationError {
            return
        } catch {
            if !Task.isCancelled, statusError != error.localizedDescription { statusError = error.localizedDescription }
        }
    }

    func fetch() async {
        guard !fetching else { return }
        fetching = true
        fetchError = nil
        do {
            _ = try await FleetBridge.call(host, ["op": "git_fetch"])
        } catch {
            fetchError = error.localizedDescription
        }
        fetching = false
        await refresh()
    }
}

struct WorkspaceGitBadge: View {
    @Environment(\.workbenchPalette) private var palette
    let change: WorkspaceGitChange

    var body: some View {
        Text(change.badge)
            .font(.system(size: 10, weight: .semibold, design: .monospaced))
            .foregroundStyle(change.color(palette))
            .help(change.summary)
            .accessibilityLabel(change.summary)
    }
}

@MainActor
struct WorkspaceGitChanges: View {
    @Environment(\.workbenchPalette) private var palette
    @ObservedObject var git: WorkspaceGit
    let openDiff: (WorkspaceGitChange) -> Void
    @State private var expanded = false

    var body: some View {
        if let repository = git.status {
            VStack(alignment: .leading, spacing: 6) {
                branchRow(repository)
                DisclosureGroup(isExpanded: $expanded) {
                    if repository.changes.isEmpty && !repository.truncated {
                        Text("Working tree clean").font(.caption).foregroundStyle(.secondary)
                            .padding(.vertical, 4)
                    }
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(repository.changes) { change in
                            Button { openDiff(change) } label: {
                                HStack(spacing: 5) {
                                    Text(change.path).lineLimit(1).truncationMode(.middle)
                                    Spacer(minLength: 2)
                                    WorkspaceGitBadge(change: change)
                                }
                                .font(.caption)
                                .frame(minHeight: 22)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .help(change.original.map { "\($0) → \(change.path)" } ?? change.path)
                        }
                    }
                    if repository.truncated {
                        Text("More changes are available in Git.").font(.caption).foregroundStyle(.secondary)
                    }
                } label: {
                    HStack {
                        Text("Git Changes").font(.caption.weight(.semibold))
                        Spacer()
                        Text("\(repository.changes.count)\(repository.truncated ? "+" : "")")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
                if let error = git.fetchError ?? git.statusError {
                    Text(error).font(.caption).foregroundStyle(palette.caution)
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            Divider()
        } else if let error = git.statusError {
            Text("Git status unavailable")
                .font(.caption)
                .foregroundStyle(palette.caution)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .help(error)
        }
    }

    private func branchRow(_ repository: WorkspaceGitStatus) -> some View {
        HStack(spacing: 5) {
            Label(repository.branch, systemImage: "arrow.triangle.branch")
                .lineLimit(1).truncationMode(.middle)
                .help(repository.upstream.map { "\(repository.branch) tracks \($0)" } ?? "\(repository.branch) has no upstream")
            Spacer(minLength: 0)
            if let upstream = repository.upstream {
                if let ahead = repository.ahead, let behind = repository.behind {
                    Text("↑\(ahead) ↓\(behind)")
                        .monospacedDigit().fixedSize()
                        .help("Ahead \(ahead), behind \(behind) compared with \(upstream).")
                        .accessibilityLabel("Ahead \(ahead), behind \(behind)")
                }
                if git.fetching {
                    ProgressView().controlSize(.mini).accessibilityLabel("Fetching from remote")
                } else {
                    Button { Task { await git.fetch() } } label: {
                        Image(systemName: "arrow.down.circle")
                    }
                    .buttonStyle(.plain)
                    .help("Fetch from the remote for \(upstream)")
                    .accessibilityLabel("Fetch Git updates")
                }
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

@MainActor
struct WorkspaceGitDiff: View {
    @Environment(\.workbenchPalette) private var palette
    private struct Section: Decodable, Identifiable {
        let title: String
        let text: String
        let truncated: Bool
        var id: String { title }
    }

    private struct Preview: Identifiable {
        let section: Section
        var id: String { section.id }
        let body: AttributedString
        let added: Int
        let removed: Int

        init(_ section: Section, untracked: Bool, palette: WorkbenchPalette) {
            self.section = section
            var body = AttributedString()
            var added = 0
            var removed = 0
            var prefixWidth = 0
            let text = section.text
            text.enumerateSubstrings(in: text.startIndex..<text.endIndex, options: [.byLines]) { line, _, range, _ in
                guard let line else { return }
                var rendered = AttributedString(text[range])
                if untracked {
                    added += 1
                    rendered.foregroundColor = palette.positive
                } else if line.hasPrefix("@@") {
                    prefixWidth = line.prefix(while: { $0 == "@" }).count - 1
                    rendered.foregroundColor = palette.tint
                } else if line.hasPrefix("diff --") {
                    prefixWidth = 0
                    rendered.foregroundColor = .secondary
                } else {
                    switch line.prefix(prefixWidth).first(where: { $0 == "+" || $0 == "-" }) {
                    case "+":
                        added += 1
                        rendered.foregroundColor = palette.positive
                        rendered.backgroundColor = palette.positive.opacity(0.11)
                    case "-":
                        removed += 1
                        rendered.foregroundColor = palette.negative
                        rendered.backgroundColor = palette.negative.opacity(0.11)
                    default:
                        if prefixWidth == 0 || line.hasPrefix("\\") { rendered.foregroundColor = .secondary }
                    }
                }
                body.append(rendered)
            }
            self.body = body
            self.added = added
            self.removed = removed
        }
    }

    let host: FleetHost
    let change: WorkspaceGitChange
    var hasUnsavedEdits = false
    let openFile: ((String) -> Void)?
    @Environment(\.dismiss) private var dismiss
    @Environment(\.workbenchTypography) private var typography
    @State private var sections: [Section]?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(change.original.map { "\($0) → \(change.path)" } ?? change.path)
                    .font(.headline).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                Text(change.summary).font(.caption).foregroundStyle(change.color(palette))
            }
            if hasUnsavedEdits {
                Label("Save your edits to include them in this diff.", systemImage: "info.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            if let sections, !sections.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(sections.map { Preview($0, untracked: change.isUntracked, palette: palette) }) { preview in
                            VStack(alignment: .leading, spacing: 7) {
                                HStack(spacing: 10) {
                                    Text(preview.section.title).font(.subheadline.weight(.semibold))
                                    Spacer()
                                    if preview.added > 0 {
                                        Text("+\(preview.added) added").foregroundStyle(palette.positive)
                                    }
                                    if preview.removed > 0 {
                                        Text("−\(preview.removed) removed").foregroundStyle(palette.negative)
                                    }
                                }
                                .font(.caption.monospacedDigit())
                                ScrollView(.horizontal) {
                                    Text(preview.body.characters.isEmpty ? AttributedString("Empty file") : preview.body)
                                        .font(typography.code)
                                        .textSelection(.enabled)
                                        .fixedSize(horizontal: true, vertical: false)
                                        .padding(9)
                                }
                                .background(palette.editor, in: RoundedRectangle(cornerRadius: 7))
                                .overlay {
                                    RoundedRectangle(cornerRadius: 7).strokeBorder(palette.separator)
                                        .allowsHitTesting(false)
                                }
                                if preview.section.truncated {
                                    Text("Diff preview truncated").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                Group {
                    if let error { Text(error).foregroundStyle(palette.caution).textSelection(.enabled) }
                    else if sections != nil { Text("No current changes for this file.").foregroundStyle(.secondary) }
                    else { ProgressView("Loading diff…") }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            HStack {
                if let openFile, !change.isDeleted {
                    Button("Open File") {
                        dismiss()
                        openFile(change.path)
                    }
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(16)
        .frame(minWidth: 540, idealWidth: 660, minHeight: 320, idealHeight: 420)
        .background(palette.surface)
        .task {
            var request: [String: Any] = ["op": "git_diff", "path": change.path, "untracked": change.isUntracked]
            if let original = change.original { request["original"] = original }
            do {
                let reply = try await FleetBridge.call(host, request)
                try Task.checkCancellation()
                guard let rawSections = reply["sections"] else {
                    throw FleetError(message: "The workspace did not return a diff.")
                }
                let decoded = try JSONDecoder().decode([Section].self, from: JSONSerialization.data(withJSONObject: rawSections))
                sections = decoded
            } catch {
                if !Task.isCancelled { self.error = error.localizedDescription }
            }
        }
    }
}
