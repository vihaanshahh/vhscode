import AppKit

@MainActor
final class SourceLineRuler: NSRulerView {
    private weak var editor: NSTextView?
    var palette = WorkbenchTheme.midnight.palette { didSet { needsDisplay = true } }
    private var lineNumbers: [Int: Int] = [:]
    private let labelFont = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)

    init(scrollView: NSScrollView, editor: NSTextView) {
        self.editor = editor
        super.init(scrollView: scrollView, orientation: .verticalRuler)
        clipsToBounds = true
        clientView = editor
    }

    required init(coder: NSCoder) { fatalError("Use init(scrollView:editor:)") }

    func updateLines() {
        guard let editor else { return }
        let text = editor.string as NSString
        var index = 0
        var number = 1
        lineNumbers = [0: number]
        while index < text.length {
            var end = 0
            var contentsEnd = 0
            text.getLineStart(nil, end: &end, contentsEnd: &contentsEnd, for: NSRange(location: index, length: 0))
            if contentsEnd < end {
                number += 1
                lineNumbers[end] = number
            }
            index = end
        }
        let width = max(36, ceil(("\(number)" as NSString).size(withAttributes: [.font: labelFont]).width) + 20)
        if ruleThickness != width { ruleThickness = width }
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let rect = dirtyRect.intersection(bounds)
        NSGraphicsContext.saveGraphicsState()
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSBezierPath(rect: rect).addClip()
        WorkbenchPalette.color(palette.sidebar).setFill()
        rect.fill()
        WorkbenchPalette.color(palette.border).setFill()
        NSRect(x: bounds.maxX - 1, y: rect.minY, width: 1, height: rect.height).fill()
        guard let editor, let layout = editor.layoutManager, let container = editor.textContainer else { return }
        let origin = editor.textContainerOrigin
        let visible = editor.visibleRect
        let containerRect = NSRect(x: 0, y: visible.minY - origin.y, width: editor.bounds.width, height: visible.height)
        let glyphs = layout.glyphRange(forBoundingRectWithoutAdditionalLayout: containerRect, in: container)
        layout.enumerateLineFragments(forGlyphRange: glyphs) { [self] line, _, _, range, _ in
            let start = layout.characterIndexForGlyph(at: range.location)
            if let number = lineNumbers[start] { draw(number, at: origin.y + line.midY, in: editor) }
        }
        if let number = lineNumbers[(editor.string as NSString).length], layout.extraLineFragmentTextContainer === container {
            draw(number, at: origin.y + layout.extraLineFragmentRect.midY, in: editor)
        }
    }

    private func draw(_ number: Int, at y: CGFloat, in editor: NSTextView) {
        let center = convert(NSPoint(x: 0, y: y), from: editor).y
        let label = "\(number)" as NSString
        let attrs: [NSAttributedString.Key: Any] = [.font: labelFont, .foregroundColor: WorkbenchPalette.color(palette.comment)]
        let size = label.size(withAttributes: attrs)
        label.draw(at: NSPoint(x: bounds.maxX - size.width - 9, y: center - size.height / 2), withAttributes: attrs)
    }
}
