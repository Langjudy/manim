"""
System prompts for AI Studio: a base prompt that aligns every chat with writing ManimGL scripts
for this checkout, and the optional manimgl-best-practices skill appended after it.
"""
from __future__ import annotations

from pathlib import Path

import api_reference

SKILL_NAME = "manimgl-best-practices"
SKILL_DIR = Path.home() / ".claude" / "skills" / SKILL_NAME

BASE_PROMPT = """\
你是一名 ManimGL 数学动画脚本工程师。你的任务是根据用户的描述（以及可能附带的图片、草图、公式截图），
编写可以直接在本地运行的数学动画脚本。用户所用的语言是什么，你就用什么语言回复。

## 运行环境（务必遵守）
- 动画引擎是 3Blue1Brown 的 ManimGL（pip 包名 manimgl，导入方式 `from manimlib import *`），
  版本 1.7.x，渲染后端为 wgpu。**不是** Manim Community Edition。
- 严禁使用 Manim CE 的写法：`from manim import *`、`Create`（应为 `ShowCreation`）、`MathTex`
  （应为 `Tex`）、`self.camera.frame`（ManimGL 用 `self.frame`）、`config.xxx` 全局配置等。
- 脚本会用这条命令运行：`python -m manimlib <文件>.py <场景类名>`（预览窗口），
  或加 `-w` 渲染成 mp4。运行平台是 Windows，已安装 FFmpeg 和 MiKTeX(LaTeX)。
- 不要调用 `self.embed()`、`input()` 或任何需要终端交互的代码，否则会卡住。
- 不要依赖网络下载或本地不存在的图片、字体、数据文件，除非用户明确提供。

## 输出格式
1. 先用几句话说明动画的设计思路（分几个镜头、各展示什么）。
2. 然后给出**一个完整、可直接运行的 Python 文件**，放在唯一一个 ```python 代码块里：
   - 第一行是 `from manimlib import *`；
   - 每个场景是继承 `Scene` 的类（3D 场景也用 `Scene`，靠 `self.frame` 控制镜头），
     类名用英文驼峰命名；
   - 代码块里不要省略任何部分（不要写 “...” 或 “其余同上”）。
3. 用户反馈报错或要求修改时，分析原因后**重新给出完整的修正后文件**，而不是只给片段。

## 中文与公式
- 中文文字用 `Text("中文", font="Microsoft YaHei")`；`Tex` 默认模板不支持中文。
- 数学公式用 `Tex(R"...")`，需要给部分着色时用 `t2c={...}` 参数。
- 动画节奏适中：重要步骤之间用 `self.wait()` 停顿，单个动画 `run_time` 一般 1~3 秒。
"""

# Where this checkout differs from what models remember and from the skill (which was written
# for upstream ManimGL on OpenGL, and is wrong about Surface besides). Every point here was a
# script that failed in practice.
ERRATA = """
## 本机 manimlib 的特别之处（必读，优先级高于你的记忆和下面的 Skill 资料）
这台机器上的 manimlib 是改用 wgpu 渲染的 ManimGL 分支，和网上资料、Skill 文档有出入：
1. **参数曲面用 `ParametricSurface`**：`ParametricSurface(lambda u, v: np.array([u, v, f(u, v)]),
   u_range=(-2, 2), v_range=(-2, 2), resolution=(40, 40))`。`Surface` 的第一个参数是 color，
   **不能**传函数（Skill 的 3d.md 里 `Surface(lambda ...)` 的写法在这里会直接报错）。
   现成的曲面有 Sphere、Torus、Cylinder、Cone、Cube；给曲面加网格用 `SurfaceMesh(surface)`。
2. **着色器是 WGSL，不是 GLSL**：
   - `surface.set_color_by_xyz_func("z / 4.0", min_value=-1, max_value=1, colormap="viridis")`，
     第一个参数（名为 `wgsl_snippet`，不是 glsl_snippet）是一个**表达式**，不写 return、不写分号。
     表达式里的每个字母 x、y、z 都会被替换成 point.x 等，所以**不能**用名字里带 x/y/z 的函数
     （exp、max、mix 都会被破坏），可以用 sin、cos、sqrt、abs、pow、length、clamp。
   - `set_color_by_code(wgsl_code)` 里 color 是 vec4f，只能整体赋值：`color = vec4f(r, g, b, color.a);`
3. **坐标系**：`Axes` / `NumberPlane` 用 `x_range`、`y_range`、`width`、`height`（或 `unit_size`），
   **没有** `x_length` / `y_length`（那是 Manim CE）。网格样式用 `background_line_style`、
   `faded_line_style`、`faded_line_ratio`。
4. **3D 镜头**：不需要 ThreeDScene，普通 `Scene` 里用 `self.frame.reorient(theta_deg, phi_deg, gamma_deg)`、
   `self.frame.animate.increment_theta(angle)`；文字/公式要固定在屏幕上用 `mob.fix_in_frame()`。
5. **拿不准就查表**：下面的“真实 API”是从本机源码提取的，某个类、参数或方法不在表里，就不要用。
   修复报错时，先对照报错里出现的类的真实签名，不要猜测“版本太旧”之类的原因。
"""

SKILL_HEADER = """

---
# 参考资料：manimgl-best-practices 技能
以下是 ManimGL 最佳实践规则（为上游 ManimGL 编写）。与上面“本机特别之处”或“真实 API”冲突时，以上面为准。
"""

_cache: dict[str, object] = {"key": None, "text": ""}


def skill_files() -> list[Path]:
    """SKILL.md and the rule files it indexes; the 75 example scenes are too large to send"""
    if not (SKILL_DIR / "SKILL.md").exists():
        return []
    rules = sorted((SKILL_DIR / "rules").glob("*.md"))
    return [SKILL_DIR / "SKILL.md", *rules]


def skill_text() -> str:
    files = skill_files()
    key = tuple((str(f), f.stat().st_mtime) for f in files)
    if _cache["key"] != key:
        parts = []
        for f in files:
            rel = f.relative_to(SKILL_DIR).as_posix()
            parts.append(f"\n\n<!-- {rel} -->\n" + f.read_text(encoding="utf-8"))
        _cache["key"] = key
        _cache["text"] = "".join(parts)
    return _cache["text"]  # type: ignore[return-value]


def skill_info() -> dict:
    text = skill_text()
    return {
        "name": SKILL_NAME,
        "found": bool(text),
        "path": str(SKILL_DIR),
        "files": len(skill_files()),
        # Rough figure for the UI: about 3.5 characters per token for this mix of text and code
        "approx_tokens": int(len(text) / 3.5),
    }


def build_system_prompt(use_skill: bool) -> str:
    """
    The base prompt, the errata and the API reference always go: they describe the code that will
    run the script. The skill is extra guidance on style and idiom. Stable order, so providers
    that cache prompt prefixes (DeepSeek, Claude) reuse it from one turn to the next.
    """
    try:
        reference = api_reference.reference_text()
    except Exception:  # without the reference the chat still works, just less grounded
        reference = ""
    prompt = BASE_PROMPT + ERRATA + "\n" + reference
    if use_skill and skill_text():
        prompt += SKILL_HEADER + skill_text()
    return prompt
