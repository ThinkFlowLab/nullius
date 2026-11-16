"""HTTP fixture doors declared by a driver's setup, without injecting agent tools.

Each service is an existing Unix socket owned by the driver. Docker mounts only
that socket into a forwarder on the unit's internal network. A local run uses
the loopback URL supplied by the driver in its prompt.
"""
import os
import re
import stat


def doors(setup):
    out = []
    services = setup.get('web_services', [])
    if not isinstance(services, list):
        raise ValueError('web_services must be a list')
    for entry in services:
        if not isinstance(entry, dict) or set(entry) != {'name', 'port', 'socket'}:
            raise ValueError('web_services entries need name, port and socket')
        name, port, path = entry['name'], entry['port'], entry['socket']
        if not isinstance(name, str) or not re.fullmatch(r'web-[a-z0-9-]{1,40}', name):
            raise ValueError('web service names must start with web-')
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('web service port must be between 1024 and 65535')
        if not isinstance(path, str) or not os.path.isabs(path) or not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise ValueError('web service socket must be an existing absolute Unix socket, not a link')
        if name in [n for n, _, _ in out]:
            raise ValueError('duplicate web service name')
        out.append((name, port, path))
    return out
