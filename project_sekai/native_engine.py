from __future__ import annotations

import importlib
import sys
from pathlib import Path

_module = None
_error = None


def available() -> bool:
    global _module, _error
    if _module is not None:
        return True
    try:
        directory = str(Path(__file__).resolve().parent / "native")
        if directory not in sys.path:
            sys.path.insert(0, directory)
        _module = importlib.import_module("maapjsk_native")
        return True
    except Exception as error:
        _error = f"{type(error).__name__}: {error}"
        return False


def unavailable_reason():
    return _error


def module():
    if not available():
        raise RuntimeError(f"Native 引擎不可用，请运行 scripts/build-native.ps1：{_error}")
    return _module


def minitouch_client():
    return module().MinitouchClient()


def parse_minitouch_log(line):
    return module().parse_minitouch_log(line)
