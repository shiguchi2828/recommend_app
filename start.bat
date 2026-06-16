@echo off
start "Outing App" /D "C:\Users\hs_28\Desktop\outing_recommend_app" "C:\Python314\python.exe" "C:\Users\hs_28\Desktop\outing_recommend_app\app.py"
ping -n 6 127.0.0.1 > nul
start "" "http://127.0.0.1:5000"
