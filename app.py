from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file
import os
import json
from datetime import datetime, timedelta
import requests
import io
import xlsxwriter
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import lru_cache
import threading
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import schedule
import time
import copy
import multiprocessing
import random
import logging
# Импортируем модули geopy
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import socket

# Настройка логирования для Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('car_raffle')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 год для статических файлов

# Настройка для работы за прокси-сервером - специальная конфигурация для Render
app.wsgi_app = ProxyFix(
    app.wsgi_app, 
    x_for=1,      # X-Forwarded-For
    x_proto=1,    # X-Forwarded-Proto
    x_host=1,     # X-Forwarded-Host
    x_port=1,     # X-Forwarded-Port
    x_prefix=1    # X-Forwarded-Prefix
)

# Переменная для определения, что приложение работает на Render
IS_RENDER = 'RENDER' in os.environ

# По умолчанию разрешаем определение местоположения на Render
if IS_RENDER and os.environ.get('ALLOW_ALL_LOCATIONS') is None:
    os.environ['ALLOW_ALL_LOCATIONS'] = 'true'
    logger.info("Установлен режим ALLOW_ALL_LOCATIONS=true на платформе Render")

# Путь к файлу данных
DATA_FILE = os.environ.get('DATA_FILE', os.path.join(os.path.dirname(__file__), 'participants.json'))

# Путь к файлу с настройками
SETTINGS_FILE = os.environ.get('SETTINGS_FILE', os.path.join(os.path.dirname(__file__), 'settings.json'))

# Добавляем блокировку для безопасной работы с файлом данных при конкурентном доступе
data_lock = threading.Lock()
settings_lock = threading.Lock()

# Создаем файл участников, если он не существует
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump([], f)

# Создаем файл настроек, если он не существует
if not os.path.exists(SETTINGS_FILE):
    with settings_lock:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "whatsapp_link": "https://chat.whatsapp.com/EIa4wkifsVQDttzjOKlOY3"
            }, f, ensure_ascii=False, indent=4)

# Кэш для настроек с временем жизни
settings_cache = {
    'data': None,
    'timestamp': 0
}
SETTINGS_CACHE_TTL = 60  # 60 секунд

# Настройки для резервного копирования
BACKUP_SETTINGS = {
    'enabled': True,
    'interval': 'daily',  # daily, hourly, custom
    'yandex_token': 'y0__xDy1a_hARjblgMguuSn6xJXlhubBW4-LmJ7Gq8ZG8kwV-zyIw',  # OAuth-токен Яндекс.Диска
    'last_backup': None,
    'custom_value': 24,    # Значение для произвольного интервала
    'custom_unit': 'hours' # Единица измерения: seconds, minutes, hours, days, weeks
}

# Добавляем событие для сигнализации об изменении настроек планировщика
scheduler_event = threading.Event()

def load_settings():
    """Загрузка настроек из файла с кэшированием"""
    global settings_cache
    current_time = datetime.now().timestamp()
    
    # Если есть актуальные данные в кэше, возвращаем их
    if settings_cache['data'] is not None and current_time - settings_cache['timestamp'] < SETTINGS_CACHE_TTL:
        return settings_cache['data']
    
    # Иначе загружаем из файла
    with settings_lock:
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                # Обновляем кэш
                settings_cache['data'] = settings
                settings_cache['timestamp'] = current_time
                return settings
        except:
            # В случае ошибки возвращаем настройки по умолчанию
            default_settings = {
                "whatsapp_link": "https://chat.whatsapp.com/EIa4wkifsVQDttzjOKlOY3"
            }
            settings_cache['data'] = default_settings
            settings_cache['timestamp'] = current_time
            return default_settings

def save_settings(settings_data):
    """Сохранение настроек в файл"""
    with settings_lock:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, ensure_ascii=False, indent=4)
        
        # Обновляем кэш
        settings_cache['data'] = settings_data
        settings_cache['timestamp'] = datetime.now().timestamp()

# Список допустимых городов и районов
ALLOWED_CITIES = [
    # Основные города
    'махачкала', 'каспийск',
    
    # Районы Махачкалы
    'кировский район', 'ленинский район', 'советский район',
    
    # Посёлки городского типа Кировского района
    'ленинкент', 'семендер', 'сулак', 'шамхал',
    
    # Сёла Кировского района
    'богатырёвка', 'красноармейское', 'остров чечень', 'шамхал-термен',
    
    # Посёлки и сёла Ленинского района
    'новый кяхулай', 'новый хушет', 'талги',
    
    # Посёлки Советского района
    'альбурикент', 'кяхулай', 'тарки',
    
    # Микрорайоны и районы
    '5-й посёлок', '5 посёлок',
    
    # Дополнительные микрорайоны и кварталы
    'каменный карьер', 'афган-городок', 'кемпинг', 'кирпичный', 
    'ккоз', 'тау', 'центральный', 'южный', 'рекреационная зона', 'финский квартал',
    
    # Пригородные районы
    'турали'
]

# Для тестирования на хостинге - разрешаем все города, если установлена переменная окружения
if os.environ.get('ALLOW_ALL_LOCATIONS') == 'true':
    def check_location_allowed(city):
        return True
else:
    def check_location_allowed(city):
        return city in ALLOWED_CITIES

# Функция для безопасного получения реального IP-адреса клиента
def get_client_ip():
    """Получение реального IP-адреса клиента с учетом особенностей Render"""
    if IS_RENDER:
        # На Render IP может быть в нескольких заголовках
        ip = request.headers.get('X-Forwarded-For')
        if ip:
            # Берем первый IP из списка (может быть несколько через запятую)
            ip = ip.split(',')[0].strip()
            logger.info(f"IP из X-Forwarded-For: {ip}")
            return ip
            
        # Проверяем другие возможные заголовки
        for header in ['X-Real-IP', 'CF-Connecting-IP', 'True-Client-IP']:
            ip = request.headers.get(header)
            if ip:
                logger.info(f"IP из {header}: {ip}")
                return ip
                
    # Если не нашли в заголовках или не на Render, используем стандартный метод
    ip = request.remote_addr
    logger.info(f"IP из remote_addr: {ip}")
    return ip

# Кэш для данных о местоположении по IP
ip_location_cache = {}

# Время жизни кэша местоположения (1 час)
IP_CACHE_TTL = 3600

