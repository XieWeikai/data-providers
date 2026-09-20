# Tianji provider for LeTools

独立 Python 插件，读取 Thor 的 ROS 2 MCAP 轨迹，输出 LeTools `DatasetSource`，由原有后端生成 LeRobot v2.1 / v3.0。无需安装 ROS，不改动 letools 源码，也不会连接或控制机器人。

## 使用

`--task` 必填且不能为空；`--fps` 为目标数据集帧率，默认 **50**，接受 1–1000 的整数。

直接转换一条轨迹或整个轨迹目录：

```bash
letools convert \
  /path/to/recordings \
  /path/to/lerobot_v21 \
  --source-format tianji --task "将线缆插入接口" --fps 50 \
  --to v2.1 --workers 2 --video-workers 2
```

推荐大量数据使用显式准备缓存，减少重复解码和有损编码：

```bash
tianji-provider prepare \
  /path/to/recordings \
  /path/to/cache_50 \
  --task "将线缆插入接口" --fps 50

letools convert \
  /path/to/cache_50 \
  /path/to/lerobot_v21 \
  --source-format tianji --task "将线缆插入接口" --fps 50 \
  --to v2.1 --workers 2 --video-workers 2
```

准备过程对每个原始四宫格只解码一次，同时产生四路 H.264 MP4；LeTools 随后复用编码视频。缓存可重复使用，修改任务文本无需重新编码。改变 FPS 或对齐策略需要一个新缓存目录。已有目录不会自动覆盖；中断时没有完成标记的目录不会被误认为有效缓存。

直接转换模式完全只读，按需提供有界 JPEG `FrameSequence`，四路相机/不同片段会各自顺序解码原视频，适合少量数据。它计算视频统计时另有一次顺序解码，并有 JPEG 到目标编码的额外损耗。准备缓存模式绕过这一额外 JPEG 编码，适合正式训练数据。

先检查对齐，无需编码视频：

```bash
tianji-provider inspect \
  /path/to/recordings \
  --task "将线缆插入接口" \
  --report /path/to/tmp/alignment.json
```

## 字段契约：thor-v1

以下采用用户指定的 `observation.action` 和单数 `observation.image`，不会另加 `action` 或 `observation.images` 别名。使用默认期待这些标准名称的训练配置时，需要显式配置字段映射。

| 字段 | 形状 / 顺序 | 单位、含义 |
|---|---|---|
| `observation.state` | float32[16]：左关节 1–7、左夹爪、右关节 1–7、右夹爪 | 关节 rad；夹爪归一化电机行程 |
| `observation.action` | float32[16]，与 state 对应 | 左右最终关节目标 rad；实际应用的夹爪归一化目标 |
| `observation.wrench` | float32[12]：左 Fx/Fy/Fz/Tx/Ty/Tz、右相同 | N、N·m；分别在 flange_L / flange_R 中表达 |
| `observation.eef` | float32[14]：左 x/y/z/qx/qy/qz/qw、右相同 | m、单位四元数；base_link 坐标系下的反馈 FK 位姿 |
| `observation.image.head_left` | video[540,960,3]，RGB | 原始四宫格左上 |
| `observation.image.head_right` | 同上 | 右上 |
| `observation.image.left_wrist` | 同上 | 左下 |
| `observation.image.right_wrist` | 同上 | 右下 |

实际视频尺寸从输入读取，原始 1920×1080 四宫格分为四张 960×540 图。不额外翻转或重做去畸变，不裁掉原图烧录的时间文字。四宫格只有一个合成帧时间戳，不声称四颗传感器硬件曝光同步。

关节位置按 `/tj/joint_states.name` 重排，不能假设消息数组顺序恒定。A/B 命令来自 `/tj/control/joint_cmd_A/B`，对应左/右臂。夹爪 state 使用 `/info/gripper_feedback_L/R.data[0]`；action 使用 `/info/gripper_target_L/R`，而非可能尚未被驱动采用的 `/control/gripperValueL/R`。

夹爪标定从每条轨迹的 `/info/web_gripper.calibration` 读取，反馈转换公式为 `(motor_rad - min_rad) / (max_rad - min_rad)`。当前数据为两侧 `[0, 1.6]` rad，但代码不硬编码这一行程。反馈轻微超出 0–1 时保留原值；该量并非毫米开口宽度，也不假定其与指尖距离线性。标定缺失、中途变化、单位不同均拒绝转换。

## 时间对齐

