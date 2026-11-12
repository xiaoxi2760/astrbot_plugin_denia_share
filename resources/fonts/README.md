# Noto Emoji fallback

`NotoEmoji.ttf` is the unmodified Noto Emoji variable monochrome font published
by Google Fonts. It is distributed under the SIL Open Font License 1.1;
the full notice is in `../../LICENSES/NotoEmoji-OFL.txt`.

`SOURCES.json` records the public upstream URL, download date, byte count and
SHA-256 of both the original font and its license. The font adds about 1.9 MiB
to the plugin and is loaded locally, without render-time network requests.

System emoji fonts are preferred when Pillow can load them at the requested
size. This font supplies missing common emoji on Windows and server systems.
Without a shaping engine, composite emoji are kept together during wrapping
and rendered with basic-symbol/text fallbacks. Unknown glyphs use `?`.
