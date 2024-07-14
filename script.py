import os
import requests
import json
import jwt
import time

# Configuration
NIGHTSCOUT_URL = os.getenv('NIGHTSCOUT_URL')
NIGHTSCOUT_TOKEN = os.getenv('NIGHTSCOUT_TOKEN')
#DEVICE_TOKENS = ['DEVICE_TOKEN1', 'DEVICE_TOKEN2']  # List of device tokens
#APNS_KEY_ID = 'YOUR_KEY_ID'
#APNS_TEAM_ID = 'YOUR_TEAM_ID'
#APNS_BUNDLE_ID = 'YOUR_BUNDLE_ID'
#APNS_P8_FILE = 'path/to/AuthKey_YOUR_KEY_ID.p8'
SECRET_FILE = 'glucose_data.json'

nsURL = NIGHTSCOUT_URL + '/api/v1/entries.json?count=1&token=' + NIGHTSCOUT_TOKEN

def get_latest_glucose_level():
    response = requests.get(nsURL)
    if response.status_code == 200:
        data = response.json()
        return data[0]['sgv']  # Assuming 'sgv' is the glucose level field
    return None

def load_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, 'r') as f:
            return json.load(f)
    return {'last_checked': None, 'last_glucose_level': None}

def save_secret(data):
    with open(SECRET_FILE, 'w') as f:
        json.dump(data, f)

'''
def send_push_notification(token, title, body):
    with open(APNS_P8_FILE) as f:
        secret = f.read()
    
    headers = {
        'alg': 'ES256',
        'kid': APNS_KEY_ID,
    }

    payload = {
        'iss': APNS_TEAM_ID,
        'iat': time.time()
    }

    token = jwt.encode(payload, secret, algorithm='ES256', headers=headers)

    notification = {
        'aps': {
            'alert': {
                'title': title,
                'body': body
            },
            'sound': 'default'
        }
    }

    response = requests.post(
        f'https://api.push.apple.com/3/device/{token}',
        json=notification,
        headers={
            'apns-topic': APNS_BUNDLE_ID,
            'authorization': f'bearer {token}',
            'content-type': 'application/json'
        }
    )
    print('Push Notification Sent:', response.status_code, response.reason)
'''

def main():
    print('hello world')
    glucose_level = get_latest_glucose_level()
    if glucose_level is not None:
        data = load_secret()
        if glucose_level > 180:  # Example condition for high glucose level
            print('high')
            #for device_token in DEVICE_TOKENS:
                #send_push_notification(device_token, 'High Glucose Level', f'Your glucose level is {glucose_level}.')
        elif glucose_level < 70:  # Example condition for low glucose level
            print('low')
        else:
            print('normal')
            #for device_token in DEVICE_TOKENS:
                #send_push_notification(device_token, 'Low Glucose Level', f'Your glucose level is {glucose_level}.')

    # Save current glucose level and timestamp
        data['last_checked'] = time.time()
        data['last_glucose_level'] = glucose_level
        save_secret(data)

        
if __name__ == '__main__':
    main()
