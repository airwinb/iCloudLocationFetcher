import abc
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
DEFAULT_RETRIEVE_INTERVAL = ICLOUD_SESSION_TIMEOUT - 10
OUTDATED_LIMIT = 250  # if icloud location timestamp is older than this, then retry
OUTDATED_LOCATION_RETRY_INTERVAL = 20

MIN_RETRIEVE_INTERVAL = 10
MAX_RETRIEVE_INTERVAL = 3600

# Constants (Do not change)
SCRIPT_VERSION = "0.6.0-SNAPSHOT"
SCRIPT_DATE = "2017-11-03"
URL_DISTANCE_PARAM = "__DISTANCE__"

# Global variables
keep_running = True
logger = None


class MonitorDevice(object):
    __metaclass__ = abc.ABCMeta
    logger = None
    monitor_devices = []

    def __init__(self, name, update_url):
        self.name = name
        self.update_url = update_url
        self.distance = -1.0
        self.location_timestamp = 0
        self.next_retrieve_timestamp = time.time()

    @classmethod
    def set_logger(cls, logger):
        cls.logger = logger

    @classmethod
    def set_monitor_devices(cls, devices):
        cls.monitor_devices = devices

    def update(self, distance, icloud_location_timestamp):
        previous_distance = self.distance
        previous_location_timestamp = self.location_timestamp
        # send update if
        # 1. real significant change
        # 2. if we haven't send an update for a long time
        if (distance != previous_distance and not (abs(distance - previous_distance == 0.1) and (distance >= 5.0))) \
                or icloud_location_timestamp - previous_location_timestamp > NR_SECONDS_WHEN_ALWAYS_UPDATE_DOMOTICZ:
            self.distance = distance
            self.location_timestamp = icloud_location_timestamp
            self.send_to_update_url()

        self.set_next_retrieve_timestamp(previous_distance, previous_location_timestamp)

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

    def get_next_retrieve_timestamp(self):
        return self.next_retrieve_timestamp

    def set_next_retrieve_timestamp(self, previous_distance, previous_location_timestamp):
        # retry shortly if the new value is quite old
        if self.location_timestamp - previous_location_timestamp > OUTDATED_LIMIT:
            self.next_retrieve_timestamp = time.time() + OUTDATED_LOCATION_RETRY_INTERVAL
            return

        if self.distance == 0.0 or previous_distance == self.distance:
            seconds_to_next_retrieve = DEFAULT_RETRIEVE_INTERVAL
        else:
            seconds_to_next_retrieve = max(MIN_RETRIEVE_INTERVAL, min(int(30 * self.distance), MAX_RETRIEVE_INTERVAL))
        self.next_retrieve_timestamp = time.time() + seconds_to_next_retrieve


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
    sleep_time = 10
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
                    sleep_time = 5
                    last_icloud_request_time = time.time()

            if connected:
                for monitor_device in monitor_devices:
                    if monitor_device.get_next_retrieve_timestamp() < now:
                        logger.debug("Getting update for %s" % monitor_device.name)
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
                                    monitor_device.update(rounded_distance_km, location_timestamp)
                                    now = time.time()
                                    location_seconds_ago = int(now - location_timestamp)
                                    next_update = int(monitor_device.get_next_retrieve_timestamp() - now)
                                    logger.info("Device '%s' was %d seconds ago at %d meter, or rounded at %.1f km. Next update in %d seconds" % (monitor_device.name, location_seconds_ago, distance, rounded_distance_km, next_update))
                    else:
                        logger.debug("Skipping update for %s" % monitor_device.name)

        except (requests.exceptions.ConnectionError, PyiCloudAPIResponseError) as e:
            # logger.warn("Exception: {0}".format(str(e)))
            now = time.time()
            logger.warn("Now - last_icloud_request_time: %d" % str(now - last_icloud_request_time))
            logger.exception("Connection error or PyiCloud exception")
            connected = False

        time.sleep(sleep_time)


if __name__ == '__main__':
    main()
