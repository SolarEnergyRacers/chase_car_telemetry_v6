# chase_car_telemetry_v6
solarenergyracers.ch chase car telemetry system

## Setup
1. set no_db mode in options.json to true
2. install python requirements (requirements.txt)
3. Define correct serial port in options.json


## Structure
- data: actual recorded data
- data_analysis: streamlit based GUI for data analysis
- ser_playback: toolset to read recorded data and do playback to emulate SER
- ser_receiver: SER receiver and real-time dashboard

## Emulation (Linux)
### Virtual port for serial data input
1. terminal 1: `socat -dd pty,raw,echo=0 pty,raw,echo=0` -> find out which /dev/pts/? to use (see command output):
1. terminal 2: in ./src, `python main.py` with 1st pts in options.json
1. terminal 3: whatever you have to send data -> write it into 2nd pts port


## Tasks
- **safety info dashboard**
    - [x] 1. get data from Xbee
    - [x] 2. display dashboard with safety critical data
- **online analyis (live, in chase car)**
    - *(1. get data from Xbee)*
    - [-] 2. share XBee data with other PCs
        - [x] 2.a live broadcast all incoming data points
        - [x] 2.b store data from Xbee for future requests
        - [-] 2.c fulfill queries for past data in specific time interval
    - [ ] 3. unify data from all sources
    - [ ] 6. visualizing data
        - [ ] 6.a tagging timestamps, labelling time intervals
        - [-] 6.b dynamically select parameters and timeframes to show
        - [-] 6.c compare different data views, somehow?
        - [-] 6.d dynamically reload data processing scripts
    - temporary file storage of dataframe?
    - ~~calculate energy gain/consumption for road ahead?~~ -> offline only?
- **offline analysis (in camp)**
    - *(3. unify data from all sources)*
    - [ ] 4. get data from AC/DC SD-card
        - no sharing or persistant storage in modified form necessary
    - [ ] 5. get data from MC SD-card
        - no sharing or persistant storage in modified form necessary
    - visualize time periods of valid data?
    - calculate energy gain+consumption for road ahead?
