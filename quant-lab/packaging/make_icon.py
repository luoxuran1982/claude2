"""用 Pillow 画应用图标，输出 icon.png / icon.ico / icon.icns 到 build/icon/。"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "build" / "icon"


def draw(size: int = 1024) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=13 * s, fill="#1d4ed8")
    for x, top, bottom, body_top, color in ((17, 16, 44, 22, "#ffffff"), (32, 12, 38, 17, "#fca5a5"), (47, 22, 50, 28, "#ffffff")):
        d.line([x * s, top * s, x * s, bottom * s], fill="#ffffff", width=int(2.4 * s))
        d.rounded_rectangle([(x - 4) * s, body_top * s, (x + 4) * s, (body_top + 14) * s], radius=1.5 * s, fill=color)
    d.line([10 * s, 52 * s, 24 * s, 44 * s, 36 * s, 47 * s, 54 * s, 32 * s], fill="#fbbf24", width=int(3 * s), joint="curve")
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
