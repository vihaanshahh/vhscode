import SwiftUI

enum WorkbenchTheme: String, CaseIterable, Identifiable {
    case midnight = "Midnight"
    case graphite = "Graphite"
    case daylight = "Daylight"

    var id: Self { self }
    var scheme: ColorScheme { self == .daylight ? .light : .dark }

    var palette: WorkbenchPalette {
        switch self {
        case .midnight:
            WorkbenchPalette(canvas: 0x161D29, sidebar: 0x1C2635, panel: 0x1F2B3B, border: 0x35465B,
                             ink: 0xDFE9F5, accent: 0x8CB8FF, success: 0x8BDDAD, warning: 0xF2C78A,
                             failure: 0xFDA5AA, keyword: 0xD0AEFF, literal: 0xF2B596, comment: 0x91A7BF)
        case .graphite:
            WorkbenchPalette(canvas: 0x1C1F23, sidebar: 0x23272D, panel: 0x282E35, border: 0x3D464F,
                             ink: 0xE5E9ED, accent: 0x8BDBCC, success: 0xA9DA9F, warning: 0xF2CD92,
                             failure: 0xF5A4AC, keyword: 0xBFB7F7, literal: 0xDBCE9B, comment: 0x9CA9B5)
        case .daylight:
            WorkbenchPalette(canvas: 0xFCFAF5, sidebar: 0xEFECE3, panel: 0xF5F2EA, border: 0xD9D3C7,
                             ink: 0x282A33, accent: 0x245AA0, success: 0x226541, warning: 0x86521C,
                             failure: 0xA92D46, keyword: 0x6D369A, literal: 0x913819, comment: 0x5E6A73)
        }
    }
}

struct WorkbenchPalette: Equatable {
    let canvas: UInt32
    let sidebar: UInt32
    let panel: UInt32
    let border: UInt32
    let ink: UInt32
    let accent: UInt32
    let success: UInt32
    let warning: UInt32
    let failure: UInt32
    let keyword: UInt32
    let literal: UInt32
    let comment: UInt32

    var editor: Color { Color(nsColor: Self.color(canvas)) }
    var navigation: Color { Color(nsColor: Self.color(sidebar)) }
    var surface: Color { Color(nsColor: Self.color(panel)) }
    var separator: Color { Color(nsColor: Self.color(border)) }
    var tint: Color { Color(nsColor: Self.color(accent)) }
    var positive: Color { Color(nsColor: Self.color(success)) }
    var caution: Color { Color(nsColor: Self.color(warning)) }
    var negative: Color { Color(nsColor: Self.color(failure)) }
    var selection: Color { tint.opacity(0.15) }

    static func color(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: Double((hex >> 16) & 255) / 255,
                green: Double((hex >> 8) & 255) / 255,
                blue: Double(hex & 255) / 255, alpha: 1)
    }
}

enum WorkbenchCodeFont: String, CaseIterable, Identifiable {
    case system = "System Monospaced"
    case menlo = "Menlo"
    case monaco = "Monaco"

    var id: Self { self }

    func native(size: CGFloat) -> NSFont {
        (self == .system ? nil : NSFont(name: rawValue, size: size))
            ?? .monospacedSystemFont(ofSize: size, weight: .regular)
    }
}

struct WorkbenchTypography: Equatable {
    var family: WorkbenchCodeFont = .system
    var size = 13

    var native: NSFont { family.native(size: CGFloat(size)) }
    var code: Font { Font(native) }
}

private enum WorkbenchPaletteKey: EnvironmentKey {
    static let defaultValue = WorkbenchTheme.midnight.palette
}

private enum WorkbenchTypographyKey: EnvironmentKey {
    static let defaultValue = WorkbenchTypography()
}

extension EnvironmentValues {
    var workbenchPalette: WorkbenchPalette {
        get { self[WorkbenchPaletteKey.self] }
        set { self[WorkbenchPaletteKey.self] = newValue }
    }

    var workbenchTypography: WorkbenchTypography {
        get { self[WorkbenchTypographyKey.self] }
        set { self[WorkbenchTypographyKey.self] = newValue }
    }
}

struct WorkbenchSettings: View {
    @AppStorage("FleetTheme") private var theme = WorkbenchTheme.midnight
    @AppStorage("FleetCodeFont") private var family = WorkbenchCodeFont.system
    @AppStorage("FleetCodeFontSize") private var size = 13

    var body: some View {
        let palette = theme.palette
        Form {
            Section("Appearance") {
                Picker("Theme", selection: $theme) {
                    ForEach(WorkbenchTheme.allCases) { Text($0.rawValue).tag($0) }
                }
                Picker("Code font", selection: $family) {
                    ForEach(WorkbenchCodeFont.allCases) { Text($0.rawValue).tag($0) }
                }
                Stepper("Code font size: \(size) pt", value: $size, in: 10...24)
            }
            Section("Preview") {
                VStack(alignment: .leading, spacing: 5) {
                    Text("// Fleet Workbench").foregroundStyle(Color(nsColor: WorkbenchPalette.color(palette.comment)))
                    (Text("let ").foregroundColor(Color(nsColor: WorkbenchPalette.color(palette.keyword)))
                     + Text("workspace = ")
                     + Text("\"ready\"").foregroundColor(Color(nsColor: WorkbenchPalette.color(palette.literal))))
                        .foregroundStyle(Color(nsColor: WorkbenchPalette.color(palette.ink)))
                }
                .font(Font(family.native(size: CGFloat(size))))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(palette.editor, in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .formStyle(.grouped)
        .frame(width: 450)
        .preferredColorScheme(theme.scheme)
        .tint(palette.tint)
    }
}
