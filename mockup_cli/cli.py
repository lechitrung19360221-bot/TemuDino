#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
import os
import base64
from pathlib import Path
from typing import Iterable, List, Tuple, Optional, Dict

import numpy as np
from PIL import Image, ImageChops
import cv2  # OpenCV for perspective warp


# -----------------------------
# Data models
# -----------------------------


@dataclass
class RectPlacement:
    # Center-based placement in normalized [0..1] coords relative to mockup size
    center_x_norm: float
    center_y_norm: float
    width_norm: float
    height_norm: float
    rotation_deg: float = 0.0


@dataclass
class PerspectivePlacement:
    # Four corners (top-left, top-right, bottom-right, bottom-left) in normalized [0..1]
    quad_norm: List[Tuple[float, float]]


@dataclass
class RenderConfig:
    mode: str  # "rect" | "perspective"
    placement: RectPlacement | PerspectivePlacement
    blend_mode: str = "normal"  # normal|multiply|screen
    opacity: float = 1.0  # 0..1
    maintain_aspect: str = "contain"  # contain|cover

    @staticmethod
    def from_dict(data: dict) -> "RenderConfig":
        mode = data.get("mode", "rect").lower()
        if mode == "rect":
            p = data.get("placement", {})
            placement = RectPlacement(
                center_x_norm=float(p.get("center_x_norm", 0.5)),
                center_y_norm=float(p.get("center_y_norm", 0.5)),
                width_norm=float(p.get("width_norm", 0.5)),
                height_norm=float(p.get("height_norm", 0.5)),
                rotation_deg=float(p.get("rotation_deg", 0.0)),
            )
        elif mode == "perspective":
            p = data.get("placement", {})
            quad = p.get("quad_norm")
            if not quad or len(quad) != 4:
                raise ValueError("perspective placement requires placement.quad_norm with 4 points")
            quad_typed: List[Tuple[float, float]] = [
                (float(x), float(y)) for (x, y) in quad
            ]
            placement = PerspectivePlacement(quad_norm=quad_typed)
        else:
            raise ValueError(f"Unsupported mode: {mode}")

        return RenderConfig(
            mode=mode,
            placement=placement,
            blend_mode=str(data.get("blend_mode", "normal")).lower(),
            opacity=float(data.get("opacity", 1.0)),
            maintain_aspect=str(data.get("maintain_aspect", "contain")).lower(),
        )


# -----------------------------
# Utility functions
# -----------------------------


def list_design_files(patterns: List[str]) -> List[Path]:
    files: List[Path] = []
    for pat in patterns:
        for p in sorted(Path().glob(pat)):
            if p.is_file() and p.suffix.lower() in {".png"}:
                files.append(p)
    return files


