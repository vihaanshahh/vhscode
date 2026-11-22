import AppKit
import SwiftUI

@main
struct SourceBufferTest {
    @MainActor
    static func main() {
        _ = NSApplication.shared
        let line = "let value = 1234567890\n"
        let original = String(repeating: line, count: 11_000)
        let buffer = SourceBuffer(path: "large.swift", text: original, revision: "a")
        var transitions = 0
        let host = NSHostingView(rootView: PlainSourceView(buffer: buffer, edited: { transitions += 1 }, save: {}))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 400),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.displayIfNeeded()
        func editor(in view: NSView) -> NSTextView? {
            if let text = view as? NSTextView { return text }
            return view.subviews.lazy.compactMap { editor(in: $0) }.first
        }
        guard let text = editor(in: host) else { fatalError("Native editor missing") }
        precondition(text.textStorage === buffer.storage)
        precondition(text.layoutManager?.allowsNonContiguousLayout == true)
        window.makeFirstResponder(text)
        text.setSelectedRange(NSRange(location: buffer.storage.length, length: 0))
        for _ in 0..<100 { text.insertText("x", replacementRange: text.selectedRange()) }
        precondition(transitions == 1 && buffer.isDirty)
        text.breakUndoCoalescing()
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        let saved = buffer.text
        text.insertText("🙂", replacementRange: text.selectedRange())
        buffer.didSave(saved, revision: "b")
        precondition(buffer.isDirty && saved == original + String(repeating: "x", count: 100))
        text.undoManager?.undo()
        precondition(buffer.text == saved && !buffer.isDirty)
        buffer.reload("new🌳\n", revision: "c")
        precondition(text.string == "new🌳\n" && !buffer.isDirty && text.undoManager?.canUndo == false)
        print("Verified 100 native edits, one dirty notification, undo, in-flight save, and reload on a 247 KiB file.")
    }
}
