"""Genera el icono de BetBot (pelota de tenis) en PNG, ICO y ICNS."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "launchers" / "icons"


def draw(size: int) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = size // 16
    ball = (m, m, size - m, size - m)
    d.ellipse(ball, fill=(204, 255, 0, 255), outline=(40, 60, 10, 255), width=max(2, size // 48))
    # costuras características de la pelota
    w = max(2, size // 40)
    d.arc((-size * 0.42, m, size * 0.58, size - m), start=-60, end=60,
          fill=(255, 255, 255, 255), width=w)
    d.arc((size * 0.42, m, size * 1.42, size - m), start=120, end=240,
          fill=(255, 255, 255, 255), width=w)
    return im


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = draw(512)
    base.save(OUT / "betbot.png")
    base.save(OUT / "betbot.ico",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    base.save(OUT / "betbot.icns")
    print(f"iconos escritos en {OUT}")


if __name__ == "__main__":
    main()
