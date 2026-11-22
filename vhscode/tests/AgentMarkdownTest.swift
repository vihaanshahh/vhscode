import Foundation

@main @MainActor
struct AgentMarkdownTest {
    static func main() {
        let source = """
        # **Heading**

        - *First item* with `inline()`
          - Nested item
        - [Documentation][docs]

        3. Ordered item

        > Quoted **answer**

        ```text
        **literal**
        ```

        | Name | Value |
        | --- | --- |
        | alpha | `beta` |

        [Blocked](file:///etc/passwd)

        [docs]: https://example.com
        """
        let blocks = AgentMarkdownDocument.parse(source)
        precondition(blocks.map { String($0.content.characters) } == [
            "Heading", "First item with inline()", "Nested item", "Documentation", "Ordered item",
            "Quoted answer", "**literal**\n", "", "Blocked"
        ])
        precondition(blocks[0].kind == .heading(1) && blocks[0].content.inlinePresentationIntent?.contains(.stronglyEmphasized) == true)
        precondition(blocks[1].marker == "•" && blocks[2].indent == 1)
        precondition(blocks[1].content.runs.contains { $0.inlinePresentationIntent?.contains(.code) == true })
        precondition(blocks[3].content.link?.absoluteString == "https://example.com")
        precondition(blocks[4].marker == "3." && blocks[5].quote)
        precondition(blocks[6].kind == .code && blocks[8].content.link == nil)
        precondition(blocks[7].rows.map { $0.map { String($0.characters) } } == [["Name", "Value"], ["alpha", "beta"]])
        precondition(String(AgentMarkdownDocument.preview(source).characters) == "Heading")
        precondition(AgentMarkdown.rendersEvent("info") && AgentMarkdown.rendersEvent("plan") && AgentMarkdown.rendersEvent("error"))
        precondition(!AgentMarkdown.rendersEvent("action") && !AgentMarkdown.rendersEvent("log"))

    }
}
