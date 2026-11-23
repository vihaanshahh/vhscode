import SwiftUI

enum SourceLanguage: CaseIterable, Sendable {
    case swift, javascript, python, cStyle, shell, configuration, markdown, markup

    init?(path: String) {
        let name = (path as NSString).lastPathComponent.lowercased()
        switch (name as NSString).pathExtension {
        case "swift": self = .swift
        case "js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx": self = .javascript
        case "py", "pyi": self = .python
        case "c", "h", "cpp", "hpp", "cc", "cxx", "cs", "go", "java", "kt", "rs": self = .cStyle
        case "sh", "bash", "zsh", "fish", "command": self = .shell
        case "json", "jsonc", "code-workspace", "toml", "yaml", "yml", "ini": self = .configuration
        case "md", "markdown": self = .markdown
        case "html", "htm", "xml", "svg": self = .markup
        default:
            guard [".bashrc", ".zshrc", ".profile"].contains(name) else { return nil }
            self = .shell
        }
    }

    var name: String {
        switch self {
        case .swift: "Swift"
        case .javascript: "JavaScript / TypeScript"
        case .python: "Python"
        case .cStyle: "Source code"
        case .shell: "Shell"
        case .configuration: "Configuration"
        case .markdown: "Markdown"
        case .markup: "Markup"
        }
    }

    enum Token: Int, CaseIterable, Sendable {
        case comment, string, literal, keyword

        func color(_ palette: WorkbenchPalette) -> UInt32 {
            switch self {
            case .comment: palette.comment
            case .string: palette.literal
            case .literal: palette.success
            case .keyword: palette.keyword
            }
        }
    }

    private static let expressions = Dictionary(uniqueKeysWithValues: allCases.compactMap { language in
        let patterns = language.patterns.map { "(" + $0 + ")" }.joined(separator: "|")
        return (try? NSRegularExpression(pattern: patterns)).map { (language, $0) }
    })

    // Bound work on the editor's largest files and keep tokenization off the main actor.
    nonisolated func tokens(in text: String) async -> [(NSRange, Token)] {
        guard text.utf16.count <= 300_000, let expression = Self.expressions[self] else { return [] }
        var tokens: [(NSRange, Token)] = []
        expression.enumerateMatches(in: text, options: [.reportProgress],
                                    range: NSRange(text.startIndex..., in: text)) { result, _, stop in
            if Task.isCancelled {
                stop.pointee = true
                return
            }
            guard let result, let token = Token.allCases.first(where: {
                result.range(at: $0.rawValue + 1).location != NSNotFound
            }) else { return }
            tokens.append((result.range, token))
        }
        return tokens
    }

    private var patterns: [String] {
        if self == .markdown {
            return [#"<!--[\s\S]*?(?:-->|\z)"#, #"```[\s\S]*?(?:```|\z)|`[^`\r\n]+`"#, #"https?://[^\s)>]+"#, #"(?m)^ {0,3}#{1,6}[ \t]+[^\r\n]+"#]
        }
        if self == .markup {
            return [#"<!--[\s\S]*?(?:-->|\z)"#, #""[^"\r\n]*"|'[^'\r\n]*'"#, #"&[\w#]+;"#, #"</?[\w:-]+|/?>"#]
        }

        let slashComments = #"/\*[\s\S]*?(?:\*/|\z)|//[^\r\n]*"#
        let hashComments = #"#[^\r\n]*"#
        let quotes = #""(?:\\[^\r\n]|[^"\\\r\n])*+"|\'(?:\\[^\r\n]|[^'\\\r\n])*+'"#
        let doubleMultiline = #""""[\s\S]*?(?:"""|\z)"#
        let singleMultiline = #"\'\'\'[\s\S]*?(?:'''|\z)"#
        let comment: String
        let string: String
        let words: String
        switch self {
        case .swift:
            comment = slashComments
            string = doubleMultiline + "|" + quotes
            words = "actor as async await break case catch class continue defer deinit do else enum extension fallthrough fileprivate for func guard if import in init inout internal is let nil nonisolated open operator private protocol public repeat return self some static struct subscript super switch throw throws try typealias var where while"
        case .javascript:
            comment = slashComments
            string = #"`(?:\\[\s\S]|[^`\\])*+(?:`|\z)"# + "|" + quotes
            words = "as async await break case catch class const continue debugger declare default delete do else enum export extends finally for from function get if implements import in instanceof interface let new null of private protected public return satisfies set static super switch this throw try type typeof undefined var void while with yield"
        case .python:
            comment = hashComments
            string = doubleMultiline + "|" + singleMultiline + "|" + quotes
            words = "and as assert async await break case class continue def del elif else except finally for from global if import in is lambda match nonlocal not or pass raise return try while with yield"
        case .cStyle:
            comment = slashComments
            string = quotes
            words = "as auto bool break case catch char class const continue default defer do double else enum extern float fn for func foreach if impl import in inline int interface let long match mod namespace new null nullptr package private protected pub public return self short signed sizeof static struct switch template this throw trait try typedef union unsigned use using var virtual void volatile while"
        case .shell:
            comment = #"(?m)(?:^|(?<=[ \t;]))#[^\r\n]*"#
            string = quotes
            words = "case do done elif else esac export fi for function if in local readonly return set then until unset while"
        case .configuration:
            comment = slashComments + "|" + hashComments
            string = doubleMultiline + "|" + singleMultiline + "|" + quotes
            words = "null"
        case .markdown, .markup:
            return []
        }
        let number = #"(?<![\w.])(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d[\d_]*)?)(?!\w)"#
        let keywords = #"\b(?:"# + words.replacingOccurrences(of: " ", with: "|") + #")\b"#
        return [comment, string, number + #"|\b(?:true|false|True|False|None)\b"#, keywords]
    }
}

struct WorkbenchFileIcon: View {
    let path: String
    @Environment(\.workbenchPalette) private var palette

    var body: some View {
        let icon = switch SourceLanguage(path: path) {
        case .swift: ("swift", palette.caution)
        case .javascript: ("chevron.left.forwardslash.chevron.right", palette.caution)
        case .python, .cStyle: ("chevron.left.forwardslash.chevron.right", palette.tint)
        case .shell: ("terminal", palette.positive)
        case .configuration: ("gearshape", palette.tint)
        case .markdown: ("doc.text", palette.positive)
        case .markup: ("chevron.left.forwardslash.chevron.right", palette.negative)
        case nil: ("doc", Color.secondary)
        }
        Image(systemName: icon.0)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(icon.1)
            .frame(width: 14)
            .accessibilityHidden(true)
    }
}
