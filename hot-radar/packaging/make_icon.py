"""用 Pillow 画应用图标（与 hotradar/web/icon.svg 同一图案），输出 icon.png / icon.ico / icon.icns 到 build/icon/。"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "build" / "icon"


def draw(size: int = 1024) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 3 * s  # macOS 图标留边
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=14 * s, fill="#1d2330")
    cx, cy = 32 * s, 34 * s
    for r, alpha in ((21, 115), (13.5, 180), (6, 255)):
        d.ellipse([cx - r * s, cy - r * s, cx + r * s, cy + r * s], outline=(240, 106, 46, alpha), width=max(2, int(2.5 * s)))
    # 扫描扇区和指针
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.pieslice([cx - 23 * s, cy - 23 * s, cx + 23 * s, cy + 23 * s], start=-45, end=0, fill=(240, 106, 46, 90))
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)
    d.line([cx, cy, 49 * s, 17 * s], fill="#ffb38a", width=max(2, int(2.5 * s)))
    d.ellipse([(44 - 3.2) * s, (25 - 3.2) * s, (44 + 3.2) * s, (25 + 3.2) * s], fill="#ffd2b8")
    d.ellipse([cx - 2.4 * s, cy - 2.4 * s, cx + 2.4 * s, cy + 2.4 * s], fill="#ffffff")
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
