from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

out_dir = Path(__file__).resolve().parents[1] / "assets"
out_dir.mkdir(exist_ok=True)
icon_path = out_dir / "icon.ico"

# Create base image
size = 256
img = Image.new("RGBA", (size, size), (59,130,246,255))  # Facebook-like blue
draw = ImageDraw.Draw(img)

# Draw rounded rectangle / circle
r = 24
draw.rounded_rectangle([(0,0),(size,size)], radius=40, fill=(59,130,246,255))

# Draw white "f" style letter
try:
    font = ImageFont.truetype("arial.ttf", 180)
except Exception:
    font = ImageFont.load_default()

text = "f"
try:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
except Exception:
    try:
        w, h = font.getsize(text)
    except Exception:
        w, h = 100, 140
# Position slightly left and down for FB-like balance
pos = ((size - w) // 2 - 6, (size - h) // 2 - 10)

# Use a thick rendering by drawing text multiple times offset
for dx, dy in [(0,0),(1,0),(-1,0),(0,1),(0,-1)]:
    draw.text((pos[0]+dx, pos[1]+dy), text, font=font, fill=(255,255,255,255))

# Save as ICO with multiple sizes
img.save(icon_path, format="ICO", sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])
print(f"Wrote icon to: {icon_path}")
