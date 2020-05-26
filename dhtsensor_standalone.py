import time
import board
import adafruit_dht
 
# Initial the dht device, with data pin connected to:
dhtDevice = adafruit_dht.DHT22(board.D24)  # pin 18 / GPIO 24
 
try:
    # Print the values to the serial port
    temperature_c = dhtDevice.temperature
    humidity = dhtDevice.humidity
    print("{:.1f}|{}".format(temperature_c, humidity))

except RuntimeError as error:
    # Errors happen fairly often, DHT's are hard to read, just keep going
    print("0|0")

