# mcap-ops provider review

Compare against `0e9c6ac`, the snapshot of the deployed provider including its DAgger changes. The MCAP operator dependency is pinned by full Git revision in `pyproject.toml`. The plugin still exposes LeTools source API 2. Use separate environments: both package variants register the same `tianji` entry point.

| Stage | What the provider owns | What mcap-ops computes |
| --- | --- | --- |
| Time axes | Observations define the intersection; use the rational tick count, include the end only when reached, form whole high-rate windows and anchor the last point. | Rounded integer-nanosecond grid. |
| Source support | Check full feedback coverage before trimming; retained EEF/wrench points use bracket gaps; video uses past age. Joint targets can initialize a prefix from aligned feedback; gripper commands need a fresh recorded sample. | Original previous/next indices and exact-or-bounded-bracket mask. The provider distinguishes expired commands from missing prefixes. |
| Edge selection | 200 ms window; joint and gripper thresholds in their own units. Preserve existing diagnostic reasons and interior pauses. | Endpoint medians, strict displacement and adjacent-row bounds. |
| Continuous signals | Split at outages; interpolate each run onto its original median-rate grid, pad endpoint values, design the same Kaiser beta-5 kernel, interpolate back to target times. | Centered rational FIR with the same global linear boundary extension. Raw wrench and commands bypass the filter. |
| Orientation | Validate all source quaternion norms, use SciPy SLERP and enforce output sign continuity. | The existing generic near-angle interpolator uses a linear approximation; it is deliberately not substituted for this rotation contract. |
| Image statistics | Decode and select original RGB tiles, weight repeated frames, compute original moments/quantiles, encode with the existing recipe. | Exact integer counts from RGB8 pixels. OpenCV is no longer a provider dependency. |

ROS schemas and devices, clock checks, duplicate handling, fixed gripper normalization, DAgger supervision rules, rejected-recording policy, table/cache formats, encoder recovery and LeRobot conversion stay in their existing layers.

## Reproduce without a recording

Install the original and refactored provider checkouts into two distinct Python 3.12 environments with the same LeTools revision and codec/dependency versions. Use the provider's declared revision of mcap-ops on the refactored side; when using a locally built release wheel install that wheel first and build the provider with its dependencies already satisfied. Keep generated input and each output in fresh directories.

From the refactored provider directory, on Linux:

```bash
python benchmarks/operator_comparison/generate.py generated
TIANJI_TEST_RECORDINGS="$PWD/generated/generated-000" "$NEW_PYTHON" -m pytest -q tests
taskset -c "$CPU_SET" python benchmarks/operator_comparison/paired.py \
  "$OLD_PYTHON" "$NEW_PYTHON" generated comparison
```

The generator needs NumPy, PyYAML, PyAV (libx264) and mcap-ros2-support. The two recordings contain 6 s each of generated numeric CDR and 1920x1080 mosaics at 30 Hz; the provider uses its default 50 Hz rows and 200 Hz wrench grid. The comparison pins one provider worker and the software encoder on both sides. Validation and output fingerprinting run outside timed sections. All four paired blocks use fresh conversions with warm filesystem caches, in alternating old/new and new/old order. Each invocation imports the installed package in its own interpreter. Alignment excludes source reading and repeats its calculation ten times; direct DataSource reads include image statistics; checked preparation includes decoding, checking, statistics and encoding four camera streams into a new cache. Reopening an existing cache is excluded.

[Measured results and output equivalence](MCAP_OPS_RESULTS.md)
