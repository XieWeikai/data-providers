"""Cheap provider discovery; expensive libraries load only when a source is used."""

from dataclasses import dataclass
from pathlib import Path

from letools.source_providers.base import SourceProvider


@dataclass(frozen=True)
class TianjiConfig:
    task: str
    fps: int = 50
    gap_policy: str = "split"
    max_gap_ms: float = 100.0
    gripper_max_gap_ms: float = 150.0
    state_resampling: str = "lowpass"
    preset: str = "thor-v1"

    def __post_init__(self):
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("--task is required and must not be blank")
        object.__setattr__(self, "task", self.task.strip())
        if type(self.fps) is not int or not 1 <= self.fps <= 1000:
            raise ValueError("--fps must be an integer from 1 to 1000")
        if self.gap_policy not in ("split", "error"):
            raise ValueError("--gap-policy must be split or error")
        if not 0 < self.max_gap_ms <= 1000 or not 0 < self.gripper_max_gap_ms <= 1000:
            raise ValueError("Gap thresholds must be finite and in (0, 1000] ms")
        if self.state_resampling not in ("lowpass", "linear"):
            raise ValueError("--state-resampling must be lowpass or linear")
        if self.preset != "thor-v1":
            raise ValueError("Only the verified thor-v1 preset is supported")


class TianjiProvider(SourceProvider[TianjiConfig]):
    name = "tianji"
    config_type = TianjiConfig

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True, help="Task instruction stored in LeRobot metadata")
        parser.add_argument("--fps", type=int, default=50, help="Output FPS (default: 50)")
        parser.add_argument("--gap-policy", choices=("split", "error"), default="split")
        parser.add_argument("--max-gap-ms", type=float, default=100.0)
        parser.add_argument("--gripper-max-gap-ms", type=float, default=150.0)
        parser.add_argument("--state-resampling", choices=("lowpass", "linear"), default="lowpass")

    def config_from_args(self, args, context):
        return TianjiConfig(**{key: getattr(args, key) for key in (
            "task", "fps", "gap_policy", "max_gap_ms", "gripper_max_gap_ms", "state_resampling")})

    def open(self, source: Path, config: TianjiConfig):
        from .source import TianjiSource
        return TianjiSource(source, config)

    def distributed_spec(self, source, config):
        raise NotImplementedError("Distributed workers are not yet validated for tianji; use local conversion")


provider = TianjiProvider()
