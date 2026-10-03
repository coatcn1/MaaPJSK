# MaaBanGDream Native 组件来源

参考本地 MaaBanGDream `b45cb2e`（2026-10-03），仓库：
<https://github.com/coatcn1/MaaBanGDream>。

复用文件：`native/src/minitouch_client.cpp`、`minitouch_log.cpp`、对应头文件，
以及 `project_sekai/native_minitouch.py` 的设备生命周期实现。
版权与 PolyForm Noncommercial 1.0.0 许可证保留在本目录的 `LICENSE`。
`native/include/maabangdream/touch_script.hpp` 仅保留回读结构；PJSK 的 SUS 解析、
十二轨触点、Native 时间窗编译及校准配置由本项目适配。

`project_sekai/native/vendor/minitouch` 保留 EvATive7 的 Apache 2.0 许可证、
来源说明与 SHA-256。构建、部署不下载或依赖另一项目的运行目录。
