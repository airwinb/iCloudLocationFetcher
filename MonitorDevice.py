import abc
import datetime
import math
import requests
import time
from constants import ACTION_NEEDED_ERROR_SLEEP_TIME
from Location import Location

MIN_RETRIEVE_INTERVAL_IN_S = 10
DEFAULT_RETRIEVE_INTERVAL_IN_S = 300  # used when home
MAX_RETRIEVE_INTERVAL_IN_S = 3600

RETRY_EXPONENTIAL_BASE_IN_S = 5
SPEED_SECONDS_PER_KM = 45

URL_DISTANCE_PARAM = "__DISTANCE__"
OUTDATED_LIMIT_IN_S = 60   # if icloud location timestamp is older than this, then retry
NR_SECONDS_WHEN_ALWAYS_UPDATE_URL = 3600
ACCURACY_TO_DISTANCE_PERCENTAGE = 20


class MonitorDevice(object):
    __metaclass__ = abc.ABCMeta
    logger = None
    send_to_server = True

    def __init__(self, name, update_url):
        self.name = name
        self.update_url = update_url
        self.update_url_timestamp = 0
        self.low_update_when_home_timespan = None

        self.apple_device = None
        self.location = None
        self.next_retrieve_timestamp = time.time()
        self.retrieve_retry_count = 0

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

    def get_next_retrieve_timestamp(self):
        return self.next_retrieve_timestamp

    def should_update(self):
        now = time.time()
        if self.next_retrieve_timestamp < now:
            return True
        else:
            return False

    def is_apple_device_ok(self):
        if self.apple_device is not None:
            if self.apple_device.content['locationEnabled']:
                return True
            else:
                self.logger.warn("Location disabled for '%s'. Next update in %d seconds" % (
                    self.name, ACTION_NEEDED_ERROR_SLEEP_TIME))
        else:
            self.logger.warn("Device '%s' not found. Next update in %d seconds" % (
                self.name, ACTION_NEEDED_ERROR_SLEEP_TIME))
        return False

    def get_location(self):
        apple_location = self.apple_device.location()
        if apple_location is not None:
            self.logger.debug(apple_location)
            self.logger.debug("location: type=%s, finished=%s, horizontalAccuracy=%f" % (
                apple_location['positionType'], apple_location['locationFinished'], apple_location['horizontalAccuracy']))
            location_timestamp = apple_location['timeStamp'] / 1000
            accuracy = math.floor(apple_location['horizontalAccuracy'])
            return Location(apple_location['latitude'], apple_location['longitude'], accuracy, location_timestamp)
        else:
            self.logger.warn("Unable to get the location for device %s" % self.name)
            return None

    def send_to_update_url(self, new_distance_km):
        url = self.update_url.replace(URL_DISTANCE_PARAM, str(new_distance_km))
        if self.send_to_server:
            self.logger.debug("About to update '%s' with '%s'" % (self.name, url))
            try:
                response = requests.get(url)
                self.update_url_timestamp = time.time()
                self.logger.debug("%s -> %s" % (url, response))
                if response.ok:
                    self.logger.info("Successfully updated distance of '%s' from %.1f to %.1f km" %
                                     (self.name, self.distance_km, new_distance_km))
                else:
                    self.logger.warn("Unable to update distance of '%s' using '%s'. Response: %s" %
                                     (self.name, url, response))
            except requests.ConnectionError, e:
                self.logger.error('Request failed %s - %s' % (url, e))
        else:
            self.update_url_timestamp = time.time()
            self.logger.info("Skipping sending update for '%s' to '%s'" % (self.name, url))

    def retrieve_location_and_update(self):
        if self.is_apple_device_ok():
            retrieved_location = self.get_location()

            if retrieved_location is not None:
                self.logger.debug("Retrieved location: %s" % retrieved_location)
                now = time.time()

                use_location = False
                if retrieved_location.can_be_same_location(self.location):
                    if retrieved_location.is_more_accurate(self.location):
                        location_message = 'More accurate location retrieved'
                        use_location = True
                    else:
                        location_message = 'Same or less accurate location retrieved'
                else:
                    location_message = 'New location retrieved'
                    use_location = True

                if use_location:
                    if self.location is None \
                            or retrieved_location.rounded_distance_km != self.location.rounded_distance_km:
                        self.send_to_update_url(retrieved_location.rounded_distance_km)
                    self.location = retrieved_location

                if retrieved_location.is_recent_enough() and retrieved_location.is_accurate_enough():
                    self.retrieve_retry_count = 0
                    next_message = 'update'
                else:
                    self.retrieve_retry_count += 1
                    next_message = 'retry'

                self.set_next_retrieve_timestamp(now)
                next_update = self.next_retrieve_timestamp - now
                self.logger.info("%s. Device '%s' was at %s. Next %s in %d seconds"
                            % (location_message, self.name, retrieved_location, next_message, next_update))
            else:
                self.retrieve_retry_count = 0
                self.next_retrieve_timestamp = time.time() + DEFAULT_RETRIEVE_INTERVAL_IN_S
        else:
            self.retrieve_retry_count = 0
            self.next_retrieve_timestamp = time.time() + ACTION_NEEDED_ERROR_SLEEP_TIME

    def set_next_retrieve_timestamp(self, now):
        # all went well, location is recent and accurate
        if self.retrieve_retry_count == 0:
            # if at home
            if self.location.rounded_distance_km == 0.0:
                self.next_retrieve_timestamp = now + self.calculate_seconds_to_sleep_when_home()
            else:  # not at home, so use distance based interval in range [min, max]
                self.next_retrieve_timestamp = now + max(MIN_RETRIEVE_INTERVAL_IN_S,
                                                         min(int(SPEED_SECONDS_PER_KM * self.location.rounded_distance_km),
                                                             MAX_RETRIEVE_INTERVAL_IN_S))
        else:
            distance_based_timeout_in_s = int(SPEED_SECONDS_PER_KM * self.location.rounded_distance_km)
            if distance_based_timeout_in_s < DEFAULT_RETRIEVE_INTERVAL_IN_S:
                # use exponential retry time in range [min, default]
                self.next_retrieve_timestamp = now + min(MIN_RETRIEVE_INTERVAL_IN_S +
                    self.retrieve_retry_count * self.retrieve_retry_count * RETRY_EXPONENTIAL_BASE_IN_S,
                                                         DEFAULT_RETRIEVE_INTERVAL_IN_S)
            else:
                # use exponential retry time in range [min, distance_based]
                self.next_retrieve_timestamp = now + min(MIN_RETRIEVE_INTERVAL_IN_S +
                    self.retrieve_retry_count * self.retrieve_retry_count * RETRY_EXPONENTIAL_BASE_IN_S,
                                                         distance_based_timeout_in_s)

    def calculate_seconds_to_sleep_when_home(self):
        if self.low_update_when_home_timespan is not None:
            # use next interval based on low_update_when_home_timespan
            now_dt = datetime.datetime.now()
            minutes_since_midnight = \
                math.floor((now_dt - now_dt.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60)
            if self.low_update_when_home_timespan[0] < self.low_update_when_home_timespan[1] \
                and self.low_update_when_home_timespan[0] < minutes_since_midnight < self.low_update_when_home_timespan[1]:
                return min((self.low_update_when_home_timespan[1] - minutes_since_midnight) * 60, MAX_RETRIEVE_INTERVAL_IN_S)
            else:
                if minutes_since_midnight > self.low_update_when_home_timespan[0]:
                    return min(((24 * 60) - minutes_since_midnight + self.low_update_when_home_timespan[1]) * 60, MAX_RETRIEVE_INTERVAL_IN_S)
                if minutes_since_midnight < self.low_update_when_home_timespan[1]:
                    return min((self.low_update_when_home_timespan[1] - minutes_since_midnight) * 60, MAX_RETRIEVE_INTERVAL_IN_S)

        return DEFAULT_RETRIEVE_INTERVAL_IN_S
