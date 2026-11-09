"""Cheap provider discovery; expensive libraries load only when a source is used."""

from dataclasses import dataclass
from pathlib import Path

from letools.source_providers.base import SourceProvider


@dataclass(frozen=True)
class TianjiConfig:
    task: str
    fps: int = 50
    wrench_fps: int = 200
    gap_policy: str = "error"
    max_gap_ms: float = 100.0
    command_max_age_ms: float = 100.0  # Deprecated compatibility field; joint targets do not expire.
    gripper_max_gap_ms: float = 150.0
    state_resampling: str = "lowpass"
    preset: str = "thor-v1"
    trim_stationary: bool = True
    motion_joint_threshold_rad: float = 0.002
    motion_gripper_threshold: float = 0.01

    processing_mode: str = "standard"
    dagger_export: str = "annotate"
    dagger_stop_speed: float = .01
    dagger_velocity_limit: float = .02
    dagger_motion_speed: float = .035
    dagger_min_pause_s: float = .10
    dagger_max_pause_s: float = 5.
    dagger_position_range: float = .004
    dagger_feedback_gap_ms: float = 40.
    dagger_command_gap_ms: float = 150.
    dagger_hold_range: float = .001
    dagger_status_ttl_ms: float = 1500.
    dagger_exclude: str = "reviewed"
    dagger_exclusion_region: str = "command"
    dagger_min_segment_frames: int = 2
    dagger_reviews_json: str = "[]"

    def __post_init__(self):
        from .dagger import validate_config
        validate_config(self)
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("--task is required and must not be blank")
        object.__setattr__(self, "task", self.task.strip())
        if type(self.fps) is not int or not 1 <= self.fps <= 1000:
            raise ValueError("--fps must be an integer from 1 to 1000")
        if type(self.wrench_fps) is not int or not 1 <= self.wrench_fps <= 1000:
            raise ValueError("--wrench-fps must be an integer from 1 to 1000")
        if self.wrench_fps < self.fps or self.wrench_fps % self.fps:
            raise ValueError("--wrench-fps must be >= --fps and an integer multiple of --fps")
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

    def add_arguments(self, parser, *, task_required=True):
        from argparse import BooleanOptionalAction
        parser.add_argument("--processing-mode", choices=("standard", "dagger"), default="standard")
        parser.add_argument("--dagger-export", choices=("annotate", "expert_segments"), default="annotate")
        parser.add_argument("--dagger-exclude", choices=("none", "reviewed", "suspected"), default="reviewed")
        parser.add_argument("--dagger-exclusion-region", choices=("motion", "command"), default="command")
        parser.add_argument("--dagger-reviews-json", default="[]")
        from .dagger import DEFAULTS
        for name, value in DEFAULTS.items():
            if name in ("dagger_exclude", "dagger_exclusion_region", "dagger_reviews_json"): continue
            parser.add_argument("--"+name.replace("_", "-"), type=type(value), default=value)
        parser.add_argument("--task", required=task_required, default=None if task_required else "validation",
                            help="Task instruction stored in LeRobot metadata")
        parser.add_argument("--fps", type=int, default=50, help="Output FPS (default: 50)")
        parser.add_argument("--wrench-fps", type=int, default=200,
                            help="Unfiltered causal wrench sample rate; integer multiple of FPS, <=1000 (default: 200)")
        parser.add_argument("--gap-policy", choices=("error",), default="error",
                            help="Missing observations fail explicitly; recordings are never split")
        parser.add_argument("--max-gap-ms", type=float, default=100.0,
                            help="Maximum feedback sample gap and video frame age (default: 100 ms)")
        parser.add_argument("--command-max-age-ms", type=float, default=100.0,
                            help="Deprecated compatibility option; joint commands have no age limit")
        parser.add_argument("--gripper-max-gap-ms", type=float, default=150.0,
                            help="Maximum recorded gripper command age; stale/missing commands fail (default: 150 ms)")
        parser.add_argument("--state-resampling", choices=("lowpass", "linear"), default="lowpass",
                            help="Joint/gripper feedback and EEF translation resampling; never filters wrench")
        parser.add_argument("--trim-stationary", action=BooleanOptionalAction, default=True,
                            help="Trim stationary beginning/end only, using joint/gripper feedback (default: enabled)")
        parser.add_argument("--motion-joint-threshold-rad", type=float, default=0.002,
                            help="Joint displacement threshold for stationary edges (default: 0.002 rad)")
        parser.add_argument("--motion-gripper-threshold", type=float, default=0.01,
                            help="Normalized gripper displacement threshold for stationary edges (default: 0.01)")

    def config_from_args(self, args, context):
        return TianjiConfig(**{key: getattr(args, key) for key in (
            "task", "fps", "wrench_fps", "gap_policy", "max_gap_ms", "command_max_age_ms", "gripper_max_gap_ms", "state_resampling",
            "trim_stationary", "motion_joint_threshold_rad", "motion_gripper_threshold",
            "processing_mode", "dagger_export", "dagger_stop_speed", "dagger_velocity_limit", "dagger_motion_speed",
            "dagger_min_pause_s", "dagger_max_pause_s", "dagger_position_range", "dagger_feedback_gap_ms",
            "dagger_command_gap_ms", "dagger_hold_range", "dagger_status_ttl_ms", "dagger_exclude",
            "dagger_exclusion_region", "dagger_min_segment_frames", "dagger_reviews_json")})

    def open(self, source: Path, config: TianjiConfig):
        from .source import TianjiSource
        return TianjiSource(source, config)

    def distributed_spec(self, source, config):
        raise NotImplementedError("Distributed workers are not yet validated for tianji; use local conversion")


provider = TianjiProvider()
