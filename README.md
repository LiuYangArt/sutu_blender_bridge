# Sutu Blender Bridge

用于将 Blender 视口画面快速发送到 Sutu 的桥接插件。

## 目标与平台

- 目标：把 Blender 当前视口或 Render Result 发送到 Sutu
- Blender 版本：5.0+
- 运行方式：插件通过 localhost TCP 与 Sutu 通信

## 安装方式

### 方式 1：从 Release 安装（推荐）

1. 在 [GitHub Releases](https://github.com/LiuYangArt/sutu_blender_bridge/releases) 下载 `sutu_blender_bridge-x.y.z.zip`
2. Blender -> `Edit` -> `Preferences` -> `Add-ons` -> `Install from Disk`
3. 选择下载的 zip 并启用插件
4. 在 3D 视图右侧侧栏的 `Sutu` 标签页找到 `Sutu Bridge` 面板

### 方式 2：开发目录安装

1. 把仓库目录加入 Blender 插件搜索路径或通过开发工具加载
2. 启用 `Sutu Blender Bridge`

## 使用说明

### 1. 连接 Sutu

1. 先启动 Sutu
2. Blender 侧栏打开 `Sutu Bridge`
3. 点击 `Connect` 建立连接

说明：
- 插件现在默认不会在 Blender 启动时自动连接
- 每次重启 Blender 后都需要手动点击 `Connect`

### 2. 实时推流视口

1. 点击 `Start Stream`
2. 在 Blender 中旋转/缩放视图，Sutu 会收到连续帧
3. 点击 `Stop Stream` 停止

### 3. 单帧发送

- `Send Viewport`：发送当前视口单帧
- `Send Render`：发送 Render Result

`Use Existing Render Result` 选项说明：
- 勾选：直接读取当前 Render Result，不触发新渲染
- 不勾选：先触发渲染，渲染完成后自动发送结果

### 4. 调试选项

- `Auto Install LZ4`：缺少依赖时尝试自动安装
- `Dump Frame Files`：导出采集帧与传输字节，便于排查问题
- `Dump Max Frames`：每次推流会话最多导出的帧数
- `Dump Directory`：调试文件输出目录

## 常见状态

- `Disabled`：连接已关闭（默认启动状态）
- `Listening`：等待连接
- `Connecting`：连接中
- `Handshaking`：握手中
- `Streaming`：连接可用，正在会话中
- `Recovering`：连接恢复中
- `Error`：发生错误

## 版本与发布

插件元数据采用单一来源：

- `addon_meta.json`：`id / name / version / tagline`

下列信息都由它驱动：

- `__init__.py` 中的 `bl_info`（名称、版本、描述）
- `bridge/client.py` 握手版本号
- `blender_manifest.toml`（通过脚本自动同步）

### 本地一键发布（Windows）

执行：

```bat
_dev\release.bat
```

不带参数时会弹出菜单，可直接选择 `patch / minor / major / 指定版本`。

也可以指定版本策略或显式版本：

```bat
_dev\release.bat minor
_dev\release.bat major
_dev\release.bat 0.3.0
```

`_dev\release.bat` 流程：

1. 自动更新版本号（`addon_meta.json`）
2. 同步 `blender_manifest.toml`
3. 提交并 push：`chore(release): vX.Y.Z`
4. 调用 GitHub Action：`release-addon.yml`
5. Action 生成 zip 并创建 GitHub Release

## 开发辅助命令

```bash
python tools/release.py print
python tools/release.py sync
python tools/release.py bump patch
python tools/release.py set 0.2.1
python tools/build_release_zip.py
```
