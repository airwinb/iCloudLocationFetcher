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
SCRIPT_VERSION = "0.4.0"
SCRIPT_DATE = "2017-11-03"

# Global variables
keep_running = True
logger = None


class MonitorDevice(object):
    __metaclass__ = abc.ABCMeta
    logger = None
    domoticz_server = None
    domoticz_authorization = None
    monitor_devices = []

    def __init__(self, name, index):
        self.name = name
        self.domoticz_index = index
        self.distance = -1.0
        self.last_update_time = 0

    @classmethod
    def set_logger(cls, logger):
        cls.logger = logger

    @classmethod
    def set_domoticz(cls, domoticz_server, domoticz_authorization):
        cls.domoticz_server = domoticz_server
        cls.domoticz_authorization = domoticz_authorization

    @classmethod
    def set_monitor_devices(cls, devices):
        cls.monitor_devices = devices

    @classmethod
    def get_monitor_device(cls, name):
        device = None
        i = 0
        while device is None and i < len(cls.monitor_devices):
            if cls.monitor_devices[i].name == name:
                device = cls.monitor_devices[i]
            i += 1
        return device

    def update(self, distance, last_update_time):
        send_to_domoticz = False
        if distance != self.distance:
            self.distance = distance
            self.last_update_time = last_update_time
            send_to_domoticz = True
        else:
            if last_update_time - self.last_update_time > NR_SECONDS_WHEN_ALWAYS_UPDATE_DOMOTICZ:
                self.last_update_time = last_update_time
                send_to_domoticz = True
        if send_to_domoticz:
            self.send_to_domoticz()

    def send_to_domoticz(self):
        if self.domoticz_server == '':
            self.logger.info("Skipping update in domoticz '%s' with index %d to value %.1f" % (self.name, self.domoticz_index, self.distance))
        else:
            self.logger.info("Update in domoticz '%s' with index %d to value %.1f" % (self.name, self.domoticz_index, self.distance))
            domoticz_url = 'http://' + self.domoticz_server + '/json.htm?type=command&param=udevice&idx=' + str(self.domoticz_index) + '&nvalue=0&svalue=' + str(self.distance)
            try:
                if self.domoticz_authorization == '':
                    result = requests.get(domoticz_url)
                else:
                    headers = {'Authorization': 'Basic bmFzOm5hc2lubG9nZ2Vu'}
                    result = requests.get(domoticz_url, headers=headers)
                self.logger.debug("%s -> %s" % (domoticz_url, result))
            except requests.ConnectionError, e:
                self.logger.error('Domoticz request failed %s - %s' % (domoticz_url, e))


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
    apple_id = config.get('GENERAL', 'apple_id')
    apple_password = config.get('GENERAL', 'apple_password')
    domoticz_server = config.get('GENERAL', 'domoticz_server')
    domoticz_authorization = config.get('GENERAL', 'domoticz_authorization')
    home_location_str = config.get('GENERAL', 'home_location')
    home_location = [float(x.strip()) for x in home_location_str.split(',')]
    devices_to_monitor_str = config.get('GENERAL', 'devices_to_monitor')
    devices_to_monitor = devices_to_monitor_str.split(',')
    monitor_devices = []
    for device_to_monitor in devices_to_monitor:
        name_and_index = device_to_monitor.split(':')
        monitor_devices.append(MonitorDevice(name_and_index[0], int(name_and_index[1])))

    icloud = pyicloud.PyiCloudService(apple_id, apple_password)
    if icloud.requires_2sa:
        logger.debug("Two-step authentication required. Please run twostep.py")
        sys.exit(1)

    connected = True
    last_icloud_request_time = time.time()

    MonitorDevice.set_logger(logger)
    MonitorDevice.set_domoticz(domoticz_server, domoticz_authorization)
    MonitorDevice.set_monitor_devices(monitor_devices)

    sleep_time = 270
    while keep_running:
        try:
            now = time.time()
            if not connected or now - last_icloud_request_time > ICLOUD_SESSION_TIMEOUT:
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
