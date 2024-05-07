# Real-Time Gamma Exposure Plotter

This repository contains the Python code for a real-time gamma exposure plotting tool designed for options trading. The tool visualizes gamma exposure per strike, changes in gamma exposure, and total gamma exposure over time, integrating live market data to provide insightful visual analytics.

## Overview

The `RealTimeGammaPlotter` class is a comprehensive tool that handles real-time data to plot:
- Gamma exposure per strike.
- Changes in gamma exposure per strike, highlighting significant changes.
- Total gamma exposure alongside SPX spot prices over time.

It is equipped with a deque data structure to manage real-time changes efficiently and matplotlib for dynamic plotting.

## Features

- **Interactive Real-Time Plotting**: Uses `matplotlib` in interactive mode to update plots in real-time.
- **Twin Y-Axis Representation**: Displays gamma exposure and SPX spot prices concurrently for comparative analysis.
- **Historical Data Analysis**: Tracks historical changes in gamma exposure to calculate and plot means and standard deviations dynamically.

## Requirements

This project requires Python 3.8 or later. Below are the Python libraries needed:

anyio==4.3.0
attrs==23.2.0
Authlib==1.3.0
autopep8==2.0.4
certifi==2024.2.2
cffi==1.16.0
charset-normalizer==3.3.2
contourpy==1.2.0
cryptography==42.0.5
cycler==0.12.1
DateTime==5.4
fonttools==4.49.0
h11==0.14.0
httpcore==1.0.4
httpx==0.27.0
idna==3.6
kiwisolver==1.4.5
matplotlib==3.8.3
mplfinance==0.12.10b0
numpy==1.26.4
outcome==1.3.0.post0
packaging==23.2
pandas==2.2.1
pillow==10.2.0
prompt-toolkit==3.0.43
psycopg2==2.9.9
pycodestyle==2.11.1
pycparser==2.21
pyparsing==3.1.2
PySocks==1.7.1
python-dateutil==2.9.0.post0
pytz==2024.1
requests==2.31.0
schedule==1.2.1
selenium==4.18.1
six==1.16.0
sniffio==1.3.1
sortedcontainers==2.4.0
tda-api==1.6.0
trio==0.24.0
trio-websocket==0.11.1
typing_extensions==4.10.0
tzdata==2024.1
urllib3==2.2.1
wcwidth==0.2.13
websockets==12.0
wsproto==1.2.0
zope.interface==6.2


To install all required packages, you can use the following command:

```bash
pip install -r requirements.txt
/bash


Contributing
Contributions to this project are welcome! Please refer to the CONTRIBUTING.md for guidelines on how to make contributions.

License
Distributed under the MIT License. See LICENSE for more information.

Contact
Feel free to contact me for any questions or feedback on the project.
