import SwiftUI

struct AgentMarkdown: View, Equatable {
    let source: String
    var font: Font = .callout

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if let blocks = MarkdownBlock.parse(source) {
                ForEach(blocks.indices, id: \.self) { index in
                    switch blocks[index] {
                    case .prose(let text):
                        inline(text).font(font)
                    case .heading(let level, let text):
                        inline(text).font(level <= 2 ? .headline : font.weight(.semibold))
                            .accessibilityAddTraits(.isHeader)
                    case .quote(let text):
                        inline(text).font(font).foregroundStyle(.secondary)
                            .padding(.leading, 9)
                            .overlay(alignment: .leading) {
                                Rectangle().fill(.secondary.opacity(0.4)).frame(width: 2)
                            }
                    case .code(let text):
                        ScrollView(.horizontal) {
                            Text(verbatim: text)
                                .font(font.monospaced())
                                .fixedSize(horizontal: true, vertical: true)
                                .padding(8)
                        }
                        .background(.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 5))
                    }
                }
            } else {
                Text(verbatim: source).font(font)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    private func inline(_ text: String) -> Text {
        guard var content = try? AttributedString(
            markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) else { return Text(verbatim: text) }
        let inertLinks: [Range<AttributedString.Index>] = content.runs.compactMap { run in
            guard let link = run.link else { return nil }
            let scheme = link.scheme?.lowercased()
            guard scheme != "http", scheme != "https" else { return nil }
            return run.range
        }
        for range in inertLinks { content[range].link = nil }
        return Text(content)
    }
}

private enum MarkdownBlock {
    case prose(String)
    case heading(Int, String)
    case quote(String)
    case code(String)

    static func parse(_ source: String) -> [Self]? {
        // Leave oversized logs selectable as a single view and bound the number of SwiftUI children.
        guard source.utf8.count <= 65_536 else { return nil }
        let lines = source.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
        var blocks: [Self] = []
        var prose: [String] = []
        var code: [Substring] = []
        var quote: [Substring] = []
        var fence: Substring?

        for line in lines {
            guard blocks.count < 256 else { return nil }
            let prefix = line.prefix(while: { $0 == " " })
            let content = line.dropFirst(min(prefix.count, 3))
            let marker = content.first
            let fenceRun = content.prefix(while: { ($0 == "`" || $0 == "~") && $0 == marker })
            let fenceSuffix = content.dropFirst(fenceRun.count)
            if let opening = fence {
                if fenceRun.first == opening.first, fenceRun.count >= opening.count,
                   fenceSuffix.allSatisfy(\.isWhitespace) {
                    blocks.append(.code(code.joined(separator: "\n")))
                    code.removeAll(keepingCapacity: true)
                    fence = nil
                } else {
                    code.append(line)
                }
                continue
            }
            if content.first == ">" {
                if !prose.isEmpty {
                    blocks.append(.prose(prose.joined(separator: "\n")))
                    prose.removeAll(keepingCapacity: true)
                }
                let text = content.dropFirst()
                quote.append(text.first == " " ? text.dropFirst() : text)
                continue
            }
            if !quote.isEmpty {
                blocks.append(.quote(quote.joined(separator: "\n")))
                quote.removeAll(keepingCapacity: true)
            }

            let headingLevel = content.prefix(while: { $0 == "#" }).count
            let heading = (1...6).contains(headingLevel) && content.dropFirst(headingLevel).first?.isWhitespace == true
            let opensFence = fenceRun.count >= 3 && (fenceRun.first == "~" || !fenceSuffix.contains("`"))
            if content.allSatisfy(\.isWhitespace) || heading || opensFence {
                if !prose.isEmpty {
                    blocks.append(.prose(prose.joined(separator: "\n")))
                    prose.removeAll(keepingCapacity: true)
                }
                if heading {
                    blocks.append(.heading(headingLevel, String(content.dropFirst(headingLevel + 1))))
                }
                if opensFence { fence = fenceRun }
                continue
            }
            if let bullet = content.first, "-*+".contains(bullet), content.dropFirst().first?.isWhitespace == true {
                prose.append(prefix + "• " + content.dropFirst(2))
            } else {
                prose.append(String(line))
            }
        }
        if !prose.isEmpty { blocks.append(.prose(prose.joined(separator: "\n"))) }
        if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))) }
        if fence != nil { blocks.append(.code(code.joined(separator: "\n"))) }
        return blocks
    }
}