1. 带 ROS Header 的信号使用 `header.stamp`；无 Header 的夹爪等消息使用 MCAP 记录时间。记录时间和 Header 相差超过 1 秒、时间倒退、视频 Header/记录时间不一致时拒绝转换，不静默切换时钟。
2. 边界取 **数据 MCAP 中所有有消息的 topic（当前 15 个，包含未输出的辅助 topic）及视频** 的交集：`T0 = max(first_timestamp)`，`T1 = min(last_timestamp)`。输出必需的信号缺失/为空会失败。
3. 全程使用 int64 纳秒；目标采样点为 `T0 + round(k × 1e9 / fps)`，逐点计算，避免将大 epoch 转 float 或累计舍入误差。最后一个采样点不得超过 T1。
4. 关节、夹爪反馈、wrench、EEF 平移默认先在各自连续区间内插值到中位采样率的规则网格，再用零相位 FIR `resample_poly` 抗混叠降采样，最后采到共同目标时间点。`--state-resampling linear` 可关闭低通，仅做分段线性插值。默认是离线重采样，低通和插值会用到相邻未来观测；它不是实时因果滤波器。
5. EEF 姿态使用最短路径 SLERP，并使输出四元数符号连续；不直接对四个分量线性插值或做普通低通。关节指令和夹爪目标使用最近一次 `timestamp <= t` 的命令，绝不使用未来指令或对指令做低通。
6. 视频同样取 `timestamp <= t` 的最近一帧，四路采用完全相同的原始帧索引。30→50 FPS 会重复画面；不会声称获得了 50 Hz 相机采样，也不会生成运动插值画面。
7. 默认命令有效年龄上限为 100 ms；夹爪目标上限为 150 ms（约 20 Hz 发布）。连续状态两端样本间隔超过 100 ms 时禁止跨越插值；视频超过 100 ms 没有新帧也判为无效。以上由 `--max-gap-ms`、`--gripper-max-gap-ms` 显式调整。
8. `--gap-policy split` 是默认值：从同一原始 bag 中拆出连续有效片段，每段至少两帧，各自成为 episode。不会把缺口两边拼成一段连续动作；单帧片段也计入丢弃数。`--gap-policy error` 则发现任意无效时间点就报错。命令停发可能表示 Idle，默认不推断 Idle 时最后一条命令能持续多久。

每段再统一生成从 0 开始的 `timestamp=k/fps`，`frame_index` 连续，`index` 全局连续。MP4 帧数严格等于表格行数；标准 N 帧视频显示时长为 N/fps，而最后一帧呈现时间是 (N−1)/fps。

对齐报告保存每路起止时间、交集、排除帧数、各信号缺口和输出片段。缓存每段的 `alignment.npz` 保存原始绝对时间、原始视频包索引、所选视频时间，可逐行追溯。报告随 `meta/info.json` 的 `tianji` 字段进入输出；完整逐行追溯数组保留在准备缓存中。

本次抽查的视频时间水印比视频消息时间晚约 8–23 ms。插件不会凭三条样本推测一个全局补偿量。没有 Header 的夹爪数据也仍含接收延迟；统一时间轴并不等于硬件同步或曝光时刻已校准。

## 验证与效率

数字统计基于最终 float32 输出逐列计算；图像统计用所有选中 RGB 像素的通道直方图计算，重复帧按实际次数计权，范围归一化到 0–1。这些是编码前图像统计，有损 MP4 解码结果会有微小差别。

数值 MCAP 每 bag 读取一次；数组重采样使用 NumPy/SciPy；只保留对齐后的低维数组，不保留全轨迹 RGB 图像。视频处理维持一个解码器和最多四个编码器，按流处理，并复用重复帧的统计计算。准备好的 source 按 episode 读取 Parquet，视频无需再次转码。插件提供稳定 locality/profile 和包含配置的 planner identity。分布式执行尚未验证，因此显式拒绝 `dist`，不宣称支持。

初始开发的测试文件和实际测试产物保存在开发机器的临时目录，没有随仓库分发。详见 [历史验证记录](VALIDATION.md)。

## 安装到已有 letools 环境

```bash
# 在 data-providers 仓库根目录运行；LeTools 已通过 uv tool 安装。
uv pip install --python "$(uv tool dir)/letools/bin/python" -e ./tianji_provider
letools providers list
```

`tianji-provider` 脚本安装在同一环境的 `bin` 中。若该命令不在 PATH 中，可以使用 `"$(uv tool dir)/letools/bin/tianji-provider"`，或者将该环境的 `bin` 加入 PATH。安装为 editable，修改本项目会立即生效。

## 实机依据与对齐参考

2026-09-20 通过 `ssh marvin` 只读确认：

- `marvin-thor`，ROS_DOMAIN_ID=24；A 命令的发布节点为 `joint_mux_node`，订阅者为 `marvin_robot_node`。
- `/opt/kernelmind/apex/install/marvin_msgs/share/marvin_msgs/msg/JointcmdArm.msg`：带 Header 的 7 维位置目标。
- `/opt/kernelmind/apex_tool/install/lib/python3.12/site-packages/dm_gripper_py/{DM_gripper,web_gripper}.py`：反馈电机角、归一化目标、实际应用目标及标定发布逻辑。检查时夹爪节点没有运行，因此标定以已录制消息为依据，不以当前默认参数代替。
- `/quad_csi_quickview` 实际参数、已安装 launch 的 CAMERA_SLOTS，以及本地参考目录的四宫格导出脚本共同确认相机顺序。
- 数据集内 JointState 名称、PoseStamped 的 base_link、WrenchStamped 的 flange_L/flange_R 均逐消息检查。wrench 保留原始发布含义，不宣称它是未经补偿的独立六维传感器读数。

方法参考：

- [ROS message_filters 官方文档](https://docs.ros.org/en/ros2_packages/rolling/api/message_filters/message_filters.html)：优先消息 Header 时间，避免把不确定接收延迟当成精确采样时间。不同频率的离线数据不能直接靠 ExactTime 配对，需要明确重采样与有效时间范围。
- [SciPy resample_poly](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html)：零相位低通 FIR 与多相重采样，用于降采样时控制混叠。
- [SciPy SLERP](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.transform.Slerp.html)：旋转最短路径插值。
- [LeRobot 数据集实现](https://github.com/huggingface/lerobot/blob/main/src/lerobot/datasets/lerobot_dataset.py)：帧时间戳与固定 FPS 的一致性要求。

参考文档本身不证明本机硬件同步；具体 topic 含义以 Thor 已部署代码及轨迹消息为准。
