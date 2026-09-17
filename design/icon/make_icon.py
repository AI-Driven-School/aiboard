"""アプリアイコンを描く(1024px → iconset → AppIcon.icns)。文字は使わないので名前が決まる前でも使える。

形: macOS の角丸(超楕円)の暗い地 + 状態色のカード 4 枚(青=端末 ❯_ / 緑=作業中 / 黄=あなたの番 / 赤=判断待ち)
  python3 design/icon/make_icon.py
"""
import math
import os
import subprocess

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
S = 4                      # 4 倍で描いて縮める(輪郭を滑らかに)
N = 1024 * S


def squircle(cx, cy, r, n=5.0, steps=720):
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + r * math.copysign(abs(c) ** (2 / n), c), cy + r * math.copysign(abs(s) ** (2 / n), s)))
    return pts


def draw(size=1024, plate=True):
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    # 影(Big Sur のグリッド: 地は 824/1024、下に柔らかい影)
    R = 412 * S
    cx, cy = N / 2, N / 2 - 6 * S
    if plate:
        sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(sh).polygon(squircle(cx, cy + 14 * S, R), fill=(0, 0, 0, 110))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18 * S)))
        # 地: 上が少し明るい炭色
        base = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        grad = Image.new("RGBA", (1, N))
        for y in range(N):
            k = y / N
            grad.putpixel((0, y), (int(44 - 20 * k), int(44 - 20 * k), int(48 - 20 * k), 255))
        grad = grad.resize((N, N))
        mask = Image.new("L", (N, N), 0)
        ImageDraw.Draw(mask).polygon(squircle(cx, cy, R), fill=255)
        base.paste(grad, (0, 0), mask)
        img.alpha_composite(base)
        # キャンバスの点格子(うっすら)
        dots = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dots)
        step = 56 * S
        for gx in range(int(cx - R), int(cx + R), step):
            for gy in range(int(cy - R), int(cy + R), step):
                dd.ellipse((gx - 3 * S, gy - 3 * S, gx + 3 * S, gy + 3 * S), fill=(255, 255, 255, 22))
        dm = Image.new("L", (N, N), 0)
        ImageDraw.Draw(dm).polygon(squircle(cx, cy, R - 60 * S), fill=255)
        img.paste(dots, (0, 0), Image.composite(dots.split()[3], Image.new("L", (N, N), 0), dm))
        # 縁の細い光
        rim = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(rim).line(squircle(cx, cy, R - 2 * S) + [squircle(cx, cy, R - 2 * S)[0]], fill=(255, 255, 255, 34), width=3 * S)
        img.alpha_composite(rim)

    # カード 4 枚(2x2・わずかにずらして「盤に置いた」感じ)
    card = 262 * S
    gap = 40 * S
    x0 = cx - card - gap / 2
    y0 = cy - card - gap / 2
    rad = 58 * S
    cards = [
        ((x0, y0), (13, 153, 255)),                       # 青: 端末
        ((x0 + card + gap, y0 + 22 * S), (20, 174, 92)),  # 緑: 作業中
        ((x0, y0 + card + gap), (255, 197, 61)),          # 黄: あなたの番
        ((x0 + card + gap, y0 + card + gap + 22 * S), (242, 72, 34)),   # 赤: 判断待ち
    ]
    for (x, y), col in cards:
        sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((x, y + 12 * S, x + card, y + card + 12 * S), rad, fill=(0, 0, 0, 120))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14 * S)))
        layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.rounded_rectangle((x, y, x + card, y + card), rad, fill=col + (255,))
        # 上半分に淡い光(平板にしすぎない)
        hl = Image.new("RGBA", (N, N), (0, 0, 0, 0))   # 上から下へ淡く明るさを抜く(テカリの帯は付けない)
        hd = ImageDraw.Draw(hl)
        for i in range(0, int(card), S):
            hd.line([(x, y + i), (x + card, y + i)], fill=(255, 255, 255, int(26 * (1 - i / card))), width=S)
        m = Image.new("L", (N, N), 0)
        ImageDraw.Draw(m).rounded_rectangle((x, y, x + card, y + card), rad, fill=255)
        layer.alpha_composite(Image.composite(hl, Image.new("RGBA", (N, N), (0, 0, 0, 0)), m))
        img.alpha_composite(layer)

    # 青いカードに ❯_(線で描く: フォントに頼らない)
    (x, y), _ = cards[0]
    d = ImageDraw.Draw(img)
    w = 30 * S
    px, py = x + 70 * S, y + card / 2
    d.line([(px, py - 52 * S), (px + 52 * S, py), (px, py + 52 * S)], fill=(255, 255, 255, 255), width=w, joint="curve")
    for ex, ey in ((px, py - 52 * S), (px, py + 52 * S), (px + 52 * S, py)):
        d.ellipse((ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2), fill=(255, 255, 255, 255))
    d.rounded_rectangle((px + 92 * S, py + 38 * S, px + 170 * S, py + 38 * S + w), w / 2, fill=(255, 255, 255, 255))
    # 赤いカードに注意の輪(判断待ち)
    (x, y), _ = cards[3]
    c2 = (x + card / 2, y + card / 2)
    d.ellipse((c2[0] - 40 * S, c2[1] - 40 * S, c2[0] + 40 * S, c2[1] + 40 * S), fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main():
    big = draw(1024)
    big.save(os.path.join(HERE, "icon-1024.png"))
    iconset = os.path.join(HERE, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for pt in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = pt * scale
            big.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(ROOT, "Resources", "AppIcon.icns")], check=True)
    # 盤・LP のファビコン(地つき 64px と 180px)
    big.resize((64, 64), Image.LANCZOS).save(os.path.join(HERE, "favicon-64.png"))   # 盤は data URI で埋め込む(CSP で外部を読まないため)
    os.makedirs(os.path.join(ROOT, "site", "img"), exist_ok=True)
    big.resize((64, 64), Image.LANCZOS).save(os.path.join(ROOT, "site", "img", "favicon.png"))
    big.resize((180, 180), Image.LANCZOS).save(os.path.join(ROOT, "site", "img", "apple-touch-icon.png"))
    # 大小を並べた確認用
    sheet = Image.new("RGBA", (1024 + 40 + 512 + 40 + 128 + 40 + 64 + 40 + 32 + 40 + 16 + 40, 1064), (236, 236, 236, 255))
    x = 20
    for px in (1024, 512, 128, 64, 32, 16):
        sheet.alpha_composite(big.resize((px, px), Image.LANCZOS), (x, 20 + (1024 - px) // 2)); x += px + 40
    sheet.save(os.path.join(HERE, "icon-sheet.png"))
    print("ok")


if __name__ == "__main__":
    main()
