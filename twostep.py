import ConfigParser
import os
import sys
from pyicloud import PyiCloudService

# read configuration
config = ConfigParser.SafeConfigParser()
for loc in os.curdir, os.path.expanduser("~"):
    try:
        with open(os.path.join(loc, "iCloudLocationFetcher.conf")) as source:
            config.readfp(source)
    except IOError:
        pass

# read credentials
apple_creds_file = config.get('GENERAL', 'apple_creds_file')
try:
    with open(apple_creds_file) as f:
        appleid = f.readline().strip()
        password = f.readline().strip()
except IOError, e:
    print("Unable to read the apple credentials file '%s': %s" % (apple_creds_file, str(e)))
    sys.exit(1)

api = PyiCloudService(appleid, password)
if api.requires_2sa:
    import click
    print("Two-step authentication required. Your trusted devices are:")
    trusted_devices = api.trusted_devices
    for i, device in enumerate(trusted_devices):
        print("%s: %s" % (i, device.get('deviceName', "SMS to %s" % device.get('phoneNumber'))))

    device = click.prompt('Which device would you like to use?', default=0)
    device = trusted_devices[device]
    if not api.send_verification_code(device):
        print("Failed to send verification code")
        sys.exit(1)
    code = click.prompt('Please enter validation code')
    if not api.validate_verification_code(dict(), code):
        print("Failed to verify verification code")
        sys.exit(1)

# provide some information on the devices
devices = api.devices
for i, device in enumerate(devices):
    print("%s: type: %s, name: %s, location enabled: %s" % (i, device.content['deviceDisplayName'], device.content['name'], str(device.content['locationEnabled'])))
