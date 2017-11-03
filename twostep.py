import sys
from pyicloud import PyiCloudService

appleid = "abc"
password = "def"

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
