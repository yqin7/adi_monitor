"""项目路径常量"""
from pathlib import Path

# app/core/paths.py -> app/core -> app -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
