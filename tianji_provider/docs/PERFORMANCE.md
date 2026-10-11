# 性能优化实验记录

每项优化按“方案 → 尝试 → profiling → 保真验证 → 接受/撤回”执行。接受门槛为同配置端到端墙钟时间至少降低 5%；微基准或单个函数加速不能单独作为接受依据。低于门槛的候选不保留实现，并记录失败结果。运行时间不包括事先已有缓存复用。

## 环境与固定对照输入（2026-10-11）

Marvin Thor，Linux aarch64，14 逻辑 CPU，专用 `/home/user/projects/letools-tianji/.venv`；LeTools `c20118b`，初始 provider HEAD `d32c412`。mcap 1.5.0、mcap-ros2-support 0.5.7、PyAV 16.1.0、NumPy 2.5.3。没有改变全局安装、机器人服务、原始数据或 LeTools 源码。

固定两条真实录制：`my_bag-26-10-09-16-24-47-3f581ff0`、`my_bag-26-10-10-11-32-06-8eed0a15`，来自 `/home/user/data_recorded/pick_and_place`，合计 182,759 条数值消息、919 个四宫格视频帧、输出 1,419 行与 8 段视频。默认主频 50 Hz、wrench 200 Hz，严格检查与静止裁剪开启，H.264 CRF 18/veryfast/yuv420p/无 B 帧。

使用实际 `data_web_platform.worker`，顺序执行 deep check → prepare → v2.1 convert → deep validation → publish，每次使用新的隔离目标。输出按值比较数值表、全部审计数组、数值与图像统计、诊断内容（只排除路径/耗时），并比较全部 H.264 编码包、PTS/DTS/时长及视频参数。源文件路径/大小/mtime 保持不变。OS 页缓存未清空；没有声称是冷盘或空载机器。

本次实验及 profiling 原始产物：`/home/user/tianji-optimization-20261011-We2Civ`。`benchmark.py` 支持 `--code CHECKOUT --source SELECTED --output NEW_RUN`，使用选定 provider checkout 和同一 LeTools 环境；`compare_runs.py REFERENCE CANDIDATE` 验证完整输出等价。每个运行目录含 worker.log、measurement.json、resources.json、cache、lerobot-v21。详细 profile 是独立诊断，不能把它自身开销当正常运行时间。

## 实验 1：schema 预编译解码——接受

瓶颈：stock ROS 解码器在每个嵌套消息调用 `type(...)`、构造 slots、生成 schema 描述字符串并做类型分支。此前两条数据的一轮数值读取约 9.5 秒，复杂类型读取函数调用 864,787 次；完整 check/prepare 又分别读取一次。

改动：新增 provider 内部 `CompiledDecoderFactory`。每个 schema 只创建一次消息类和字段读取计划，将类型判断移出逐消息热循环；继续使用 stock schema parser/CDR reader，完整解码 Header、辅助 topic 和其他字段。每个 MCAP 独立缓存 schema.id，不跨文件污染；不修改第三方全局函数。依赖私有解析接口，版本范围收窄至 `mcap-ros2-support>=0.5.7,<0.6`，升级必须重跑差分测试。

实验按 A-B-B-A 顺序执行，以减小负载/缓存顺序偏差：

| 版本 | 两次端到端时间 | 中位时间 | check 中位时间 | prepare 中位时间 |
|---|---|---:|---:|---:|
| d32c412 基线 | 89.096 / 87.872 s | 88.484 s | 15.587 s | 71.535 s |
| 仅解码优化 | 72.169 / 72.380 s | 72.275 s | 7.391 s | 63.519 s |

端到端时间降低 **18.32%**（吞吐约提高 22.43%），超过 5% 门槛；两次候选均快于两次基线。峰值进程树 RSS 从约 501–505 MB 降至 469–471 MB；采样 CPU 总时间从约 88–89 秒降至约 72.7 秒。画质、字段、采样率、检查和转换阶段均未删减。

验证：实机运行 `test_decoder.py`、`test_command_coverage.py`、`test_wrench_windows.py`，设置两条 `TIANJI_TEST_RECORDINGS`，**96 项通过**，包括两条录制的 stock/new `read_numeric` 全量差分。测试覆盖嵌套结构、各类数组、大小端、短负载、非有限数、空消息、递归 schema 和缓存隔离。两轮全部输出数值/审计/统计/编码包完全一致。函数级分析输出 `decoder-check.pstats`。

判定：接受，单独提交。这里只证明这两条样本的端到端提升；125 条目录的最终时间目标需后续全批实测。
