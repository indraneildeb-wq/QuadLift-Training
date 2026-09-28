from pathlib import Path

# agent_core/paths.py -> agent_core -> agent -> disruption-agent-system (project root)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def resolve(path_str: str) -> Path:
    """Resolve a config-file path relative to the project root, unless already absolute."""
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT_DIR / p)
