#!/usr/bin/env python3
# Файл для запуска приложения через WSGI-сервер

import os
import threading
from app import app, run_scheduler, init_backup_settings

# Инициализация настроек резервного копирования
init_backup_settings()

# Запуск планировщика резервного копирования в отдельном потоке
backup_thread = threading.Thread(target=run_scheduler, daemon=True)
backup_thread.start()

# Для запуска через gunicorn
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port) 