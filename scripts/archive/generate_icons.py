#!/usr/bin/env python3
"""Generate simple PWA icons using PIL"""
try:
    from PIL import Image, ImageDraw, ImageFont

    def create_icon(size, filename):
        # Create image with red background
        img = Image.new('RGB', (size, size), color='#FF453A')
        draw = ImageDraw.Draw(img)

        # Draw white "RN" text
        font_size = int(size * 0.5)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except:
            font = ImageFont.load_default()

        text = "RN"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        position = ((size - text_width) // 2, (size - text_height) // 2 - bbox[1])
        draw.text(position, text, fill='white', font=font)

        img.save(f'/opt/rednun/static/{filename}')
        print(f"Created {filename}")

    create_icon(192, 'icon-192.png')
    create_icon(512, 'icon-512.png')

except ImportError:
    print("PIL not installed, creating placeholder icons")
    # Create minimal 1x1 placeholder
    with open('/opt/rednun/static/icon-192.png', 'wb') as f:
        # Minimal valid PNG (1x1 red pixel)
        f.write(bytes.fromhex('89504e470d0a1a0a0000000d494844520000000100000001010300000025db56ca00000003504c5445ff0000008a91c6000000017452530040e6d8660000000a4944415408d76360000000020001e221bc330000000049454e44ae426082'))
    with open('/opt/rednun/static/icon-512.png', 'wb') as f:
        f.write(bytes.fromhex('89504e470d0a1a0a0000000d494844520000000100000001010300000025db56ca00000003504c5445ff0000008a91c6000000017452530040e6d8660000000a4944415408d76360000000020001e221bc330000000049454e44ae426082'))
    print("Created placeholder icons")
