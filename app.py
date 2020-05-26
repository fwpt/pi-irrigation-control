from flask import Flask
from flask import render_template
from flask import request
from flask import g
from math import sqrt

import sqlite3

import calendar
from datetime import datetime, timedelta, timezone

import time
import board
import Adafruit_GPIO.SPI as SPI
import Adafruit_MCP3008
from gpiozero import CPUTemperature
import RPi.GPIO as GPIO

import subprocess

# WARNING: no authentication required in this experimental version so make sure you don't expose to the internet!
# If you understand what this means, remove line 491 in handle_post() to allow POSTs and potentially anyone to control your valve
# This is an alpha version hacked together in a few hours. If interested in this project, come back later for an improved version and use this script for inspiration only

# INITIALIZATIONS 
DATABASE = 'db/database.db'

# Initial the dht device, with data pin connected to:
# This causes libgpiod_pulsei to hang on 100% CPU > moved to separate thread for now
#dhtDevice = adafruit_dht.DHT22(board.D24)  # pin 18 / GPIO 24
dhtDevice = None

# Software SPI configuration:
# MCP3008 CLK   to Pi pin 18    > GPIO 11 (23)
# MCP3008 DOUT  to Pi pin 23    > GPIO 09 (21)
# MCP3008 DIN   to Pi pin 24    > GPIO 10 (19)
# MCP3008 CS/SHDN to Pi pin 25  > GPIO 08 (24)
CLK  = 11
MISO = 9
MOSI = 10
CS   = 8
mcp = Adafruit_MCP3008.MCP3008(clk=CLK, cs=CS, miso=MISO, mosi=MOSI)

channel_pump = 5
sensor_power_switch = 6 # channel 6 connected to relay and only powers all sensors if turned on (turn off after use because sensors can overheat if on for too long)
sig_on = GPIO.LOW
sig_off = GPIO.HIGH

# GPIO setup
GPIO.setmode(GPIO.BCM)

# Valve control
GPIO.setup(channel_pump, GPIO.OUT)
GPIO.output(channel_pump, sig_off) # need this on initialization to ensure pump is/stays off

# Sensor power control
GPIO.setup(sensor_power_switch, GPIO.OUT)
GPIO.output(sensor_power_switch, sig_off) # need this on initialization to ensure sensors are powered off

app = Flask(__name__)

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.commit()
        db.close()

