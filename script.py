import os
import json
import requests
import jwt
import pandas as pd
#import numpy as np
from datetime import datetime, timedelta
import time
from requests.auth import HTTPBasicAuth
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import ssl
import httpx
import asyncio
#import datetime

# Constants
EXTREME_HIGH_BG_THRESHOLD = 250  # mg/dL
EXTREME_LOW_BG_THRESHOLD = 50    # mg/dL
HIGH_BG_THRESHOLD = 180          # mg/dL --to 180
LOW_BG_THRESHOLD = 70            # mg/dL
RAPID_CHANGE_THRESHOLD = 2       # mg/dL per minute
TREND_PERIOD = 60                # minutes
UPWARD_TREND_THRESHOLD = 1       # mg/dL per minute
DOWNWARD_TREND_THRESHOLD = -1    # mg/dL per minute
TIME_IN_RANGE = (70, 180)        # mg/dL
POST_MEAL_PERIOD = 120           # minutes
POST_MEAL_THRESHOLD = 50         # mg/dL change

NIGHTSCOUT_URL = os.getenv('NIGHTSCOUT_URL')
NIGHTSCOUT_TOKEN = os.getenv('NIGHTSCOUT_TOKEN')
URL = os.getenv('PERSISTENT_STORAGE_URL')
USERNAME = os.getenv('PERSISTENT_STORAGE_USERNAME')
PASSWORD = os.getenv('PERSISTENT_STORAGE_PW')


APNS_KEY_ID = os.getenv('APNS_KEY_ID')
APNS_TEAM_ID = os.getenv('APNS_TEAM_ID')
APNS_BUNDLE_ID = os.getenv('APNS_BUNDLE_ID')
APNS_P8_FILE = os.getenv('APNS_P8_FILE')

def read_tokens():
    # Read data from file on the server.
    response = requests.get('http://nightscout.enricoartuso.com/device_tokens.txt', auth=HTTPBasicAuth(USERNAME, PASSWORD))
    if response.status_code == 200:
        data = response.text.split(',')
        return data
    else:
        print('Failed to read data:', response.status_code, response.text)
        return None

DEVICE_TOKENS = read_tokens()


COOL_DOWN_PERIODS = {
    "extreme_high_bg": 30,  # minutes
    "extreme_low_bg": 2,   # minutes
    "high_bg": 30,          # minutes --should be 60
    "low_bg": 5,           # minutes
    "rapid_rise": 15,       # minutes
    "rapid_fall": 5,       # minutes
    "upward_trend": 30,     # minutes
    "downward_trend": 30,   # minutes
    "time_in_range": 120,   # minutes
    "post_meal": 120        # minutes
}

def read_p8_file():
    # Read data from the JSON file on the server.
    response = requests.get(APNS_P8_FILE, auth=HTTPBasicAuth(USERNAME, PASSWORD))
    if response.status_code == 200:
        data = response.text
        return data
    else:
        print('Failed to read data:', response.status_code, response.text)
        return None


async def send_push_notification(device_token, title, body):
    secret = read_p8_file()

    headers = {
        'alg': 'ES256',
        'kid': APNS_KEY_ID,
    }

    payload = {
        'iss': APNS_TEAM_ID,
        'iat': int(time.time())
    }

    private_key = serialization.load_pem_private_key(secret.encode(), password=None, backend=default_backend())
    token = jwt.encode(payload, private_key, algorithm='ES256', headers=headers)

    notification = {
        'aps': {
            'alert': {
                'title': title,
                'body': body
            },
            'sound': 'default'
        },
        'interruption-level': 'time-sensitive'
    }

    url = f'https://api.sandbox.push.apple.com/3/device/{device_token}'

    headers = {
        'apns-topic': APNS_BUNDLE_ID,
        'authorization': f'bearer {token}',
        'content-type': 'application/json'
    }

    '''
    print(f'secret: {secret}')
    print(f'headers: {headers}')
    print(f'payload: {payload}')
    print(f'url: {url}')
    '''

    async with httpx.AsyncClient(http2=True) as client:
        response = await client.post(url, headers=headers, json=notification)

    print(response.status_code, response.text)
    
    '''
    headers = {
        'alg': 'ES256',
        'kid': APNS_KEY_ID,
    }

    payload = {
        'iss': APNS_TEAM_ID,
        'iat': time.time()
    }

    token = jwt.encode(payload, secret, algorithm='ES256', headers=headers)

    conn = http.client.HTTPSConnection('api.push.apple.com', 443, context=ssl._create_unverified_context())

    notification = {
        'aps': {
            'alert': {
                'title': title,
                'body': body
            },
            'sound': 'default'
        },
        'interruption-level': 'time-sensitive'
    }


    conn.request(
        'POST',
        f'/3/device/{token}',
        body=json.dumps(notification),
        headers={
            'apns-topic': APNS_BUNDLE_ID,
            'authorization': f'bearer {token}',
            'content-type': 'application/json'
        }
    )
    
    response = conn.getresponse()
    print(response.status, response.reason)

'''
    

