"""用 Pillow 画应用图标（蓝底白色播放键 + 三行字幕），输出 build/icon/icon.png / .ico / .icns。"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "build" / "icon"


def draw(size=1024):
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 4 * s  # macOS 图标留边
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=13 * s, fill="#2563eb")
    d.polygon([(22 * s, 14 * s), (46 * s, 28 * s), (22 * s, 42 * s)], fill="#ffffff")
    for i, w in enumerate((36, 28)):
        y = (47 + i * 5) * s
        d.rounded_rectangle([(32 - w / 2) * s, y, (32 + w / 2) * s, y + 2.4 * s], radius=1.2 * s, fill="#bfd3ff")
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