@app.route('/irrictrl')
def index():

    irri_cycles = []
    with app.app_context():
        try:

            # build list with recent irrigation cycles, grouped by all cycles that where supplied within one hour
            cycles_group = [] 

            # 0 valve, 1 dt, 2 status, 3 source, 4 control_type
            start_dt = datetime.now() - timedelta(days=21)
            is_first = True
            for res in query_db('select * from irrigation_log where dt >= ? order by dt asc', (start_dt, )):

                row = {}
                dt = dt_utc2local(datetime.fromisoformat(res[1]), 'python')
                row['dt'] = dt
                row['dt_friendly'] = dt.strftime("%a %d/%m %H:%M") #:%S")
                row['valve_id'] = '#' + str(res[0])[6:]
                row['status'] = str(res[2]) 
                if row['status'] == 'status_on':
                    row['status'] = 'ON'
                elif row['status'] == 'status_off':
                    row['status'] = 'OFF'
                #row['source'] = str(res[3]) 
                row['control_type'] = str(res[4]) 
                row['test'] = False
                if 'Test' in row['control_type']:
                    row['test'] = True
                
                if is_first and row['status'] == 'OFF':
                    # this can happen with sql select on date and should ignore this item (next one will be ON)
                    continue
                is_first = False

                # now loop all other irrigation logs in the group and determine if this is new cycle or add to current
                match = False
                for c in cycles_group:
                    if row['valve_id'] == c['valve_id'] and row['dt'] >= c['dt_cycle_range_start'] and row['dt'] <= c['dt_cycle_range_end']:
                        # add this to current cycles 
                        c['data'].append(row)
                        match = True

                if not match:
                    # new cycle
                    c = {}
                    c['valve_id'] = row['valve_id']
                    c['dt_cycle_range_start'] = row['dt'] - timedelta(hours=1)
                    c['dt_cycle_range_end'] = row['dt'] + timedelta(hours=1)
                    c['data'] = []
                    c['data'].append(row)
                    cycles_group.append(c)
                    #print(c)

            # all data is in cycles_group now - do calc and add summary for each cycle
            for ci in range(len(cycles_group)):

                c = cycles_group[ci]
                is_last_cycle = (ci == (len(cycles_group) - 1))

                if len(c['data']) < 2 or (len(c['data']) % 2) != 0:
                    # invalid
                    print('Invalid cycle group data, length should be 2 minimum (1 ON, 1 OFF) and even item length')
                    continue

                # data should be in asc order so expect ON OFF ON OFF
                c['dt_start'] = c['data'][0]['dt_friendly'] # first data el
                c['dt_finish'] = c['data'][-1]['dt_friendly'] # last data el
                c['total_cycles'] = int(len(c['data']) / 2) # should not require round as number of items is even (equal amount of ON and OFF)
                c['total_seconds'] = 0 # sumarise below
                c['age'] = ''
                if is_last_cycle:
                    # time since last cycle (dynamic age until now)
                    c['age'] = str(dt_utc2local(datetime.now(), 'python') - c['data'][0]['dt'])[0:-13] + ' h.'
                else:
                    # time since prev cycle (can't tell first cycle unless query db for older one)
                    c['age'] = str(cycles_group[ci + 1]['data'][-1]['dt'] - c['data'][0]['dt'])[0:-13] + ' h.'

                #for i in range(len(c['data']),,2):
                for i in range(len(c['data'])):

                    # merge this item and next one for calculation (one cycle ON+OFF); ignore odd index (even is ON, odd is OFF)
                    if (i % 2) != 0:
                        continue

                    start_c = c['data'][i]
                    end_c = c['data'][i+1]
                    duration = (end_c['dt'] - start_c['dt']).total_seconds()
                    c['total_seconds'] = c['total_seconds'] + duration
                    
                c['total_ml'] = int(round(1 * c['total_seconds'], 1)) # best guess, amount is ~1ml per second with 2bar water pressure (regulated)
                c['total_seconds'] = int(round(c['total_seconds'], 0))
                
                #unset(c['data'])
                irri_cycles.append(c)

            # reverse order to desc
            irri_cycles.reverse()

        except RuntimeError as error:
            print(error.args[0])

    return render_template('valvectrl.html', irri_cycles=irri_cycles) 

