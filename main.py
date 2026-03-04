"""
自定义嘴替表情包插件 - 单文件版
配置通过 AstrBot WebUI 管理，支持远程图片URL
"""

import json
import asyncio
import hashlib
from urllib.parse import urlparse
import tempfile
from pathlib import Path
from typing import Optional

import aiohttp

from sketchbook import TextStyle, PasteStyle, DrawerRegion, TextFitDrawer

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools


PLUGIN_PATH = Path(__file__).parent


# ══════════════════════════════════════════════════════════════
#  图片缓存
# ══════════════════════════════════════════════════════════════

class ImageCache:
    """URL → 本地缓存文件"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _make_cache_name(self, url: str, real_url: str = "") -> str:
        """根据 URL 生成缓存文件名，优先用重定向后的真实 URL 提取扩展名"""
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        # 优先从真实URL取扩展名，其次从原始URL取
        for u in (real_url, url):
            if u:
                ext = Path(urlparse(u).path).suffix
                if ext and 1 < len(ext) <= 5 and ext[1:].isalpha():
                    return f"{h}{ext}"
        return f"{h}.png"

    def _find_existing(self, url: str) -> Optional[Path]:
        """查找已有缓存（不管扩展名）"""
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        for f in self.cache_dir.iterdir():
            if f.name.startswith(h):
                return f
        return None

    async def get(self, source: str) -> str:
        """URL或本地相对路径 → 可用的本地绝对路径"""
        if not source:
            raise FileNotFoundError("图片路径为空")

        # 本地路径
        if not source.startswith(("http://", "https://")):
            local = PLUGIN_PATH / source
            if local.exists():
                return str(local)
            raise FileNotFoundError(f"本地文件不存在: {local}")

        # 检查缓存
        existing = self._find_existing(source)
        if existing:
            return str(existing)

        # 下载（带 User-Agent + 跟随重定向）
        logger.info(f"下载图片: {source}")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                source,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"下载失败 HTTP {resp.status}: {source}")
                data = await resp.read()
                real_url = str(resp.url)

        if len(data) < 8:
            raise RuntimeError(f"下载的文件过小，可能不是有效图片: {source}")

        cache_name = self._make_cache_name(source, real_url)
        cached = self.cache_dir / cache_name
        cached.write_bytes(data)
        logger.info(f"已缓存: {cached.name} ({len(data)} bytes)")
        return str(cached)

    def clear(self):
        for f in self.cache_dir.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
        logger.info("图片缓存已清空")


# ══════════════════════════════════════════════════════════════
#  绘图
# ══════════════════════════════════════════════════════════════

def draw_sign(
    base_image: str,
    overlay_image: Optional[str],
    font: str,
    region: dict,
    text: str,
    text_color: tuple,
) -> bytes:
    """底图 + 文字 + 遮罩 → PNG bytes"""
    r = region
    drawer = TextFitDrawer(
        base_image=base_image,
        font=font,
        overlay_image=overlay_image if overlay_image else None,
        region=DrawerRegion(r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]),
    )
    return drawer.draw(
        text=text,
        style=TextStyle(color=tuple(text_color)),
    )


# ══════════════════════════════════════════════════════════════
#  插件主体
# ══════════════════════════════════════════════════════════════

@register(
    "custom_sign_meme",
    "YourName",
    "自定义嘴替举牌表情包插件",
    "1.0.0",
    "https://github.com/你的仓库"
)
class CustomSignPlugin(Star):
    """自定义嘴替表情包插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cache: Optional[ImageCache] = None

        # 运行时缓存的图片路径
        self._base_image: str = ""
        self._overlay_image: Optional[str] = None
        self._font: str = ""
        self._faces: dict[str, str] = {}
        self._text_color: tuple = (0, 0, 0, 255)

    # ── 生命周期 ─────────────────────────────────────────

    async def initialize(self):
        """初始化：下载/缓存图片"""
        data_dir = StarTools.get_data_dir("astrbot_plugin_mouthpiece")
        self.cache = ImageCache(data_dir / "image_cache")

        await self._load_images()

        char_name = self.config.get("character_name", "角色")
        logger.info(f"嘴替插件就绪 | 角色: {char_name} | 表情数: {len(self._faces)}")

    async def _load_images(self):
        """根据配置加载所有图片"""
        try:
            self._base_image = await self.cache.get(
                self.config.get("base_image_url", "assets/anan/base.png")
            )

            overlay_url = self.config.get("overlay_image_url", "")
            if overlay_url:
                self._overlay_image = await self.cache.get(overlay_url)
            else:
                self._overlay_image = None

            self._font = str(PLUGIN_PATH / self.config.get(
                "font_path", "assets/fonts/SourceHanSansSC-Bold.otf"
            ))

            # 文字颜色
            r = self.config.get("text_color_r", 0)
            g = self.config.get("text_color_g", 0)
            b = self.config.get("text_color_b", 0)
            self._text_color = (r, g, b, 255)

            # 表情
            faces_str = self.config.get("faces", "{}")
            try:
                faces_dict = json.loads(faces_str) if isinstance(faces_str, str) else faces_str
            except json.JSONDecodeError:
                logger.warning(f"faces 配置解析失败，使用空表情列表")
                faces_dict = {}

            self._faces = {}
            for name, source in faces_dict.items():
                try:
                    self._faces[name] = await self.cache.get(source)
                except Exception as e:
                    logger.warning(f"加载表情 '{name}' 失败: {e}")

        except Exception as e:
            logger.error(f"初始化图片失败: {e}")

    def _get_text_region(self) -> dict:
        """获取文字区域配置"""
        tr = self.config.get("text_region", {})
        return {
            "x": tr.get("x", 100),
            "y": tr.get("y", 432),
            "w": tr.get("w", 319),
            "h": tr.get("h", 204),
        }

    async def terminate(self):
        logger.info("嘴替插件已卸载")

    # ── 内部绘图 ─────────────────────────────────────────

    async def _generate_image(self, text: str, face: Optional[str] = None) -> bytes:
        """生成举牌图片 → PNG bytes"""
        base = self._faces.get(face, self._base_image) if face else self._base_image

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            draw_sign,
            base,
            self._overlay_image,
            self._font,
            self._get_text_region(),
            text,
            self._text_color,
        )

    # ── 指令：举牌说话 ──────────────────────────────────

    @filter.command("嘴替")
    async def handle_sign_says(self, event: AstrMessageEvent):
        """让角色举牌说话

        用法: 安安说 [文本] [表情]
        """
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            face_list = ", ".join(self._faces.keys()) or "无"
            cmd = self.config.get("command_name", "安安说")
            yield event.plain_result(f"用法: {cmd} [文本] [表情]\n可用表情: {face_list}")
            return

        content = parts[1].strip()
        face = None

        # 尝试从末尾提取表情
        last_space = content.rfind(" ")
        if last_space != -1:
            potential_face = content[last_space + 1:].strip()
            if potential_face in self._faces:
                face = potential_face
                content = content[:last_space]

        content = content.replace("\\n", "\n")

        try:
            image_bytes = await self._generate_image(content, face)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                f.write(image_bytes)
                temp_path = f.name
            try:
                yield event.image_result(temp_path)
            finally:
                Path(temp_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"生成举牌图片失败: {e}")
            yield event.plain_result(f"生成失败: {e}")

    # ── AI 工具 ──────────────────────────────────────────

    @filter.llm_tool(name="character_sign_meme")
    async def tool_sign_says(
        self,
        event: AstrMessageEvent,
        text: str,
        face: str = "",
    ) -> MessageEventResult:
        """生成角色举牌表情包。当你想用表情包强调语气、玩梗、吐槽、或表达强烈情绪时调用。

        Args:
            text(str): 写在牌子上的文字，简短有力，建议15字以内
            face(str): 表情名称，可选，不填则用默认表情
        """
        face = face.strip() if face else None
        try:
            image_bytes = await self._generate_image(text, face)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                f.write(image_bytes)
                temp_path = f.name
            try:
                # 文字 + 图片一起发，确保聊天历史里有文本
                yield event.plain_result(f"「{text}」")
                yield event.image_result(temp_path)
            finally:
                Path(temp_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"AI工具生成举牌图片失败: {e}")
            yield event.plain_result(f"表情包生成失败: {e}")

    # ── 管理指令 ─────────────────────────────────────────

    @filter.command("嘴替刷新")
    async def handle_refresh(self, event: AstrMessageEvent):
        """清空图片缓存并重新下载"""
        if self.cache:
            self.cache.clear()
        await self._load_images()
        yield event.plain_result(f"✅ 已刷新，当前 {len(self._faces)} 个表情可用")

    @filter.command("嘴替帮助")
    async def handle_help(self, event: AstrMessageEvent):
        """显示帮助"""
        name = self.config.get("character_name", "角色")
        faces = ", ".join(self._faces.keys()) or "无"
        yield event.plain_result(
            f"🎨 {name}嘴替插件\n\n"
            f"📖 /嘴替 [文本] [表情]\n"
            f"   可用表情: {faces}\n"
            f"   文本中 \\n 可换行\n\n"
            f"🔧 /嘴替刷新 - 更换图片后执行\n\n"
            f"🤖 AI会在合适时机自动举牌"
        )
