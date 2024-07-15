import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
#import datetime

# Constants
EXTREME_HIGH_BG_THRESHOLD = 250  # mg/dL
EXTREME_LOW_BG_THRESHOLD = 50    # mg/dL
HIGH_BG_THRESHOLD = 180          # mg/dL
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

COOL_DOWN_PERIODS = {
    "extreme_high_bg": 30,  # minutes
    "extreme_low_bg": 30,   # minutes
    "high_bg": 60,          # minutes
    "low_bg": 60,           # minutes
    "rapid_rise": 15,       # minutes
    "rapid_fall": 15,       # minutes
    "upward_trend": 30,     # minutes
    "downward_trend": 30,   # minutes
    "time_in_range": 120,   # minutes
    "post_meal": 120        # minutes
}


def save_data(data):
    # Save data to the JSON file on the server.
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.put(URL, auth=HTTPBasicAuth(USERNAME, PASSWORD), headers=headers, data=json.dumps(data))
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
    
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)
    start_time_str = start_time.isoformat() + 'Z'
    end_time_str = end_time.isoformat() + 'Z'

    params = {
        'token': access_token,
        'find[dateString][$gte]': start_time_str,
        'find[dateString][$lte]': end_time_str,
        'count=1000'
    }

    response = requests.get(f"{url}/api/v1/entries.json", params=params)
    response.raise_for_status()
    data = response.json()

    return data

def process_data(data):
    
    #Processes raw data to fill in missing minute gaps linearly.
    
    # Convert data to DataFrame
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['dateString'])
    df.set_index('date', inplace=True)

    # Resample data to 1-minute intervals
    df = df.resample('1T').mean()

    # Interpolate missing values
    df['sgv'] = df['sgv'].interpolate()

    return df

def getBGinTime(minutes_ago, df):
    
    #Returns the blood sugar level a certain number of minutes ago.
    
    target_time = datetime.utcnow() - timedelta(minutes=minutes_ago)
    target_time = target_time.replace(second=0, microsecond=0)

    if target_time in df.index:
        return df.loc[target_time, 'sgv']
    else:
        return None


def getCurrentTime():
    return datetime.datetime.now()

# Alert Functions
def trigger_extreme_high_bg_alert():
    print("Extreme High BG Alert Triggered")

def trigger_extreme_low_bg_alert():
    print("Extreme Low BG Alert Triggered")

def trigger_high_bg_alert():
    print("High BG Alert Triggered")

def trigger_low_bg_alert():
    print("Low BG Alert Triggered")

def trigger_rapid_rise_alert():
    print("Rapid Rise Alert Triggered")

def trigger_rapid_fall_alert():
    print("Rapid Fall Alert Triggered")

def trigger_upward_trend_alert():
    print("Upward Trend Alert Triggered")

def trigger_downward_trend_alert():
    print("Downward Trend Alert Triggered")

def trigger_time_in_range_alert():
    print("Time-in-Range Alert Triggered")

def trigger_post_meal_alert():
    print("Post-Meal Alert Triggered")

# Main Logic
def check_alerts():
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

    def should_trigger_alert(alert_name, priority, condition):
        nonlocal highest_priority, alert_to_trigger
        last_alert_time = data.get(alert_name, {}).get('last_alert_time')
        cool_down_period = COOL_DOWN_PERIODS[alert_name]

        if last_alert_time:
            last_alert_time = datetime.datetime.fromisoformat(last_alert_time)
            if current_time - last_alert_time < datetime.timedelta(minutes=cool_down_period):
                last_alert_priority = data.get('last_alert_priority', float('inf'))
                if priority < last_alert_priority:
                    if condition:
                        if priority < highest_priority:
                            highest_priority = priority
                            alert_to_trigger = alert_name
                return
        if condition:
            if priority < highest_priority:
                highest_priority = priority
                alert_to_trigger = alert_name

    should_trigger_alert("extreme_high_bg", 1, current_bg > hysteresis_extreme_high)
    should_trigger_alert("extreme_low_bg", 1, current_bg < hysteresis_extreme_low)
    should_trigger_alert("high_bg", 2, current_bg > hysteresis_high)
    should_trigger_alert("low_bg", 2, current_bg < hysteresis_low)
    
    # Rapid Rise/Fall
    bg_5_minutes_ago = getBGinTime(5, df)
    rate_of_rise = (current_bg - bg_5_minutes_ago) / 5
    rate_of_fall = (bg_5_minutes_ago - current_bg) / 5
    should_trigger_alert("rapid_rise", 3, rate_of_rise > RAPID_CHANGE_THRESHOLD)
    should_trigger_alert("rapid_fall", 3, rate_of_fall > RAPID_CHANGE_THRESHOLD)

    # Upward/Downward Trend
    bg_60_minutes_ago = getBGinTime(TREND_PERIOD, df)
    trend = (current_bg - bg_60_minutes_ago) / TREND_PERIOD
    should_trigger_alert("upward_trend", 4, trend > UPWARD_TREND_THRESHOLD)
    should_trigger_alert("downward_trend", 4, trend < DOWNWARD_TREND_THRESHOLD)

    # Time-in-Range
    out_of_range_duration = data.get('out_of_range_duration', 0)
    last_out_of_range_time = data.get('last_out_of_range_time', current_time.isoformat())
    last_out_of_range_time = datetime.datetime.fromisoformat(last_out_of_range_time)
    
    if current_bg < TIME_IN_RANGE[0] or current_bg > TIME_IN_RANGE[1]:
        out_of_range_duration += (current_time - last_out_of_range_time).total_seconds() / 60
        data['out_of_range_duration'] = out_of_range_duration
        data['last_out_of_range_time'] = current_time.isoformat()
    else:
        data['last_out_of_range_time'] = current_time.isoformat()
    
    should_trigger_alert("time_in_range", 5, out_of_range_duration > COOL_DOWN_PERIODS["time_in_range"])

    # Post-Meal
    meal_time = data.get('last_meal_time')
    if meal_time:
        meal_time = datetime.datetime.fromisoformat(meal_time)
        if current_time - meal_time >= datetime.timedelta(minutes=POST_MEAL_PERIOD):
            bg_change = current_bg - getBGinTime(POST_MEAL_PERIOD, df)
            should_trigger_alert("post_meal", 6, abs(bg_change) > POST_MEAL_THRESHOLD)

    # Trigger the highest priority alert
    if alert_to_trigger:
        trigger_function = globals()[f'trigger_{alert_to_trigger}_alert']
        trigger_function()
        data[alert_to_trigger] = {'last_alert_time': current_time.isoformat()}
        data['last_alert_priority'] = highest_priority
        save_data(data)

# Example usage
check_alerts()