@app.route('/store_measures')
def store_measures():

    # turn on sensors first
    GPIO.output(sensor_power_switch, sig_on)  # Turn sensor power on

    # wait 15 seconds for sensors to stabalize
    time.sleep(15)

    channel = 0 # moist sensor signal is connected to channel 0

    # Iterate over all sensors and take 10 samples with 4 second pause time
    sleep_time = 5
    samples_count = 5
    sensor = dict()
    sensor['temp'] = []
    sensor['humid'] = []
    sensor['moist'] = []
    sensor['cpu'] = []

    timestamp = datetime.now()

    # collect the samples from all sensors; improve this to modular aproach later and allow for custom frequencies per sensor
    for i in range(samples_count):

        try:
            # Temp from DHT22 AM2302
            # Now in subprocess to prevent freezing unreliable libio
            output = subprocess.check_output(['/usr/bin/python3', './dhtsensor_standalone.py'])
            output = output.decode().rstrip()
            if len(output) > 0:
                
                #print("DHT22 Sensor output:", output)
                temperature_c, humidity = output.split('|')

                # convert values back to numeric
                temperature_c = float(temperature_c)
                humidity = float(humidity)

                if temperature_c > 50 or temperature_c < 0 or humidity > 100 or humidity < 0:
                    # invid reading
                    temperature_c = 0
                    humidity = 0

            else:
                print("No data in DHT22 sensor output")
                temperature_c = 0
                humidity = 0

        except RuntimeError as error:
            # Errors happen fairly often, DHT's are hard to read, just keep going
            print(error.args[0])
            temperature_c = 0
            humidity = 0

        try:
            # Soil moisture from Capacitive soil moisture sensor v1.2 anolog sensor via MCP3008 DA
            moist_level = mcp.read_adc(0)
        except RuntimeError as error:
            print(error.args[0])
            moist_level = 0

        try:
            # Pi system CPU temp
            cpu = CPUTemperature()
            cpu_c = cpu.temperature
        except RuntimeError as error:
            print(error.args[0])
            cpu_c = 0

        #measure_vals = "It. {} | Temp: {:.1f} C    Humidity: {}%    Moist volt {}    CPU temp: {} C".format(i, temperature_c, humidity, moist_level, cpu_c)
        #print(measure_vals)
        if temperature_c > 0:
            sensor['temp'].append(temperature_c)
            sensor['humid'].append(humidity)
        sensor['moist'].append(moist_level)
        sensor['cpu'].append(cpu_c)
        time.sleep(sleep_time)

    # turn sensors off
    GPIO.output(sensor_power_switch, sig_off)  # Turn sensor power off
    time.sleep(1)

    # Check if we have measures from temp sensor; sometimes we don't get any reading at all so just store 0 for now to measure how often this happens
    if len(sensor['temp']) == 0:
        sensor['temp'].append(0)
        sensor['humid'].append(0)

    # Get db cursor
    with app.app_context():
        cur = None
        try:
            db = get_db()
            cur = db.cursor()

            # init db (once)
            cur.execute('CREATE TABLE IF NOT EXISTS measure_val (sensor TEXT, dt TEXT, val REAL)')
            db.commit()

        except RuntimeError as error:
            print(error.args[0])

        # Now go normalise these values
        sensors = ['temp', 'humid', 'moist', 'cpu']
        for s in sensors:
            if len(sensor[s]) > 1:
                value_norm, mean = normalize_average(sensor[s])
                value_norm = round(value_norm, 1)
            else:
                value_norm = round(sensor[s][0], 1)

            # Store values in database
            if not cur:
                print('Cannot insert in db (have no cursor)')
            else:
                cur.execute('INSERT INTO measure_val (sensor, dt, val) VALUES (?, ?, ?)',
                    (s, timestamp, value_norm))
                db.commit

        return ''

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def normalize_average(lst):
    # Calculates standard deviation for the list provided
    num_items = len(lst)
    mean = sum(lst) / float(num_items)
    differences = [x - mean for x in lst]
    sq_differences = [d ** 2 for d in differences]
    ssd = sum(sq_differences)
    variance = ssd / float(num_items)
    sd = sqrt(variance)
     
    # keep only valid data within sd
    final_list = [x for x in lst if ((x >= mean - sd) and (x <= mean + sd))]
    num_items = len(final_list)
    norm_mean = sum(final_list) / float(num_items)
    
    return norm_mean, mean

def water_control(valve_id, status, initiator):

    is_test = False

    if valve_id == 'valve_1':
        valve_gpio_channel = channel_pump
    else:
        # Error for now, have only 1 valve in this temporary setup
        print('Invalid water control valve_id requested', valve_id)
        return False

    valve_signal = sig_off # use as default to fail to status off
    if status == 'status_on':
        valve_signal = sig_on
    elif status == 'status_off':
        valve_signal = sig_off
    else:
        print('Invalid water control status requested', status)
        return False

    # Manually triggered by initiator
    source = initiator
    control_type = 'Manual'
    if is_test:
        control_type = control_type + ' Test'
    timestamp = datetime.now()

    # Log state change in db
    # Get db cursor
    with app.app_context():
        cur = None
        try:
            db = get_db()
            cur = db.cursor()

            # init db (once)
            cur.execute('CREATE TABLE IF NOT EXISTS irrigation_log (valve TEXT, dt TEXT, status TEXT, source TEXT, control_type TEXT)')
            db.commit()

        except RuntimeError as error:
            print(error.args[0])

        # Store values in database
        if not cur:
            print('Cannot insert in db (have no cursor)')
        else:
            cur.execute('INSERT INTO irrigation_log (valve, dt, status, source, control_type) VALUES (?, ?, ?, ?, ?)',
                (valve_id, timestamp, status, source, control_type))
            db.commit

    if not is_test:
        GPIO.output(valve_gpio_channel, valve_signal)  # Turn pump on/off

    return True

def dt_utc2local(dt, output_method):

    # Sydney timezone hack for now
    if output_method == 'python':
        dt = dt + timedelta(hours=9)
    # Don't need conversion when reflected in html as Javascript takes care of that

    return dt

