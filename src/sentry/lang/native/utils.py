from __future__ import absolute_import

import re
import six
import logging

from collections import namedtuple
from symbolic import parse_addr, arch_from_macho, arch_is_known

from sentry.interfaces.contexts import DeviceContextType

logger = logging.getLogger(__name__)

# Regular expression to parse OS versions from a minidump OS string
VERSION_RE = re.compile(r'(\d+\.\d+\.\d+)\s+(.*)')

# Regular expression to guess whether we're dealing with Windows or Unix paths
WINDOWS_PATH_RE = re.compile(r'^[a-z]:\\', re.IGNORECASE)

AppInfo = namedtuple('AppInfo', ['id', 'version', 'build', 'name'])


def image_name(pkg):
    split = '\\' if WINDOWS_PATH_RE.match(pkg) else '/'
    return pkg.rsplit(split, 1)[-1]


def find_all_stacktraces(data):
    """Given a data dictionary from an event this returns all
    relevant stacktraces in a list.  If a frame contains a raw_stacktrace
    property it's preferred over the processed one.
    """
    rv = []

    def _probe_for_stacktrace(container):
        raw = container.get('raw_stacktrace')
        if raw is not None:
            rv.append((raw, container))
        else:
            processed = container.get('stacktrace')
            if processed is not None:
                rv.append((processed, container))

    exc_container = data.get('exception')
    if exc_container:
        for exc in exc_container['values']:
            _probe_for_stacktrace(exc)

    # The legacy stacktrace interface does not support raw stacktraces
    stacktrace = data.get('stacktrace')
    if stacktrace:
        rv.append((stacktrace, None))

    threads = data.get('threads')
    if threads:
        for thread in threads['values']:
            _probe_for_stacktrace(thread)

    return rv


def get_sdk_from_event(event):
    sdk_info = (event.get('debug_meta') or {}).get('sdk_info')
    if sdk_info:
        return sdk_info
    os = (event.get('contexts') or {}).get('os')
    if os and os.get('type') == 'os':
        return get_sdk_from_os(os)


def get_sdk_from_os(data):
    if 'name' not in data or 'version' not in data:
        return
    try:
        version = six.text_type(data['version']).split('-', 1)[0] + '.0' * 3
        system_version = tuple(int(x) for x in version.split('.')[:3])
    except ValueError:
        return

    return {
        'sdk_name': data['name'],
        'version_major': system_version[0],
        'version_minor': system_version[1],
        'version_patchlevel': system_version[2],
        'build': data.get('build'),
    }


def cpu_name_from_data(data):
    """Returns the CPU name from the given data if it exists."""
    device = DeviceContextType.primary_value_for_data(data)
    if device:
        arch = device.get('arch')
        if isinstance(arch, six.string_types):
            return arch

    # TODO: kill this here.  we want to not support that going forward
    unique_cpu_name = None
    images = (data.get('debug_meta') or {}).get('images') or []
    for img in images:
        if img.get('arch') and arch_is_known(img['arch']):
            cpu_name = img['arch']
        elif img.get('cpu_type') is not None \
                and img.get('cpu_subtype') is not None:
            cpu_name = arch_from_macho(img['cpu_type'], img['cpu_subtype'])
        else:
            cpu_name = None
        if unique_cpu_name is None:
            unique_cpu_name = cpu_name
        elif unique_cpu_name != cpu_name:
            unique_cpu_name = None
            break

    return unique_cpu_name


def rebase_addr(instr_addr, obj):
    return parse_addr(instr_addr) - parse_addr(obj.addr)


def sdk_info_to_sdk_id(sdk_info):
    if sdk_info is None:
        return None
    rv = '%s_%d.%d.%d' % (
        sdk_info['sdk_name'], sdk_info['version_major'], sdk_info['version_minor'],
        sdk_info['version_patchlevel'],
    )
    build = sdk_info.get('build')
    if build is not None:
        rv = '%s_%s' % (rv, build)
    return rv