@lru_cache(maxsize=128)
def get_location_from_ip(ip_address):
    """Получение информации о местоположении по IP-адресу с использованием нескольких методов"""
    # Проверяем кэш
    current_time = datetime.now().timestamp()
    if ip_address in ip_location_cache:
        cache_entry = ip_location_cache[ip_address]
        if current_time - cache_entry['timestamp'] < IP_CACHE_TTL:
            logger.info(f"Использован кэш для IP {ip_address}")
            return cache_entry['data']
    
    try:
        # Для тестового режима и локальной разработки
        if ip_address == '127.0.0.1' or ip_address == 'localhost':
            logger.info(f"Локальная разработка, возвращаем тестовые данные для IP: {ip_address}")
            return {
                'city': 'махачкала',
                'region': 'Дагестан',
                'country': 'Россия'
            }
        
        logger.info(f"Определение местоположения для IP: {ip_address}")
        
        # Пытаемся использовать ip-api.com (надежный сервис)
        try:
            logger.info(f"Пробуем определить местоположение через ip-api.com")
            response = requests.get(f"http://ip-api.com/json/{ip_address}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    logger.info(f"Успешно получили данные от ip-api.com: {data}")
                    result = {
                        'city': data.get('city', '').lower(),
                        'region': data.get('regionName', ''),
                        'country': data.get('country', '')
                    }
                    # Сохраняем в кэш
                    ip_location_cache[ip_address] = {
                        'data': result,
                        'timestamp': current_time
                    }
                    return result
                else:
                    logger.warning(f"ip-api.com вернул ошибку: {data}")
            else:
                logger.warning(f"ip-api.com вернул код {response.status_code}")
        except Exception as e:
            logger.error(f"Ошибка при использовании ip-api.com: {e}")
        
        # Пробуем альтернативный сервис ipinfo.io
        try:
            logger.info(f"Пробуем определить местоположение через ipinfo.io")
            response = requests.get(f"https://ipinfo.io/{ip_address}/json", timeout=5)
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Получили данные от ipinfo.io: {data}")
                if 'city' in data:
                    result = {
                        'city': data.get('city', '').lower(),
                        'region': data.get('region', ''),
                        'country': data.get('country', '')
                    }
                    # Сохраняем в кэш
                    ip_location_cache[ip_address] = {
                        'data': result,
                        'timestamp': current_time
                    }
                    return result
            else:
                logger.warning(f"ipinfo.io вернул код {response.status_code}")
        except Exception as e:
            logger.error(f"Ошибка при использовании ipinfo.io: {e}")
        
        # Как запасной вариант для IP-адресов Дагестана, определяем по диапазону
        # Для примера (это надо заменить на реальные диапазоны Дагестана)
        dagestan_ip_ranges = [
            '176.15.', '95.153.', '62.183.', '5.164.', '46.61.'
        ]
        
        for prefix in dagestan_ip_ranges:
            if ip_address.startswith(prefix):
                logger.info(f"IP {ip_address} определен как IP из Дагестана по диапазону")
                return {
                    'city': 'махачкала',
                    'region': 'Дагестан',
                    'country': 'Россия'
                }
        
        # Пытаемся использовать geopy как последний вариант
        logger.info(f"Пробуем определить местоположение через geopy")
        try:
            geolocator = Nominatim(user_agent="car_raffle_app_v2")
            location = geolocator.geocode(ip_address, timeout=5)
            
            if location:
                logger.info(f"Geopy нашел местоположение: {location.address}")
                address = geolocator.reverse(f"{location.latitude}, {location.longitude}", timeout=5)
                
                if address and address.raw.get('address'):
                    address_data = address.raw['address']
                    city = address_data.get('city', '').lower()
                    if not city:
                        city = address_data.get('town', '').lower()
                    if not city:
                        city = address_data.get('village', '').lower()
                    
                    result = {
                        'city': city,
                        'region': address_data.get('state', ''),
                        'country': address_data.get('country', '')
                    }
                    
                    logger.info(f"Определены данные через geopy: {result}")
                    
                    # Сохраняем в кэш
                    ip_location_cache[ip_address] = {
                        'data': result,
                        'timestamp': current_time
                    }
                    return result
            else:
                logger.warning(f"Geopy не смог найти местоположение для IP {ip_address}")
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            logger.error(f"Ошибка при определении местоположения через geopy: {e}")
        
        # Временный вариант: возвращаем данные по умолчанию
        logger.warning(f"Не удалось определить город, возвращаем неизвестный город")
        return {
            'city': 'неизвестный город',
            'region': 'неизвестный регион',
            'country': 'Россия'
        }
        
    except Exception as e:
        logger.error(f"Критическая ошибка при определении местоположения: {e}")
        # Безопасное возвращение значения по умолчанию в случае ошибки
        return {
            'city': 'неизвестный город',
            'region': 'неизвестный регион',
            'country': 'Россия'
        }

@lru_cache(maxsize=128)
def get_location_from_coordinates(lat, lng):
    """Получение информации о местоположении по координатам с использованием нескольких методов"""
    try:
        logger.info(f"Определение местоположения по координатам: {lat}, {lng}")
        
        # Прямой запрос к OpenStreetMap Nominatim API
        try:
            logger.info(f"Пробуем определить через прямой запрос к OSM API")
            response = requests.get(
                f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}&zoom=18&addressdetails=1",
                headers={'User-Agent': 'CarRaffle/1.0'},
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Данные от OSM API: {data}")
                if 'address' in data:
                    city = data['address'].get('city', '').lower()
                    if not city:
                        city = data['address'].get('town', '').lower()
                    if not city:
                        city = data['address'].get('village', '').lower()
                    if not city and 'state' in data['address'] and 'дагестан' in data['address']['state'].lower():
                        city = 'махачкала'
                        
                    logger.info(f"Определен город через OSM API: {city}")
                    
                    return {
                        'city': city,
                        'region': data['address'].get('state', ''),
                        'country': data['address'].get('country', '')
                    }
        except Exception as e:
            logger.error(f"Ошибка при использовании OSM API: {e}")
        
        # Создаем геокодер geopy
        logger.info(f"Пробуем определить через geopy")
        geolocator = Nominatim(user_agent="car_raffle_app_v2")
        
        # Получаем информацию о местоположении по координатам
        location = geolocator.reverse(f"{lat}, {lng}", timeout=5)
        
        if location and location.raw.get('address'):
            address_data = location.raw['address']
            logger.info(f"Данные от geopy: {address_data}")
            
            city = address_data.get('city', '').lower()
            if not city:
                city = address_data.get('town', '').lower()
            if not city:
                city = address_data.get('village', '').lower()
            if not city and 'locality' in address_data:
                city = address_data.get('locality', '').lower()
            if not city and 'suburb' in address_data:
                city = address_data.get('suburb', '').lower()
            
            # Проверка на районы Махачкалы
            if not city and 'state' in address_data and 'state_district' in address_data:
                state = address_data.get('state', '').lower()
                district = address_data.get('state_district', '').lower()
                if 'дагестан' in state and ('махачкала' in district or 'махачкалинский' in district):
                    city = 'махачкала'
                    
            # Запасной вариант для Дагестана
            if not city and 'state' in address_data and 'дагестан' in address_data.get('state', '').lower():
                # Проверяем попадание в координаты Махачкалы (грубое приближение)
                if 42.9 <= float(lat) <= 43.1 and 47.3 <= float(lng) <= 47.6:
                    city = 'махачкала'
            
            logger.info(f"Определен город через geopy: {city}")
                    
            return {
                'city': city,
                'region': address_data.get('state', ''),
                'country': address_data.get('country', '')
            }
        else:
            logger.warning(f"Geopy не вернул данных для координат {lat}, {lng}")
        
        # Проверка попадания в область Махачкалы (грубый вариант)
        if 42.9 <= float(lat) <= 43.1 and 47.3 <= float(lng) <= 47.6:
            logger.info(f"Координаты {lat}, {lng} попадают в регион Махачкалы")
            return {
                'city': 'махачкала',
                'region': 'Дагестан',
                'country': 'Россия'
            }
            
        # В случае неудачи, возвращаем данные по умолчанию
        logger.warning(f"Не удалось определить город по координатам, возвращаем данные по умолчанию")
        return {
            'city': 'неизвестный город',
            'region': 'неизвестный регион',
            'country': 'Россия'
        }
    except Exception as e:
        logger.error(f"Критическая ошибка при определении местоположения по координатам: {e}")
        # Безопасное возвращение значения по умолчанию в случае ошибки
        return {
            'city': 'неизвестный город',
            'region': 'неизвестный регион',
            'country': 'Россия'
        }

# Кэш для участников с временем жизни
participants_cache = {
    'data': None,
    'timestamp': 0
}
PARTICIPANTS_CACHE_TTL = 60  # 60 секунд

def load_participants():
    """Загрузка данных участников из файла с кэшированием"""
    global participants_cache
    current_time = datetime.now().timestamp()
    
    # Если есть актуальные данные в кэше, возвращаем их
    if participants_cache['data'] is not None and current_time - participants_cache['timestamp'] < PARTICIPANTS_CACHE_TTL:
        return participants_cache['data']
    
    # Иначе загружаем из файла
    with data_lock:
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                participants = json.load(f)
                # Обновляем кэш
                participants_cache['data'] = participants
                participants_cache['timestamp'] = current_time
                return participants
        except:
            return []

def save_participant(participant_data):
    """Сохранение данных участника в файл"""
    with data_lock:
        participants = load_participants()
        participants.append(participant_data)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(participants, f, ensure_ascii=False, indent=4)
        
        # Обновляем кэш
        participants_cache['data'] = participants
        participants_cache['timestamp'] = datetime.now().timestamp()

def is_phone_registered(phone):
    """Проверка, зарегистрирован ли уже данный номер телефона"""
    participants = load_participants()
    # Нормализуем телефон для сравнения (удаляем все, кроме цифр)
    normalized_phone = ''.join(filter(str.isdigit, phone))
    
    for participant in participants:
        normalized_participant_phone = ''.join(filter(str.isdigit, participant['phone']))
        if normalized_participant_phone == normalized_phone:
            return True
    return False

def get_ticket_by_phone(phone):
    """Получение данных участника по номеру телефона"""
    participants = load_participants()
    # Нормализуем телефон для сравнения (удаляем все, кроме цифр)
    normalized_phone = ''.join(filter(str.isdigit, phone))
    
    for participant in participants:
        # Убедимся, что у участника есть номер телефона
        if not participant.get('phone'):
            continue
            
        normalized_participant_phone = ''.join(filter(str.isdigit, participant['phone']))
        
        # Сначала проверяем точное совпадение
        if normalized_participant_phone == normalized_phone:
            return {
                'ticket_number': participant.get('ticket_number'),
                'full_name': participant.get('full_name')
            }
        
        # Если номера имеют разную длину, но последние 10 цифр совпадают 
        # (разные форматы записи российских номеров: +7/8 в начале)
        if (len(normalized_participant_phone) >= 10 and len(normalized_phone) >= 10 and 
            normalized_participant_phone[-10:] == normalized_phone[-10:]):
            return {
                'ticket_number': participant.get('ticket_number'),
                'full_name': participant.get('full_name')
            }
    
    return None

# Функция для генерации уникального 4-значного номера
def generate_unique_ticket_number():
    """Генерация последовательного номера участника (1, 2, 3, ...)"""
    participants = load_participants()
    
    # Если список участников пуст, начинаем с 1
    if not participants:
        return 1
    
    # Находим максимальный существующий номер
    max_number = 0
    for participant in participants:
        ticket_number = participant.get('ticket_number', 0)
        if isinstance(ticket_number, (int, float)) and ticket_number > max_number:
            max_number = ticket_number
    
    # Получаем следующий номер (просто увеличиваем максимальный на 1)
    next_number = max_number + 1
    
    return next_number

@app.route('/')
def index():
    """Главная страница с формой регистрации"""
    settings = load_settings()
    return render_template('index.html', whatsapp_link=settings.get('whatsapp_link'))

@app.route('/check-coordinates')
def check_coordinates():
    """Проверка местоположения пользователя по координатам"""
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    
    logger.info(f"Запрос на проверку координат: lat={lat}, lng={lng}")
    
    if not lat or not lng:
        logger.warning("Не указаны координаты в запросе")
        return jsonify({"status": "error", "message": "Не указаны координаты"})
    
    # Извлекаем реальный IP-адрес пользователя с учетом особенностей Render
    ip_address = get_client_ip()
    logger.info(f"IP-адрес пользователя: {ip_address}")
    
    try:
        # Пытаемся определить местоположение даже на Render
        location = get_location_from_coordinates(lat, lng)
        
        if not location:
            logger.warning("Не удалось определить местоположение по координатам")
            # Если не удалось определить по координатам, пробуем по IP
            logger.info("Пробуем определить по IP-адресу")
            location = get_location_from_ip(ip_address)
            
            if not location:
                logger.error("Не удалось определить местоположение ни по координатам, ни по IP")
                # На Render разрешаем доступ в любом случае
                if IS_RENDER:
                    return jsonify({
                        "status": "success", 
                        "allowed": True,
                        "city": "неизвестный город (render)"
                    })
                else:
                    return jsonify({"status": "error", "message": "Не удалось определить местоположение"})
        
        city = location.get('city', '').lower()
        logger.info(f"Определенный город: {city}")
        
        # Дополнительная проверка для неизвестных городов в Дагестане
        if city == 'неизвестный город' and location.get('region', '').lower() == 'дагестан':
            logger.info("Неизвестный город в Дагестане, предполагаем Махачкалу")
            city = 'махачкала'
            
        # Для хостинга и тестирования - принудительно разрешаем всем
        if os.environ.get('ALLOW_ALL_LOCATIONS') == 'true' or IS_RENDER:
            logger.info("ALLOW_ALL_LOCATIONS=true или Render, разрешаем участие для всех")
            allowed = True
        else:
            allowed = check_location_allowed(city)
            logger.info(f"Результат проверки города {city}: разрешено={allowed}")
        
        return jsonify({
            "status": "success", 
            "allowed": allowed,
            "city": city
        })
    except Exception as e:
        logger.error(f"Ошибка при обработке запроса координат: {e}")
        # В случае ошибки на Render разрешаем пользователю участвовать
        if IS_RENDER:
            return jsonify({
                "status": "success", 
                "allowed": True,
                "city": "неизвестный город (ошибка определения)"
            })
        else:
            return jsonify({"status": "error", "message": f"Ошибка при определении местоположения: {str(e)}"})

@app.route('/check-location')
def check_location():
    """Проверка местоположения пользователя по IP"""
    # Извлекаем реальный IP-адрес пользователя с учетом особенностей Render
    ip_address = get_client_ip()
    logger.info(f"Запрос на проверку IP: {ip_address}")
    
    # Для локальной разработки используем тестовый режим
    if ip_address == '127.0.0.1' or ip_address == 'localhost':
        logger.info("Локальная разработка, возвращаем тестовый режим")
        return jsonify({"status": "success", "allowed": True, "city": "махачкала (тестовый режим)"})
    
    try:
        # Пытаемся определить местоположение даже на Render
        location = get_location_from_ip(ip_address)
        
        if not location:
            logger.warning(f"Не удалось определить местоположение для IP {ip_address}")
            # На Render разрешаем доступ в любом случае
            if IS_RENDER:
                return jsonify({
                    "status": "success", 
                    "allowed": True,
                    "city": "неизвестный город (render)"
                })
            else:
                return jsonify({"status": "error", "message": "Не удалось определить местоположение"})
        
        city = location.get('city', '').lower()
        logger.info(f"Определенный город по IP: {city}")
        
        # Для хостинга и тестирования - принудительно разрешаем всем
        if os.environ.get('ALLOW_ALL_LOCATIONS') == 'true' or IS_RENDER:
            logger.info("ALLOW_ALL_LOCATIONS=true или Render, разрешаем участие для всех")
            allowed = True
        else:
            allowed = check_location_allowed(city)
            logger.info(f"Результат проверки города {city}: разрешено={allowed}")
        
        return jsonify({
            "status": "success", 
            "allowed": allowed,
            "city": city
        })
    except Exception as e:
        logger.error(f"Ошибка при обработке запроса IP: {e}")
        # В случае ошибки на Render разрешаем пользователю участвовать
        if IS_RENDER:
            return jsonify({
                "status": "success", 
                "allowed": True,
                "city": "неизвестный город (ошибка определения)"
            })
        else:
            return jsonify({"status": "error", "message": f"Ошибка при определении местоположения: {str(e)}"})

@app.route('/check-phone')
def check_phone():
    """Проверка существования номера телефона в базе данных"""
    phone = request.args.get('phone')
    
    if not phone:
        return jsonify({"exists": False})
    
    # Проверяем, зарегистрирован ли уже данный номер телефона
    if is_phone_registered(phone):
        return jsonify({
            "exists": True, 
            "message": "Этот номер телефона уже зарегистрирован в розыгрыше. Регистрация возможна только один раз."
        })
    
    return jsonify({"exists": False})

@app.route('/register', methods=['POST'])
def register():
    """Регистрация участника"""
    # Проверка на AJAX-запрос
    is_ajax_request = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    # Получение данных из формы
    full_name = request.form.get('full_name')
    phone = request.form.get('phone')
    age = request.form.get('age')
    gender = request.form.get('gender')
    
    # Валидация данных
    if not full_name or not phone or not age or not gender:
        if is_ajax_request:
            return jsonify({'success': False, 'message': 'Пожалуйста, заполните все поля формы!'}), 400
        flash('Пожалуйста, заполните все поля формы!', 'danger')
        return redirect(url_for('index'))
    
    # Проверка, зарегистрирован ли уже данный номер телефона
    if is_phone_registered(phone):
        if is_ajax_request:
            return jsonify({'success': False, 'message': 'Этот номер телефона уже зарегистрирован в розыгрыше. Регистрация возможна только один раз.'}), 400
        flash('Этот номер телефона уже зарегистрирован в розыгрыше. Регистрация возможна только один раз.', 'danger')
        return redirect(url_for('index'))
    
    # Получение координат
    latitude = request.form.get('latitude')
    longitude = request.form.get('longitude')
    
    # Проверка местоположения по координатам, если они предоставлены
    location = None
    is_allowed = False
    
    # Если установлена переменная окружения, то разрешаем всем
    if os.environ.get('ALLOW_ALL_LOCATIONS') == 'true':
        is_allowed = True
    else:
        if latitude and longitude:
            location = get_location_from_coordinates(latitude, longitude)
            if location and check_location_allowed(location.get('city', '').lower()):
                is_allowed = True
        
        # Если координаты не предоставлены или не удалось определить местоположение,
        # пробуем определить по IP
        if not is_allowed:
            ip_address = request.remote_addr
            if ip_address == '127.0.0.1':  # Для локальной разработки
                is_allowed = True
            else:
                ip_location = get_location_from_ip(ip_address)
                if ip_location and check_location_allowed(ip_location.get('city', '').lower()):
                    is_allowed = True
                    location = ip_location
    
    # Если пользователь не из разрешенного города
    if not is_allowed:
        if is_ajax_request:
            return jsonify({'success': False, 'message': 'К сожалению, вы не можете участвовать в розыгрыше. Розыгрыш доступен только для жителей Махачкалы и Каспийска.'}), 400
        return redirect(url_for('index'))
    
    # Генерация уникального номера участника
    ticket_number = generate_unique_ticket_number()
    
    # Создание записи об участнике
    participant = {
        'full_name': full_name,
        'phone': phone,
        'age': age,
        'gender': gender,
        'ip_address': request.remote_addr,
        'location': location,
        'coordinates': {
            'latitude': latitude,
            'longitude': longitude,
            'city': location.get('city', '') if location else None
        } if latitude and longitude else None,
        'registration_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ticket_number': ticket_number  # Используем уникальный 4-значный номер
    }
    
    # Сохранение данных участника
    save_participant(participant)
    
    # Получаем общее количество участников для определения номера
    participants = load_participants()
    participant_number = len(participants)
    
    # Сохраняем номер билета в сессии для возможности получения его позже
    session['ticket_number'] = participant['ticket_number']
    
    # Возвращаем разные ответы в зависимости от типа запроса
    if is_ajax_request:
        return jsonify({
            'success': True, 
            'message': 'Вы успешно зарегистрированы для участия в розыгрыше!',
            'participant_number': participant_number,
            'ticket_number': participant['ticket_number']  # Отправляем номер билета в ответе
        })
    
    # Перенаправление на страницу успеха с передачей номера билета в URL
    flash('Вы успешно зарегистрированы для участия в розыгрыше!', 'success')
    return redirect(url_for('success', ticket=participant['ticket_number']))

@app.route('/success')
def success():
    """Страница успешной регистрации"""
    return render_template('success.html')

@app.route('/get-ticket-number')
def get_ticket_number():
    """Получение номера участника из сессии"""
    ticket_number = session.get('ticket_number')
    if ticket_number:
        return jsonify({'success': True, 'ticket_number': ticket_number})
    else:
        return jsonify({'success': False, 'message': 'Номер не найден. Возможно, вы еще не зарегистрировались или сессия истекла.'}), 404

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    """Административная панель"""
    # В реальном проекте здесь должна быть надежная авторизация
    if request.method == 'POST':
        password = request.form.get('password')
        secure_password = "kvdarit_avto35"  # Новый пароль администратора
        if password == secure_password:  # Безопасный пароль с комбинацией букв, цифр и специальных символов
            session['admin'] = True
        else:
            flash('Неверный пароль!', 'danger')
    
    if session.get('admin'):
        all_participants = load_participants()
        settings = load_settings()
        
        # Получаем параметры пагинации из запроса
        page = request.args.get('page', 1, type=int)
        per_page = 30  # Количество участников на странице
        
        # Вычисляем общее количество страниц
        total_participants = len(all_participants)
        total_pages = (total_participants + per_page - 1) // per_page  # Округление вверх
        
        # Проверяем корректность номера страницы
        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages
        
        # Получаем участников для текущей страницы
        start_idx = (page - 1) * per_page
        end_idx = min(start_idx + per_page, total_participants)
        current_participants = all_participants[start_idx:end_idx]
        
        return render_template('admin.html', 
                              participants=current_participants, 
                              settings=settings,
                              pagination={
                                  'page': page,
                                  'per_page': per_page,
                                  'total_pages': total_pages,
                                  'total_participants': total_participants
                              })
    else:
        return render_template('admin_login.html')

@app.route('/delete-participants', methods=['POST'])
def delete_participants():
    # Проверка, что пользователь является администратором
    if not session.get('admin'):
        return jsonify({'success': False, 'message': 'Доступ запрещен'}), 403
    
    try:
        # Очистка файла participants.json
        with data_lock:
            with open(DATA_FILE, 'w') as f:
                json.dump([], f)
            
            # Обновляем кэш
            participants_cache['data'] = []
            participants_cache['timestamp'] = datetime.now().timestamp()
            
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/delete-participant/<int:index>', methods=['POST'])
def delete_participant(index):
    # Проверка, что пользователь является администратором
    if not session.get('admin'):
        return jsonify({'success': False, 'message': 'Доступ запрещен'}), 403
    
    try:
        # Загрузка списка участников
        with data_lock:
            participants = load_participants()
            
            # Проверка валидности индекса
            if index < 0 or index >= len(participants):
                return jsonify({'success': False, 'message': 'Участник не найден'}), 404
            
            # Удаление участника
            del participants[index]
            
            # Сохранение обновленного списка
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(participants, f, ensure_ascii=False, indent=4)
            
            # Обновляем кэш
            participants_cache['data'] = participants
            participants_cache['timestamp'] = datetime.now().timestamp()
                
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/export-to-excel', methods=['GET'])
def export_to_excel():
    """Генерация Excel-файла с данными участников"""
    # Проверка, что пользователь является администратором
    if not session.get('admin'):
        flash('Доступ запрещен. Пожалуйста, войдите как администратор.', 'danger')
        return redirect(url_for('admin'))
    
    try:
        # Загрузка данных участников
        participants = load_participants()
        
        # Создание объекта для записи Excel-файла
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)
        worksheet = workbook.add_worksheet('Участники')
        
        # Форматирование
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#007bff',
            'font_color': 'white',
            'border': 1
        })
        
        cell_format = workbook.add_format({
            'border': 1
        })
        
        # Установка ширины столбцов
        worksheet.set_column('A:A', 25)  # Имя
        worksheet.set_column('B:B', 10)  # Номер участника
        worksheet.set_column('C:C', 20)  # Телефон
        worksheet.set_column('D:D', 10)  # Возраст
        worksheet.set_column('E:E', 15)  # Пол
        worksheet.set_column('F:F', 20)  # Город
        worksheet.set_column('G:G', 20)  # Регион
        worksheet.set_column('H:H', 20)  # Страна
        worksheet.set_column('I:I', 25)  # Время регистрации
        worksheet.set_column('J:J', 30)  # Координаты
        worksheet.set_column('K:K', 20)  # IP-адрес
        
        # Заголовки столбцов
        headers = [
            'Имя', 'Номер участника', 'Телефон', 'Возраст', 'Пол', 'Город', 'Регион', 'Страна', 
            'Время регистрации', 'Координаты', 'IP-адрес'
        ]
        
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
        
        # Заполнение данными
        for i, participant in enumerate(participants):
            row = i + 1
            
            # Безопасное извлечение данных
            full_name = str(participant.get('full_name', ''))
            ticket_number = str(participant.get('ticket_number', ''))
            phone = str(participant.get('phone', ''))
            age = str(participant.get('age', ''))
            gender = 'Мужской' if str(participant.get('gender', '')) == 'male' else 'Женский'
            
            # Безопасное извлечение данных о местоположении
            city = ''
            region = ''
            country = ''
            
            # Получение города из координат (если они есть)
            coordinates = participant.get('coordinates', {})
            if coordinates and isinstance(coordinates, dict):
                city_from_coords = coordinates.get('city', '')
                if city_from_coords:
                    city = city_from_coords
            
            # Если город не определен из координат, пробуем получить его из location
            if not city:
                location = participant.get('location', {})
                if location and isinstance(location, dict):
                    city = location.get('city', '')
                    region = location.get('region', '')
                    country = location.get('country', '')
            
            # Форматирование координат
            coords = ''
            if coordinates and isinstance(coordinates, dict):
                lat = coordinates.get('latitude', '')
                lng = coordinates.get('longitude', '')
                if lat and lng:
                    coords = f"{lat}, {lng}"
            
            # IP-адрес
            ip_address = str(participant.get('ip_address', ''))
            
            # Время регистрации
            reg_time = str(participant.get('registration_time', ''))
            
            # Капитализация строк
            if city:
                city = city.capitalize()
            if region:
                region = region.capitalize()
            if country:
                country = country.capitalize()
            
            # Данные для записи
            data = [
                full_name,
                ticket_number,
                phone,
                age,
                gender,
                city,
                region,
                country,
                reg_time,
                coords,
                ip_address
            ]
            
            # Запись данных в Excel
            for col, value in enumerate(data):
                worksheet.write(row, col, value, cell_format)
        
        # Закрытие и возврат Excel-файла
        workbook.close()
        output.seek(0)
        
        # Формирование имени файла с текущей датой
        current_date = datetime.now().strftime('%Y-%m-%d')
        filename = f'participants_{current_date}.xlsx'
        
        return send_file(
            output, 
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
            as_attachment=True, 
            download_name=filename
        )
    except Exception as e:
        import traceback
        logger.error(f"Ошибка при создании Excel-файла: {str(e)}")
        logger.error(traceback.format_exc())  # Печать полного трейсбека ошибки в консоль
        flash(f'Ошибка при создании Excel-файла: {str(e)}', 'danger')
        return redirect(url_for('admin'))

