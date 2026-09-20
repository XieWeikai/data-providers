"""Cheap provider discovery; expensive libraries load only when a source is used."""

from dataclasses import dataclass
from pathlib import Path

from letools.source_providers.base import SourceProvider


@dataclass(frozen=True)
class TianjiConfig:
    task: str
    fps: int = 50
    gap_policy: str = "error"
    max_gap_ms: float = 100.0
    command_max_age_ms: float = 100.0
    gripper_max_gap_ms: float = 150.0
    state_resampling: str = "lowpass"
    preset: str = "thor-v1"
    trim_stationary: bool = True
    motion_joint_threshold_rad: float = 0.002
    motion_gripper_threshold: float = 0.01

    def __post_init__(self):
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("--task is required and must not be blank")
        object.__setattr__(self, "task", self.task.strip())
        if type(self.fps) is not int or not 1 <= self.fps <= 1000:
            raise ValueError("--fps must be an integer from 1 to 1000")
        if self.gap_policy != "error":
            raise ValueError("Episodes are never split; --gap-policy must be error for missing observations")
        if any(not 0 < v <= 1000 for v in (self.max_gap_ms, self.command_max_age_ms, self.gripper_max_gap_ms)):
            raise ValueError("Gap thresholds must be finite and in (0, 1000] ms")
        if self.state_resampling not in ("lowpass", "linear"):
            raise ValueError("--state-resampling must be lowpass or linear")
        if self.preset != "thor-v1":
            raise ValueError("Only the verified thor-v1 preset is supported")
        if type(self.trim_stationary) is not bool:
            raise ValueError("trim_stationary must be a boolean")
        if not 0 < self.motion_joint_threshold_rad <= 1 or not 0 < self.motion_gripper_threshold <= 1:
            raise ValueError("Motion thresholds must be finite and in (0, 1]")


class TianjiProvider(SourceProvider[TianjiConfig]):
    name = "tianji"
    api_version = 2
    config_type = TianjiConfig

    def add_arguments(self, parser):
        from argparse import BooleanOptionalAction
        parser.add_argument("--task", required=True, help="Task instruction stored in LeRobot metadata")
        parser.add_argument("--fps", type=int, default=50, help="Output FPS (default: 50)")
        parser.add_argument("--gap-policy", choices=("error",), default="error",
                            help="Missing observations fail explicitly; recordings are never split")
        parser.add_argument("--max-gap-ms", type=float, default=100.0,
                            help="Maximum feedback sample gap and video frame age (default: 100 ms)")
        parser.add_argument("--command-max-age-ms", type=float, default=100.0,
                            help="Joint command age before using aligned joint feedback (default: 100 ms)")
        parser.add_argument("--gripper-max-gap-ms", type=float, default=150.0,
                            help="Gripper command age before using aligned gripper feedback (default: 150 ms)")
        parser.add_argument("--state-resampling", choices=("lowpass", "linear"), default="lowpass")
        parser.add_argument("--trim-stationary", action=BooleanOptionalAction, default=True,
                            help="Trim stationary beginning/end only, using joint/gripper feedback (default: enabled)")
        parser.add_argument("--motion-joint-threshold-rad", type=float, default=0.002,
                            help="Joint displacement threshold for stationary edges (default: 0.002 rad)")
        parser.add_argument("--motion-gripper-threshold", type=float, default=0.01,
                            help="Normalized gripper displacement threshold for stationary edges (default: 0.01)")

    def config_from_args(self, args, context):
        return TianjiConfig(**{key: getattr(args, key) for key in (
            "task", "fps", "gap_policy", "max_gap_ms", "command_max_age_ms", "gripper_max_gap_ms", "state_resampling",
            "trim_stationary", "motion_joint_threshold_rad", "motion_gripper_threshold")})

    def open(self, source: Path, config: TianjiConfig):
        from .source import TianjiSource
        return TianjiSource(source, config)

    def distributed_spec(self, source, config):
        raise NotImplementedError("Distributed workers are not yet validated for tianji; use local conversion")


provider = TianjiProvider()