def save_data(data):
    # Save data to the JSON file on the server.
    headers = {
        'Content-Type': 'application/json'
    }
    #response = requests.post(URL.replace('NSNotifier-Persistent.json', 'write-JSON.php'), auth=HTTPBasicAuth(USERNAME, PASSWORD), headers=headers, data=json.dumps(data))
    response = requests.post(URL.replace('NSNotifier-Persistent.json', 'write-JSON.php'), auth=HTTPBasicAuth(USERNAME, PASSWORD), headers=headers, data=json.dumps(data))
    if response.status_code == 200:
        print('Data saved successfully.')
    else:
        print('Failed to save data:', response.status_code, response.text)

def read_data():
    # Read data from the JSON file on the server.
    response = requests.get(URL, auth=HTTPBasicAuth(USERNAME, PASSWORD))
    if response.status_code == 200:
        data = response.json()
        return data
    else:
        print('Failed to read data:', response.status_code, response.text)
        return None




def get_nightscout_data(url, access_token, hours=8):
    
    #Fetches blood sugar data from the Nightscout server for the last specified hours.
    
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=hours)
    start_time_str = start_time.isoformat() + 'Z'
    end_time_str = end_time.isoformat() + 'Z'

    params = {
        'token': access_token,
        'find[dateString][$gte]': start_time_str,
        'find[dateString][$lte]': end_time_str,
        'count': 1000
    }

    response = requests.get(f"{url}/api/v1/entries.json", params=params)
    response.raise_for_status()
    data = response.json()

    return data

def process_data(data):
    
    #Processes raw data to fill in missing minute gaps linearly.
    
    # Convert data to DataFrame
    df = pd.DataFrame(data)
    # Convert dateString to datetime and set as index
    df['date'] = pd.to_datetime(df['dateString'])
    df.set_index('date', inplace=True)

    # Keep only the 'sgv' column for resampling
    df_sgv = df[['sgv']]

    # Resample data to 1-minute intervals
    df_resampled = df_sgv.resample('min').mean()

    # Interpolate missing values
    df_resampled['sgv'] = df_resampled['sgv'].interpolate()

    return df_resampled

def getBGinTime(minutes_ago, df):
    
    #Returns the blood sugar level a certain number of minutes ago.
    most_recent_date = df.index.max()
    
    target_time = most_recent_date - timedelta(minutes=minutes_ago)
    target_time = target_time.replace(second=0, microsecond=0)

    if target_time in df.index:
        return df.loc[target_time, 'sgv']
    else:
        return None


def getCurrentTime():
    return datetime.now()

