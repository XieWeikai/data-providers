"""Explicit preparation and alignment diagnostics for the external provider."""

import argparse
from pathlib import Path
import json

from .provider import provider


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tianji-provider")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read-only alignment audit; does not decode/encode full video")
    prepare = commands.add_parser("prepare", help="Prepare aligned Parquet/MP4 cache with one quad decode per bag")
    for command in (inspect, prepare):
        command.add_argument("source", type=Path)
        provider.add_arguments(command)
    inspect.add_argument("--report", type=Path, help="Write detailed JSON report to this explicit path")
    prepare.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        config = provider.config_from_args(args, None)
        if args.command == "prepare":
            from .prepare import prepare as run_prepare
            result = run_prepare(args.source, args.destination, config)
            print(json.dumps({"cache": str(args.destination), "episodes": len(result["episodes"]),
                              "frames": sum(e["length"] for e in result["episodes"]), "fps": config.fps}, ensure_ascii=False))
        else:
            from .source import TianjiSource
            source = TianjiSource(args.source, config, _collect_image_stats=False)
            result = {"fps": config.fps, "episodes": len(source.episodes), "frames": source.metadata.total_frames, "reports": source.reports}
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
            print(json.dumps({k: v for k, v in result.items() if k != "reports"}, ensure_ascii=False))
            for report in source.reports:
                print(f"{Path(report['bag']).name}: {report['kept_frames']}/{report['grid_frames']} frames, "
                      f"{len(report['segments'])} segments, video max age {report['video_max_age_ms']:.3f} ms")
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(2, f"tianji-provider: {error}\n")


if __name__ == "__main__":
    main()
