from base64 import b64decode
from struct import iter_unpack
from zlib import decompress

from rich.color import Color
from rich.style import Style
from rich.text import Text

_WIDTH = 44
_HEIGHT = 42
# RGB565 pixels sampled from the reference image. Neutral gray background pixels
# are keyed to Ego's background so the portrait blends into the welcome screen.
_RGB565_DATA = (
    "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"
    "ikmATZCgSw+P/vnL9xv/A2eOOR8="
)


def _rgb565_color(value: int) -> Color:
    red = (value >> 11) & 0x1F
    green = (value >> 5) & 0x3F
    blue = value & 0x1F
    return Color.from_rgb(
        (red << 3) | (red >> 2),
        (green << 2) | (green >> 4),
        (blue << 3) | (blue >> 2),
    )


def halfcell_portrait() -> Text:
    packed_pixels = decompress(b64decode(_RGB565_DATA))
    pixels = tuple(_rgb565_color(value) for (value,) in iter_unpack(">H", packed_pixels))
    portrait = Text(no_wrap=True, overflow="crop")

    for top_row in range(0, _HEIGHT, 2):
        top_offset = top_row * _WIDTH
        bottom_offset = (top_row + 1) * _WIDTH
        for column in range(_WIDTH):
            portrait.append(
                "▀",
                Style(
                    color=pixels[top_offset + column],
                    bgcolor=pixels[bottom_offset + column],
                ),
            )
        if top_row + 2 < _HEIGHT:
            portrait.append("\n")

    return portrait
