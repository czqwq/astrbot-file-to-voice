# astrbot_plugin_file_to_voice

AstrBot QQ 平台文件转语音插件。

**功能**：将 QQ 聊天中的文件/图片转换为语音消息发送。

## 安装

1. 将插件文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 确保系统中已安装 **ffmpeg**：
   - Windows: `winget install ffmpeg` 或从 [ffmpeg.org](https://ffmpeg.org) 下载
   - Linux: `sudo apt install ffmpeg` (Debian/Ubuntu) 或 `sudo dnf install ffmpeg` (Fedora)
   - macOS: `brew install ffmpeg`
3. AstrBot 启动时会自动安装 Python 依赖（`ffmpeg-python`）

## 使用方法

### 方式一：引用模式

在 QQ 群/私聊中，**回复（引用）一条文件消息**，并发送触发指令（如 `/转语音`），机器人会自动下载被引用的文件并转为语音发送。

> 群聊中需要 @机器人 来唤醒：`@机器人 /转语音`

### 方式二：等待模式

1. 先发送触发指令（如 `/转语音`），机器人提示等待文件
2. 在超时时间内发送文件，机器人自动转换并发送语音

## 配置

首次运行后，插件目录下会生成 `config.json`，修改后**重载插件**即可生效：

```json
{
  "trigger_words": ["/转语音", "/tovoice", "/2vc"],
  "blacklist_groups": [],
  "whitelist_groups": [],
  "output_format": "mp3",
  "ffmpeg_path": "ffmpeg",
  "waiting_timeout": 60
}
```

| 配置项 | 说明 |
|--------|------|
| `trigger_words` | 触发词列表，在群聊中配合 @机器人 使用 |
| `blacklist_groups` | 群黑名单（群号），**优先级最高** |
| `whitelist_groups` | 群白名单（群号），为空则所有群允许 |
| `output_format` | 输出音频格式：`mp3` / `wav` / `amr` / `ogg` / `flac` / `aac` / `m4a` / `opus` |
| `ffmpeg_path` | ffmpeg 可执行文件路径，默认使用 PATH 中的 `ffmpeg` |
| `waiting_timeout` | 等待模式下等待文件的超时时间（秒） |

### 黑白名单规则

- 私聊始终允许
- **黑名单优先级 > 白名单**：在黑名单中的群无论如何都被禁止
- 白名单为空时，所有群允许（除非被黑名单拦截）
- 白名单非空时，仅白名单中的群允许

## 支持的输入格式

ffmpeg 支持的所有常见格式，包括但不限于：

- 视频：mp4、avi、mkv、mov、flv、webm …
- 音频：mp3、wav、flac、aac、ogg、wma、m4a …
- 图片：jpg、png、gif、bmp、webp …（提取其中的音频流，若图片无音频则转换失败）

## 依赖

### Python (requirements.txt)
- `ffmpeg-python >= 0.2.0`（ffmpeg 的 Python 封装）

### 系统
- **ffmpeg**（命令行工具，需单独安装）

## 平台支持

- ✅ QQ (OneBot V11 / aiocqhttp)
- ❌ 其他平台暂不支持
