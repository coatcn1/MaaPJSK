# 第三方组件

本项目未以本文为整个仓库另选许可证；游戏名称、画面和商标仍属于各权利人。发布包中的 UI 模板只包含运行所需的小型按钮／文字裁剪，不包含玩家完整截图。

| 组件 | 固定来源 | 许可证 |
| --- | --- | --- |
| MFAAvalonia | https://github.com/SweetRian/MFAAvalonia ，v2.12.0（7cb1e404）；本项目设置页／更新 overlay | GPL-3.0，`licenses/MFAAvalonia-GPL-3.0.txt`；overlay 源码及构建脚本在本仓库公开 |
| MaaFramework / maafw | https://github.com/MaaXYZ/MaaFramework ，5.10.2 | LGPL-3.0，`licenses/MaaFramework-LGPL-3.0.txt`；动态库和 Python wheel 保留原上游来源 |
| MaaBanGDream 复用代码 | `third_party/maabangdream/` 中的来源说明 | PolyForm Noncommercial 1.0.0，`licenses/MaaBanGDream-PolyForm.txt` |
| minitouch | `project_sekai/native/vendor/minitouch/README.md` 中的来源与哈希 | Apache-2.0，`licenses/minitouch.txt` |
| PP-OCRv5 mobile recognition | https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec_onnx | Apache-2.0，`licenses/PaddleOCR-Apache-2.0.txt`；模型及字典 SHA256 见随包 NOTICE |
| CPython 与 Python 运行依赖 | `runtime-compatibility.json`、`requirements-runtime.txt` | 各自原许可证保留在便携运行环境的 dist-info／licenses 等目录；不是以本项目许可证重新授权 |
| .NET、Avalonia、SukiUI 与 MFA 其他依赖 | 固定 MFA 标签的 NuGet 依赖与源码 | 各自原许可证；发布构建保留上游目录及许可，不以项目名称替换权利人 |

Native 扩展、MaaFramework、MFA 的接口与授权边界独立。发行包不带下载谱面，用户通过谱面管理页独立同步。离线 OCR 在运行时不联网。
