"""trolobot — телеграм-бот-персонаж для одного чата."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("trolobot")
except PackageNotFoundError:
    __version__ = "0.0.0"
