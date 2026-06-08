#!/usr/bin/env python3
"""
香港医院病床实时监控系统 - 启动入口
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from backend.app import app

if __name__ == "__main__":
    print("=" * 60)
    print("  🏥 香港医院病床实时监控系统")
    print("=" * 60)
    print()
    print("  📍 管理后台:  http://127.0.0.1:8989")
    print("  🖥️ 大屏预览:  http://127.0.0.1:8989/preview")
    print()
    print("  📋 功能:")
    print("   · 基础数据管理")
    print("   · 模拟数据生成（含进度条）")
    print("   · 按医院查询病人/维修数据")
    print("   · CSV 下载与打包导出")
    print("   · 四大屏轮播配置")
    print("   · 大屏预览（可调节模拟日期）")
    print()
    print("  ⌨️  Ctrl+C 停止服务")
    print("=" * 60)

    app.run(host="0.0.0.0", port=8989, debug=True, use_reloader=False)
