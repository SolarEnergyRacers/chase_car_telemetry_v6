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
