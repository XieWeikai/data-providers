# 高频 wrench 窗口（0.6.0）

[使用手册](../README.md) · [全部参数](PARAMETERS.md) · [实现设计](DESIGN.md)

## 字段与频率

只输出一个力字段 `observation.wrench_windows`，每行形状 `[N,12]`，其中 `N=wrench_fps/fps`。不输出旧 `observation.wrench`，也不另存 wrench_history。数据集主时间轴、state、action、EEF 和视频仍按 `fps` 输出。

| 参数 | 默认 | 约束 |
|---|---:|---|
| `--fps` | 50 | 整数 1–1000 |
| `--wrench-fps` | 200 | 整数 1–1000，≥ fps，且为 fps 的整数倍 |

50/200 得到 `[4,12]`，50/1000 得到 `[20,12]`，100/200 得到 `[2,12]`，50/50 得到 `[1,12]`。50/199、50/25、30/200 都拒绝；30 Hz 主时间轴可以搭配 210 Hz wrench。没有 `--wrench-mode`，没有静默改频率或舍入窗口长度的行为。

第一轴是从旧到新的采样点；第二轴依次为左 Fx/Fy/Fz/Tx/Ty/Tz、右 Fx/Fy/Fz/Tx/Ty/Tz。力单位 N，力矩单位 N·m；各自仍处于 flange_L/flange_R 坐标系，不旋转到公共坐标系，也不减去重力或偏置。输出 float32。

## 时间窗口如何覆盖轨迹

在观测、辅助信号、视频交集与四路命令覆盖包络确定的候选区间 `[T0,T1]` 内，先构建唯一的高频网格：

```text
H[k] = T0 + round(k × 1e9 / wrench_fps)    （int64 纳秒）
W[i,j] = H[i × N + j]                    j=0,...,N-1
主帧时间 G[i] = W[i,N-1]
```

每个主帧携带截至它的 N 个高频点。默认 50/200，以 T0 为相对零点：

| 主帧绝对时间相对 T0 | 本行窗口相对 T0 | LeRobot timestamp |
|---:|---|---:|
| 15 ms | 0、5、10、15 ms | 0.00 s |
| 35 ms | 20、25、30、35 ms | 0.02 s |
| 55 ms | 40、45、50、55 ms | 0.04 s |

表中尚未执行首尾静止裁剪。裁剪按整行执行，同时裁剪所有数值和视频；保留的 wrench 窗口不能再单独错位。裁剪后第一个主帧重新记为 timestamp=0，窗口内更早的历史相对它为负数，这是当前决策的过去观测，不是跨 episode 取数。

所有窗口拼接后是一个连续高频网格，没有重复窗口或中间缺段。首帧需要 `(N-1)/wrench_fps` 秒历史，不能在 T0 伪造过去；末尾不足一个完整窗口的高频点不输出、不填充。至少两行才可转换。修改 wrench-fps 会改变窗口长度和首帧相位，因此也可能略微改变帧数、裁剪边界和检查结论；不会把其他字段的目标频率提高为 wrench-fps。

每个高频网格点独立整数舍入，不逐次累加近似周期；主帧从同一网格导出。非整纳秒周期仅有最多 1 ns 的舍入差异，不随轨迹长度漂移。

## 不滤波的具体含义

对每侧、每个 W[i,j]，选择该侧 `header.stamp <= W[i,j]` 的最后一条原始 wrench，直接复制六个测量分量（最终转 float32）。左右侧分别搜索自己的时间戳。

- 不调用 FIR、resample_poly、滑动均值或其他平滑。
- 不做线性或四元数等数值插值；不合成力测量。
- 不选择未来消息，即便未来那条距离更近。
- `--state-resampling lowpass/linear` 都不会改变 wrench 数值。该参数仍用于关节/夹爪反馈与 EEF 平移，其离线滤波并非因果处理；不能把整份数据集都称为因果预处理。

