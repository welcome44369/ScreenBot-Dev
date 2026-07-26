from enum import Enum, auto


class AppState(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    RUNNING = auto()
    PAUSED = auto()
    RECORDING = auto()
    ERROR = auto()
    EXITING = auto()