# Alert Functions
async def trigger_extreme_high_bg_alert(bg):
    print("Extreme High BG Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F534 Very high blood sugar', f'Your blood sugar is {int(bg)} mg/dL.')

async def trigger_extreme_low_bg_alert(bg):
    print("Extreme Low BG Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F198 VERY LOW blood sugar', f'Your blood sugar is {int(bg)} mg/dL.')

async def trigger_high_bg_alert(bg):
    print("High BG Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F7E1 High blood sugar', f'Your blood sugar is {int(bg)} mg/dL.')

async def trigger_low_bg_alert(bg):
    print("Low BG Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F534 Low blood sugar', f'Your blood sugar is {int(bg)} mg/dL.')

async def trigger_rapid_rise_alert(bg):
    print("Rapid Rise Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F53C Blood sugar rising rapidly', f'Currently, your blood sugar is {int(bg)} mg/dL.')

async def trigger_rapid_fall_alert(bg):
    print("Rapid Fall Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F53D Blood sugar falling rapidly', f'Currently, your blood sugar is {int(bg)} mg/dL.')

async def trigger_upward_trend_alert(bg):
    print("Upward Trend Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u2197 Blood sugar going up', f'Currently, your blood sugar is {int(bg)} mg/dL.')

async def trigger_downward_trend_alert(bg):
    print("Downward Trend Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u2198 Blood sugar going down', f'Currently, your blood sugar is {int(bg)} mg/dL.')

async def trigger_time_in_range_alert(bg):
    print("Time-in-Range Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, '\u1F7E0 Blood sugar has been out of range', f'Currently, your blood sugar is {int(bg)} mg/dL.')

'''
async def trigger_post_meal_alert(bg):
    print("Post-Meal Alert Triggered")
    for device_token in DEVICE_TOKENS:
        #print(device_token)
        await send_push_notification(device_token, 'Check your blood sugar after your meal', f'Currently, your blood sugar is {int(bg)}.')
'''

# Main Logic
async def main():
    current_time = getCurrentTime()
    data = read_data()

    # Fetch data
    bg_data = get_nightscout_data(NIGHTSCOUT_URL, NIGHTSCOUT_TOKEN)

    # Process data to fill gaps
    df = process_data(bg_data)
    current_bg = getBGinTime(0, df)

    # Hysteresis adjustment for priority 1 and 2 alerts
    hysteresis_extreme_high = EXTREME_HIGH_BG_THRESHOLD + 10 if current_bg > EXTREME_HIGH_BG_THRESHOLD + 10 else EXTREME_HIGH_BG_THRESHOLD - 10
    hysteresis_extreme_low = EXTREME_LOW_BG_THRESHOLD - 10 if current_bg < EXTREME_LOW_BG_THRESHOLD - 10 else EXTREME_LOW_BG_THRESHOLD + 10
    hysteresis_high = HIGH_BG_THRESHOLD + 10 if current_bg > HIGH_BG_THRESHOLD + 10 else HIGH_BG_THRESHOLD - 10
    hysteresis_low = LOW_BG_THRESHOLD - 10 if current_bg < LOW_BG_THRESHOLD - 10 else LOW_BG_THRESHOLD + 10

    # Determine the highest priority alert that should be triggered
    highest_priority = float('inf')
    alert_to_trigger = None
    alert_args = None
    alert_kwargs = None

    def should_trigger_alert(alert_name, priority, condition, *args, **kwargs):
        nonlocal highest_priority, alert_to_trigger, alert_args, alert_kwargs
        #last_alert_time = data.get(alert_name, {}).get('last_alert_time')
        last_alert_time = data.get('last_alert_time')
        cool_down_period = COOL_DOWN_PERIODS[alert_name]

        if last_alert_time:
            last_alert_time = datetime.fromisoformat(last_alert_time)
            if current_time - last_alert_time < timedelta(minutes=cool_down_period):
                last_alert_priority = data.get('last_alert_priority', float('inf'))
                if priority < last_alert_priority:
                    if condition:
                        if priority < highest_priority:
                            highest_priority = priority
                            alert_to_trigger = alert_name
                            alert_args = args
                            alert_kwargs = kwargs
                return
        if condition:
            if priority < highest_priority:
                highest_priority = priority
                alert_to_trigger = alert_name
                alert_args = args
                alert_kwargs = kwargs

    should_trigger_alert("extreme_high_bg", 1, current_bg > hysteresis_extreme_high, current_bg)
    should_trigger_alert("extreme_low_bg", 1, current_bg < hysteresis_extreme_low, current_bg)
    should_trigger_alert("high_bg", 2, current_bg > hysteresis_high, current_bg)
    should_trigger_alert("low_bg", 2, current_bg < hysteresis_low, current_bg)
    
    # Rapid Rise/Fall
    bg_5_minutes_ago = getBGinTime(5, df)
    rate_of_rise = (current_bg - bg_5_minutes_ago) / 5
    rate_of_fall = (bg_5_minutes_ago - current_bg) / 5
    should_trigger_alert("rapid_rise", 3, rate_of_rise > RAPID_CHANGE_THRESHOLD, current_bg)
    should_trigger_alert("rapid_fall", 3, rate_of_fall > RAPID_CHANGE_THRESHOLD, current_bg)

    # Upward/Downward Trend
    bg_60_minutes_ago = getBGinTime(TREND_PERIOD, df)
    trend = (current_bg - bg_60_minutes_ago) / TREND_PERIOD
    should_trigger_alert("upward_trend", 4, trend > UPWARD_TREND_THRESHOLD, current_bg)
    should_trigger_alert("downward_trend", 4, trend < DOWNWARD_TREND_THRESHOLD, current_bg)

    # Time-in-Range
    out_of_range_duration = data.get('out_of_range_duration', 0)
    last_out_of_range_time = data.get('last_out_of_range_time', current_time.isoformat())
    last_out_of_range_time = datetime.fromisoformat(last_out_of_range_time)
    
    if current_bg < TIME_IN_RANGE[0] or current_bg > TIME_IN_RANGE[1]:
        out_of_range_duration += (current_time - last_out_of_range_time).total_seconds() / 60
        data['out_of_range_duration'] = out_of_range_duration
        data['last_out_of_range_time'] = current_time.isoformat()
    else:
        data['out_of_range_duration'] = 0
        data['last_out_of_range_time'] = None
    
    should_trigger_alert("time_in_range", 5, out_of_range_duration > COOL_DOWN_PERIODS["time_in_range"], current_bg)

    # Post-Meal
    '''
    meal_time = data.get('last_meal_time')
    if meal_time:
        meal_time = datetime.fromisoformat(meal_time)
        if current_time - meal_time >= timedelta(minutes=POST_MEAL_PERIOD):
            bg_change = current_bg - getBGinTime(POST_MEAL_PERIOD, df)
            should_trigger_alert("post_meal", 6, abs(bg_change) > POST_MEAL_THRESHOLD, current_bg)
    '''

    # Trigger the highest priority alert
    if alert_to_trigger:
        #alert_name, args, kwargs = alert_to_trigger
        trigger_function = globals()[f'trigger_{alert_to_trigger}_alert']
        await trigger_function(*alert_args, **alert_kwargs)
        #data[alert_to_trigger] = {'last_alert_time': current_time.isoformat()}
        data['last_alert_time'] = current_time.isoformat()
        data['last_alert_priority'] = highest_priority
        #print(data)
        save_data(data)

if __name__ == '__main__':
    asyncio.run(main())