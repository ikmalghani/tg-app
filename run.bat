@echo off

cd tg-upload
call venv\Scripts\activate.bat
cd ..
python tg-app.py
