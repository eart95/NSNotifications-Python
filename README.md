# Nightscout Checker

A Python script to monitor Nightscout server glucose levels and send push notifications using APNs.

## Setup

1. Clone the repository:
    ```bash
    git clone https://github.com/your-username/nightscout-checker.git
    cd nightscout-checker
    ```

2. Configure environment variables in your Northflank project:
    - `NIGHTSCOUT_URL`
    - `APNS_KEY_ID`
    - `APNS_TEAM_ID`
    - `APNS_BUNDLE_ID`
    - `APNS_P8_FILE`
    - `DEVICE_TOKENS`

3. Build and run the project locally:
    ```bash
    docker build -t nightscout-checker .
    docker run -e NIGHTSCOUT_URL='https://your-nightscout-server.herokuapp.com/api/v1/entries.json?count=1'                -e APNS_KEY_ID='YOUR_KEY_ID'                -e APNS_TEAM_ID='YOUR_TEAM_ID'                -e APNS_BUNDLE_ID='YOUR_BUNDLE_ID'                -e APNS_P8_FILE='path/to/AuthKey_YOUR_KEY_ID.p8'                -e DEVICE_TOKENS='DEVICE_TOKEN1,DEVICE_TOKEN2'                nightscout-checker
    ```

4. Deploy to Northflank:
    - Follow the steps to create a new service, configure environment variables, and set up a cron job to run the script every 5 minutes.
