import AppKit
import SwiftUI

@MainActor
final class SourceBuffer: ObservableObject, Identifiable {
    nonisolated let path: String
    nonisolated var id: String { path }
    let storage: NSTextStorage
    @Published private(set) var isDirty = false
    @Published var isSaving = false
    @Published var diskRevision: String?
    var revision: String
    private var savedText: NSString

    init(path: String, text: String, revision: String) {
        self.path = path
        self.savedText = text as NSString
        self.revision = revision
        storage = NSTextStorage(string: text, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
            .foregroundColor: NSColor.textColor,
        ])
    }

    var text: String { storage.string }
    var name: String { (path as NSString).lastPathComponent }

    @discardableResult
    func updateDirty() -> Bool {
        let dirty = storage.length != savedText.length || !storage.mutableString.isEqual(savedText)
        guard dirty != isDirty else { return false }
        isDirty = dirty
        return true
    }

    func didSave(_ snapshot: String, revision: String) {
        savedText = snapshot as NSString
        self.revision = revision
        diskRevision = nil
        updateDirty()
    }

    func reload(_ contents: String, revision: String) {
        for layout in storage.layoutManagers {
            layout.textContainers.first?.textView?.undoManager?.removeAllActions()
        }
        storage.replaceCharacters(in: NSRange(location: 0, length: storage.length), with: contents)
        didSave(contents, revision: revision)
    }
}

@MainActor
struct PlainSourceView: NSViewRepresentable {
    let buffer: SourceBuffer
    let edited: () -> Void
    let save: () -> Void
    @Environment(\.workbenchPalette) private var palette
    @Environment(\.workbenchTypography) private var typography

    func makeCoordinator() -> Coordinator { Coordinator(buffer: buffer, edited: edited) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true
        let layout = NSLayoutManager()
        layout.allowsNonContiguousLayout = true
        let container = NSTextContainer(size: NSSize(width: CGFloat.greatestFiniteMagnitude,
                                                     height: CGFloat.greatestFiniteMagnitude))
        layout.addTextContainer(container)
        buffer.storage.addLayoutManager(layout)
        let editor = SourceTextView(frame: scroll.contentView.bounds, textContainer: container)
        editor.delegate = context.coordinator
        editor.textContainerInset = NSSize(width: 16, height: 12)
        editor.textContainer?.widthTracksTextView = false
        editor.textContainer?.containerSize = NSSize(width: CGFloat.greatestFiniteMagnitude,
                                                     height: CGFloat.greatestFiniteMagnitude)
        editor.minSize = .zero
        editor.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        editor.isVerticallyResizable = true
        editor.isHorizontallyResizable = true
        editor.autoresizingMask = [.width]
        editor.isRichText = false
        editor.allowsUndo = true
        editor.usesFindBar = true
        editor.isAutomaticQuoteSubstitutionEnabled = false
        editor.isAutomaticDashSubstitutionEnabled = false
        editor.isAutomaticTextReplacementEnabled = false
        editor.isAutomaticSpellingCorrectionEnabled = false
        editor.isContinuousSpellCheckingEnabled = false
        editor.isAutomaticLinkDetectionEnabled = false
        editor.setAccessibilityLabel("Editor for \(buffer.path)")
        editor.save = save
        scroll.documentView = editor
        let ruler = SourceLineRuler(scrollView: scroll, editor: editor)
        scroll.verticalRulerView = ruler
        scroll.hasVerticalRuler = true
        scroll.rulersVisible = true
        context.coordinator.ruler = ruler
        context.coordinator.editor = editor
        buffer.storage.delegate = context.coordinator
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.edited = edited
        guard let editor = scroll.documentView as? SourceTextView else { return }
        editor.save = save
        context.coordinator.setAppearance(palette, typography: typography)
    }