原始约 1,000 Hz 而目标 200 Hz 时，每 5 ms 只选择一条过去测量。**不滤波不等于不丢信息**：未选中的 1 ms 窄峰可能遗漏，高频成分可能混叠。提高到 1,000 Hz 可提高时间密度，但原始消息有抖动，仍可能重复选择上一条或跳过多条消息；这不是逐条原始消息归档。需要完全保留全部原始消息时应保留 MCAP。

## 缺口检查与审计

每个保留的高频点都必须有有效观测支撑。沿用 `--max-gap-ms`（默认 100 ms）：若目标点严格处于超过阈值的两个原始观测之间则拒绝；恰好命中真实消息的点允许。保持取样也不能绕过这个检查。诊断的 `affected_samples` 统计高频点，`affected_frames` 统计受影响的主行。

默认 100 ms 是既有通用观测容忍度，不代表 200 Hz 或 1,000 Hz 下严格保证每个周期都有新消息。需要更严格标准时可降低该阈值，但它同时作用于关节、夹爪反馈、EEF 和视频；当前没有单独的 wrench-gap 参数。

Features 保存以下说明，v2.1/v3.0 转换均保留：

```json
{
  "dtype": "float32",
  "shape": [4, 12],
  "names": null,
  "sampling_rate_hz": 200,
  "sampling_policy": "previous_raw_sample",
  "window_anchor": "last_sample",
  "filtered": false,
  "sample_offsets_seconds": [-0.015, -0.010, -0.005, 0.0]
}
```

另有 `component_names` 给出 12 个分量名称。外层 `fps` 仍是主行频率；某些后端添加的 feature.fps 也表示行频率，内部力采样率应读 `sampling_rate_hz`。

Prepare 缓存的 `alignment.npz` 额外保存：

- `wrench_time_ns`：`[行数,N]`，精确高频目标时间。
- `wrench_sample_time_ns`：`[行数,N,2]`，左右实际选中的源消息时间。

可用两者检查每个分量的采样年龄、重复取样和因果性。报告记录高频网格原点、首个保留窗口索引和各侧最大采样年龄。逐样本审计留在准备缓存，不自动复制到最终 LeRobot 目录；需要追溯请保留缓存和原始 MCAP。

## 存储与训练读取

Parquet 使用嵌套 fixed-size list 存 `[N,12]`，不展平。每个 episode 的 min/max/mean/std 按主行轴统计，形状同为 `[N,12]`，count 为主行数，不是高频点数。训练时可按该统计对各窗口位置归一化，或由训练端明确选择跨时间位置共用的通道统计。

一个普通 batch 的力数据形状为 `[B,N,12]`。训练模型需要显式读取 `observation.wrench_windows` 并配置时序编码器或展平输入；保存字段不意味着所有现成策略会自动使用它。若再读取多个主帧历史，可能增加一个主帧历史轴；不要将内层 N 误认为额外的动作决策帧。

Visualizer、Doctor、editor 的目标数据格式支持和模型输入支持是不同问题。CLI 转换和验证不保证任意外部 UI 都会将 `[N,12]` 正确画成高频曲线；正确的绘图时间应是主帧 timestamp 加 sample_offsets_seconds。

## 使用与迁移

```bash
tianji-provider check /path/to/recordings --fps 50 --wrench-fps 200
tianji-provider prepare /path/to/recordings /path/to/new-cache \
  --task 'Pick and place' --fps 50 --wrench-fps 200
letools convert /path/to/new-cache /path/to/new-lerobot \
  --source-format tianji --task 'Pick and place' \
  --fps 50 --wrench-fps 200 --to v2.1
```

实际批处理应使用使用手册中的 `&&` 链，在检查失败时停止。所有步骤保持相同配置。省略两个 FPS 参数即为 50/200。换用 1,000 Hz 时，三个步骤都指定 `--wrench-fps 1000`。

0.6.0 同时把夹爪 action 从 `/info/gripper_target_L/R` 改为 `/control/gripperValueL/R`。因此旧缓存 `tianji-thor-v1/v2/v3` 都不能复用，新格式为 `tianji-thor-v4`；必须从原始 MCAP 重建。旧的 50 Hz 力输出不能恢复新字段需要的高频信息。已有最终数据集保持原状，重新转换应指定新目录。
