"""
secrets/ — netorch credential provider package.

Python stdlib has a module also named 'secrets'. We make our package
transparent to stdlib callers (like starlette) by re-exporting all
stdlib secrets symbols from our __init__.py using the builtins bypass.
"""
import sys
import importlib.util

# Load the stdlib 'secrets' module directly from its file path,
# bypassing sys.path so we don't recurse back into this package.
def _load_stdlib_secrets():
    import importlib.machinery
    # Find the real stdlib secrets spec by searching only stdlib paths
    for path in sys.path:
        # Skip our own package directory
        if path and __file__.startswith(path):
            continue
        spec = importlib.machinery.PathFinder.find_spec("secrets", [path])
        if spec and spec.origin and spec.origin != __file__:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None

_stdlib = _load_stdlib_secrets()
if _stdlib is not None:
    token_bytes   = _stdlib.token_bytes    # noqa: F401
    token_hex     = _stdlib.token_hex      # noqa: F401
    token_urlsafe = _stdlib.token_urlsafe  # noqa: F401
    choice        = _stdlib.choice         # noqa: F401
    randbelow     = _stdlib.randbelow      # noqa: F401
    randbits      = _stdlib.randbits       # noqa: F401
    SystemRandom  = _stdlib.SystemRandom   # noqa: F401
    compare_digest = _stdlib.compare_digest  # noqa: F401
    DEFAULT_ENTROPY = _stdlib.DEFAULT_ENTROPY  # noqa: F401
