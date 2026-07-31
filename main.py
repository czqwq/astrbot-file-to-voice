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
    "1.0.0",
)
class FileToVoice(Star):
    """QQ 平台文件转语音插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)

        # 防御：确保 logger 可用（Star.__init__ 在某些加载路径下可能未设置）
        if not hasattr(self, "logger"):
            import logging
            self.logger = logging.getLogger("astrbot")

        # 等待状态： (session_id, sender_id) → 过期时间戳
        # 使用 session_id + sender_id 组合键，确保群聊中每个用户独立等待
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

        self._config_path: Path = Path(__file__).resolve().parent / "config.json"

    # ======================================================================
    # 生命周期
    # ======================================================================

    async def initialize(self) -> None:
        self._load_config()
        self.logger.info(
            "file_to_voice 已就绪 | trigger=%s | blacklist=%s | whitelist=%s | "
            "output=%s | timeout=%ds",
            self.trigger_words,
            self.blacklist_groups,
            self.whitelist_groups,
            self.output_format,
            self.waiting_timeout,
        )

    async def terminate(self) -> None:
        self.waiting_sessions.clear()
        self.logger.info("file_to_voice 已终止")

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
                # 同步 output_codec
                fmt = self.output_format.lstrip(".").lower()
                self.output_codec = _CODEC_MAP.get(fmt, "libmp3lame")
                self.logger.info("配置文件加载成功: %s", self._config_path)
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                self.logger.error("配置文件读取失败，使用默认配置: %s", exc)
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
            self.logger.info("已写入默认配置文件: %s", self._config_path)
        except OSError as exc:
            self.logger.error("写入配置文件失败: %s", exc)

    # ======================================================================
    # 权限校验
    # ======================================================================

    def _check_group_permission(self, group_id: str) -> bool:
        """检查群权限。

        规则（按优先级）：
        1. 私聊始终允许
        2. 黑名单优先 — 在黑名单中的群直接拒绝
        3. 白名单非空时 — 只有白名单中的群允许
        4. 白名单为空 — 所有群允许（除非被黑名单拦截）
        """
        if not group_id:
            return True  # 私聊
        if group_id in self.blacklist_groups:
            self.logger.info("群 %s 命中黑名单，已拒绝", group_id)
            return False
        if self.whitelist_groups and group_id not in self.whitelist_groups:
            self.logger.info("群 %s 不在白名单中，已拒绝", group_id)
            return False
        return True

    # ======================================================================
    # 平台断言
    # ======================================================================

    @staticmethod
    def _is_qq_platform(event: AstrMessageEvent) -> bool:
        """仅处理 aiocqhttp (OneBot V11 / QQ) 平台的消息。"""
        return event.get_platform_name() == "aiocqhttp"

    @staticmethod
    def _waiting_key(event: AstrMessageEvent) -> tuple[str, str]:
        """生成等待状态的键：(session_id, sender_id)。

        群聊中 session_id 相同但 sender_id 不同，确保每个用户独立等待；
        私聊中 sender_id 即 session_id，同样正确。
        """
        return (event.unified_msg_origin, event.get_sender_id())

    # ======================================================================
    # 核心逻辑 — 文件解析 & 转换
    # ======================================================================

    async def _resolve_file_path(self, comp: File | Image | Reply) -> str | None:
        """从消息组件获取本地文件路径。

        支持 File、Image，以及 Reply（遍历 chain 提取 File/Image）。
        """
        # ---- 展开 Reply ----
        if isinstance(comp, Reply):
            if not comp.chain:
                self.logger.warning("Reply 组件的 chain 为空")
                return None
            for sub in comp.chain:
                if isinstance(sub, (File, Image)):
                    comp = sub
                    break
            else:
                self.logger.warning("被引用消息中未包含 File 或 Image 组件")
                return None

        # ---- File 组件 ----
        if isinstance(comp, File):
            path = await comp.get_file()
            if path and os.path.exists(path):
                self.logger.info("File 下载完成: %s", path)
                return os.path.abspath(path)
            self.logger.warning("File.get_file() 返回空路径")
            return None

        # ---- Image 组件 ----
        if isinstance(comp, Image):
            try:
                path = await comp.convert_to_file_path()
                if path and os.path.exists(path):
                    self.logger.info("Image 解析完成: %s", path)
                    return os.path.abspath(path)
            except Exception as exc:
                self.logger.warning("Image.convert_to_file_path 失败: %s", exc)
            return None

        self.logger.warning("不支持的组件类型: %s", type(comp).__name__)
        return None

    async def _convert_to_audio(self, input_path: str) -> str | None:
        """使用 ffmpeg 将输入文件转为音频。

        Returns:
            输出音频文件的绝对路径，失败时返回 None。
        """
        suffix = f".{self.output_format.lstrip('.')}"
        fd, output_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)

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
                    "转换成功: %s → %s (%.1f KB)", input_path, output_path, size_kb
                )
                return output_path

            self.logger.error("ffmpeg 输出文件为空")
            return None

        except ffmpeg.Error as exc:
            stderr = (exc.stderr or b"").decode(errors="replace")[:500]
            self.logger.error("ffmpeg 转换失败: %s", stderr)
            self._remove_temp_file(output_path)
            return None
        except Exception as exc:
            self.logger.error("音频转换异常: %s", exc)
            self._remove_temp_file(output_path)
            return None

    @staticmethod
    def _remove_temp_file(path: str) -> None:
        """安全删除临时文件。"""
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
        """完整的 下载→转换→发送→清理 流程（async generator）。"""
        # 1. 下载 / 解析文件
        file_path = await self._resolve_file_path(comp)
        if not file_path:
            yield event.chain_result([
                Plain("❌ 未能获取文件，请确认引用的消息中包含文件或图片。"),
            ])
            return

        # 2. ffmpeg 转换
        audio_path = await self._convert_to_audio(file_path)
        if not audio_path:
            yield event.chain_result([
                Plain(
                    "❌ 文件转换为语音失败，"
                    "请检查文件是否损坏或格式是否受支持。"
                ),
            ])
            return

        # 3. 发送语音
        yield event.chain_result([
            Plain("🔊 正在发送语音…"),
        ])
        yield event.chain_result([
            Record.fromFileSystem(audio_path),
        ])

        # 4. 清理临时文件
        try:
            os.remove(audio_path)
        except OSError:
            pass

    # ======================================================================
    # Handler 1 — 触发命令（引用模式 / 等待模式入口）
    # ======================================================================

    @filter.command("转语音", alias={"/tovoice", "/2vc", "/转语音"})
    async def cmd_convert(self, event: AstrMessageEvent):
        """文件转语音指令。

        用法一（引用模式）：回复一条文件消息 + @机器人 /转语音
        用法二（等待模式）：@机器人 /转语音 → 收到提示后发送文件
        """
        if not self._is_qq_platform(event):
            return

        group_id = event.get_group_id()
        if not self._check_group_permission(group_id):
            yield event.plain_result("⛔ 该群不在本插件允许列表中。")
            return

        # ---------- 引用模式 ----------
        for comp in event.get_messages():
            if isinstance(comp, Reply) and comp.chain:
                if any(isinstance(s, (File, Image)) for s in comp.chain):
                    async for result in self._process_and_send(event, comp):
                        yield result
                    return

        # ---------- 等待模式 ----------
        key = self._waiting_key(event)
        self.waiting_sessions[key] = time.time() + self.waiting_timeout
        yield event.chain_result([
            Plain(
                f"📎 请在 {self.waiting_timeout} 秒内发送要转换的文件\n"
                "支持格式：mp4 / mp3 / wav / flac / ogg / jpg / png / gif …"
            ),
        ])

    # ======================================================================
    # Handler 2 — 等待模式下捕获文件
    # ======================================================================

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message_for_waiting(self, event: AstrMessageEvent):
        """当会话处于等待状态时，检查消息中是否包含文件并触发转换。"""
        key = self._waiting_key(event)

        # 快速路径：不在等待列表则直接返回
        if key not in self.waiting_sessions:
            return

        # 超时清理
        if time.time() > self.waiting_sessions[key]:
            del self.waiting_sessions[key]
            return

        if not self._is_qq_platform(event):
            return

        group_id = event.get_group_id()
        if not self._check_group_permission(group_id):
            del self.waiting_sessions[key]
            return

        # 查找 File / Image 组件
        for comp in event.get_messages():
            if isinstance(comp, (File, Image)):
                del self.waiting_sessions[key]  # 消费等待状态
                async for result in self._process_and_send(event, comp):
                    yield result
                event.stop_event()  # 阻止后续 handler（避免 cmd_convert 重复处理）
                return
