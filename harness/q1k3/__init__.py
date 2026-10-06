"""Runtime for playing a local WebGL game from a Harbor task."""

from .webgame import GameSpec, GameSession, HarnessError

__all__ = ["GameSession", "GameSpec", "HarnessError"]
