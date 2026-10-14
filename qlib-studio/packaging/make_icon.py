"""用 Pillow 画应用图标（与 qlibstudio/web/icon.svg 同一图案：三根 K 线 + 一条收益曲线），
输出 icon.png / icon.ico / icon.icns 到 build/icon/。"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "build" / "icon"
BLUE, RED, GOLD = "#5aa0ff", "#ff6b5e", "#ffd166"


def draw(size: int = 1024) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 3 * s  # macOS 图标留边
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=14 * s, fill="#16213a")
    w = max(2, int(2 * s))
    for x, (top, bottom), (y0, y1), color in ((17, (22, 46), (28, 40), BLUE), (32, (16, 42), (20, 35), RED),
                                             (47, (12, 34), (15, 27), RED)):
        d.line([x * s, top * s, x * s, bottom * s], fill=color, width=w)
        d.rounded_rectangle([(x - 4) * s, y0 * s, (x + 4) * s, y1 * s], radius=1.5 * s, fill=color)
    pts = [(10 * s, 52 * s), (24 * s, 46 * s), (36 * s, 48 * s), (54 * s, 36 * s)]
    d.line(pts, fill=GOLD, width=max(3, int(3 * s)), joint="curve")
    return img


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    img = draw()
    img.save(OUT / "icon.png")
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(OUT / "icon.icns")
    return OUT


if __name__ == "__main__":
    print(main())
