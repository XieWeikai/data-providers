# 验证记录

本文保留初始开发环境的历史验证结果。文中的 `../tmp`、绝对路径及测试脚本指开发机器上的本地产物，未随本仓库分发；不是克隆仓库后自带的测试资源。

验证日期：2026-09-20。环境：本机 macOS 26.1 / Apple Silicon，10 个逻辑 CPU，Python 3.12；LeTools 0.1.0、native 0.2.0、PyAV 16、SciPy 1.18.1、mcap 1.4.0。原始数据与缓存均在本机 Downloads 所在文件系统上。没有操作机器人，也没有改动 letools 源码。

## 全量时间与信号检查

`../tmp/all-bags-alignment.json` 保存全部 32 条 bag 的逐路报告：

- 32 条均成功读取，必需字段、关节名称、EEF/wrench 坐标系、夹爪标定检查通过。
- 默认 50 FPS，所有已录制 topic 与视频的时间交集内共 88,059 个网格点。
- 默认缺口处理保留 83,865 帧，分成 75 个连续 episode；排除 4,194 帧，包括不满足时效要求的点和不足两帧的孤立片段。
- 最大视频帧年龄为 99.422 ms，低于默认 100 ms 阈值；这是被选视频消息时间到目标网格的差，不是曝光同步精度。
- 本次全量扫描约 429 秒，其中部分时间与真实样本视频编码并行，不能据此推断独占吞吐。

这里只完成全量数值/时间扫描，以及每条视频开头的尺寸/解码检查；没有对全部 32 条做完整视频转换。原始数据没有被裁剪、改写或删除。

## 实际视频与 LeRobot 输出

样本：`my_bag-26-09-20-11-22-42-29fbb87d`，原始文件总计 163,618,525 字节。输出 3 个片段，帧数分别为 392、1123、284，总计 1,799 帧；每段均含四路 960×540 RGB 视频。

测试产物均在 `../tmp`：

- `real-cache-50/`：显式准备缓存，27,396,583 字节。
- `real-v21-50/`、`real-v30-50/`：两个版本的真实转换结果。
- `real-verification.json`：独立验证明细。

验证包括：

1. 两个版本 deep validation 均通过，零错误、零警告。
2. 表格数值、task、索引、各字段统计和缓存一致；所有输出时间点位于每个输入信号的首末时间之间。
3. 对全部 1,799 行，重新从原始 MCAP 做标量“最后一个不晚于目标时间的样本”查找，左右臂和左右夹爪 action 与转换结果逐值一致。
4. 完整解码 v2.1 的 12 段视频，逐帧验证帧数、尺寸和 50 FPS 呈现时间。
5. 完整解码 v3 的四路合并视频，与 v2.1 每帧 RGB 哈希比较，**全部逐像素一致**。
6. 各段每路相机首、中、末帧，与原始四宫格对应帧/区域比较，共 36 次。H.264 有损编码后，最大平均像素绝对误差为 2.093/255；未发现错相机、错帧或错误排序。

H.264 合并后 AVCC/Annex-B 编码包表示可能改变，因此 LeTools 的原始 packet digest 检查对本例 v3 报不一致；不能将这一检查当成解码图像不一致。这里保留该限制，并用完整解码后的逐像素比较验证。v2.1 与准备缓存的 packet digest 也一致。

样本的显式准备视频阶段为 43.25 秒，约 41.6 个输出时间点/秒（四路共约 166.4 帧/秒）。该时间包含视频解码、四路编码、完整像素统计和准备文件写入，不包含之前的数值读取。四个编码器各 1 线程，H.264 解码器 2 线程，逐 bag 处理。缓存写出 v2.1 为 2.73 秒、v3.0 为 3.06 秒，均设置 data workers=2、video workers=2；这些是一次本地测量，不是大规模性能保证。

另一次独立的数值/时间检查耗时 9.54 秒；通过 `resource.getrusage` 记录到进程峰值 RSS 为 542,572,544 字节（约 517 MiB）。该数值包含 Python/LeTools/SciPy 和解码读取的内存，不是视频编码阶段的峰值。

## 可控数据测试

`../tmp/test_tianji_alignment.py` 与 `../tmp/test_tianji_end_to_end.py` 最终共 **20 项通过**。完整合成轨迹位于 `../tmp/synthetic/`，测试产物在 `../tmp/pytest-final/`。

- 纳秒 epoch 精度、30/50/59/120/1000 Hz 网格无累计漂移。
- 交集为空、越界外推、倒退时间、重复时间保留最后一次更新。
- 命令不读取未来值，区分状态插值缺口与命令过期，连续片段切分。
- 137 Hz 分量降到 50 Hz 时被抑制，3 Hz 分量保持幅相；SLERP 正确跨越 ±180° 和四元数符号翻转。
- 必填 task、默认 FPS、非法配置、不可变配置、provider.open 不扫描/写文件、不同 provider 的 CLI 参数隔离。
- 真实 ROS 2 CDR 格式的合成 MCAP，反序关节名称、已知轨迹、已知接收延迟、四色相机区域；分别在 30/50/60 FPS 下转换 v2.1 与 v3.0 并验证数据和视频。
- 快速视频 CDR envelope 解析与 mcap-ros2-support 官方 schema decoder 逐包比较。
- 显式缓存重复使用、修改 task、FPS 不匹配、已有/不完整目录拒绝覆盖。

运行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/Users/xieweikai/Downloads/tianji_dataset/tmp \
  /Users/xieweikai/.local/share/uv/tools/letools/bin/python -m pytest \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_alignment.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_end_to_end.py \
  --basetemp=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-rerun \
  -o cache_dir=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-cache -q
```

## 原有转换路径回归

同一真实 v2.1 样本转换到 v3.0，2 个数据 worker / 2 个视频 worker。通过测试进程内过滤 entry-point 的方式模拟未加载插件基线，不修改安装包。每组 3 次，深度验证在计时区域外：

| 条件 | 第一次（含冷启动开销） | 后两次中位数 |
|---|---:|---:|
| 未发现 Tianji 插件 | 0.752 s | 0.0734 s |
| 已发现 Tianji 插件 | 0.732 s | 0.0703 s |

这个小样本没有观察到回归；毫秒级差异不足以说明性能提升。原始结果见 `../tmp/benchmark-baseline.json` 和 `../tmp/benchmark-plugin.json`。未执行分布式或集群吞吐测试。

最后检查 `/Users/xieweikai/other/letools` 的 Git 工作区无变化，HEAD 仍为 `44b71c999a12f7453e989e7536938a2e57d2a862`。插件通过独立安装包 entry-point 注册，`tianji-provider` 与 `letools` 均可从其他目录调用。