@app.route('/measures')
def measures():

    measure_vals = []    
    with app.app_context():
        try:
            db = get_db()
            cur = db.cursor()

            row = {}
            last_dt = None
            # show data from last 7 days
            start_dt = datetime.now() - timedelta(days=7, minutes=30)
            for res in query_db('select * from measure_val where dt >= ? order by dt desc', (start_dt, )):
                dt = res[1]
                if dt != last_dt:
                    # new row, add prev and reset
                    last_dt = dt
                    if last_dt != None:
                        measure_vals.append(row)
                        row = {}
                    dt = dt_utc2local(datetime.fromisoformat(dt), 'python')
                    row['dt'] = str(dt)[0:-10] #:49.518873
                row['sensor_' + res[0]] = str(res[2]) 

            if len(row) != 0:
                measure_vals.append(row)

        except RuntimeError as error:
            print(error.args[0])

    return render_template('measures.html', measures=measure_vals) 

@app.route('/measures_chart')
def measures_chart():

    measures_moist = ''
    measures_temp = ''
    measures_humid = ''
    measures_irri = ''
    start_dt = datetime.now() - timedelta(days=7, minutes=30)
    with app.app_context():
        try:
            db = get_db()
            cur = db.cursor()

            # This is crazy copy/paste for now, need to work on modular sensor logic
            # Moist sensor
            for res in query_db('select * from measure_val where sensor = \'moist\' and dt >= ? order by dt desc',(start_dt, )):
                dt = dt_utc2local(datetime.fromisoformat(res[1]), 'js')
                dt = int(dt.timestamp()) * 1000
                val = res[2]
                if val > 540 or val < 380:
                    # invalid data; ignore
                    continue

                if measures_moist != '':
                    measures_moist = measures_moist + ','
                measures_moist = measures_moist + '{t: ' + str(dt) + ', y: -' + str(val) + '}'

            # Temp sensor
            for res in query_db('select * from measure_val where sensor = \'temp\' and dt >= ? order by dt desc', (start_dt, )):
                dt = dt_utc2local(datetime.fromisoformat(res[1]), 'js')
                dt = int(dt.timestamp()) * 1000
                val = res[2]
                if val <= 0:
                    # won't expect temps below zero in Sydney, hopefully ;)
                    continue
                    
                if measures_temp != '':
                    measures_temp = measures_temp + ','
                measures_temp = measures_temp + '{t: ' + str(dt) + ', y: ' + str(val) + '}'

            # Humidity sensor
            for res in query_db('select * from measure_val where sensor = \'humid\' and dt >= ? order by dt desc', (start_dt, )):
                dt = dt_utc2local(datetime.fromisoformat(res[1]), 'js')
                dt = int(dt.timestamp()) * 1000
                val = res[2]
                if val <= 0 or val > 100:
                    continue
                    
                if measures_humid != '':
                    measures_humid = measures_humid + ','
                measures_humid = measures_humid + '{t: ' + str(dt) + ', y: ' + str(val) + '}'

            # Include recent irrigation cycles
            for res in query_db('select * from irrigation_log where dt >= ? order by dt desc', (start_dt, )):
                dt = dt_utc2local(datetime.fromisoformat(res[1]), 'js')
                dt = int(dt.timestamp()) * 1000
                val = res[2]

                if measures_irri != '':
                    measures_irri = measures_irri + ','
                measures_irri = measures_irri + '{t: ' + str(dt) + ', y: 10}'

        except RuntimeError as error:
            print(error.args[0])

    return render_template('measures_chart.html', measures_moist=measures_moist, measures_temp=measures_temp, measures_humid=measures_humid, measures_irri=measures_irri)

@app.route('/irrictrl/valvectrl', methods=['POST'])
def handle_post():
    # show the post with the given id, the id is an integer
    return 'READ THE WARNING - POST DISABLED BECAUSE THIS SCRIPT DOES NOT REQUIRE AUTHENTICAION YET'
    if request.form['action'] == 'switch_on':
        #GPIO.output(channel_pump, sig_on)  # Turn pump on
        res = water_control('valve_1', 'status_on', 'web_user')
        if res:
            return 'ON<br><br><br><a href="/irrictrl" style="font-size:40px">&laquo; BACK</a>'
        else:
            return 'Failed to switch ON<br><br><br><a href="/irrictrl" style="font-size:40px">&laquo; BACK</a>'
    
    if request.form['action'] == 'switch_off':
        #GPIO.output(channel_pump, sig_off)  # Turn pump off
        res = water_control('valve_1', 'status_off', 'web_user')
        if res:
            return 'OFF<br><br><br><a href="/irrictrl" style="font-size:40px">&laquo; BACK</a>'
        else:
            return 'Failed to switch OFF<br><br><br><a href="/irrictrl" style="font-size:40px">&laquo; BACK</a>'
    
    return 'Unknown POST request';
