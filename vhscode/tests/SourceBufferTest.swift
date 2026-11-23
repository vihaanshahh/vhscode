import AppKit
import SwiftUI

@main
struct SourceBufferTest {
    @MainActor
    static func main() async throws {
        _ = NSApplication.shared
        let line = "let value = 1234567890\n"
        let original = String(repeating: line, count: 11_000)
        let buffer = SourceBuffer(path: "large.swift", text: original, revision: "a")
        var transitions = 0
        func source(_ theme: WorkbenchTheme, _ typography: WorkbenchTypography = WorkbenchTypography()) -> some View {
            PlainSourceView(buffer: buffer, edited: { transitions += 1 }, save: {})
                .environment(\.workbenchPalette, theme.palette)
                .environment(\.workbenchTypography, typography)
        }
        let host = NSHostingView(rootView: source(.midnight))
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
        try await Task.sleep(for: .milliseconds(20))
        let saved = buffer.text
        text.insertText("🙂", replacementRange: text.selectedRange())
        buffer.didSave(saved, revision: "b")
        precondition(buffer.isDirty && saved == original + String(repeating: "x", count: 100))
        text.undoManager?.undo()
        precondition(buffer.text == saved && !buffer.isDirty)
        buffer.reload("new🌳\n", revision: "c")
        precondition(text.string == "new🌳\n" && !buffer.isDirty && text.undoManager?.canUndo == false)
        print("Verified 100 native edits, one dirty notification, undo, in-flight save, and reload on a 247 KiB file.")

        func expectColor(_ color: UInt32, at index: Int) async throws {
            for _ in 0..<100 {
                if text.layoutManager?.temporaryAttribute(.foregroundColor, atCharacterIndex: index,
                                                          effectiveRange: nil) as? NSColor == WorkbenchPalette.color(color) { return }
                try await Task.sleep(for: .milliseconds(10))
            }
            preconditionFailure("Expected syntax color at UTF-16 offset \(index)")
        }

        let sample = "let tree = \"🌳\" // comment\n"
        buffer.reload(sample, revision: "d")
        try await expectColor(WorkbenchTheme.midnight.palette.keyword, at: 0)
        try await expectColor(WorkbenchTheme.midnight.palette.literal, at: 12)
        try await expectColor(WorkbenchTheme.midnight.palette.comment, at: 16)
        text.setSelectedRange(NSRange(location: 0, length: 3))
        text.insertText("var", replacementRange: text.selectedRange())
        text.breakUndoCoalescing()
        let selection = text.selectedRange()
        host.rootView = source(.daylight, WorkbenchTypography(family: .menlo, size: 17))
        try await expectColor(WorkbenchTheme.daylight.palette.keyword, at: 0)
        precondition(editor(in: host) === text && text.selectedRange() == selection)
        precondition(text.backgroundColor == WorkbenchPalette.color(WorkbenchTheme.daylight.palette.canvas))
        precondition((buffer.storage.attribute(.font, at: 0, effectiveRange: nil) as? NSFont)?.pointSize == 17)
        precondition(buffer.isDirty && buffer.text == "var" + String(sample.dropFirst(3)))
        text.undoManager?.undo()
        precondition(buffer.text == sample && !buffer.isDirty)
        precondition((buffer.storage.attribute(.font, at: 0, effectiveRange: nil) as? NSFont)?.pointSize == 17)
        text.insertText("42", replacementRange: NSRange(location: 0, length: 3))
        try await expectColor(WorkbenchTheme.daylight.palette.success, at: 0)
        print("Verified live theme and font changes, syntax recoloring, selection, and undo across appearance changes.")
    }
}
