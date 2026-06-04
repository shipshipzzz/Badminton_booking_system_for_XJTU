#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
滑块验证码匹配 demo（针对 bg*.jpeg + sd*.png 这一类数据集）

使用方法：
1. 安装依赖
   pip install -r requirements.txt

2. 将数据集解压到某个目录（例如 ./pics），目录中应包含：
   - bg0.jpeg, bg1.jpeg, ... 背景图
   - sd0.png, sd1.png, ...   对应的滑块图
   且编号一一对应：bg12.jpeg <-> sd12.png

3. 运行程序：
   python run_slider_match.py --data_dir ./pics --output_dir ./output --rows 5 --cols 5

程序会：
- 自动按照数字编号将 bgX 与 sdX 配对
- 使用【梯度 + 归一化模板匹配】算法估计滑块在背景上的横坐标
- 在背景图上用 alpha 融合的方式“还原”滑块位置，并在左上角写上编号、x 坐标与匹配得分
- 每 rows*cols 张结果拼接成一张大图 grid_000.png, grid_001.png, ...
- 在 output/results.txt 中保存每一对图像的 (id, bg 文件名, sd 文件名, x 坐标, 匹配得分)

你可以通过查看这些 grid_*.png 来肉眼检验算法的匹配正确率。
"""

import os
import math
import glob
import re
import argparse

import cv2
import numpy as np


def numeric_id(path: str, prefix: str) -> int:
    """
    从文件名中提取数字编号，例如:
    path='bg12.jpeg', prefix='bg' -> 12
    """
    base = os.path.basename(path)
    pattern = r"%s(\d+)" % re.escape(prefix)
    m = re.search(pattern, base)
    if not m:
        return None
    return int(m.group(1))


def build_pairs(data_dir: str):
    """
    根据数据目录中 bg*.jpeg 和 sd*.png 自动配对
    返回：[(id, bg_path, sd_path), ...] 按 id 排好序
    """
    bg_paths = glob.glob(os.path.join(data_dir, "bg*.jpeg"))
    sd_paths = glob.glob(os.path.join(data_dir, "sd*.png"))

    if not bg_paths or not sd_paths:
        raise RuntimeError(
            f"在 {data_dir} 下没有找到 bg*.jpeg 或 sd*.png，请确认数据目录是否正确。"
        )

    bg_dict = {}
    for p in bg_paths:
        idx = numeric_id(p, "bg")
        if idx is not None:
            bg_dict[idx] = p

    pairs = []
    missing_bg = []
    for p in sd_paths:
        idx = numeric_id(p, "sd")
        if idx is None:
            continue
        bg_path = bg_dict.get(idx)
        if bg_path is None:
            missing_bg.append(idx)
            continue
        pairs.append((idx, bg_path, p))

    if missing_bg:
        print("Warning: The following slider IDs have no corresponding background:", missing_bg)

    pairs.sort(key=lambda x: x[0])
    return pairs


def preprocess_for_match(img: np.ndarray, use_alpha_mask: bool = False):
    """
    对输入图像做预处理，得到单通道的“特征图”和可选的 mask。
    这里使用的是 x 方向梯度 + 对比度受限自适应直方图均衡 (CLAHE)，
    在很多滑块验证码上比直接用灰度值匹配更鲁棒。

    返回:
      feature: uint8, 单通道图像
      mask:    uint8, 与 feature 同尺寸的 0/255 mask，或 None
    """
    if img.ndim == 2:
        gray = img
        alpha = None
    else:
        if img.shape[2] == 4:
            bgr = img[..., :3]
            alpha = img[..., 3]
        else:
            bgr = img[..., :3]
            alpha = None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # 小的高斯模糊，抑制噪声
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    # x 方向梯度（滑块只在水平方向移动，用 x 梯度更有针对性）
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_x = cv2.convertScaleAbs(grad_x)

    # 对梯度做一次对比度增强，使边缘更加明显
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    grad_x = clahe.apply(grad_x)

    if use_alpha_mask and img.ndim == 3 and img.shape[2] == 4:
        # 只对不透明区域做匹配
        mask = np.where(alpha > 0, 255, 0).astype("uint8")
    else:
        mask = None

    return grad_x, mask


def match_slider_images(bg: np.ndarray, sd: np.ndarray):
    """
    对已经解码到内存的图像做匹配。
    返回 (top_left, score, bg_img, sd_img)
    """
    if bg is None:
        raise RuntimeError("无法读取背景图像数据")
    if sd is None:
        raise RuntimeError("无法读取滑块图像数据")

    bg_feat, _ = preprocess_for_match(bg, use_alpha_mask=False)
    sd_feat, mask = preprocess_for_match(sd, use_alpha_mask=True)

    method = cv2.TM_CCORR_NORMED  # 对应“相关系数归一化”，数值越大越相似

    if mask is not None:
        res = cv2.matchTemplate(bg_feat, sd_feat, method, mask=mask)
    else:
        res = cv2.matchTemplate(bg_feat, sd_feat, method)

    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
    top_left = max_loc
    score = float(max_val)

    return top_left, score, bg, sd


def match_slider(bg_path: str, sd_path: str):
    """
    从磁盘读取图片后做匹配。
    """
    bg = cv2.imread(bg_path, cv2.IMREAD_COLOR)
    if bg is None:
        raise RuntimeError(f"无法读取背景图: {bg_path}")

    sd = cv2.imread(sd_path, cv2.IMREAD_UNCHANGED)
    if sd is None:
        raise RuntimeError(f"无法读取滑块图: {sd_path}")

    return match_slider_images(bg, sd)


def overlay_slider(bg: np.ndarray, sd: np.ndarray, top_left):
    """
    把滑块按匹配结果的坐标叠加回背景图，方便肉眼检查。
    支持 BGRA（带透明通道）的滑块。
    """
    x, y = int(top_left[0]), int(top_left[1])
    h_bg, w_bg = bg.shape[:2]
    h_sd, w_sd = sd.shape[:2]

    if sd.ndim == 2:
        sd_bgr = cv2.cvtColor(sd, cv2.COLOR_GRAY2BGR)
        alpha = np.ones((h_sd, w_sd), dtype="float32")
    elif sd.shape[2] == 3:
        sd_bgr = sd
        alpha = np.ones((h_sd, w_sd), dtype="float32")
    else:
        sd_bgr = sd[..., :3]
        alpha = sd[..., 3].astype("float32") / 255.0

    out = bg.copy()

    # 计算 ROI，注意边界裁剪
    x1 = max(x, 0)
    y1 = max(y, 0)
    x2 = min(x + w_sd, w_bg)
    y2 = min(y + h_sd, h_bg)

    if x1 >= x2 or y1 >= y2:
        # 完全落在图外，直接返回原图
        return out

    roi = out[y1:y2, x1:x2]
    sd_crop = sd_bgr[y1 - y:y2 - y, x1 - x:x2 - x]
    alpha_crop = alpha[y1 - y:y2 - y, x1 - x:x2 - x]

    alpha_crop_3 = alpha_crop[..., None]

    blended = alpha_crop_3 * sd_crop.astype("float32") + (1.0 - alpha_crop_3) * roi.astype("float32")
    roi[:] = blended.astype("uint8")

    return out


def make_grid(images, rows: int, cols: int, tile_width: int = 320):
    """
    把若干张图像按 rows x cols 拼成一张大图。
    会先把每张图等比例缩放到统一宽度 tile_width。
    """
    if not images:
        raise ValueError("images 不能为空")

    h0, w0 = images[0].shape[:2]
    scale = float(tile_width) / float(w0)
    tile_h = int(round(h0 * scale))
    tile_w = tile_width

    resized = [cv2.resize(img, (tile_w, tile_h)) for img in images]

    canvas_h = rows * tile_h
    canvas_w = cols * tile_w
    canvas = np.ones((canvas_h, canvas_w, 3), dtype="uint8") * 255

    for idx, img in enumerate(resized):
        if idx >= rows * cols:
            break
        r = idx // cols
        c = idx % cols
        y1 = r * tile_h
        y2 = y1 + tile_h
        x1 = c * tile_w
        x2 = x1 + tile_w
        canvas[y1:y2, x1:x2] = img

    return canvas


def parse_args():
    parser = argparse.ArgumentParser(description="滑块验证码匹配 demo")
    parser.add_argument(
        "--data_dir", type=str, default="pics",
        help="包含 bg*.jpeg / sd*.png 的数据目录"
    )
    parser.add_argument(
        "--output_dir", type=str, default="output",
        help="输出可视化结果和结果文本的目录"
    )
    parser.add_argument(
        "--rows", type=int, default=5,
        help="每张大图中小图的行数"
    )
    parser.add_argument(
        "--cols", type=int, default=5,
        help="每张大图中小图的列数"
    )
    parser.add_argument(
        "--tile_width", type=int, default=320,
        help="单张结果图在拼接前缩放到的宽度，越大细节越清晰，但大图文件也越大"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    pairs = build_pairs(args.data_dir)
    if not pairs:
        raise RuntimeError("没有找到任何可配对的 bg/sd 图像。")

    per_grid = args.rows * args.cols

    results_path = os.path.join(args.output_dir, "results.txt")
    f = open(results_path, "w", encoding="utf-8")
    f.write("id\tbg_name\tsd_name\tx\ty\tscore\n")

    print(f"Found {len(pairs)} pairs. Outputting {args.rows}x{args.cols} grids (every {per_grid} items).")

    grid_idx = 0
    idx_in_chunk = 0
    tiles = []

    for idx, bg_path, sd_path in pairs:
        top_left, score, bg_img, sd_img = match_slider(bg_path, sd_path)
        x, y = top_left

        vis = overlay_slider(bg_img, sd_img, top_left)

        # 在图上写上编号和匹配信息，方便肉眼检查
        text = f"id={idx}, x={x}, score={score:.3f}"
        cv2.putText(
            vis, text, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8,
            (0, 0, 255), 2, cv2.LINE_AA
        )

        tiles.append(vis)

        f.write(f"{idx}\t{os.path.basename(bg_path)}\t{os.path.basename(sd_path)}\t{x}\t{y}\t{score:.6f}\n")

        idx_in_chunk += 1

        # 每满 rows*cols 张，输出一张大图
        if idx_in_chunk == per_grid:
            grid_img = make_grid(tiles, args.rows, args.cols, tile_width=args.tile_width)
            out_path = os.path.join(args.output_dir, f"grid_{grid_idx:03d}.png")
            cv2.imwrite(out_path, grid_img)
            print(f"Saved visualization: {out_path}")

            grid_idx += 1
            idx_in_chunk = 0
            tiles = []

    # 如果最后一组不足 rows*cols，也要输出
    if tiles:
        grid_img = make_grid(tiles, args.rows, args.cols, tile_width=args.tile_width)
        out_path = os.path.join(args.output_dir, f"grid_{grid_idx:03d}.png")
        cv2.imwrite(out_path, grid_img)
        print(f"Saved visualization: {out_path}")

    f.close()
    print(f"Detailed results written to: {results_path}")


if __name__ == "__main__":
    main()
