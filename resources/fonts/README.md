# Bundled fallback fonts

`NotoEmoji.ttf` is the static regular-weight (wght=400) instance of the Noto Emoji
monochrome variable font published by Google Fonts. It retains every glyph. It is distributed under the SIL Open Font License 1.1;
the full notice is in `../../LICENSES/NotoEmoji-OFL.txt`.

`SOURCES.json` records the public upstream URL, download date, byte count and
SHA-256 of the original font, distributed instance and license. The font adds about 0.85 MiB
to the plugin and is loaded locally, without render-time network requests.

System emoji fonts are preferred when Pillow can load them at the requested
size. This font supplies missing common emoji on Windows and server systems.
Without a shaping engine, composite emoji are kept together during wrapping
and rendered with basic-symbol/text fallbacks. Unknown glyphs use `?`.

`DeniaHangul-Regular.ttf` is the Hangul subset of the Nanum Gothic font
distributed by MusicUID. It supplies Korean glyphs in music search results
while retaining the configured font for surrounding text. The build removes
TrueType hinting and uses a different family name under the SIL Open Font
License 1.1. See `../../LICENSES/DeniaHangul-OFL.txt` and `SOURCES.json`.
