@echo off
REM 小说拆书工程 · 一键入口
REM 双击本文件 → 用 settings.json 的配置重新生成全部可视化产物（拆书详情页 / 知识图谱 / 对比报告）
REM 产物输出到 output/pedia_<小说名>_<日期>/  文件夹（仓库父目录 output/，输出统一），双击 index.html 即可浏览。
REM
REM 想跑全流程（抽取 → 收集 → 挖掘 → 可视化）：
REM   python src/cli.py extract --backend qwen3-8b
REM   python src/cli.py collect --backend qwen3-8b
REM   python src/cli.py mine    --backend qwen3-8b
REM   python src/cli.py viz     --backend qwen3-8b
cd /d "%~dp0"
if "%1"=="" (
    python src/cli.py viz
) else (
    python src/cli.py %*
)
