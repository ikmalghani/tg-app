#!/usr/bin/env bash

set -e

cd tg-upload
source venv/bin/activate
cd ..
python3 tg-app.py