def ensure_rgba(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA":
        return img.convert("RGBA")
    return img


def resize_with_aspect(img: Image.Image, target_w: int, target_h: int, mode: str) -> Image.Image:
    # mode: contain (letterbox within target), cover (fill target and crop overflow),
    # or stretch (exactly target size, ignoring aspect ratio)
    src_w, src_h = img.size
    if src_w == 0 or src_h == 0:
        return img
    if mode == "stretch":
        return img.resize((max(1, int(target_w)), max(1, int(target_h))), resample=Image.Resampling.LANCZOS)
    scale_w = target_w / src_w
    scale_h = target_h / src_h
    if mode == "cover":
        scale = max(scale_w, scale_h)
    else:
        scale = min(scale_w, scale_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    return img.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)


def rotate_image_rgba(img: Image.Image, rotation_deg: float) -> Image.Image:
    if abs(rotation_deg) < 1e-6:
        return img
    # expand=True to keep entire rotated image; transparent fill
    return img.rotate(angle=rotation_deg, expand=True, fillcolor=(0, 0, 0, 0))


def apply_opacity(img_rgba: Image.Image, opacity: float) -> Image.Image:
    opacity = max(0.0, min(1.0, float(opacity)))
    if opacity >= 0.999:
        return img_rgba
    r, g, b, a = img_rgba.split()
    a_arr = np.array(a, dtype=np.float32)
    a_arr *= opacity
    a_arr = np.clip(a_arr, 0, 255).astype(np.uint8)
    a_new = Image.fromarray(a_arr, mode="L")
    return Image.merge("RGBA", (r, g, b, a_new))


def blend_normal(base: Image.Image, overlay: Image.Image) -> Image.Image:
    # base RGBA, overlay RGBA with alpha
    return Image.alpha_composite(base, overlay)


def blend_numpy(base: Image.Image, overlay: Image.Image, mode: str) -> Image.Image:
    # Perform blending using numpy with alpha considered
    base_rgba = ensure_rgba(base)
    over_rgba = ensure_rgba(overlay)

    base_arr = np.array(base_rgba).astype(np.float32) / 255.0
    over_arr = np.array(over_rgba).astype(np.float32) / 255.0

    b_rgb = base_arr[..., :3]
    b_a = base_arr[..., 3:4]
    o_rgb = over_arr[..., :3]
    o_a = over_arr[..., 3:4]

    # Pre-multiplied alpha blending helpers
    # out_rgb = f(b_rgb, o_rgb) * o_a + b_rgb * (1 - o_a)
    if mode == "multiply":
        f_rgb = b_rgb * o_rgb
    elif mode == "screen":
        f_rgb = 1.0 - (1.0 - b_rgb) * (1.0 - o_rgb)
    elif mode == "overlay":
        # overlay per channel
        mask = b_rgb <= 0.5
        f_rgb = np.empty_like(b_rgb)
        f_rgb[mask] = 2.0 * b_rgb[mask] * o_rgb[mask]
        f_rgb[~mask] = 1.0 - 2.0 * (1.0 - b_rgb[~mask]) * (1.0 - o_rgb[~mask])
    elif mode == "lighten":
        f_rgb = np.maximum(b_rgb, o_rgb)
    elif mode == "darken":
        f_rgb = np.minimum(b_rgb, o_rgb)
    else:
        # Fallback to normal
        f_rgb = o_rgb

    out_rgb = f_rgb * o_a + b_rgb * (1.0 - o_a)
    out_a = o_a + b_a * (1.0 - o_a)
    out = np.concatenate([out_rgb, out_a], axis=-1)
    out = np.clip(out * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def paste_overlay(
    mockup_rgba: Image.Image,
    overlay_rgba: Image.Image,
    center_xy: Tuple[int, int],
    blend_mode: str,
) -> Image.Image:
    # Create same size canvas and paste overlay at target center
    canvas = Image.new("RGBA", mockup_rgba.size, (0, 0, 0, 0))
    ow, oh = overlay_rgba.size
    cx, cy = center_xy
    x0 = int(round(cx - ow / 2))
    y0 = int(round(cy - oh / 2))
    canvas.alpha_composite(overlay_rgba, dest=(x0, y0))

    bm = blend_mode.lower()
    if bm == "normal":
        return blend_normal(mockup_rgba, canvas)
    elif bm in {"multiply", "screen", "overlay", "lighten", "darken"}:
        return blend_numpy(mockup_rgba, canvas, bm)
    else:
        return blend_normal(mockup_rgba, canvas)


# -----------------------------
# Core rendering
# -----------------------------


def render_one(
    mockup_path: Path,
    design_path: Path,
    config: RenderConfig,
) -> Image.Image:
    mockup = ensure_rgba(Image.open(mockup_path))
    design = ensure_rgba(Image.open(design_path))

    mw, mh = mockup.size
    if config.mode == "rect":
        # Target rect in pixels
        rp = config.placement  # type: ignore[assignment]
        assert isinstance(rp, RectPlacement)
        tw = max(1, int(round(rp.width_norm * mw)))
        th = max(1, int(round(rp.height_norm * mh)))
        cx = int(round(rp.center_x_norm * mw))
        cy = int(round(rp.center_y_norm * mh))

        # Resize with aspect strategy
        resized = resize_with_aspect(design, tw, th, mode=config.maintain_aspect)

        # Rotate
        rotated = rotate_image_rgba(resized, rp.rotation_deg)

        # Opacity
        overlay = apply_opacity(rotated, config.opacity)

        # Blend
        out = paste_overlay(mockup, overlay, (cx, cy), config.blend_mode)
        return out

    elif config.mode == "perspective":
        pp = config.placement  # type: ignore[assignment]
        assert isinstance(pp, PerspectivePlacement)
        # Compute pixel quad
        quad_px = np.array(
            [
                [pp.quad_norm[0][0] * mw, pp.quad_norm[0][1] * mh],
                [pp.quad_norm[1][0] * mw, pp.quad_norm[1][1] * mh],
                [pp.quad_norm[2][0] * mw, pp.quad_norm[2][1] * mh],
                [pp.quad_norm[3][0] * mw, pp.quad_norm[3][1] * mh],
            ],
            dtype=np.float32,
        )

        # Optionally scale design first based on average width/height of quad
        # For now, use original design size
        dw, dh = design.size
        src = np.array([[0, 0], [dw - 1, 0], [dw - 1, dh - 1], [0, dh - 1]], dtype=np.float32)
        dst = quad_px
        H = cv2.getPerspectiveTransform(src, dst)

        # Convert design RGBA to BGRA for OpenCV
        design_rgba = ensure_rgba(design)
        design_arr = np.array(design_rgba, dtype=np.uint8)
        design_bgra = cv2.cvtColor(design_arr, cv2.COLOR_RGBA2BGRA)

        # Warp onto a canvas same size as mockup
        warped_bgra = cv2.warpPerspective(
            design_bgra,
            H,
            dsize=(mw, mh),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0),
        )

        # Convert back to RGBA PIL
        warped_rgba_arr = cv2.cvtColor(warped_bgra, cv2.COLOR_BGRA2RGBA)
        warped_rgba = Image.fromarray(warped_rgba_arr, mode="RGBA")

        # Apply opacity
        overlay = apply_opacity(warped_rgba, config.opacity)

        # Blend with selected mode
        bm = config.blend_mode
        if bm == "normal":
            out = blend_normal(mockup, overlay)
        elif bm in {"multiply", "screen", "overlay", "lighten", "darken"}:
            out = blend_numpy(mockup, overlay, bm)
        else:
            out = blend_normal(mockup, overlay)
        return out
    else:
        raise ValueError(f"Unsupported mode: {config.mode}")


def render_one_rect_pixels(
    mockup_path: Path,
    design_path: Path,
    center_x_px: int,
    center_y_px: int,
    target_width_px: int,
    target_height_px: int,
    rotation_deg: float = 0.0,
    maintain_aspect: str = "contain",
    opacity: float = 1.0,
    blend_mode: str = "normal",
) -> Image.Image:
    mockup = ensure_rgba(Image.open(mockup_path))
    design = ensure_rgba(Image.open(design_path))

    # Resize
    resized = resize_with_aspect(design, target_width_px, target_height_px, mode=maintain_aspect)
    rotated = rotate_image_rgba(resized, rotation_deg)
    overlay = apply_opacity(rotated, opacity)
    out = paste_overlay(mockup, overlay, (int(center_x_px), int(center_y_px)), blend_mode)
    return out


def save_image(img: Image.Image, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve PNG with alpha; if jpg requested, convert to RGB
    if out_path.suffix.lower() in {".jpg", ".jpeg"}:
        img_rgb = img.convert("RGB")
        img_rgb.save(out_path, quality=95)
    else:
        img.save(out_path)


def upload_to_imgbb(image_path: Path, api_key: str) -> Dict[str, str]:
    try:
        import requests  # lazy import
    except Exception as e:
        raise RuntimeError("The 'requests' package is required for --upload-imgbb. Please install it.")

    url = "https://api.imgbb.com/1/upload"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    resp = requests.post(url, data={"key": api_key, "image": b64}, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"imgbb upload failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"imgbb upload failed: {data}")
    item = data.get("data", {})
    return {
        "url": item.get("url", ""),
        "delete_url": item.get("delete_url", ""),
        "id": item.get("id", ""),
    }


def slugify_filename(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("._") or "file"


def build_output_name(pattern: str, design_path: Path, index: int) -> str:
    name_no_ext = design_path.stem
    return (
        pattern.replace("{name}", name_no_ext)
        .replace("{index}", str(index))
        .replace("{ext}", design_path.suffix.lstrip("."))
    )


def cmd_preview(args: argparse.Namespace) -> None:
    cfg = RenderConfig.from_dict(json.loads(Path(args.config).read_text()))
    img = render_one(Path(args.mockup), Path(args.design), cfg)
    out = Path(args.output)
    save_image(img, out)
    print(f"Saved preview to {out}")


def cmd_render(args: argparse.Namespace) -> None:
    cfg = RenderConfig.from_dict(json.loads(Path(args.config).read_text()))
    designs = list_design_files(args.designs)
    if not designs:
        print("No design PNG files found.")
        sys.exit(1)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    uploaded: List[Dict[str, str]] = []
    for idx, dp in enumerate(designs, start=1):
        img = render_one(Path(args.mockup), dp, cfg)
        filename = build_output_name(args.pattern, dp, idx)
        filename = slugify_filename(filename)
        out_path = out_dir / filename
        if not (out_path.suffix.lower() in {".png", ".jpg", ".jpeg"}):
            # default to png
            out_path = out_path.with_suffix(".png")
        save_image(img, out_path)
        count += 1
        if args.verbose:
            print(f"Saved: {out_path}")

        if args.upload_imgbb:
            key = args.imgbb_key or os.getenv("IMGBB_API_KEY", "")
            if not key:
                raise RuntimeError("--upload-imgbb requires --imgbb-key or IMGBB_API_KEY env var")
            info = upload_to_imgbb(out_path, key)
            uploaded.append({"file": str(out_path), **info})
            if args.verbose:
                print(f"Uploaded to imgbb: {info.get('url')}")
    print(f"Rendered {count} images to {out_dir}")
    if args.upload_imgbb:
        print("\nImgBB results:")
        for item in uploaded:
            print(f"- {item['file']} -> {item.get('url')} (delete: {item.get('delete_url')})")


def _center_from_anchor(anchor: str, x: int, y: int, w: int, h: int) -> Tuple[int, int]:
    if anchor == "topleft":
        return x + w // 2, y + h // 2
    return x, y


def cmd_preview_simple(args: argparse.Namespace) -> None:
    cx, cy = _center_from_anchor(args.anchor, args.x, args.y, args.w, args.h)
    img = render_one_rect_pixels(
        Path(args.mockup),
        Path(args.design),
        center_x_px=cx,
        center_y_px=cy,
        target_width_px=args.w,
        target_height_px=args.h,
        rotation_deg=args.rotation,
        maintain_aspect=args.aspect,
        opacity=args.opacity,
        blend_mode=args.blend,
    )
    out = Path(args.output)
    save_image(img, out)
    print(f"Saved preview to {out}")


def cmd_render_simple(args: argparse.Namespace) -> None:
    designs = list_design_files(args.designs)
    if not designs:
        print("No design PNG files found.")
        sys.exit(1)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cx, cy = _center_from_anchor(args.anchor, args.x, args.y, args.w, args.h)

    count = 0
    uploaded: List[Dict[str, str]] = []
    for idx, dp in enumerate(designs, start=1):
        img = render_one_rect_pixels(
            Path(args.mockup),
            dp,
            center_x_px=cx,
            center_y_px=cy,
            target_width_px=args.w,
            target_height_px=args.h,
            rotation_deg=args.rotation,
            maintain_aspect=args.aspect,
            opacity=args.opacity,
            blend_mode=args.blend,
        )
        filename = build_output_name(args.pattern, dp, idx)
        filename = slugify_filename(filename)
        out_path = out_dir / filename
        if not (out_path.suffix.lower() in {".png", ".jpg", ".jpeg"}):
            out_path = out_path.with_suffix(".png")
        save_image(img, out_path)
        count += 1
        if args.verbose:
            print(f"Saved: {out_path}")

        if args.upload_imgbb:
            key = args.imgbb_key or os.getenv("IMGBB_API_KEY", "")
            if not key:
                raise RuntimeError("--upload-imgbb requires --imgbb-key or IMGBB_API_KEY env var")
            info = upload_to_imgbb(out_path, key)
            uploaded.append({"file": str(out_path), **info})
            if args.verbose:
                print(f"Uploaded to imgbb: {info.get('url')}")
    print(f"Rendered {count} images to {out_dir}")
    if args.upload_imgbb:
        print("\nImgBB results:")
        for item in uploaded:
            print(f"- {item['file']} -> {item.get('url')} (delete: {item.get('delete_url')})")


def cmd_init_config(args: argparse.Namespace) -> None:
    mode = args.mode.lower()
    if mode == "rect":
        template = {
            "mode": "rect",
            "placement": {
                "center_x_norm": 0.5,
                "center_y_norm": 0.5,
                "width_norm": 0.6,
                "height_norm": 0.4,
                "rotation_deg": 0.0,
            },
            "blend_mode": "normal",
            "opacity": 1.0,
            "maintain_aspect": "contain",
        }
    elif mode == "perspective":
        template = {
            "mode": "perspective",
            "placement": {
                # Top-left, top-right, bottom-right, bottom-left in normalized coords
                "quad_norm": [
                    [0.2, 0.2],
                    [0.8, 0.2],
                    [0.8, 0.8],
                    [0.2, 0.8],
                ],
            },
            "blend_mode": "normal",
            "opacity": 1.0,
        }
    else:
        raise ValueError("mode must be 'rect' or 'perspective'")

    Path(args.output).write_text(json.dumps(template, indent=2))
    print(f"Wrote config template to {args.output}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch mockup renderer (Pillow + OpenCV not required for rect mode)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init-config", help="Create a config template JSON")
    sp.add_argument("--mode", choices=["rect", "perspective"], default="rect")
    sp.add_argument("--output", default="mockup_config.json", help="Path to write config JSON")
    sp.set_defaults(func=cmd_init_config)

    sp = sub.add_parser("preview", help="Render a single design for preview")
    sp.add_argument("--mockup", required=True, help="Path to mockup JPG/PNG")
    sp.add_argument("--design", required=True, help="Path to a design PNG")
    sp.add_argument("--config", required=True, help="Config JSON path")
    sp.add_argument("--output", default="preview.png", help="Where to save preview image")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("render", help="Render batch from multiple design PNGs")
    sp.add_argument("--mockup", required=True, help="Path to mockup JPG/PNG")
    sp.add_argument(
        "--designs",
        nargs="+",
        required=True,
        help="Glob patterns for design PNGs, e.g. 'designs/*.png'",
    )
    sp.add_argument("--config", required=True, help="Config JSON path")
    sp.add_argument("--out-dir", default="outputs", help="Output directory")
    sp.add_argument(
        "--pattern",
        default="{name}_mockup.png",
        help="Filename pattern. Vars: {name}, {index}, {ext}",
    )
    sp.add_argument("--verbose", action="store_true")
    sp.add_argument("--upload-imgbb", action="store_true", help="Upload results to imgbb")
    sp.add_argument("--imgbb-key", default=None, help="ImgBB API key (or set IMGBB_API_KEY env)")
    sp.set_defaults(func=cmd_render)

    # Simple, config-less commands (pixel-based)
    sp = sub.add_parser(
        "preview-simple",
        help="Preview using pixel params (no config): center or topleft + width/height",
    )
    sp.add_argument("--mockup", required=True, help="Path to mockup JPG/PNG")
    sp.add_argument("--design", required=True, help="Path to a design PNG")
    sp.add_argument("--output", default="preview.png", help="Where to save preview image")
    sp.add_argument("--anchor", choices=["center", "topleft"], default="center")
    sp.add_argument("--x", type=int, required=True, help="X (center or top-left depending on anchor)")
    sp.add_argument("--y", type=int, required=True, help="Y (center or top-left depending on anchor)")
    sp.add_argument("--w", type=int, required=True, help="Target width in pixels")
    sp.add_argument("--h", type=int, required=True, help="Target height in pixels")
    sp.add_argument("--rotation", type=float, default=0.0)
    sp.add_argument("--aspect", choices=["contain", "cover"], default="contain")
    sp.add_argument("--opacity", type=float, default=1.0)
    sp.add_argument("--blend", choices=["normal", "multiply", "screen", "overlay", "lighten", "darken"], default="normal")
    sp.set_defaults(func=cmd_preview_simple)

    sp = sub.add_parser(
        "render-simple",
        help="Batch render using pixel params (no config)",
    )
    sp.add_argument("--mockup", required=True, help="Path to mockup JPG/PNG")
    sp.add_argument(
        "--designs",
        nargs="+",
        required=True,
        help="Glob patterns for design PNGs, e.g. 'designs/*.png'",
    )
    sp.add_argument("--out-dir", default="outputs", help="Output directory")
    sp.add_argument("--pattern", default="{name}_mockup.png", help="Filename pattern. Vars: {name}, {index}, {ext}")
    sp.add_argument("--anchor", choices=["center", "topleft"], default="center")
    sp.add_argument("--x", type=int, required=True)
    sp.add_argument("--y", type=int, required=True)
    sp.add_argument("--w", type=int, required=True)
    sp.add_argument("--h", type=int, required=True)
    sp.add_argument("--rotation", type=float, default=0.0)
    sp.add_argument("--aspect", choices=["contain", "cover"], default="contain")
    sp.add_argument("--opacity", type=float, default=1.0)
    sp.add_argument("--blend", choices=["normal", "multiply", "screen", "overlay", "lighten", "darken"], default="normal")
    sp.add_argument("--verbose", action="store_true")
    sp.add_argument("--upload-imgbb", action="store_true", help="Upload results to imgbb")
    sp.add_argument("--imgbb-key", default=None, help="ImgBB API key (or set IMGBB_API_KEY env)")
    sp.set_defaults(func=cmd_render_simple)

    return p


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


