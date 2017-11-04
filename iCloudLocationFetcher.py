import abc
import datetime
import ConfigParser
import logging
import math
import os
import pyicloud
import requests
import signal
import sys
import time
from pyicloud.exceptions import PyiCloudAPIResponseError

# Configuration
NR_SECONDS_WHEN_ALWAYS_UPDATE_DOMOTICZ = 3600
ICLOUD_SESSION_TIMEOUT = 280  # in seconds; 300 seconds is too long

# Constants (Do not change)
SCRIPT_VERSION = "0.5.0"
SCRIPT_DATE = "2017-11-03"
URL_DISTANCE_PARAM = "__DISTANCE__"

# Global variables
keep_running = True
logger = None


class MonitorDevice(object):
    __metaclass__ = abc.ABCMeta
    logger = None
    domoticz_server = None
    domoticz_authorization = None
    monitor_devices = []

    def __init__(self, name, update_url):
        self.name = name
        self.update_url = update_url
        self.distance = -1.0
        self.last_update_time = 0

    @classmethod
    def set_logger(cls, logger):
        cls.logger = logger

    @classmethod
    def set_monitor_devices(cls, devices):
        cls.monitor_devices = devices

    def update(self, distance, last_update_time):
        send_update = False
        if distance != self.distance:
            self.distance = distance
            self.last_update_time = last_update_time
            send_update = True
        else:
            if last_update_time - self.last_update_time > NR_SECONDS_WHEN_ALWAYS_UPDATE_DOMOTICZ:
                self.last_update_time = last_update_time
                send_update = True
        if send_update:
            self.send_to_update_url()

    def send_to_update_url(self):
        url = self.update_url.replace(URL_DISTANCE_PARAM, str(self.distance))
        if self.update_url == '':
            self.logger.info("Skipping sending update for '%s' to '%s'" % (self.name, url))
        else:
            self.logger.info("Update '%s' with '%s'" % (self.name, url))
            try:
                result = requests.get(url)
                self.logger.debug("%s -> %s" % (url, result))
            except requests.ConnectionError, e:
                self.logger.error('Request failed %s - %s' % (url, e))


def distance_meters(origin, destination):
    lat1, lon1 = origin
    lat2, lon2 = destination
    radius_earth_km = 6371

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) * math.sin(dlat / 2) + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) \
                                                  * math.sin(dlon / 2) * math.sin(dlon / 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    d = int(round(radius_earth_km * c * 1000))

    return d


# Initialisation
def init_logging(config):
    log_file = config.get('GENERAL', 'log_file')
    log_level_from_config = config.get('GENERAL', 'log_level')
    log_to_console = config.getboolean('GENERAL', 'log_to_console')
    log_level = logging.INFO
    if log_level_from_config == 'DEBUG':
        log_level = logging.DEBUG
    elif log_level_from_config == 'INFO':
        log_level = logging.INFO
    elif log_level_from_config == 'WARNING':
        log_level = logging.WARNING

    # logging to file
    logging.basicConfig(level=log_level, format='%(asctime)s %(levelname)-8s %(message)s',
                        filename=log_file, filemode='w')
    # logging to console
    if log_to_console:
        console = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)

    local_logger = logging.getLogger('locations2domoticz')

    return local_logger


def get_apple_device(devices, name):
    device = None
    i = 0
    while device is None and i < len(devices.keys()):
        if devices[i].content['name'] == name:
            device = devices[i]
        i += 1
    return device


# Main program
def main():
    global keep_running, logger

    # read configuration
    config = ConfigParser.SafeConfigParser()
    for loc in os.curdir, os.path.expanduser("~"):
        try:
            with open(os.path.join(loc, "iCloudLocationFetcher.conf")) as source:
                config.readfp(source)
        except IOError:
            pass

    logger = init_logging(config)

    logger.info("---")
    logger.info("iCloudLocationFetcher, v%s, %s" % (SCRIPT_VERSION, SCRIPT_DATE))

    def signal_handler(signal1, frame):
        global keep_running, logger
        logger.info('Signal received: %s from %s' % (signal1, frame))
        logger.debug('Setting keep_running to False')
        keep_running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # read other configuration
    apple_creds_file = config.get('GENERAL', 'apple_creds_file')
    try:
        with open(apple_creds_file) as f:
            apple_id = f.readline().strip()
            apple_password = f.readline().strip()
    except IOError, e:
        logger.error("Unable to read the apple credentials file '%s': %s" % (apple_creds_file, str(e)))
        sys.exit(1)

    home_location_str = config.get('GENERAL', 'home_location')
    home_location = [float(x.strip()) for x in home_location_str.split(',')]
    devices_to_monitor_str = config.get('GENERAL', 'devices_to_monitor')
    devices_to_monitor = devices_to_monitor_str.strip().split('\n')
    monitor_devices = []
    for device_to_monitor in devices_to_monitor:
        name_and_url = device_to_monitor.split(',')
        monitor_devices.append(MonitorDevice(name_and_url[0], name_and_url[1]))

    MonitorDevice.set_logger(logger)
    MonitorDevice.set_monitor_devices(monitor_devices)

    connected = False
    last_icloud_request_time = 0
    sleep_time = 270
    icloud = None
    while keep_running:
        try:
            now = time.time()
            if icloud is None or not connected or now - last_icloud_request_time > ICLOUD_SESSION_TIMEOUT:
                icloud = pyicloud.PyiCloudService(apple_id, apple_password)
                if icloud.requires_2sa:
                    logger.error("Two-step authentication required. Please run twostep.py")
                    sleep_time = 900
                else:
                    connected = True
                    last_icloud_request_time = time.time()

            if connected:
                for monitor_device in monitor_devices:
                    apple_device = get_apple_device(icloud.devices, monitor_device.name)
                    if apple_device is not None:
                        if apple_device.content['locationEnabled']:
                            location = apple_device.location()
                            last_icloud_request_time = time.time()
                            if location is not None:
                                logger.debug(location)
                                location_timestamp = location['timeStamp'] / 1000
                                device_location = (location['latitude'], location['longitude'])
                                distance = distance_meters(home_location, device_location)
                                rounded_distance_km = math.floor(distance / 100) / 10.0
                                location_date = datetime.datetime.fromtimestamp(location_timestamp).strftime('%Y-%m-%d %H:%M:%S')
                                logger.info("Device '%s' on %s at %d meter, or rounded at %.1f km" % (monitor_device.name, location_date, distance, rounded_distance_km))
                                monitor_device.update(rounded_distance_km, location_timestamp)

        except (requests.exceptions.ConnectionError, PyiCloudAPIResponseError) as e:
            logger.warn("Exception: {0}".format(str(e)))
            connected = False

        time.sleep(sleep_time)


if __name__ == '__main__':
    main()
