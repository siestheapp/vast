from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_original() -> tuple[ModuleType, Path]:
    try:
        dist = importlib.metadata.distribution("mcp")
    except importlib.metadata.PackageNotFoundError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            "The 'mcp' package is required to use vast-mcp. Install model-context-protocol first."
        ) from exc

    package_root = Path(dist.locate_file("mcp"))
    init_path = package_root / "__init__.py"

    spec = importlib.util.spec_from_file_location(
        "_vast_mcp_original",
        init_path,
        submodule_search_locations=[str(package_root)],
    )
    if not spec or not spec.loader:  # pragma: no cover - defensive
        raise RuntimeError("Failed to locate the original 'mcp' package implementation.")

    module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    loader.exec_module(module)
    return module, package_root


_original_module, _original_path = _load_original()

# Ensure our package path is searched first, then fall back to the official SDK modules.
__path__ = [str(Path(__file__).resolve().parent), str(_original_path)]

# Mirror the public API of the original package.
__all__ = list(getattr(_original_module, "__all__", []))
for name in __all__:
    if name not in globals():
        globals()[name] = getattr(_original_module, name)

# Preserve common module attributes.
for attr in ("__doc__", "__annotations__", "__version__"):
    if hasattr(_original_module, attr):
        globals()[attr] = getattr(_original_module, attr)


def __getattr__(name: str):  # pragma: no cover - passthrough helper
    try:
        return getattr(_original_module, name)
    except AttributeError as exc:  # pragma: no cover - passthrough helper
        raise AttributeError(f"module 'mcp' has no attribute {name!r}") from exc


def __dir__() -> list[str]:  # pragma: no cover - passthrough helper
    return sorted(set(list(globals().keys()) + dir(_original_module)))


# Maintain reference so the interpreter can locate submodules during imports.
sys.modules.setdefault("_vast_mcp_original", _original_module)
