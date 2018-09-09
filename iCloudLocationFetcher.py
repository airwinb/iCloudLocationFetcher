#!/usr/bin/python

import ConfigParser
import logging
import os
import pyicloud
import requests
import signal
import sys
import time
from pyicloud.exceptions import PyiCloudAPIResponseError
from constants import ACTION_NEEDED_ERROR_SLEEP_TIME
from Location import Location
from MonitorDevice import MonitorDevice

MIN_SLEEP_TIME = 1
MAX_SLEEP_TIME = 3600
RECOVERABLE_ERROR_SLEEP_TIME = 60
MAX_SESSION_TIME = 1800  # icloud will respond with HTTP 450 if session is not used within this time

# Constants (Do not change)
SCRIPT_VERSION = "0.9.0-SNAPSHOT"
SCRIPT_DATE = "2018-09-08"

# Global variables
keep_running = True
logger = None


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
    config = ConfigParser.SafeConfigParser({'low_updates_when_home': None,
                                            'send_to_server': "true",
                                            'home_radius': "0.0"})
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
    Location.set_home_position(home_location)

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
            if icloud is None:
                icloud = pyicloud.PyiCloudService(apple_id, apple_password, "~/.iCloudLocationFetcher")
                if icloud.requires_2sa:
                    logger.error("Two-step authentication required. Please run twostep.py")
                    sleep_time = ACTION_NEEDED_ERROR_SLEEP_TIME
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
                now = time.time()
                next_sleep_time = MAX_SLEEP_TIME
                for monitor_device in monitor_devices:
                    if monitor_device.should_update():
                        monitor_device.retrieve_location_and_update()
                        next_update = int(monitor_device.get_next_retrieve_timestamp() - now)
                        next_sleep_time = min(next_sleep_time, next_update + MIN_SLEEP_TIME)
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
            logger.exception("Unexpected exception. Sleeping for {0} seconds".format(ACTION_NEEDED_ERROR_SLEEP_TIME))
            icloud = None
            sleep_time = ACTION_NEEDED_ERROR_SLEEP_TIME

        if sleep_time >= MAX_SESSION_TIME:
            icloud = None
            logger.debug("Sleeping for %d seconds and resetting icloud connection" % sleep_time)
        else:
            logger.debug("Sleeping for %d seconds" % sleep_time)
        time.sleep(sleep_time)


if __name__ == '__main__':
    main()
