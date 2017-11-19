#!/usr/bin/python

import abc
import ConfigParser
import datetime
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
DEFAULT_RETRIEVE_INTERVAL = 270
OUTDATED_LIMIT = 60  # if icloud location timestamp is older than this, then retry
OUTDATED_LOCATION_RETRY_INTERVAL = 15
MAX_RETRIEVE_RETRIES = 3  # if after this many retries the location is still old, then revert to DEFAULT_RETRIEVE_INTERVAL
MIN_RETRIEVE_INTERVAL = 10
MAX_RETRIEVE_INTERVAL = 3600

MIN_SLEEP_TIME = 1
MAX_SLEEP_TIME = 3600
RECOVERABLE_ERROR_SLEEP_TIME = 300
ACTION_NEEDED_ERROR_SLEEP_TIME = 3600

# Constants (Do not change)
SCRIPT_VERSION = "0.8.0-SNAPSHOT"
SCRIPT_DATE = "2017-11-19"
URL_DISTANCE_PARAM = "__DISTANCE__"

# Global variables
keep_running = True
logger = None


class MonitorDevice(object):
    __metaclass__ = abc.ABCMeta
    logger = None

    def __init__(self, name, update_url):
        self.name = name
        self.update_url = update_url
        self.update_url_timestamp = 0
        self.distance = -1.0
        self.location_timestamp = 0
        self.next_retrieve_timestamp = time.time()
        self.retrieve_retry_count = 0
        self.apple_device = None
        self.low_update_when_home_timespan = None

    @classmethod
    def set_logger(cls, value):
        cls.logger = value

    @classmethod
    def set_send_to_server(cls, value):
        cls.send_to_server = value

    def get_apple_device(self):
        return self.apple_device

    def set_apple_device(self, value):
        self.apple_device = value

    def set_low_update_when_home_timespan(self, value):
        self.low_update_when_home_timespan = value

    def update(self, distance, icloud_location_timestamp):
        previous_distance = self.distance
        self.location_timestamp = icloud_location_timestamp
        now = time.time()
        # send update if
        # 1. real significant change
        # 2. if we haven't send an update for a long time
        if (distance != previous_distance and not (abs(distance - previous_distance == 0.1) and (distance >= 5.0))) \
                or now - self.update_url_timestamp > NR_SECONDS_WHEN_ALWAYS_UPDATE_DOMOTICZ:
            self.distance = distance
            self.send_to_update_url(previous_distance)

        self.set_next_retrieve_timestamp()

    def send_to_update_url(self, previous_distance):
        url = self.update_url.replace(URL_DISTANCE_PARAM, str(self.distance))
        if self.send_to_server:
            self.logger.debug("About to update '%s' with '%s'" % (self.name, url))
            try:
                response = requests.get(url)
                self.update_url_timestamp = time.time()
                self.logger.debug("%s -> %s" % (url, response))
                if response.ok:
                    self.logger.info("Successfully updated distance of '%s' from %.1f to %.1f km" % (self.name, previous_distance, self.distance))
                else:
                    self.logger.warn("Unable to update distance of '%s' using '%s'. Response: %s" % (self.name, url, response))
            except requests.ConnectionError, e:
                self.logger.error('Request failed %s - %s' % (url, e))
        else:
            self.logger.info("Skipping sending update for '%s' to '%s'" % (self.name, url))

    def get_next_retrieve_timestamp(self):
        return self.next_retrieve_timestamp

    def set_next_retrieve_timestamp(self):
        now = time.time()

        # received old location
        if now - self.location_timestamp > OUTDATED_LIMIT:
            # can still retry to get up-to-date reading
            if self.retrieve_retry_count < MAX_RETRIEVE_RETRIES:
                self.next_retrieve_timestamp = now + OUTDATED_LOCATION_RETRY_INTERVAL
                self.retrieve_retry_count += 1
            else:  # use distance based interval in range [default, max]
                self.next_retrieve_timestamp = now + max(DEFAULT_RETRIEVE_INTERVAL,
                                                         min(int(30 * self.distance), MAX_RETRIEVE_INTERVAL))
                self.retrieve_retry_count = 0
        else:  # location is recent
            self.retrieve_retry_count = 0
            # if at home
            if self.distance == 0.0:
                self.next_retrieve_timestamp = now + self.calculate_seconds_to_sleep_when_home()
            else:  # not at home, so use distance based interval in range [min, max]
                self.next_retrieve_timestamp = now + max(MIN_RETRIEVE_INTERVAL, min(int(30 * self.distance), MAX_RETRIEVE_INTERVAL))

    def calculate_seconds_to_sleep_when_home(self):
        if self.low_update_when_home_timespan is not None:
            # use next interval based on low_update_when_home_timespan
            now_dt = datetime.datetime.now()
            minutes_since_midnight = math.floor((now_dt - now_dt.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60)
            if self.low_update_when_home_timespan[0] < self.low_update_when_home_timespan[1] \
                        and self.low_update_when_home_timespan[0] <  minutes_since_midnight < self.low_update_when_home_timespan[1]:
                return min((self.low_update_when_home_timespan[1] - minutes_since_midnight) * 60, MAX_RETRIEVE_INTERVAL)
            else:
                if minutes_since_midnight > self.low_update_when_home_timespan[0]:
                    return min(((24 * 60) - minutes_since_midnight + self.low_update_when_home_timespan[1]) * 60, MAX_RETRIEVE_INTERVAL)
                if minutes_since_midnight < self.low_update_when_home_timespan[1]:
                    return min((self.low_update_when_home_timespan[1] - minutes_since_midnight) * 60, MAX_RETRIEVE_INTERVAL)

        return DEFAULT_RETRIEVE_INTERVAL


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
    config = ConfigParser.SafeConfigParser({'low_updates_when_home': None, 'send_to_server': "true"})
    config_exists = False
    for loc in os.curdir, os.path.expanduser("~"), os.path.join(os.path.expanduser("~"), "iCloudLocationFetcher"):
        try:
            with open(os.path.join(loc, "iCloudLocationFetcher.conf")) as source:
                config.readfp(source)
                config_exists = True
        except IOError:
            pass

    if not config_exists:
        print("Error: Unable to find the 'iCloudLocationFetcher.conf' file. \n"
              "Put it in the current directory, in ~ or in ~/iCloudLocationFetcher.\n")
        sys.exit(1)

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

    low_updates_when_home_timespan = None
    low_updates_when_home_str = config.get('GENERAL', 'low_updates_when_home')
    if low_updates_when_home_str is not None:
        low_updates_when_home_start_minutes = None
        low_updates_when_home_end_minutes = None
        low_updates_when_home_list = [x.strip() for x in low_updates_when_home_str.split('-')]
        if len(low_updates_when_home_list) == 2:
            hours_min_start_list = [int(x.strip()) for x in low_updates_when_home_list[0].split(':')]
            if len(hours_min_start_list) == 2:
                low_updates_when_home_start_minutes = 60 * hours_min_start_list[0] + hours_min_start_list[1]
            hours_min_end_list = [int(x.strip()) for x in low_updates_when_home_list[1].split(':')]
            if len(hours_min_end_list) == 2:
                low_updates_when_home_end_minutes = 60 * hours_min_end_list[0] + hours_min_end_list[1]

        if low_updates_when_home_start_minutes is None or low_updates_when_home_end_minutes is None:
            logger.warn("Invalid format of 'low_updates_when_home' parameter in config. Found '%s', but format should be '23:30-07:00'" % low_updates_when_home_str)
        else:
            logger.info("Low updates starting from %s (%d minutes) to %s (%d minutes)" % (low_updates_when_home_list[0], low_updates_when_home_start_minutes, low_updates_when_home_list[1], low_updates_when_home_end_minutes))
            low_updates_when_home_timespan = [low_updates_when_home_start_minutes, low_updates_when_home_end_minutes]

    send_to_server = config.getboolean('GENERAL', 'send_to_server')
    devices_to_monitor_str = config.get('GENERAL', 'devices_to_monitor')
    devices_to_monitor = devices_to_monitor_str.strip().split('\n')
    monitor_devices = []
    for device_to_monitor in devices_to_monitor:
        name_and_url = device_to_monitor.split(',')
        monitor_device = MonitorDevice(name_and_url[0], name_and_url[1])
        monitor_device.set_low_update_when_home_timespan(low_updates_when_home_timespan)
        monitor_devices.append(monitor_device)

    MonitorDevice.set_logger(logger)
    MonitorDevice.set_send_to_server(send_to_server)

    sleep_time = MIN_SLEEP_TIME
    icloud = None
    while keep_running:
        try:
            now = time.time()
            if icloud is None:
                icloud = pyicloud.PyiCloudService(apple_id, apple_password, "~/.iCloudLocationFetcher")
                if icloud.requires_2sa:
                    logger.error("Two-step authentication required. Please run twostep.py")
                    sleep_time = RECOVERABLE_ERROR_SLEEP_TIME
                    icloud = None
                else:
                    sleep_time = MIN_SLEEP_TIME
                    icloud_devices = icloud.devices
                    # set corresponding devices
                    for monitor_device in monitor_devices:
                        logger.info("Searching for '%s' in iCloud devices" % monitor_device.name)
                        apple_device = get_apple_device(icloud_devices, monitor_device.name)
                        if apple_device is not None:
                            monitor_device.set_apple_device(apple_device)
                            logger.info("Found iCloud device '%s'" % str(apple_device))
                        else:
                            logger.warn("No iCloud device found with name '%s'" % monitor_device.name)

            if icloud is not None:
                next_sleep_time = MAX_SLEEP_TIME
                for monitor_device in monitor_devices:
                    if monitor_device.get_next_retrieve_timestamp() < now:
                        logger.debug("Getting update for %s" % monitor_device.name)
                        apple_device = monitor_device.get_apple_device()
                        if apple_device is not None:
                            if apple_device.content['locationEnabled']:
                                location = apple_device.location()
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
                                    next_sleep_time = min(next_sleep_time, next_update + MIN_SLEEP_TIME)
                                    logger.info("Device '%s' was %d seconds ago at %d meter, or rounded at %.1f km. Next update in %d seconds" % (monitor_device.name, location_seconds_ago, distance, rounded_distance_km, next_update))
                            else:
                                next_sleep_time = min(next_sleep_time, ACTION_NEEDED_ERROR_SLEEP_TIME)
                                logger.warn("Location disabled for '%s'. Next update in %d seconds" % (monitor_device.name, ACTION_NEEDED_ERROR_SLEEP_TIME))
                        else:
                            next_sleep_time = min(next_sleep_time, ACTION_NEEDED_ERROR_SLEEP_TIME)
                            logger.warn("Device '%s' not found. Next update in %d seconds" % (monitor_device.name, ACTION_NEEDED_ERROR_SLEEP_TIME))

                    else:
                        next_update = int(monitor_device.get_next_retrieve_timestamp() - now)
                        logger.debug("Update not needed yet for '%s'. Next update in %d seconds" % (monitor_device.name, next_update))
                        next_sleep_time = min(next_sleep_time, next_update + MIN_SLEEP_TIME)
                    sleep_time = next_sleep_time

        except PyiCloudAPIResponseError as e:
            logger.warn("PyiCloudAPIResponseError: {0}. Sleeping for {1} seconds".format(str(e), str(RECOVERABLE_ERROR_SLEEP_TIME)))
            icloud = None
            sleep_time = RECOVERABLE_ERROR_SLEEP_TIME
        except requests.exceptions.ConnectionError as e:
            logger.warn("ConnectionError: {0}. Sleeping for {1} seconds".format(str(e), str(RECOVERABLE_ERROR_SLEEP_TIME)))
            icloud = None
            sleep_time = RECOVERABLE_ERROR_SLEEP_TIME
        except requests.Timeout as e:
            logger.warn("Timout: {0}. Sleeping for {1} seconds".format(str(e), str(RECOVERABLE_ERROR_SLEEP_TIME)))
            icloud = None
            sleep_time = RECOVERABLE_ERROR_SLEEP_TIME
        except:
            logger.exception("Unexpected exception. Sleeping for {0} seconds".format(RECOVERABLE_ERROR_SLEEP_TIME))
            icloud = None
            sleep_time = ACTION_NEEDED_ERROR_SLEEP_TIME

        logger.debug("Sleeping for %d seconds" % sleep_time)
        time.sleep(sleep_time)


if __name__ == '__main__':
    main()
