"""AstrBot QQ 平台文件转语音插件。

功能：
1. 【引用模式】回复（引用）一个文件消息，同时发送触发指令（如 /转语音），
   机器人下载被引用文件，用 ffmpeg 转为语音后发送。
2. 【等待模式】先发送触发指令进入等待，再发送文件，自动转换并发送语音。

特性：
- 可配置触发词（在 config.json 中修改）
- 群黑名单 / 白名单（黑名单优先）
- 基于 ffmpeg 的多格式支持（mp4/mp3/wav/flac/ogg/jpg/png 等）
- 仅支持 QQ (OneBot V11 / aiocqhttp) 平台
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import ffmpeg

from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File, Image, Plain, Record, Reply
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.event.filter import EventMessageType
except ImportError:
    from astrbot.core.star.filter.event_message_type import EventMessageType

# ==========================================================================
# 默认配置（config.json 不存在时自动创建）
# ==========================================================================
_DEFAULT_CONFIG: dict = {
    "trigger_words": ["/转语音", "/tovoice", "/2vc"],
    "blacklist_groups": [],
    "whitelist_groups": [],
    "output_format": "mp3",
    "ffmpeg_path": "ffmpeg",
    "waiting_timeout": 60,
    # ---------- 消息文案（设为 "" 或 false 可关闭对应消息） ----------
    "msg_processing": "🔊 正在转换并发送语音…",
    "msg_success": "",          # 发送成功后的提示，留空不发送
    "msg_failed": "❌ 文件转换为语音失败，请检查文件是否损坏或格式是否受支持。",
    "msg_no_file": "❌ 未能获取文件，请确认引用的消息中包含文件或图片。",
    "msg_blacklisted": "⛔ 该群不在本插件允许列表中。",
    "msg_waiting": "📎 请在 {timeout} 秒内发送要转换的文件（支持 mp4/mp3/wav/flac/jpg/png …）",
    "send_processing_msg": True,  # 是否发送「正在转换…」提示
    "send_success_msg": False,    # 转换完成后是否再发一条文字确认
}

# ffmpeg 输出格式 → 编码器 映射
_CODEC_MAP: dict[str, str] = {
    "mp3": "libmp3lame",
    "wav": "pcm_s16le",
    "amr": "libopencore_amrnb",
    "ogg": "libvorbis",
    "flac": "flac",
    "aac": "aac",
    "m4a": "aac",
    "opus": "libopus",
}


@register(
    "astrbot_plugin_file_to_voice",
    "czqwq",
    "将引用文件转为语音发送的QQ平台插件",
    "1.0.3",
)
class FileToVoice(Star):
    """QQ 平台文件转语音插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)

        # 防御：确保 logger 可用
        if not hasattr(self, "logger"):
            import logging
            self.logger = logging.getLogger("astrbot")

        # 等待状态： (session_id, sender_id) → 过期时间戳
        self.waiting_sessions: dict[tuple[str, str], float] = {}

        # 可配置项（由 _load_config 填充）
        self.trigger_words: list[str] = _DEFAULT_CONFIG["trigger_words"]
        self.blacklist_groups: list[str] = _DEFAULT_CONFIG["blacklist_groups"]
        self.whitelist_groups: list[str] = _DEFAULT_CONFIG["whitelist_groups"]
        self.output_format: str = _DEFAULT_CONFIG["output_format"]
        self.output_codec: str = _CODEC_MAP.get(
            self.output_format.lstrip(".").lower(), "libmp3lame"
        )
        self.ffmpeg_path: str = _DEFAULT_CONFIG["ffmpeg_path"]
        self.waiting_timeout: int = _DEFAULT_CONFIG["waiting_timeout"]

        # 消息文案
        self.msg_processing: str = _DEFAULT_CONFIG["msg_processing"]
        self.msg_success: str = _DEFAULT_CONFIG["msg_success"]
        self.msg_failed: str = _DEFAULT_CONFIG["msg_failed"]
        self.msg_no_file: str = _DEFAULT_CONFIG["msg_no_file"]
        self.msg_blacklisted: str = _DEFAULT_CONFIG["msg_blacklisted"]
        self.msg_waiting: str = _DEFAULT_CONFIG["msg_waiting"]
        self.send_processing_msg: bool = _DEFAULT_CONFIG["send_processing_msg"]
        self.send_success_msg: bool = _DEFAULT_CONFIG["send_success_msg"]

        self._config_path: Path = Path(__file__).resolve().parent / "config.json"

    # ======================================================================
    # 生命周期
    # ======================================================================

    async def initialize(self) -> None:
        self._load_config()
        self.logger.info(
            "[file_to_voice] 已就绪 trigger=%s blacklist=%s whitelist=%s "
            "output=%s timeout=%ds processing_msg=%s",
            self.trigger_words,
            self.blacklist_groups,
            self.whitelist_groups,
            self.output_format,
            self.waiting_timeout,
            self.send_processing_msg,
        )

    async def terminate(self) -> None:
        self.waiting_sessions.clear()
        self.logger.info("[file_to_voice] 已终止")

    # ======================================================================
    # 配置
    # ======================================================================

    def _load_config(self) -> None:
        """加载 config.json，不存在时生成默认文件。"""
        if self._config_path.exists():
            try:
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("config.json 顶层必须是 JSON 对象")
                for key in _DEFAULT_CONFIG:
                    if key in data:
                        setattr(self, key, data[key])
                fmt = self.output_format.lstrip(".").lower()
                self.output_codec = _CODEC_MAP.get(fmt, "libmp3lame")
                self.logger.info("[file_to_voice] 配置文件加载成功: %s", self._config_path)
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                self.logger.error("[file_to_voice] 配置读取失败，使用默认: %s", exc)
        else:
            self._save_config()

    def _save_config(self) -> None:
        """保存当前配置到 config.json。"""
        data = {k: getattr(self, k) for k in _DEFAULT_CONFIG}
        try:
            self._config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.logger.info("[file_to_voice] 已写入默认配置: %s", self._config_path)
        except OSError as exc:
            self.logger.error("[file_to_voice] 写入配置失败: %s", exc)

    # ======================================================================
    # 权限 & 平台
    # ======================================================================

    def _check_group_permission(self, group_id: str) -> bool:
        if not group_id:
            return True
        if group_id in self.blacklist_groups:
            self.logger.info("[file_to_voice] 群 %s 命中黑名单", group_id)
            return False
        if self.whitelist_groups and group_id not in self.whitelist_groups:
            self.logger.info("[file_to_voice] 群 %s 不在白名单", group_id)
            return False
        return True

    @staticmethod
    def _is_qq_platform(event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp"

    @staticmethod
    def _waiting_key(event: AstrMessageEvent) -> tuple[str, str]:
        return (event.unified_msg_origin, event.get_sender_id())

    # ======================================================================
    # 核心逻辑
    # ======================================================================

    async def _resolve_file_path(self, comp: File | Image | Reply) -> str | None:
        if isinstance(comp, Reply):
            if not comp.chain:
                self.logger.warning("[file_to_voice] Reply.chain 为空")
                return None
            for sub in comp.chain:
                if isinstance(sub, (File, Image)):
                    comp = sub
                    break
            else:
                self.logger.warning("[file_to_voice] 引用消息中无 File/Image")
                return None

        if isinstance(comp, File):
            self.logger.info("[file_to_voice] 开始下载 File: name=%s url=%s", comp.name, comp.url)
            path = await comp.get_file()
            self.logger.info("[file_to_voice] File.get_file() → %s", path)
            if path and os.path.exists(path):
                self.logger.info("[file_to_voice] File 就绪: %s (%d bytes)", path, os.path.getsize(path))
                return os.path.abspath(path)
            self.logger.warning("[file_to_voice] File.get_file() 返回空或文件不存在")
            return None

        if isinstance(comp, Image):
            self.logger.info("[file_to_voice] 开始解析 Image: url=%s", comp.url)
            try:
                path = await comp.convert_to_file_path()
                self.logger.info("[file_to_voice] Image.convert_to_file_path() → %s", path)
                if path and os.path.exists(path):
                    self.logger.info("[file_to_voice] Image 就绪: %s (%d bytes)", path, os.path.getsize(path))
                    return os.path.abspath(path)
            except Exception as exc:
                self.logger.warning("[file_to_voice] Image 解析失败: %s", exc)
            return None

        self.logger.warning("[file_to_voice] 不支持的组件: %s", type(comp).__name__)
        return None

    async def _convert_to_audio(self, input_path: str) -> str | None:
        suffix = f".{self.output_format.lstrip('.')}"
        fd, output_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self.logger.info(
            "[file_to_voice] ffmpeg: %s → %s (codec=%s)",
            input_path, output_path, self.output_codec,
        )

        try:
            stream = (
                ffmpeg.input(input_path)
                .output(
                    output_path,
                    vn=None,
                    acodec=self.output_codec,
                    ar=44100,
                    ac=1,
                    **{"b:a": "64k"},
                )
                .overwrite_output()
            )
            await asyncio.to_thread(
                stream.run, capture_stdout=True, capture_stderr=True
            )

            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                size_kb = os.path.getsize(output_path) / 1024
                self.logger.info(
                    "[file_to_voice] ffmpeg 完成: %.1f KB → %s", size_kb, output_path,
                )
                return output_path

            self.logger.error("[file_to_voice] ffmpeg 输出文件为空")
            return None

        except ffmpeg.Error as exc:
            stderr = (exc.stderr or b"").decode(errors="replace")[:500]
            self.logger.error("[file_to_voice] ffmpeg Error: %s", stderr)
            self._remove_temp_file(output_path)
            return None
        except Exception as exc:
            self.logger.error("[file_to_voice] ffmpeg 异常: %s", exc)
            self._remove_temp_file(output_path)
            return None

    @staticmethod
    def _remove_temp_file(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    async def _process_and_send(
        self,
        event: AstrMessageEvent,
        comp: File | Image | Reply,
    ):
        """完整的 下载→转换→发送→清理 流程。"""
        self.logger.info("[file_to_voice] === 开始处理 ===")

        # 1. 解析文件
        file_path = await self._resolve_file_path(comp)
        if not file_path:
            self.logger.warning("[file_to_voice] 解析失败，退出")
            if self.msg_no_file:
                yield event.plain_result(self.msg_no_file)
            return

        # 2. ffmpeg 转换
        audio_path = await self._convert_to_audio(file_path)
        if not audio_path:
            self.logger.error("[file_to_voice] 转换失败，退出")
            if self.msg_failed:
                yield event.plain_result(self.msg_failed)
            return

        audio_size = os.path.getsize(audio_path)
        self.logger.info("[file_to_voice] 准备发送语音: %s (%d bytes)", audio_path, audio_size)

        # 3. 发送提示（可配置关闭）
        if self.send_processing_msg and self.msg_processing:
            self.logger.info("[file_to_voice] 发送处理提示: %s", self.msg_processing)
            yield event.plain_result(self.msg_processing)
            # 给 QQ 协议端一点时间处理前一条消息
            await asyncio.sleep(0.3)

        # 4. 发送语音
        self.logger.info("[file_to_voice] 构造 Record 组件，开始发送…")
        record = Record.fromFileSystem(audio_path)
        self.logger.info(
            "[file_to_voice] Record: file=%s path=%s",
            record.file, record.path,
        )
        yield event.chain_result([record])
        self.logger.info("[file_to_voice] Record yield 完成")

        # 5. 成功确认（可配置）
        if self.send_success_msg and self.msg_success:
            await asyncio.sleep(0.3)
            yield event.plain_result(self.msg_success)

        # 6. 清理
        self._remove_temp_file(audio_path)
        self.logger.info("[file_to_voice] === 处理结束 ===")

    # ======================================================================
    # Handler 1 — 触发命令
    # ======================================================================

    @filter.command("转语音", alias={"/tovoice", "/2vc", "/转语音"})
    async def cmd_convert(self, event: AstrMessageEvent):
        """文件转语音指令。"""
        self.logger.info(
            "[file_to_voice] cmd_convert 触发 | sender=%s | group=%s | platform=%s",
            event.get_sender_id(), event.get_group_id(), event.get_platform_name(),
        )

        if not self._is_qq_platform(event):
            self.logger.info("[file_to_voice] 非 QQ 平台，跳过")
            return

        group_id = event.get_group_id()
        if not self._check_group_permission(group_id):
            self.logger.info("[file_to_voice] 权限拒绝 group=%s", group_id)
            if self.msg_blacklisted:
                yield event.plain_result(self.msg_blacklisted)
            return

        # 引用模式
        for comp in event.get_messages():
            if isinstance(comp, Reply) and comp.chain:
                if any(isinstance(s, (File, Image)) for s in comp.chain):
                    self.logger.info("[file_to_voice] 引用模式：检测到 Reply+File/Image")
                    async for result in self._process_and_send(event, comp):
                        yield result
                    return

        # 等待模式
        key = self._waiting_key(event)
        self.waiting_sessions[key] = time.time() + self.waiting_timeout
        self.logger.info("[file_to_voice] 进入等待模式 key=%s timeout=%ds", key, self.waiting_timeout)
        if self.msg_waiting:
            yield event.plain_result(
                self.msg_waiting.format(timeout=self.waiting_timeout),
            )

    # ======================================================================
    # Handler 2 — 等待模式下捕获文件
    # ======================================================================

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message_for_waiting(self, event: AstrMessageEvent):
        key = self._waiting_key(event)
        if key not in self.waiting_sessions:
            return

        if time.time() > self.waiting_sessions[key]:
            self.logger.info("[file_to_voice] 等待超时 key=%s", key)
            del self.waiting_sessions[key]
            return

        if not self._is_qq_platform(event):
            return

        group_id = event.get_group_id()
        if not self._check_group_permission(group_id):
            del self.waiting_sessions[key]
            return

        for comp in event.get_messages():
            if isinstance(comp, (File, Image)):
                self.logger.info("[file_to_voice] 等待模式：收到文件 key=%s", key)
                del self.waiting_sessions[key]
                async for result in self._process_and_send(event, comp):
                    yield result
                event.stop_event()
                return
