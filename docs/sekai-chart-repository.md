# 日服在线谱面库

在 MFA 设置中显式增量维护日服库，显示本地数量，更新最新目录、新增或变化的谱面和封面；演出期间不下载资源。新歌曲上线后先停止演出并维护完整目录，正常任务不会自动联网补库，也不会通过降低身份或难度门槛猜测未收录歌曲。

## MFA 入口

设置 → 谱面管理 → **增量更新谱面**。需要提前复查近三个月的资源时，使用 **检查近期资源**。

显示已取得谱面的歌曲数、谱面数、封面数、错误数、更新时间，以及本次新增、更新、本地复用、联网未变和下载量。下载量统计校验通过的资源正文，不包含目录和错误响应。同步中显示逐首进度，支持取消。开始同步前须停止所有 MFA 演出任务，维护期间也会阻止新任务开始。取消会结束下载进程，保留已校验的文件和原来的完整 manifest，下次同步可继续复用。

界面使用固定 MFAAvalonia v2.12.0 构建，生成文件位于项目的 `.local/chart-settings/`。构建脚本导出上游源码到忽略目录，保留上游许可证；部署时验证通用 MFA Core 库的 SHA256。更新 MFA 后须重新确认兼容性并构建。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-chart-settings.ps1 -MfaSource "<MFA源码目录>" -RuntimeSource "<通用MFA目录>"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/launch-mfa.ps1
```

本机开发版已构建该页面。同步配置由启动脚本生成到部署目录的 `config/chart-sync.json`，指向项目的 `resource/charts`；部署不复制整库，不修改 PowerShell Profile 或 PATH。

## 来源与更新

- 歌曲和难度目录：[Sekai-World/sekai-master-db-diff](https://github.com/Sekai-World/sekai-master-db-diff)。读取公开 Git 引用，固定同一个提交，再读取该提交的 `musics.json` 和 `musicDifficulties.json`，避免两个目录混用不同版本；不依赖匿名 GitHub API 配额。
- 原始 SUS：[sekai.best 日服资源镜像](https://storage.sekai.best/sekai-jp-assets/music/music_score/0766_01/master.txt)。资源路径为 `music/music_score/<四位歌曲ID>_01/<difficulty>.txt`，原样保存为 `.sus`。
- 封面使用歌曲目录的 `assetbundleName`，路径为 `music/jacket/<封面资源名>/<封面资源名>.png`。这个字段是封面名，不能当作谱面目录名。

默认同步 Easy / Normal / Hard / Expert / Master / Append 中实际存在的谱面，跳过发布时间在未来的歌曲。元数据音符数和等级只用于记录，不等于已经解析了触控时序。

每次更新先取得当前目录，比较歌曲与难度元数据。默认增量处理规则：

- 新歌曲、新难度、缺失或本地校验损坏的文件会下载；已有文件每次都核对安全路径、大小、SHA256 和内容结构。
- 元数据未变、近期已确认的文件直接本地复用，不向镜像逐个发送请求。元数据明确变化时，只重新确认受影响的资源。
- 按歌曲的日服 `publishedAt` 计算三个自然月，边界日期按目标月份的末日截断，满三个月即停止复查。完整、未变资源不再定期或手动联网复查；缺失、损坏、此前失败或明确变化的资源仍会处理。
- 近三个月的文件，距上次远端成功确认满七天时自动复查；**检查近期资源** 可以提前复查。复用不会刷新远端确认时间。
- 旧版库从已校验的 master 快照生成比较指纹，复用原有文件；缓存记录丢失时可从完整索引恢复。取消前已下载成功的资源也按指纹续传。

联网复查使用 HTTP 条件请求：未变化返回 304，变化则下载并计算独立的 SHA256。ETag 只用于 HTTP 缓存，不能当作 SHA256。默认 6 个并发工作线程，包含超时、有限重试、响应类型和 SUS 结构检查。404 不重试；单个资源失败不会中断其余歌曲。目录未变化而镜像单独修订资源时，近期资源由复查发现；超过三个月的资源按用户要求不再自动发现这类修订。

目录或镜像可能比游戏更新晚。索引存在而资源返回 404 时会明确记录缺失；如果已有校验通过的旧文件，则保留并标记 `stale`，显示“未确认最新”，不会把它算作本次更新成功。

## 内容与布局

```text
resource/charts/
  manifest.json                    # 完整日服库索引
  manifest-selection.json          # 指定歌曲或难度的独立验证结果
  master/musics-<sha256>.json
  master/musicDifficulties-<sha256>.json
  sekai-jp/<歌曲ID>/<difficulty>-<sha256>.sus
  sekai-jp/<歌曲ID>/jacket-<sha256>.png
  .cache/<资源URL哈希>.json          # 元数据指纹、远端确认时间、续传及 HTTP 条件缓存记录
  .sync.lock                       # 操作系统维护锁
```

每首歌曲保存日文标题、读音、词曲作者、发行和公开时间、封面资源名；每张谱面保存歌曲 ID、难度、等级、元数据音符总数、原始响应 SHA256、来源 URL 和更新时间；每张封面保存路径、SHA256 和图像尺寸。原始 SUS 的 `#TITLE` 可能为空，标题使用 `manifest.json` 中的歌曲元数据。

