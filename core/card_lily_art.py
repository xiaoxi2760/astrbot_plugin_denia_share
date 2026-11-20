"""Use the garden painting as edge ornament around the Denia-family card grid."""

from PIL import Image, ImageChops, ImageDraw


def _fade(length: int, near: int = 0, far: int = 0) -> Image.Image:
    mask = Image.new("L", (length, 1))
    mask.putdata([round(255 * min(1, i / near if near else 1,
                                  (length - 1 - i) / far if far else 1)) for i in range(length)])
    return mask


def _paint_crop(canvas: Image.Image, artwork: Image.Image, box: tuple, x: int, y: int,
                width: int, feathers: tuple, opacity: float) -> None:
    aw, ah = artwork.size
    left, top, right, bottom = box
    height = max(1, round(width * ah * (bottom - top) / (aw * (right - left))))
    size = (width, height)
    piece = artwork.resize(size, Image.Resampling.LANCZOS,
                           box=(aw * left, ah * top, aw * right, ah * bottom))
    l, r, t, b = feathers
    horizontal = _fade(width, round(width * l), round(width * r)).resize(size)
    vertical = _fade(height, round(height * t), round(height * b)).transpose(Image.Transpose.TRANSPOSE)
    mask = ImageChops.multiply(horizontal, vertical.resize(size))
    piece.putalpha(mask.point(lambda a: a * opacity))
    canvas.alpha_composite(piece, (x, y))


def draw_garden(canvas: Image.Image, artwork: Image.Image, footer_y: int, flower_y: int,
                *, quiet_flower: bool, custom_gradient: bool) -> None:
    w, h = canvas.size
    aw, ah = artwork.size
    # The low-detail center provides the cloud color. Detail crops stay undistorted.
    mist = artwork.resize((w, h), Image.Resampling.BILINEAR, box=(aw*.20, ah*.32, aw*.80, ah*.60))
    canvas.paste(Image.blend(canvas, mist, 0.20 if custom_gradient else 0.44))
    strength = 0.69 if custom_gradient else 1.0
    flower_w = max(1, round(w * (.46 if quiet_flower else .53)))
    _paint_crop(canvas, artwork, (.31, 0, 1, .35), w - flower_w, flower_y, flower_w,
                (.48, 0, 0, .30), (.34 if quiet_flower else .78) * strength)
    # A low cloudbank enters from the left and fades under the signature.
    cloud_w = max(1, round(w * .59))
    cloud_box = (.52, .745, .985, .90)
    cloud_h = round(cloud_w * ah * (cloud_box[3] - cloud_box[1]) / (aw * (cloud_box[2] - cloud_box[0])))
    _paint_crop(canvas, artwork, cloud_box, 0, footer_y - cloud_h, cloud_w,
                (0, .40, .30, .17), .72 * strength)
    # Vial and lower veil sit alongside the closing line, above the link divider.
    vial_w = max(1, round(w * .218))
    vial_box = (0, .59, .52, 1)
    vial_h = round(vial_w * ah * (1 - vial_box[1]) / (aw * vial_box[2]))
    _paint_crop(canvas, artwork, vial_box, w - vial_w, footer_y - vial_h + round(w*.034),
                vial_w, (.10, .20, .16, .13), .98 * strength)


def page_flower(canvas: Image.Image, artwork: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Let the flower show through the corner of the feed's single sheet."""
    margin = max(4, round(canvas.width * .012))
    width = max(1, round(canvas.width * .27))
    _paint_crop(canvas, artwork, (.44, 0, 1, .32), box[2] - width - margin, box[1] + margin, width,
                (.47, .19, .22, .65), .22)


def silver_panel(canvas: Image.Image, box: tuple[int, int, int, int], fill: str,
                 silver: str, cut: int) -> None:
    """Pearlescent bevel, sharing the night theme's clipped-corner silhouette."""
    x0, y0, x1, y1 = box
    points = [(x0, y0 + cut), (x0 + cut, y0), (x1, y0), (x1, y1 - cut),
              (x1 - cut, y1), (x0, y1)]
    d = ImageDraw.Draw(canvas)
    d.polygon(points, fill=fill, outline=silver)
    inset = max(3, cut // 4)
    d.line((x0 + cut + inset, y0 + inset, x1 - inset, y0 + inset), fill="#5B496C")