    static func dismantleNSView(_ scroll: NSScrollView, coordinator: Coordinator) {
        coordinator.highlightTask?.cancel()
        if coordinator.buffer.storage.delegate === coordinator { coordinator.buffer.storage.delegate = nil }
        guard let editor = scroll.documentView as? SourceTextView, let layout = editor.layoutManager else { return }
        editor.delegate = nil
        editor.undoManager?.removeAllActions()
        layout.textStorage?.removeLayoutManager(layout)
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate, NSTextStorageDelegate {
        let buffer: SourceBuffer
        private let language: SourceLanguage?
        var edited: () -> Void
        weak var editor: SourceTextView?
        weak var ruler: SourceLineRuler?
        var highlightTask: Task<Void, Never>?
        private var palette: WorkbenchPalette?
        private var typography: WorkbenchTypography?
        private var sourceAttributes: [NSAttributedString.Key: Any] = [:]

        init(buffer: SourceBuffer, edited: @escaping () -> Void) {
            self.buffer = buffer
            language = SourceLanguage(path: buffer.path)
            self.edited = edited
        }

        func setAppearance(_ palette: WorkbenchPalette, typography: WorkbenchTypography) {
            guard let editor, self.palette != palette || self.typography != typography else { return }
            let themeChanged = self.palette != palette
            self.palette = palette
            self.typography = typography
            ruler?.palette = palette

            let ink = WorkbenchPalette.color(palette.ink)
            let canvas = WorkbenchPalette.color(palette.canvas)
            editor.backgroundColor = canvas
            editor.enclosingScrollView?.backgroundColor = canvas
            editor.insertionPointColor = ink
            let highlight = WorkbenchPalette.color(palette.accent).blended(withFraction: 0.72, of: canvas) ?? canvas
            editor.selectedTextAttributes = [.backgroundColor: highlight, .foregroundColor: ink]

            let paragraph = NSMutableParagraphStyle()
            paragraph.lineSpacing = 3
            sourceAttributes = [
                .font: typography.native, .foregroundColor: ink, .paragraphStyle: paragraph,
            ]
            editor.defaultParagraphStyle = paragraph
            buffer.storage.setAttributes(sourceAttributes, range: NSRange(location: 0, length: buffer.storage.length))
            editor.typingAttributes = sourceAttributes
            if themeChanged { refreshHighlighting() }
        }

        nonisolated func textStorage(_ textStorage: NSTextStorage, willProcessEditing editedMask: NSTextStorageEditActions,
                                     range editedRange: NSRange, changeInLength delta: Int) {
            guard editedMask.contains(.editedCharacters), editedRange.length > 0 else { return }
            // AppKit's undo restores the attributes that were present before a theme or font change.
            MainActor.assumeIsolated {
                if !sourceAttributes.isEmpty { self.buffer.storage.setAttributes(sourceAttributes, range: editedRange) }
            }
        }

        nonisolated func textStorage(_ textStorage: NSTextStorage, didProcessEditing editedMask: NSTextStorageEditActions,
                                     range editedRange: NSRange, changeInLength delta: Int) {
            // SourceBuffer owns the storage and mutates it on the main actor.
            if editedMask.contains(.editedCharacters) { MainActor.assumeIsolated { refreshHighlighting() } }
        }

        private func refreshHighlighting() {
            guard let palette, let layout = editor?.layoutManager else { return }
            highlightTask?.cancel()
            let buffer = self.buffer
            let language = self.language
            highlightTask = Task { [weak layout, weak ruler] in
                guard (try? await Task.sleep(for: .milliseconds(120))) != nil else { return }
                ruler?.updateLines()
                guard let language else { return }
                let tokens = await language.tokens(in: buffer.text)
                guard !Task.isCancelled, let layout else { return }
                // Layout-only color keeps syntax out of undo, clipboard contents, and saves.
                layout.removeTemporaryAttribute(.foregroundColor, forCharacterRange: NSRange(location: 0, length: buffer.storage.length))
                let colors = SourceLanguage.Token.allCases.map { WorkbenchPalette.color($0.color(palette)) }
                for (range, token) in tokens {
                    layout.addTemporaryAttribute(.foregroundColor, value: colors[token.rawValue], forCharacterRange: range)
                }
            }
        }

        func textDidChange(_ notification: Notification) {
            if buffer.updateDirty() { edited() }
        }
    }

    final class SourceTextView: NSTextView {
        private let editorUndo = UndoManager()
        override var undoManager: UndoManager? { editorUndo }
        var save: (() -> Void)?

        override func performKeyEquivalent(with event: NSEvent) -> Bool {
            let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if modifiers == .command, event.charactersIgnoringModifiers == "s", let save {
                save()
                return true
            }
            return super.performKeyEquivalent(with: event)
        }
    }
}