文件以内容哈希命名，并采用临时文件与原子替换写入。更新、中断或取消不会改坏旧 manifest 引用的版本；全部处理完成后才原子替换完整 manifest。同步进程强制结束后操作系统会释放维护锁。指定少量歌曲的验证写入独立索引，不会缩小完整库。

整个 `resource/charts/` 都被 Git 忽略，不随源码上传。离线使用可通过 `ChartRepository.load_chart(song_id, difficulty)` 加载原始 SUS，再次检查路径、身份和 SHA256；这个读取入口不联网。[单人谱面演出](solo-chart-live.md) 使用该入口生成触控时序；原有自动演出仍使用游戏内 AUTO LIVE。

## 命令行

使用已安装 MaaPJSK 依赖的 Python；脚本按自身路径定位项目，不依赖当前目录：

```powershell
& "<python.exe路径>" "<项目路径>/scripts/sync_sekai_catalog.py"
& "<python.exe路径>" "<项目路径>/scripts/sync_sekai_catalog.py" --check-recent
& "<python.exe路径>" "<项目路径>/scripts/sync_sekai_catalog.py" --status
& "<python.exe路径>" "<项目路径>/scripts/sync_sekai_catalog.py" --song-ids 730 766 784
```

无参数时增量更新；`--check-recent` 联网检查近三个月的资源，和界面入口一致。保留命令行 `--force` 作为显式强制修复入口：忽略年龄及条件缓存，重新获取所有所选资源，不会由默认更新或界面自动调用；它与 `--check-recent` 互斥。`--workers`、`--timeout`、`--retries` 控制网络开销；`--difficulties` 可验证特定难度。`--status` 只读本地索引。退出码：0 完成，2 完成但有资源错误，1 目录或配置等致命错误，130 已取消。

## 本机验证结果

2026-10-03，固定目录版本 `5f1573048cc4be6da076150e240f76a08daa103a`：索引 727 首，跳过 3 首未公开歌曲，处理 724 首；取得 722 首歌曲的 3743 张谱面和 722 张封面，约 664 MiB。

两个目录保留的歌曲资源全部返回 404：241「アサガオの散る頃に」、290「どんな結末がお望みだい？」。各缺五种谱面及一张封面，共 12 个错误；没有把它们记成下载成功。

2026-10-04 增量改造后，固定目录版本 `f341f86d133043823b51c8b1dfecdc76497203cd`，真实更新约 20.2 秒：本地复用 4465 个资源，仅新增 774「ラビットホール」的五张谱面及封面，下载正文 1.23 MiB。当前取得 723 首歌曲的 3748 张谱面和 723 个封面；镜像仍有上述 12 个缺失。请求记录为 3 次目录请求、6 次新增资源下载和 12 次缺失资源重试，没有逐个请求已复用资源。

谱面库自动测试覆盖增量复用、新歌曲及难度、局部元数据变更、旧库迁移、七天复查、三个自然月边界、条件更新、远端修改、缓存损坏、错误响应、部分缺失、取消续传、并发锁、索引失败保护、离线读取及路径越界。MFA 设置页另有离屏测试，在内存中加载真实控件并验证两个更新模式的参数、按钮绑定、中文进度、子进程退出、演出时拒绝同步、维护时拒绝演出、取消及索引保护；没有桌面输入或 Maa 控制器。

```powershell
python -m unittest discover -s tests -v
dotnet run --project tests/MfaChartSettingsSmoke/MfaChartSettingsSmoke.csproj -c Release -r win-x64 -- "<python.exe路径>" ".local/chart-settings/settings-offscreen.png"
```

离屏测试依赖先运行 `build-chart-settings.ps1`。原始谱面、封面及目录快照保留各自的数据来源，不改变它们原有的内容权利。


### 2026-10-06 新发布歌曲目录维护

协力运行中的 767「デーモンロード」身份未确认来自旧目录未收录新歌，而非识别门槛故障。旧快照生成于 2026-10-05，准备页标题 OCR 已完整读取且置信度约 0.998，但本地候选目录没有该标题。保持封面、标题冲突、实际难度及首音安全门槛，不修改识别或同步算法。

停止并关闭 MFA 后执行正常完整增量维护，固定 master 版本 `9e053e3c2cc3e291d930c121919f1b291c1279fc`：远端索引 733 首，跳过 5 首未来发布歌曲，本地已公开目录记录从 727 增至 728，唯一新增为 767，零移除。新增五张谱面及一张封面，校验通过正文 1,033,580 字节，本地复用 4483 个资源。241／290 的既有 12 个 404 仍如实保留，新增目标没有资源错误。

767 EXPERT 为 Lv.25、956 音，本地 SUS 哈希校验、读取、解析、4414 个触控动作编译及生命周期验证通过。更新后原准备截图通过封面特征确认（136 个内点）、标题相似度 1.0 和实际 EXPERT 门槛；730／144 两张旧真实最终封面回放仍通过。尚无 767 清晰最终封面或本次实际输入验收，因此只能确认资源维护及原准备页身份恢复，不能称为演奏稳定或整曲验收。

后续新曲同样先在无演出任务时显式维护完整日服目录，核对新增歌曲封面、实际难度和 SUS 哈希，再开始任务。指定歌曲验证写入独立选择索引，不能代替完整目录维护。
