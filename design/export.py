#!/usr/bin/env python3
"""
Export the Night Shift hero (design/hero.html) as a GIF, and an MP4 or a still on request.

  python3 design/export.py                 writes docs/hero.gif only (the README hero: 960 px, 12 fps, 32 colours)
  python3 design/export.py --mp4           also writes docs/hero.mp4 (1280x640, 30 fps)
  python3 design/export.py --png           also writes a 2x still, docs/hero.png
  python3 design/export.py --out somewhere --gif-width 1280 --gif-fps 15 --gif-colors 128

Needs: Python 3.9+, Playwright with Chromium (pip install playwright &&
playwright install chromium), ffmpeg on the PATH, and either network access so
the page can load its two fonts from Google Fonts, or --font-css pointing at a
local stylesheet (see that option) so the render runs offline. The shipped
docs/hero.gif was made with the defaults plus --font-css, using the Fraunces
(opsz) and Nunito woff2 files from Fontsource 5.3.0.

The animation is one 7 second loop. Frames are stepped by setting the SVG
clock directly, so the result is frame-exact and the loop is seamless.
"""

import argparse
import asyncio
import pathlib
import shutil
import subprocess
import tempfile

LOOP_SECONDS = 7.0
W, H = 1280, 640
HERE = pathlib.Path(__file__).resolve().parent


async def offline_fonts(context, font_css):
    """Serve the Google Fonts request from a local stylesheet and block all other network.

    The stylesheet's url() entries must read https://fonts.gstatic.com/local/<file>,
    where <file> sits next to the stylesheet.
    """
    css = pathlib.Path(font_css).resolve()
    cors = {"Access-Control-Allow-Origin": "*"}

    async def route(r):
        url = r.request.url
        if url.startswith("file:"):
            await r.continue_()
        elif url.startswith("https://fonts.googleapis.com/"):
            await r.fulfill(path=str(css), content_type="text/css", headers=cors)
        elif url.startswith("https://fonts.gstatic.com/local/"):
            await r.fulfill(path=str(css.parent / url.rsplit("/", 1)[1]), content_type="font/woff2", headers=cors)
        else:
            await r.abort()
    await context.route("**/*", route)


async def capture(html, frames_dir, fps, still_path, channel, font_css=""):
    from playwright.async_api import async_playwright
    n = round(LOOP_SECONDS * fps)
    async with async_playwright() as p:
        browser = await p.chromium.launch(**({"channel": channel} if channel else {}))
        context = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
        if font_css:
            await offline_fonts(context, font_css)
        if still_path:  # 2x still, only with --png (the repo ships the GIF only)
            page = await context.new_page()
            await page.goto(html.as_uri())
            await page.evaluate("document.fonts.ready")
            await page.evaluate("() => { const s = document.querySelector('svg'); s.pauseAnimations(); s.setCurrentTime(0); }")
            await page.screenshot(path=str(still_path))
        await context.close()
        # 1x frames for the GIF and MP4
        context = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
        if font_css:
            await offline_fonts(context, font_css)
        page = await context.new_page()
        await page.goto(html.as_uri())
        await page.evaluate("document.fonts.ready")
        await page.evaluate("document.querySelector('svg').pauseAnimations()")
        for i in range(n):
            await page.evaluate("t => document.querySelector('svg').setCurrentTime(t)", i / fps)
            await page.screenshot(path=str(frames_dir / f"f{i:04d}.png"))
        await browser.close()
    return n


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description="Export the Night Shift hero as a GIF and an MP4")
    ap.add_argument("--html", default=str(HERE / "hero.html"))
    ap.add_argument("--out", default=str(HERE.parent / "docs"))
    ap.add_argument("--fps", type=int, default=30, help="capture and MP4 frame rate")
    ap.add_argument("--gif-fps", type=int, default=12)
    ap.add_argument("--gif-width", type=int, default=960, help="GIF width in px; height follows")
    ap.add_argument("--gif-colors", type=int, default=32,
                    help="GIF palette size; below 32 the sweep's afterglow shows steps")
    ap.add_argument("--mp4-loops", type=int, default=2, help="how many 7 s loops the MP4 holds")
    ap.add_argument("--channel", default="", help='Playwright browser channel, e.g. "chrome"')
    ap.add_argument("--mp4", action="store_true", help="also write hero.mp4")
    ap.add_argument("--png", action="store_true", help="also write a 2x still, hero.png")
    ap.add_argument("--font-css", default="",
                    help="local @font-face stylesheet that stands in for Google Fonts; all other network is blocked")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is not on the PATH")

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        frames = pathlib.Path(tmp)
        asyncio.run(capture(pathlib.Path(args.html).resolve(), frames, args.fps, out / "hero.png" if args.png else None, args.channel,
                            args.font_css))
        src = ["-framerate", str(args.fps), "-i", str(frames / "f%04d.png")]
        # GIF: one loop, its own palette, light dithering so the fading sweep does not band.
        # stats_mode=full with bayer_scale=5 keeps the defaults (960 px, 12 fps, 32 colours) near 0.75 MB
        # with no visible banding.
        flt = (f"fps={args.gif_fps},scale={args.gif_width}:-1:flags=lanczos,split[a][b];"
               f"[a]palettegen=max_colors={args.gif_colors}:stats_mode=full[p];"
               "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
        run(["ffmpeg", "-y", *src, "-filter_complex", flt, "-loop", "0", str(out / "hero.gif")])
        if args.mp4:
            # MP4: H.264, plays everywhere, loops joined end to end
            run(["ffmpeg", "-y", "-stream_loop", str(args.mp4_loops - 1), *src, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-crf", "18", "-movflags", "+faststart", str(out / "hero.mp4")])
    for name in ((("hero.png",) if args.png else ()) + ("hero.gif",) + (("hero.mp4",) if args.mp4 else ())):
        f = out / name
        print(f"{f}  {f.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
