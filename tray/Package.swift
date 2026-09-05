// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "StructorTray",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "StructorTray",
            path: "Sources/StructorTray"
        )
    ]
)
