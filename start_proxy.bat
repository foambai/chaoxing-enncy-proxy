@echo off
chcp 65001 >nul
cd /d %~dp0
title 言溪题库本地融合代理
echo ============================================
echo  言溪题库本地融合代理 (保持此窗口开启)
echo  查询用量可在 tk.enncy.cn 的 dashboard 查看
echo ============================================
python enncy_proxy.py
pause
