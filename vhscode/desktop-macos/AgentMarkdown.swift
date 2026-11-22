import SwiftUI

struct AgentMarkdown: View, Equatable {
    let source: String
    var font: Font = .callout

    static func rendersEvent(_ kind: String) -> Bool {
        ["result", "info", "plan", "error", "risk"].contains(kind)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(AgentMarkdownDocument.parse(source)) { block in
                switch block.kind {
                case .code:
                    ScrollView(.horizontal) {
                        Text(verbatim: String(block.content.characters))
                            .font(font.monospaced())
                            .fixedSize(horizontal: true, vertical: true)
                            .padding(8)
                    }
                    .background(.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 5))
                case .heading(let level):
                    inline(block.content).font(level <= 2 ? .headline : font.weight(.semibold))
                        .accessibilityAddTraits(.isHeader)
                case .table:
                    ScrollView(.horizontal) {
                        Grid(alignment: .leading, horizontalSpacing: 13, verticalSpacing: 6) {
                            ForEach(block.rows.indices, id: \.self) { index in
                                GridRow {
                                    ForEach(block.rows[index].indices, id: \.self) { column in
                                        inline(block.rows[index][column])
                                            .font(index == 0 ? font.weight(.semibold) : font)
                                    }
                                }
                                if index == 0 { Divider() }
                            }
                        }.padding(6)
                    }
                case .rule:
                    Divider()
                case .paragraph:
                    paragraph(block)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    private func inline(_ value: AttributedString) -> Text {
        var content = value
        let codeRanges = content.runs.compactMap { run in
            run.inlinePresentationIntent?.contains(.code) == true ? run.range : nil
        }
        for range in codeRanges { content[range].font = font.monospaced() }
        return Text(content)
    }

    private func paragraph(_ block: AgentMarkdownDocument.Block) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 5) {
            if let marker = block.marker { Text(marker).accessibilityHidden(true) }
            inline(block.content)
        }
        .font(font)
        .padding(.leading, CGFloat(block.indent * 14 + (block.quote ? 9 : 0)))
        .overlay(alignment: .leading) {
            if block.quote { Rectangle().fill(.secondary.opacity(0.4)).frame(width: 2) }
        }
    }
}

enum AgentMarkdownDocument {
    struct Block: Identifiable {
        enum Kind: Equatable {
            case paragraph, heading(Int), code, rule, table
        }

        let id: Int
        let kind: Kind
        var content = AttributedString()
        var marker: String?
        var indent = 0
        var quote = false
        var rows: [[AttributedString]] = []
    }

    // The parser retains link labels but only makes web URLs clickable.
    static func parse(_ source: String) -> [Block] {
        let bounded = String(decoding: source.utf8.prefix(256 * 1024), as: UTF8.self)
        guard let formatted = try? AttributedString(markdown: bounded) else {
            return [Block(id: 0, kind: .paragraph, content: AttributedString(bounded))]
        }
        var blocks: [Block] = []
        var lastRow: Int?
        var markedItems = Set<Int>()
        for run in formatted.runs {
            let components = run.presentationIntent?.components ?? []
            var text = AttributedString(formatted[run.range])
            text.presentationIntent = nil
            if let link = run.link, !["http", "https"].contains(link.scheme?.lowercased() ?? "") {
                text.link = nil
            }
            if let table = components.last, case .table = table.kind,
               let leaf = components.first, case .tableCell(let column) = leaf.kind,
               components.count >= 3, (0..<120).contains(column) {
                if blocks.last?.id != table.identity {
                    blocks.append(Block(id: table.identity, kind: .table))
                    lastRow = nil
                }
                let index = blocks.count - 1
                let row = components[1].identity
                if lastRow != row {
                    blocks[index].rows.append([])
                    lastRow = row
                }
                let rowIndex = blocks[index].rows.count - 1
                while blocks[index].rows[rowIndex].count <= column {
                    blocks[index].rows[rowIndex].append(AttributedString())
                }
                blocks[index].rows[rowIndex][column].append(text)
                continue
            }
            let leaf = components.first
            let identity = leaf?.identity ?? 0
            if blocks.last?.id == identity {
                blocks[blocks.count - 1].content.append(text)
                continue
            }
            let kind: Block.Kind = switch leaf?.kind {
            case .header(let level): .heading(level)
            case .codeBlock: .code
            case .thematicBreak: .rule
            default: .paragraph
            }
            var block = Block(id: identity, kind: kind, content: text)
            var item: (id: Int, ordinal: Int)?
            var count = 0
            for component in components.dropFirst() {
                switch component.kind {
                case .blockQuote:
                    block.quote = true
                case .listItem(let ordinal):
                    if item == nil && count == 0 { item = (component.identity, ordinal) }
                case .orderedList, .unorderedList:
                    count += 1
                    if let item, markedItems.insert(item.id).inserted {
                        if case .orderedList = component.kind { block.marker = "\(item.ordinal)." }
                        else { block.marker = "•" }
                    }
                    item = nil
                default: break
                }
            }
            block.indent = max(0, count - (block.marker == nil ? 0 : 1))
            blocks.append(block)
        }
        return blocks
    }

    static func preview(_ source: String) -> AttributedString {
        for block in parse(source) {
            var content = block.rows.first?.first ?? block.content
            if !content.characters.isEmpty {
                content.link = nil
                return content
            }
        }
        return AttributedString()
    }
}
