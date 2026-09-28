"""Every workspace package may import only itself and the packages it declares as dependencies.

Scans all imports (including ones inside functions) so a package can't quietly reach into another."""

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]

# package folder -> packages it may import (itself is always allowed)
ALLOWED = {
    "core": set(),
    "mcp-servers": {"core"},
    "agents/common": {"core", "mcp-servers"},
    "agents/disruption-monitor": {"core", "agents/common"},
    "agents/route-optimizer": {"core", "agents/common"},
    "agents/vendor-negotiator": {"core", "agents/common"},
    "agents/status-analyst": {"core", "agents/common"},
    "agents/chat-assistant": {"core", "agents/common"},
    "app": {"core", "mcp-servers", "agents/common", "agents/disruption-monitor", "agents/route-optimizer",
            "agents/vendor-negotiator", "agents/status-analyst", "agents/chat-assistant"},
    "ui": {"core"},
}
AGENT_DIRS = {"common": "agents/common", "disruption_monitor": "agents/disruption-monitor",
              "route_optimizer": "agents/route-optimizer", "vendor_negotiator": "agents/vendor-negotiator",
              "status_analyst": "agents/status-analyst", "chat_assistant": "agents/chat-assistant"}


def owner(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != "oceanbridge" or len(parts) == 1:
        return None
    if parts[1] == "agents":
        return AGENT_DIRS.get(parts[2]) if len(parts) > 2 else None
    if parts[1] == "mcp_servers":
        return "mcp-servers"
    if parts[1] in ("runtime", "flow", "chat", "api"):
        return "app"
    if parts[1] == "ui":
        return "ui"
    return "core"


def imports(py: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}  # "from oceanbridge.agents import x"
    return found


def test_packages_only_import_their_declared_dependencies():
    violations = []
    for pkg, allowed in ALLOWED.items():
        for py in (PACKAGES / pkg / "src").rglob("*.py"):
            for mod in imports(py):
                target = owner(mod)
                if target and target != pkg and target not in allowed:
                    violations.append(f"{py.relative_to(PACKAGES)} imports {mod} (package {target})")
    assert not violations, "\n".join(violations)


def test_no_package_claims_the_shared_namespaces():
    for init in [PACKAGES.glob("*/src/oceanbridge/__init__.py"), PACKAGES.glob("*/*/src/oceanbridge/__init__.py"),
                 PACKAGES.glob("*/*/src/oceanbridge/agents/__init__.py")]:
        assert not list(init), "oceanbridge and oceanbridge.agents must stay namespace packages"
