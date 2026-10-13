"""用 Pillow 画应用图标（与 svf/web/icon.svg 同一图案），输出 icon.png / icon.ico / icon.icns 到 build/icon/。"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "build" / "icon"


def draw(size: int = 1024) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4 * s, 4 * s, 60 * s, 60 * s], radius=13 * s, fill="#1f3a8a")
    d.rounded_rectangle([12 * s, 16 * s, 40 * s, 48 * s], radius=4 * s, fill="#ffffff")
    d.polygon([(22 * s, 25 * s), (22 * s, 39 * s), (33 * s, 32 * s)], fill="#1f3a8a")
    for i, alpha in enumerate((255, 190, 128)):
        y = (16 + 12 * i) * s
        d.rounded_rectangle([44 * s, y, 52 * s, y + 8 * s], radius=1.5 * s, fill=(245, 158, 11, alpha))
    return img


def main() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    img = draw()
    img.save(OUT / "icon.png")
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(OUT / "icon.icns")
    return OUT


if __name__ == "__main__":
    print(main())
