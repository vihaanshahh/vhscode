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
        editor.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        editor.textContainerInset = NSSize(width: 12, height: 10)
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
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.edited = edited
        guard let editor = scroll.documentView as? SourceTextView else { return }
        editor.save = save
    }

    static func dismantleNSView(_ scroll: NSScrollView, coordinator: Coordinator) {
        guard let editor = scroll.documentView as? SourceTextView, let layout = editor.layoutManager else { return }
        editor.delegate = nil
        editor.undoManager?.removeAllActions()
        layout.textStorage?.removeLayoutManager(layout)
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        let buffer: SourceBuffer
        var edited: () -> Void

        init(buffer: SourceBuffer, edited: @escaping () -> Void) {
            self.buffer = buffer
            self.edited = edited
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
