#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
滑块验证码匹配HTTP服务

启动方式：
    cd slider_match_code
    pip install flask flask-cors
    python slider_server.py

服务地址：http://localhost:5000

API接口：
    POST /match
    请求体: {"bg": "data:image/jpeg;base64,...", "slider": "data:image/png;base64,..."}
    响应: {"rawX": 124, "displayX": 54, "score": 0.68, "success": true}
"""

import os
import sys
import base64
import traceback
import cv2
import numpy as np
from flask import Flask, request, jsonify
from flask_cors import CORS

# 确保能导入同目录的匹配模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_slider_match

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# 坐标转换参数
ORIGINAL_WIDTH = 590
DISPLAY_WIDTH = 260
SCALE_RATIO = DISPLAY_WIDTH / ORIGINAL_WIDTH


def decode_base64_image(data_url, flags):
    """
    解码 base64 图片数据 URL，直接返回 OpenCV 图像

    Args:
        data_url: data:image/jpeg;base64,/9j/4AAQ... 格式的数据
        flags: cv2.imdecode 解码标志

    Returns:
        OpenCV 图像对象
    """
    # 移除data URL前缀
    if ',' in data_url:
        base64_data = data_url.split(',', 1)[1]
    else:
        base64_data = data_url

    # 解码
    image_data = base64.b64decode(base64_data)
    image_array = np.frombuffer(image_data, dtype=np.uint8)
    image = cv2.imdecode(image_array, flags)
    if image is None:
        raise ValueError("invalid image data")
    return image


@app.route('/match', methods=['POST'])
def match():
    """
    滑块匹配接口

    请求体JSON格式:
    {
        "bg": "data:image/jpeg;base64,...",     # 背景图base64
        "slider": "data:image/png;base64,..."   # 滑块图base64
    }

    响应JSON格式:
    {
        "success": true,
        "rawX": 124,        # 原始图片上的X坐标
        "rawY": 85,         # 原始图片上的Y坐标
        "displayX": 54,     # 显示尺寸上的X坐标（用于拖拽）
        "score": 0.6847,    # 匹配置信度
        "message": "匹配成功"
    }
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({
                'success': False,
                'error': '请求体为空',
                'message': '请提供bg和slider图片数据'
            }), 400

        bg_base64 = data.get('bg')
        slider_base64 = data.get('slider')

        if not bg_base64 or not slider_base64:
            return jsonify({
                'success': False,
                'error': '缺少必要参数',
                'message': '请提供bg和slider图片数据'
            }), 400

        # 直接在内存中解码，避免临时文件 I/O
        bg_img = decode_base64_image(bg_base64, cv2.IMREAD_COLOR)
        sd_img = decode_base64_image(slider_base64, cv2.IMREAD_UNCHANGED)

        print(f"[INFO] 接收到匹配请求")
        print(f"  背景图 shape: {getattr(bg_img, 'shape', None)}")
        print(f"  滑块图 shape: {getattr(sd_img, 'shape', None)}")

        # 调用匹配算法
        top_left, score, _, _ = run_slider_match.match_slider_images(bg_img, sd_img)

        raw_x, raw_y = top_left
        display_x = int(raw_x * SCALE_RATIO)

        print(f"[INFO] 匹配结果: rawX={raw_x}, displayX={display_x}, score={score:.4f}")

        return jsonify({
            'success': True,
            'rawX': int(raw_x),
            'rawY': int(raw_y),
            'displayX': display_x,
            'score': round(score, 4),
            'scaleRatio': round(SCALE_RATIO, 4),
            'message': '匹配成功'
        })

    except Exception as e:
        error_msg = str(e)
        traceback_str = traceback.format_exc()
        print(f"[ERROR] 匹配失败: {error_msg}")
        print(traceback_str)

        return jsonify({
            'success': False,
            'error': error_msg,
            'message': '匹配失败，请检查图片格式'
        }), 500

@app.route('/health', methods=['GET'])
def health():
    """健康检查接口"""
    return jsonify({
        'status': 'ok',
        'service': 'slider-match-server',
        'version': '1.0.0'
    })


@app.route('/', methods=['GET'])
def index():
    """首页说明"""
    return """
    <html>
    <head><title>滑块匹配服务</title></head>
    <body>
        <h1>滑块验证码匹配服务</h1>
        <h2>API接口</h2>
        <h3>POST /match</h3>
        <pre>
请求体:
{
    "bg": "data:image/jpeg;base64,...",
    "slider": "data:image/png;base64,..."
}

响应:
{
    "success": true,
    "rawX": 124,
    "displayX": 54,
    "score": 0.68
}
        </pre>
        <h3>GET /health</h3>
        <p>健康检查接口</p>
    </body>
    </html>
    """


if __name__ == '__main__':
    import sys
    import io
    # 修复Windows控制台编码问题
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    print("=" * 50)
    print("Slider Captcha Match Server")
    print("=" * 50)
    print(f"Original image width: {ORIGINAL_WIDTH}px")
    print(f"Display image width: {DISPLAY_WIDTH}px")
    print(f"Scale ratio: {SCALE_RATIO:.4f}")
    print("=" * 50)
    print("Starting server...")
    print("Visit http://localhost:5000 for API docs")
    print("Press Ctrl+C to stop")
    print("=" * 50)

    app.run(host='0.0.0.0', port=5000, debug=False)
