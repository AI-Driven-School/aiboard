// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AIBoard",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(url: "https://github.com/Lakr233/libghostty-spm.git", exact: "1.6.20260909"),
    ],
    targets: [
        .executableTarget(name: "AIBoard", dependencies: [.product(name: "GhosttyTerminal", package: "libghostty-spm")], path: "Sources/AIBoard"),
    ]
)