@app.route('/update-whatsapp-link', methods=['POST'])
def update_whatsapp_link():
    """Обновление ссылки на WhatsApp-сообщество"""
    # Проверка, что пользователь является администратором
    if not session.get('admin'):
        return jsonify({'success': False, 'message': 'Доступ запрещен'}), 403
    
    try:
        new_link = request.form.get('whatsapp_link', '').strip()
        if not new_link:
            return jsonify({'success': False, 'message': 'Ссылка не может быть пустой'}), 400
        
        # Загрузка текущих настроек
        settings = load_settings()
        
        # Обновление ссылки
        settings['whatsapp_link'] = new_link
        
        # Сохранение обновленных настроек
        save_settings(settings)
        
        return jsonify({'success': True, 'message': 'Ссылка успешно обновлена'})
    except Exception as e:
        logger.error(f"Ошибка при обновлении ссылки на WhatsApp-сообщество: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/update-backup-settings', methods=['POST'])
def update_backup_settings():
    """Обновление настроек резервного копирования"""
    # Проверка, что пользователь является администратором
    if not session.get('admin'):
        return jsonify({'success': False, 'message': 'Доступ запрещен'}), 403
    
    try:
        # Получаем данные из формы
        backup_enabled = request.form.get('backup_enabled') == 'true'
        yandex_token = request.form.get('yandex_token', '').strip()
        backup_interval = request.form.get('backup_interval', 'daily')
        
        # Получаем настройки произвольного расписания
        custom_value = request.form.get('custom_value', '24')
        custom_unit = request.form.get('custom_unit', 'hours')
        
        # Проверяем, что значение интервала является положительным числом
        try:
            custom_value = int(custom_value)
            if custom_value <= 0:
                return jsonify({'success': False, 'message': 'Интервал должен быть положительным числом'}), 400
        except ValueError:
            return jsonify({'success': False, 'message': 'Интервал должен быть числом'}), 400
        
        if backup_enabled and not yandex_token:
            return jsonify({'success': False, 'message': 'Укажите токен Яндекс.Диска для резервного копирования'}), 400
        
        # Загрузка текущих настроек
        settings = load_settings()
        
        # Обновление настроек резервного копирования
        if 'backup_settings' not in settings:
            settings['backup_settings'] = copy.deepcopy(BACKUP_SETTINGS)
        
        # Сохраняем предыдущие значения для проверки, были ли изменения
        old_enabled = settings['backup_settings'].get('enabled', False)
        old_interval = settings['backup_settings'].get('interval', 'daily')
        old_value = settings['backup_settings'].get('custom_value', 24)
        old_unit = settings['backup_settings'].get('custom_unit', 'hours')
        
        # Обновляем настройки
        settings['backup_settings']['enabled'] = backup_enabled
        settings['backup_settings']['yandex_token'] = yandex_token
        settings['backup_settings']['interval'] = backup_interval
        settings['backup_settings']['custom_value'] = custom_value
        settings['backup_settings']['custom_unit'] = custom_unit
        
        # Сохранение обновленных настроек
        save_settings(settings)
        
        # Сигнализируем планировщику, что настройки изменились и нужно перезапустить расчёты
        # Особенно если включили резервное копирование или изменили настройки интервала
        if (not old_enabled and backup_enabled) or \
           (old_interval != backup_interval) or \
           (backup_interval == 'custom' and (old_value != custom_value or old_unit != custom_unit)):
            # Устанавливаем флаг события, чтобы планировщик пересчитал время
            scheduler_event.set()
        
        # Формируем информационное сообщение о следующей резервной копии
        next_backup_message = ""
        if backup_enabled:
            if backup_interval == 'daily':
                next_backup_message = " Следующая копия будет создана в 03:00."
            elif backup_interval == 'hourly':
                next_backup_message = " Следующая копия будет создана в начале следующего часа."
            elif backup_interval == 'custom':
                # Используем фактически сохраненные значения из настроек
                value = settings['backup_settings']['custom_value']
                unit = settings['backup_settings']['custom_unit']
                
                unit_name = ""
                if unit == 'seconds':
                    unit_name = "секунд"
                elif unit == 'minutes':
                    unit_name = "минут"
                elif unit == 'hours':
                    unit_name = "часов"
                elif unit == 'days':
                    unit_name = "дней"
                elif unit == 'weeks':
                    unit_name = "недель"
                
                next_backup_message = f" Следующая копия будет создана через {value} {unit_name} после последнего резервного копирования."
        
        return jsonify({'success': True, 'message': 'Настройки резервного копирования обновлены.' + next_backup_message})
    except Exception as e:
        logger.error(f"Ошибка при обновлении настроек резервного копирования: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/create-backup', methods=['POST'])
def manual_backup():
    """Ручное создание резервной копии"""
    # Проверка, что пользователь является администратором
    if not session.get('admin'):
        return jsonify({'success': False, 'message': 'Доступ запрещен'}), 403
    
    try:
        # Получаем данные участников
        participants = load_participants()
        if not participants:
            return jsonify({'success': False, 'message': 'Нет данных для резервного копирования'}), 400
        
        # Загрузка настроек
        settings = load_settings()
        
        # Получаем токен Яндекс.Диска
        yandex_token = request.form.get('yandex_token') or settings.get('backup_settings', {}).get('yandex_token')
        if not yandex_token:
            return jsonify({'success': False, 'message': 'Не указан токен Яндекс.Диска для резервного копирования'}), 400
        
        # Создаем и отправляем резервную копию
        success = send_backup_to_yadisk(participants, yandex_token)
        
        if success:
            # Обновляем время последнего резервного копирования
            if 'backup_settings' not in settings:
                settings['backup_settings'] = copy.deepcopy(BACKUP_SETTINGS)
            
            settings['backup_settings']['last_backup'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_settings(settings)
            
            return jsonify({'success': True, 'message': 'Резервная копия успешно загружена на Яндекс.Диск'})
        else:
            return jsonify({'success': False, 'message': 'Не удалось создать резервную копию'}), 500
    except Exception as e:
        logger.error(f"Ошибка при создании резервной копии: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

# Добавляем настройку для сжатия ответов
@app.after_request
def add_header(response):
    # Кэширование статических файлов
    if 'Cache-Control' not in response.headers:
        if request.path.startswith('/static/'):
            # Кэшировать статические файлы на 1 год
            response.headers['Cache-Control'] = 'public, max-age=31536000'
        else:
            # Не кэшировать HTML-страницы
            response.headers['Cache-Control'] = 'no-store'
    return response

# Функция для создания и загрузки резервной копии на Яндекс.Диск
def send_backup_to_yadisk(json_data, token):
    """Загрузка резервной копии данных на Яндекс.Диск"""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"[{datetime.now()}] Начинаем создание резервной копии и загрузку на Яндекс.Диск")
        
        # Создаем Excel-файл
        excel_data = create_excel_backup(json_data)
        logger.info(f"[{datetime.now()}] Excel файл создан в памяти")
        
        # Создаем JSON-файл
        json_str = json.dumps(json_data, ensure_ascii=False, indent=4)
        json_bytes = json_str.encode('utf-8')
        logger.info(f"[{datetime.now()}] JSON файл создан в памяти")
        
        # Путь на Яндекс.Диске, где будут храниться резервные копии
        folder_path = "/kvdarit_avto35_backup"
        
        # Создаем папку на Яндекс.Диске, если она не существует
        headers = {"Authorization": f"OAuth {token}"}
        create_folder_url = "https://cloud-api.yandex.net/v1/disk/resources"
        
        logger.info(f"[{datetime.now()}] Проверяем/создаем папку {folder_path} на Яндекс.Диске")
        response = requests.put(
            create_folder_url,
            params={"path": folder_path, "overwrite": "true"},
            headers=headers
        )
        
        if response.status_code not in [200, 201, 409]:  # 409 - папка уже существует
            logger.warning(f"[{datetime.now()}] Ошибка при создании папки на Яндекс.Диске: {response.status_code}, {response.text}")
            return False
        
        # Загружаем Excel-файл
        excel_filename = f"participants_{timestamp}.xlsx"
        excel_upload_url = "https://cloud-api.yandex.net/v1/disk/resources/upload"
        excel_params = {
            "path": f"{folder_path}/{excel_filename}",
            "overwrite": "true"
        }
        
        # Получаем URL для загрузки Excel-файла
        logger.info(f"[{datetime.now()}] Получаем URL для загрузки Excel файла")
        response = requests.get(excel_upload_url, params=excel_params, headers=headers)
        if response.status_code == 200:
            href = response.json().get("href", "")
            # Загружаем данные на полученный URL
            logger.info(f"[{datetime.now()}] Загружаем Excel файл на Яндекс.Диск")
            upload_response = requests.put(href, data=excel_data.getvalue())
            if upload_response.status_code != 201:
                logger.warning(f"[{datetime.now()}] Ошибка при загрузке Excel-файла: {upload_response.status_code}, {upload_response.text}")
                return False
            logger.info(f"[{datetime.now()}] Excel файл успешно загружен")
        else:
            logger.warning(f"[{datetime.now()}] Ошибка при получении URL для загрузки Excel-файла: {response.status_code}, {response.text}")
            return False
        
        # Загружаем JSON-файл
        json_filename = f"participants_{timestamp}.json"
        json_params = {
            "path": f"{folder_path}/{json_filename}",
            "overwrite": "true"
        }
        
        # Получаем URL для загрузки JSON-файла
        logger.info(f"[{datetime.now()}] Получаем URL для загрузки JSON файла")
        response = requests.get(excel_upload_url, params=json_params, headers=headers)
        if response.status_code == 200:
            href = response.json().get("href", "")
            # Загружаем данные на полученный URL
            logger.info(f"[{datetime.now()}] Загружаем JSON файл на Яндекс.Диск")
            upload_response = requests.put(href, data=json_bytes)
            if upload_response.status_code != 201:
                logger.warning(f"[{datetime.now()}] Ошибка при загрузке JSON-файла: {upload_response.status_code}, {upload_response.text}")
                return False
            logger.info(f"[{datetime.now()}] JSON файл успешно загружен")
        else:
            logger.warning(f"[{datetime.now()}] Ошибка при получении URL для загрузки JSON-файла: {response.status_code}, {response.text}")
            return False
        
        logger.info(f"[{datetime.now()}] Резервная копия успешно сохранена на Яндекс.Диске: {excel_filename}, {json_filename}")
        return True
    except Exception as e:
        logger.error(f"[{datetime.now()}] Критическая ошибка при создании резервной копии на Яндекс.Диск: {e}")
        logger.error(traceback.format_exc())  # Выводим полный стек вызовов для отладки
        return False

def create_excel_backup(json_data):
    """Создание Excel-файла с данными участников"""
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)
    worksheet = workbook.add_worksheet('Участники')
    
    # Форматирование
    header_format = workbook.add_format({
        'bold': True,
        'bg_color': '#007bff',
        'font_color': 'white',
        'border': 1
    })
    
    cell_format = workbook.add_format({
        'border': 1
    })
    
    # Заголовки
    headers = ['№', 'Номер участника', 'ФИО', 'Телефон', 'Возраст', 'Пол', 'Город', 'Дата регистрации', 'IP-адрес']
    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)
    
    # Данные участников
    for row, participant in enumerate(json_data, start=1):
        worksheet.write(row, 0, row, cell_format)
        worksheet.write(row, 1, participant.get('ticket_number', ''), cell_format)
        worksheet.write(row, 2, participant.get('full_name', ''), cell_format)
        worksheet.write(row, 3, participant.get('phone', ''), cell_format)
        worksheet.write(row, 4, participant.get('age', ''), cell_format)
        gender = 'Мужской' if participant.get('gender') == 'male' else 'Женский'
        worksheet.write(row, 5, gender, cell_format)
        
        # Определяем город из координат или IP
        city = ''
        if participant.get('coordinates') and participant['coordinates'].get('city'):
            city = participant['coordinates']['city']
        elif participant.get('location') and participant['location'].get('city'):
            city = participant['location']['city']
        worksheet.write(row, 6, city, cell_format)
        
        worksheet.write(row, 7, participant.get('registration_time', ''), cell_format)
        worksheet.write(row, 8, participant.get('ip_address', ''), cell_format)
    
    # Автонастройка ширины столбцов
    for i, width in enumerate([5, 15, 25, 15, 8, 10, 15, 20, 15]):
        worksheet.set_column(i, i, width)
        
    workbook.close()
    output.seek(0)
    return output

# Функция для создания и отправки резервной копии
def create_backup():
    """Функция для создания и отправки резервной копии"""
    try:
        logger.info(f"[{datetime.now()}] Запуск процесса создания резервной копии")
        settings = load_settings()
        if not settings.get('backup_settings', {}).get('enabled', False):
            logger.info(f"[{datetime.now()}] Резервное копирование отключено в настройках")
            return False
        
        # Проверка наличия токена Яндекс.Диска
        yandex_token = settings.get('backup_settings', {}).get('yandex_token', '')
        if not yandex_token:
            logger.info(f"[{datetime.now()}] Не указан токен Яндекс.Диска для резервного копирования")
            return False
        
        # Получаем данные участников
        participants = load_participants()
        if not participants:
            logger.info(f"[{datetime.now()}] Нет данных участников для резервного копирования")
            return False
        
        logger.info(f"[{datetime.now()}] Отправка резервной копии на Яндекс.Диск (участников: {len(participants)})")
        
        # Отправляем резервную копию на Яндекс.Диск
        success = send_backup_to_yadisk(participants, yandex_token)
        if success:
            # Обновляем время последнего резервного копирования
            settings['backup_settings']['last_backup'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_settings(settings)
            logger.info(f"[{datetime.now()}] Резервная копия успешно создана и отправлена")
            return True
        else:
            logger.warning(f"[{datetime.now()}] Ошибка при отправке резервной копии на Яндекс.Диск")
            return False
    except Exception as e:
        logger.error(f"[{datetime.now()}] Критическая ошибка при создании резервной копии: {e}")
        logger.error(traceback.format_exc())
        return False

# Инициализация настроек резервного копирования при запуске
def init_backup_settings():
    settings = load_settings()
    if 'backup_settings' not in settings:
        settings['backup_settings'] = copy.deepcopy(BACKUP_SETTINGS)
        save_settings(settings)
    elif 'yandex_token' not in settings['backup_settings'] or not settings['backup_settings']['yandex_token']:
        # Если токен отсутствует или пустой, добавляем его из настроек по умолчанию
        settings['backup_settings']['yandex_token'] = BACKUP_SETTINGS['yandex_token']
        save_settings(settings)

# Планировщик резервного копирования
def run_scheduler():
    logger.info(f"[{datetime.now()}] Запущен планировщик резервного копирования")
    next_time = None
    
    # Проверка токена Яндекс Диска при запуске
    settings = load_settings()
    backup_settings = settings.get('backup_settings', {})
    yandex_token = backup_settings.get('yandex_token', '')
    
    if not yandex_token:
        logger.warning(f"[{datetime.now()}] ВНИМАНИЕ: Токен Яндекс.Диска не задан. Резервное копирование не будет работать!")
    else:
        logger.info(f"[{datetime.now()}] Токен Яндекс.Диска найден: {yandex_token[:5]}...{yandex_token[-5:]}")
    
    # При запуске создаем тестовую резервную копию, чтобы проверить работоспособность
    if backup_settings.get('enabled', False):
        logger.info(f"[{datetime.now()}] Создание тестовой резервной копии при запуске планировщика...")
        success = create_backup()
        if success:
            logger.info(f"[{datetime.now()}] Тестовая резервная копия успешно создана")
        else:
            logger.warning(f"[{datetime.now()}] ОШИБКА: Не удалось создать тестовую резервную копию")
    
    while True:
        settings = load_settings()
        backup_settings = settings.get('backup_settings', {})
        
        if not backup_settings.get('enabled', False):
            # Если резервное копирование отключено, проверяем раз в минуту
            logger.info(f"[{datetime.now()}] Резервное копирование отключено в настройках")
            # Проверяем на событие каждую секунду для более быстрого отклика
            for _ in range(60):
                if scheduler_event.is_set():
                    logger.info(f"[{datetime.now()}] Получен сигнал об изменении настроек")
                    scheduler_event.clear()  # Сбрасываем флаг
                    break
                time.sleep(1)
            continue
        
        current_time = datetime.now()
        
        # Если было событие изменения настроек, сбрасываем расчет времени и проверяем сразу
        if scheduler_event.is_set():
            logger.info(f"[{current_time}] Обрабатываем изменение настроек резервного копирования")
            scheduler_event.clear()  # Сбрасываем флаг
            next_time = None
            # Если включён пользовательский интервал и интервал короткий - создаем резервную копию немедленно
            interval = backup_settings.get('interval', 'daily')
            if interval == 'custom':
                value = int(backup_settings.get('custom_value', 24))
                unit = backup_settings.get('custom_unit', 'hours')
                logger.info(f"[{current_time}] Новый интервал резервного копирования: {value} {unit}")
                if unit in ['seconds', 'minutes'] or (unit == 'hours' and value < 1):
                    logger.info(f"[{current_time}] Создание резервной копии немедленно после изменения настроек")
                    if create_backup():
                        # Обновляем время последнего резервного копирования в файле настроек
                        settings = load_settings()
                        settings['backup_settings']['last_backup'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                        save_settings(settings)
                        logger.info(f"[{current_time}] Обновлено время последнего резервного копирования: {settings['backup_settings']['last_backup']}")
        
        # Рассчитываем время следующего резервного копирования
        if next_time is None:
            # Первый запуск или настройки изменились
            interval = backup_settings.get('interval', 'daily')
            
            if interval == 'daily':
                # Ежедневное резервное копирование в 03:00
                next_time = current_time.replace(hour=3, minute=0, second=0, microsecond=0)
                if current_time >= next_time:
                    next_time += timedelta(days=1)
                logger.info(f"[{current_time}] Следующее резервное копирование (daily): {next_time}")
            elif interval == 'hourly':
                # Ежечасное резервное копирование в начале часа
                next_time = current_time.replace(minute=0, second=0, microsecond=0)
                if current_time >= next_time:
                    next_time += timedelta(hours=1)
                logger.info(f"[{current_time}] Следующее резервное копирование (hourly): {next_time}")
            elif interval == 'custom':
                # Произвольный интервал
                value = int(backup_settings.get('custom_value', 24))
                unit = backup_settings.get('custom_unit', 'hours')
                
                # Получаем последнее время резервного копирования
                last_backup = backup_settings.get('last_backup')
                
                if last_backup:
                    try:
                        last_backup_time = datetime.strptime(last_backup, '%Y-%m-%d %H:%M:%S')
                        logger.info(f"[{current_time}] Последнее резервное копирование было в: {last_backup_time}")
                        
                        # Рассчитываем следующее время на основе последнего резервного копирования
                        if unit == 'seconds':
                            next_time = last_backup_time + timedelta(seconds=value)
                        elif unit == 'minutes':
                            next_time = last_backup_time + timedelta(minutes=value)
                        elif unit == 'hours':
                            next_time = last_backup_time + timedelta(hours=value)
                        elif unit == 'days':
                            next_time = last_backup_time + timedelta(days=value)
                        elif unit == 'weeks':
                            next_time = last_backup_time + timedelta(weeks=value)
                        else:
                            next_time = last_backup_time + timedelta(hours=24)
                        
                        logger.info(f"[{current_time}] Следующее резервное копирование (custom {value} {unit}): {next_time}")
                            
                        # Если рассчитанное время уже прошло, делаем резервную копию сейчас
                        if next_time <= current_time:
                            logger.info(f"[{current_time}] Рассчитанное время уже прошло, делаем копию сейчас")
                            next_time = current_time
                    except Exception as e:
                        logger.error(f"[{current_time}] Ошибка при разборе даты последнего бэкапа: {e}")
                        next_time = current_time
                else:
                    # Если нет записи о последнем резервном копировании, делаем сейчас
                    logger.info(f"[{current_time}] Нет данных о последнем резервном копировании, делаем копию сейчас")
                    next_time = current_time
        
        # Проверяем, наступило ли время для создания резервной копии
        if current_time >= next_time:
            logger.info(f"[{current_time}] Время создания автоматической резервной копии")
            # Создаем резервную копию и обновляем метку времени только в случае успеха
            if create_backup():
                # Обновляем время последнего резервного копирования в файле настроек
                settings = load_settings()
                settings['backup_settings']['last_backup'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                save_settings(settings)
                logger.info(f"[{current_time}] Время последнего резервного копирования обновлено: {settings['backup_settings']['last_backup']}")
                
                # Сбрасываем счетчик для следующего резервного копирования
                next_time = None
            else:
                # Если копирование не удалось, попробуем снова через минуту
                logger.info(f"[{current_time}] Резервное копирование не удалось, следующая попытка через минуту")
                next_time = current_time + timedelta(minutes=1)
        else:
            # Для коротких интервалов используем более частые проверки
            interval = backup_settings.get('interval', 'daily')
            if interval == 'custom':
                unit = backup_settings.get('custom_unit', 'hours')
                if unit == 'seconds':
                    # Для секунд проверяем каждую секунду
                    wait_seconds = 1
                elif unit == 'minutes':
                    # Для минут проверяем каждые 5 секунд
                    wait_seconds = 5
                else:
                    # Для других интервалов проверяем не чаще раза в минуту
                    wait_seconds = min(60, (next_time - current_time).total_seconds())
            else:
                # Для стандартных интервалов проверяем не чаще раза в минуту
                wait_seconds = min(60, (next_time - current_time).total_seconds())
            
            if wait_seconds <= 0:
                wait_seconds = 1
                
            logger.info(f"[{current_time}] Ожидание {wait_seconds} сек. до следующей проверки. Следующее резервное копирование в {next_time}")
            
            # Разбиваем ожидание на короткие интервалы для быстрого отклика на события
            for _ in range(int(wait_seconds)):
                if scheduler_event.is_set():
                    logger.info(f"[{datetime.now()}] Получен сигнал об изменении настроек во время ожидания")
                    break
                time.sleep(1)

# Запуск фонового задания для резервного копирования
def start_backup_scheduler():
    # Запускаем планировщик в отдельном потоке вместо процесса
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("Планировщик резервного копирования запущен в отдельном потоке")

# Функция инициализации приложения для запуска планировщика
def init_app(flask_app):
    # Инициализация настроек резервного копирования
    init_backup_settings()
    # Запуск планировщика резервного копирования
    # start_backup_scheduler()  # Закомментировали для предотвращения автозапуска при импорте

# Предотвращаем автоматический запуск при импорте
# init_app(app)

# Убедимся, что планировщик запускается только при непосредственном запуске приложения
if __name__ == '__main__':
    # Инициализация настроек резервного копирования
    init_backup_settings()
    # Запуск планировщика резервного копирования
    start_backup_scheduler()
    
    # Для продакшена используйте WSGI-сервер (gunicorn или uwsgi)
    # gunicorn -w 4 -b 0.0.0.0:5000 app:app
    app.run(debug=False, host='0.0.0.0') 

@app.route('/find-ticket', methods=['POST'])
def find_ticket():
    """Поиск номера участника по номеру телефона"""
    phone = request.form.get('phone', '')
    
    if not phone:
        return jsonify({'success': False, 'message': 'Пожалуйста, введите номер телефона.'})
    
    # Нормализуем телефон для поиска (удаляем все, кроме цифр)
    normalized_phone = ''.join(filter(str.isdigit, phone))
    
    # Проверяем, что номер телефона полный (не менее 11 цифр для российского номера)
    if len(normalized_phone) < 11:
        return jsonify({'success': False, 'message': 'Пожалуйста, введите полный номер телефона.'})
    
    # Если номер начинается с 8, заменяем на 7 для стандартизации
    if normalized_phone.startswith('8') and len(normalized_phone) == 11:
        normalized_phone = '7' + normalized_phone[1:]
    
    ticket_data = get_ticket_by_phone(normalized_phone)
    
    if ticket_data:
        return jsonify({
            'success': True, 
            'message': 'Номер участника найден!',
            'ticket_number': ticket_data['ticket_number'],
            'full_name': ticket_data['full_name']
        })
    else:
        return jsonify({
            'success': False, 
            'message': 'Этот номер телефона не зарегистрирован в розыгрыше.'
        }) 
