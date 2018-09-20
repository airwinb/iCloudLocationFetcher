# -*- coding: utf-8 -*-
import abc
import math
import time

ACCURATE_LIMIT_IN_M = 100  # if location accuracy is smaller than this then it is accurate
ACCURATE_LIMIT_WHEN_HOME_IN_M = 300  # if home, if accuracy is within this percentage of distance, then accurate enough
ACCURACY_TO_DISTANCE_PERCENTAGE = 20  # if accuracy is within this percentage of the distance, then accurate enough


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


class Location(object):
    __metaclass__ = abc.ABCMeta
    home_position = None  # type: 'list'

    def __init__(self, latitude, longitude, accuracy, timestamp):
        self.latitude = latitude
        self.longitude = longitude
        self.accuracy = accuracy
        self.timestamp = timestamp

        self.distance_to_home = distance_meters((self.latitude, self.longitude), Location.home_position)
        # assume home if home_location is within accuracy
        if self.distance_to_home < self.accuracy:
            self.rounded_distance_km = 0.0
        else:
            self.rounded_distance_km = math.floor(self.distance_to_home / 100) / 10.0

    @classmethod
    def set_home_position(cls, home_position):
        cls.home_position = home_position

    def distance_to(self, other_location):
        return distance_meters((self.latitude, self.longitude), (other_location.latitude, other_location.longitude))

    def is_recent_enough(self, max_elapsed_time_in_s):
        return time.time() - self.timestamp < max_elapsed_time_in_s

    def is_accurate_enough(self):
        if self.accuracy < ACCURATE_LIMIT_IN_M:
            return True
        if self.rounded_distance_km == 0.0 and self.accuracy < ACCURATE_LIMIT_WHEN_HOME_IN_M:
            return True
        # accuracy is also fine if it is within 20% of the distance
        if self.accuracy < (self.distance_to_home / 100) * ACCURACY_TO_DISTANCE_PERCENTAGE:
            return True
        return False

    def is_home(self):
        return self.rounded_distance_km == 0.0

    def can_be_same_location(self, other_location):
        if other_location is None:
            return False
        if self.is_home() and other_location.is_home():
            return True
        distance_to_other = self.distance_to(other_location)
        return distance_to_other < max(self.accuracy, other_location.accuracy)

    def is_more_accurate(self, other_location):
        if other_location is None:
            return True
        if self.accuracy < other_location.accuracy:
            return True
        if self.accuracy == other_location.accuracy:
            return self.distance_to_home < other_location.distance_to_home
        return False

    def __str__(self):
        seconds_ago = time.time() - self.timestamp
        return "%d ± %dm, -%ds, %.1fkm" \
               % (self.distance_to_home, self.accuracy, seconds_ago, self.rounded_distance_km)
