# -*- coding: utf-8 -*-
"""生成应用图标 PNG 和 ICO 文件。"""

from pathlib import Path
import math

from PIL import Image, ImageDraw


def create_icon(size: int = 256) -> Image.Image:
    """生成带渐变背景和媒体图案的图标。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 圆角矩形背景
    r = size // 8  # corner radius
    # 渐变填充：顶部 indigo → 底部深紫
    for y in range(size):
        t = y / size
        r_val = int(30 + (129 - 30) * (1 - t))  # #1e1e2e → #818cf8
        g_val = int(30 + (140 - 30) * (1 - t))
        b_val = int(46 + (248 - 46) * (1 - t))
        draw.rectangle([0, y, size, y + 1], fill=(r_val, g_val, b_val))

    # 绘制圆角遮罩
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, size - 1, size - 1], r, fill=255)
    img.putalpha(mask)

    # 重新获取 draw 用于前景绘制
    draw = ImageDraw.Draw(img)

    # 前景图案：播放三角形 + 胶片齿孔
    cx, cy = size // 2, size // 2
    scale = size / 256

    # 播放三角形
    tri_size = int(48 * scale)
    tri_x = cx - int(14 * scale)
    tri_y = cy - tri_size // 2
    draw.polygon([
        (tri_x, tri_y),
        (tri_x, tri_y + tri_size),
        (tri_x + tri_size, tri_y + tri_size // 2),
    ], fill=(205, 214, 244))  # light text color

    # 胶片齿孔 - 左右各 4 个
    hole_size = int(8 * scale)
    hole_offset = int(28 * scale)
    for i in range(4):
        hy = int(cy - tri_size * 0.8 + i * tri_size * 0.55)
        # 左侧
        draw.rounded_rectangle(
            [cx - hole_offset - hole_size, hy,
             cx - hole_offset + hole_size, hy + hole_size * 2],
            hole_size, fill=(205, 214, 244))
        # 右侧
        draw.rounded_rectangle(
            [cx + hole_offset - hole_size, hy,
             cx + hole_offset + hole_size, hy + hole_size * 2],
            hole_size, fill=(205, 214, 244))

    return img


def main() -> None:
    base = Path(__file__).parent

    # PNG 256x256
    icon = create_icon(256)
    png_path = base / "icon.png"
    icon.save(png_path, "PNG")
    print(f"已生成: {png_path}")

    # ICO (包含多尺寸)
    sizes = [16, 32, 48, 256]
    icons = [create_icon(s) for s in sizes]
    ico_path = base / "icon.ico"
    icons[0].save(ico_path, "ICO", sizes=[(s, s) for s in sizes],
                  append_images=icons[1:])
    print(f"已生成: {ico_path}")


if __name__ == "__main__":
    main()
