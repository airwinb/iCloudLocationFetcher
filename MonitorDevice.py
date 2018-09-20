import abc
import datetime
import math
import requests
import time
from constants import ACTION_NEEDED_ERROR_SLEEP_TIME
from Location import Location

MIN_RETRIEVE_INTERVAL_IN_S = 15
MAX_RETRIEVE_INTERVAL_IN_S = 3600
DEFAULT_RETRIEVE_INTERVAL_IN_S = 300  # used when home, or long at same location near home

RETRY_EXPONENTIAL_BASE_IN_S = 3
SPEED_SECONDS_PER_KM = 40

URL_DISTANCE_PARAM = "__DISTANCE__"
OUTDATED_LIMIT_IN_S = 60  # if icloud location timestamp is older than this, then retry
OUTDATED_LIMIT_IN_S_IN_HOME_PERIOD = 600  # if icloud location timestamp is older than this during home period, then retry

ACCURACY_TO_DISTANCE_PERCENTAGE = 20


class MonitorDevice(object):
    __metaclass__ = abc.ABCMeta
    logger = None
    send_to_server = True

    def __init__(self, name, update_url):
        self.name = name
        self.update_url = update_url
        self.update_url_timestamp = 0
        self.home_period = None

        self.apple_device = None
        self.location_retrieved = None
        self.location_stored = None
        self.next_retrieve_timestamp = time.time()
        self.retrieve_retry_count = 0
        self.same_location_count = 0

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

    def set_home_period(self, value):
        self.home_period = value

    def get_next_retrieve_timestamp(self):
        return self.next_retrieve_timestamp

    def should_update(self):
        return self.next_retrieve_timestamp < time.time()

    def is_moving(self):
        return self.same_location_count == 0

    def is_apple_device_ok(self):
        if self.apple_device is not None:
            if self.apple_device.content['locationEnabled']:
                return True
            else:
                self.logger.warn("Location disabled for '%s'. Next retry in %d seconds" % (
                    self.name, ACTION_NEEDED_ERROR_SLEEP_TIME))
        else:
            self.logger.warn("Device '%s' not found. Next retry in %d seconds" % (
                self.name, ACTION_NEEDED_ERROR_SLEEP_TIME))
        return False

    def update_location_retrieved(self):
        apple_location = self.apple_device.location()
        if apple_location is not None:
            self.logger.debug(apple_location)
            self.logger.debug("location: type=%s, finished=%s, horizontalAccuracy=%f" % (
                apple_location['positionType'], apple_location['locationFinished'], apple_location['horizontalAccuracy']))
            location_timestamp = apple_location['timeStamp'] / 1000
            accuracy = math.floor(apple_location['horizontalAccuracy'])
            self.location_retrieved = Location(apple_location['latitude'], apple_location['longitude'], accuracy, location_timestamp)
            return True
        else:
            self.logger.warn("Unable to get the location for device %s. Next retry in %d seconds" %
                             (self.name, ACTION_NEEDED_ERROR_SLEEP_TIME))
            return False

    def is_within_home_period(self):
        if self.home_period is None:
            return False

        # use next interval based on low_update_when_home_timespan
        now_dt = datetime.datetime.now()
        minutes_since_midnight = \
            math.floor((now_dt - now_dt.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60)
        if self.home_period[0] < self.home_period[1]:
            return self.home_period[0] < minutes_since_midnight < self.home_period[1]
        else:
            return minutes_since_midnight > self.home_period[0] or minutes_since_midnight < self.home_period[1]

    def send_to_update_url(self, old_distance_km, new_distance_km):
        url = self.update_url.replace(URL_DISTANCE_PARAM, str(new_distance_km))
        if self.send_to_server:
            self.logger.debug("About to update '%s' with '%s'" % (self.name, url))
            try:
                response = requests.get(url)
                self.update_url_timestamp = time.time()
                self.logger.debug("%s -> %s" % (url, response))
                if response.ok:
                    self.logger.info("Successfully updated distance of '%s' from %.1f to %.1f km" %
                                     (self.name, old_distance_km, new_distance_km))
                else:
                    self.logger.warn("Unable to update distance of '%s' using '%s'. Response: %s" %
                                     (self.name, url, response))
            except requests.ConnectionError, e:
                self.logger.error('Request failed %s - %s' % (url, e))
        else:
            self.update_url_timestamp = time.time()
            self.logger.info("Skipping sending update for '%s' to '%s'" % (self.name, url))

    def is_retrieved_location_better_and_message(self):
        '''
        Promotes the location_retrieved to the location_stored if
        - location_stored is None
        - location_retrieved overlaps with location_stored, but is more accurate
        - location_retrieved does not overlap location_stored
        :return: a boolean and a string
        '''
        if self.location_stored is None:
            self.same_location_count = 0
            return True, 'Setting initial location'

        if self.location_retrieved.can_be_same_location(self.location_stored):
            self.same_location_count += 1
            if self.location_retrieved.is_more_accurate(self.location_stored):
                return True, 'Not moving (%d) but using more accurate location' % self.same_location_count
            else:
                return False, 'Not moving (%d)' % self.same_location_count
        else:
            self.same_location_count = 0
            message = 'On the move'
            if self.location_retrieved.is_home():
                message = 'Arrived home'
            if self.location_stored.is_home():
                message = 'Left home'
            return True, message

    def update_retrieve_retry_count(self):
        recent_limit = OUTDATED_LIMIT_IN_S
        if self.is_within_home_period():
            recent_limit = OUTDATED_LIMIT_IN_S_IN_HOME_PERIOD

        if not self.location_retrieved.is_recent_enough(recent_limit):
            self.retrieve_retry_count += 1
            return 'Location is not recent enough'
        if not self.location_retrieved.is_accurate_enough():
            self.retrieve_retry_count += 1
            return 'Location is not accurate enough'
        self.retrieve_retry_count = 0
        return 'Location is acceptable'

    def update_next_retrieve_timestamp(self, now):
        # all went well, location is recent and accurate
        if self.retrieve_retry_count == 0:
            # if at home
            if self.location_retrieved.is_home():
                self.next_retrieve_timestamp = now + self.calculate_seconds_to_sleep_when_home()
            else:  # not at home, so use distance based interval in range [min, max]
                regular_wait_seconds = int(SPEED_SECONDS_PER_KM * self.location_retrieved.rounded_distance_km)
                if regular_wait_seconds < DEFAULT_RETRIEVE_INTERVAL_IN_S:
                    # optionally increase waiting and use distance based interval in [min, DEFAULT]
                    self.next_retrieve_timestamp = now + max(MIN_RETRIEVE_INTERVAL_IN_S,
                        min(regular_wait_seconds + int(math.pow(RETRY_EXPONENTIAL_BASE_IN_S, self.same_location_count)),
                           DEFAULT_RETRIEVE_INTERVAL_IN_S))
                else:
                    # use distance based interval
                    self.next_retrieve_timestamp = regular_wait_seconds
        else:
            (retry_div, retry_mod) = divmod(self.retrieve_retry_count, 3)
            additional_minutes = 0
            if retry_mod == 0:
                additional_minutes = retry_div
            # use retry time in range [min, default]
            self.next_retrieve_timestamp = now + max(MIN_RETRIEVE_INTERVAL_IN_S,
                        min(MIN_RETRIEVE_INTERVAL_IN_S + additional_minutes * 60, DEFAULT_RETRIEVE_INTERVAL_IN_S))

    def log_update_message(self, now, status_message, location_message):
        next_update = self.next_retrieve_timestamp - now
        next_message = 'Next update'
        if self.retrieve_retry_count > 0:
            next_message = 'Retry %d' % self.retrieve_retry_count
        elif not self.location_retrieved.is_home() and not self.is_moving():
            next_message = 'Next increased (%d) update' % self.same_location_count
        old_location_string = 'Unknown'
        if self.location_stored is not None:
            old_location_string = self.location_stored.__str__()
        self.logger.info("Device %s: %s. Old: (%s), New: (%s). %s. %s in %d seconds"
                         % (self.name, status_message, old_location_string, self.location_retrieved, location_message,
                            next_message, next_update))

    def retrieve_location_and_update(self):
        if self.is_apple_device_ok():
            if self.update_location_retrieved():
                (location_is_better, status_message) = self.is_retrieved_location_better_and_message()
                if location_is_better:
                    old_distance_km = -1.0
                    if self.location_stored is not None:
                        old_distance_km = self.location_stored.rounded_distance_km
                    if old_distance_km != self.location_retrieved.rounded_distance_km:
                        self.send_to_update_url(old_distance_km, self.location_retrieved.rounded_distance_km)
                now = time.time()
                location_message = self.update_retrieve_retry_count()
                self.update_next_retrieve_timestamp(now)
                self.log_update_message(now, status_message, location_message)
                if location_is_better:
                    self.location_stored = self.location_retrieved
            else:
                self.retrieve_retry_count = 0
                self.next_retrieve_timestamp = time.time() + ACTION_NEEDED_ERROR_SLEEP_TIME
        else:
            self.retrieve_retry_count = 0
            self.next_retrieve_timestamp = time.time() + ACTION_NEEDED_ERROR_SLEEP_TIME

    def calculate_seconds_to_sleep_when_home(self):
        if self.home_period is not None:
            # use next interval based on low_update_when_home_timespan
            now_dt = datetime.datetime.now()
            minutes_since_midnight = \
                math.floor((now_dt - now_dt.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60)
            if self.home_period[0] < self.home_period[1] \
                and self.home_period[0] < minutes_since_midnight < self.home_period[1]:
                return min((self.home_period[1] - minutes_since_midnight) * 60, MAX_RETRIEVE_INTERVAL_IN_S)
            else:
                if minutes_since_midnight > self.home_period[0]:
                    return min(((24 * 60) - minutes_since_midnight + self.home_period[1]) * 60, MAX_RETRIEVE_INTERVAL_IN_S)
                if minutes_since_midnight < self.home_period[1]:
                    return min((self.home_period[1] - minutes_since_midnight) * 60, MAX_RETRIEVE_INTERVAL_IN_S)

        return DEFAULT_RETRIEVE_INTERVAL_IN_S
